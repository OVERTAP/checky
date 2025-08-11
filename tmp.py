# -*- coding: utf-8 -*-
"""
MEXC 세션 랭킹봇 (GitHub Actions 단발 실행)
- 랭킹: 전일 🇰🇷KST 세션(05:00→04:59) '세션 저점→(이후)고점' 상승률
- 트렌드(구간): 가장 가까운 05:00(KST)부터 30분 간격 '연속 구간' 증감률 (4칸)
  * 최초 실행(수동 등, 분이 00/30이 아닐 때): 0% | Δ | Δ | Δ | HH시
  * 이후 실행(스케줄 30분 간격): Δ | Δ | Δ | Δ
  * 임계(|Δ| ≥ 2.00%): 상승 🚀 / 하락 💥 이모지 부착
- 대상: watchlist.json 전 종목 (모두 랭킹 정렬)
- 전송: 텔레그램 (4096자 제한 대비 자동 분할)
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional, Tuple

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import ccxt

# ─────────────────────────────────────────────────────────────
# 0) 환경 변수
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WATCHLIST_PATH     = os.getenv("WATCHLIST_PATH", "watchlist.json")
TIMEFRAME          = os.getenv("TIMEFRAME", "5m")
TOP_N              = int(os.getenv("TOP_N", "999999"))      # 전체 출력 기본
TREND_STEP_MIN     = int(os.getenv("TREND_STEP_MIN", "30")) # 30분 고정 권장
TREND_COUNT        = int(os.getenv("TREND_COUNT", "4"))     # 구간 4칸
LINES_PER_MESSAGE  = int(os.getenv("LINES_PER_MESSAGE", "9999"))  # 메시지 분할(랭킹 줄 기준)
# 강렬 이모지 임계
DELTA_EMOJI_THRESH = float(os.getenv("DELTA_EMOJI_THRESH", "2.0"))  # 2.00%

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비었어요.")

# ─────────────────────────────────────────────────────────────
# 1) 시간 유틸
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

def previous_kst_session_bounds(now_utc: datetime) -> Tuple[datetime, datetime]:
    """전일 KST 세션(05:00→04:59:59) 범위 반환."""
    now_kst = now_utc.astimezone(KST)
    today_05 = now_kst.replace(hour=5, minute=0, second=0, microsecond=0)
    if now_kst >= today_05:
        start_kst = today_05 - timedelta(days=1)
        end_kst   = today_05 - timedelta(seconds=1)
    else:
        start_kst = today_05 - timedelta(days=2)
        end_kst   = today_05 - timedelta(days=1, seconds=-1)
    return start_kst, end_kst

def latest_5am_kst_at_or_before(now_utc: datetime) -> datetime:
    """현재 시각 기준으로 직전(포함) 05:00 KST 반환 (오늘 05:00 또는 어제 05:00)."""
    now_kst  = now_utc.astimezone(KST)
    today_05 = now_kst.replace(hour=5, minute=0, second=0, microsecond=0)
    return today_05 if now_kst >= today_05 else (today_05 - timedelta(days=1))

def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)

# ─────────────────────────────────────────────────────────────
# 2) 텔레그램
# ─────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, data=data, timeout=20)
        if r.status_code != 200:
            print(f"[텔레그램 오류] {r.text}")
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

# ─────────────────────────────────────────────────────────────
# 3) MEXC + 워치리스트
# ─────────────────────────────────────────────────────────────
def create_mexc_swap():
    ex = ccxt.mexc({"enableRateLimit": True, "options": {"defaultType": "swap"}})
    ex.load_markets()
    return ex

def load_watchlist(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("watchlist.json 포맷은 리스트여야 해요.")
    return [s.strip() for s in data if isinstance(s, str) and s.strip()]

def resolve_symbol_for_mexc(raw: str, markets: Dict) -> Optional[str]:
    """바이낸스풍 심볼을 MEXC 포맷으로 보정."""
    candidates = [
        raw,
        raw.replace("/USDT:USDT", "_USDT"),
        raw.replace("/USDT", "_USDT"),
        raw.replace("-USDT-SWAP", "_USDT"),
    ]
    seen, unique = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            unique.append(c)
    for c in unique:
        if c in markets:
            m = markets[c]
            if (m.get("type") == "swap") and (m.get("quote") == "USDT") and m.get("active", True):
                return c
    return None

# ─────────────────────────────────────────────────────────────
# 4) 랭킹 계산 (전일 세션 저→고)
# ─────────────────────────────────────────────────────────────
def compute_session_performance(ex, symbol: str, since_ms: int, until_ms: int, timeframe: str) -> Optional[Dict]:
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=800)
    except Exception as e:
        print(f"[OHLCV 실패] {symbol} - {e}")
        return None
    if not ohlcv:
        return None

    rows = [row for row in ohlcv if since_ms <= row[0] <= until_ms]
    if len(rows) < 2:
        return None

    lows  = [(ts, low)  for ts, _, _, low,  _, _ in rows]
    low_ts,  low_price  = min(lows,  key=lambda x: x[1])

    after_low           = [row for row in rows if row[0] >= low_ts]
    highs_after_low     = [(ts, high) for ts, _, high, _, _, _ in after_low]
    high_ts, high_price = max(highs_after_low, key=lambda x: x[1])

    pct = 0.0
    if low_price > 0 and high_price >= low_price:
        pct = (high_price - low_price) / low_price * 100.0

    return {"symbol": symbol, "pct": pct, "low": low_price, "high": high_price, "low_ts": low_ts, "high_ts": high_ts}

# ─────────────────────────────────────────────────────────────
# 5) 30분 '연속 구간' 증감률 4칸 계산
# ─────────────────────────────────────────────────────────────
def last_n_interval_deltas_30m(ex, symbol: str, base_5am_kst: datetime, now_utc: datetime, n: int) -> List[float]:
    """
    05:00 KST부터 30분 간격 경계(B0,B1,...)의 '가격'을 만들고,
    마지막 n개의 '연속 구간' 증감률 Δ_i = (P_i - P_{i-1})/P_{i-1}*100을 반환.
    경계 가격 P_i는 해당 경계 시각 '이하' 마지막 캔들의 close(5m) 사용.
    """
    start_ms = to_ms(base_5am_kst)
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe="5m", since=start_ms, limit=1000)
    except Exception as e:
        print(f"[트렌드 실패] {symbol} - {e}")
        return []
    if not ohlcv:
        return []

    # 경계 생성
    now_kst = now_utc.astimezone(KST)
    # 마지막 경계는 '현재 시각을 넘지 않는' 30분 스텝
    minutes_from_base = int((now_kst - base_5am_kst).total_seconds() // 60)
    last_step_index = minutes_from_base // 30  # B_last
    if last_step_index < 1:
        return []  # 구간이 하나도 완성되지 않음

    boundaries = [base_5am_kst + timedelta(minutes=30 * i) for i in range(0, last_step_index + 1)]

    # 각 경계의 P_i 선택 (<= 경계 시각 가장 최근 close)
    prices = []
    idx = 0
    for b in boundaries:
        b_ms = to_ms(b)
        while idx + 1 < len(ohlcv) and ohlcv[idx + 1][0] <= b_ms:
            idx += 1
        # 경계 이전 데이터가 하나도 없을 수 있음 → 스킵
        if idx < len(ohlcv) and ohlcv[idx][0] <= b_ms and ohlcv[idx][4] and ohlcv[idx][4] > 0:
            prices.append(float(ohlcv[idx][4]))
        else:
            prices.append(None)

    # Δ 계산
    deltas = []
    for i in range(1, len(prices)):
        p_prev, p_cur = prices[i-1], prices[i]
        if p_prev and p_cur and p_prev > 0:
            deltas.append((p_cur - p_prev) / p_prev * 100.0)
        else:
            deltas.append(0.0)

    # 마지막 n개
    if len(deltas) >= n:
        return deltas[-n:]
    return deltas

# ─────────────────────────────────────────────────────────────
# 6) 메시지 포맷 + 분할 전송
# ─────────────────────────────────────────────────────────────
def format_block_header(day_label: str) -> str:
    return f"📈 {day_label} 세션(05:00→04:59) 상승률 순위\n"

def format_rank_line(rank: int, symbol: str, pct: float) -> str:
    flair = "🔥" if pct >= 10 else "⚡️"
    if rank == 1:
        return f"🥇   {symbol} {flair}  {pct:.2f}%"
    elif rank == 2:
        return f"🥈   {symbol} {flair}  {pct:.2f}%"
    elif rank == 3:
        return f"🥉   {symbol} {flair}  {pct:.2f}%"
    else:
        return f"{rank}.  {symbol} {flair}  {pct:.2f}%"

def _fmt_delta(v: float) -> str:
    # 부호 항상 표시
    s = f"{v:+.2f}%"
    if v >= DELTA_EMOJI_THRESH:
        return f"{s} 🚀"
    if v <= -DELTA_EMOJI_THRESH:
        return f"{s} 💥"
    return s

def format_delta_line(deltas: List[float], first_run_style: bool, now_kst: datetime) -> str:
    """
    first_run_style: 0% | Δ | Δ | Δ | HH시
    else           : Δ | Δ | Δ | Δ
    """
    if not deltas:
        return "      -"
    parts = [_fmt_delta(v) for v in deltas]
    if first_run_style:
        hour_label = f"{now_kst.strftime('%H')}시"
        # 최초 실행은 '0%'를 맨 앞에 추가하고 끝에 'HH시'
        # 델타 3개만 보여주고 총 4칸 맞춤 (요구사항)
        show = parts[-3:] if len(parts) >= 3 else parts  # 부족하면 있는 만큼
        return "      " + " | ".join(["0%"] + show + [hour_label])
    # 이후 실행: 4칸 그대로
    return "      " + " | ".join(parts[-TREND_COUNT:])

def send_ranked_messages(day_label: str, ranked: List[Dict], trend_map: Dict[str, List[float]], first_run_style: bool, now_kst: datetime) -> None:
    header = format_block_header(day_label)
    buf = header
    lines_in_msg = 0
    rank = 1

    def flush():
        nonlocal buf, lines_in_msg
        if lines_in_msg > 0:
            send_telegram(buf.rstrip())
        buf = header
        lines_in_msg = 0

    for item in ranked:
        line1 = format_rank_line(rank, item["symbol"], item["pct"])
        line2 = format_delta_line(trend_map.get(item["symbol"], []), first_run_style, now_kst)
        chunk = ("\n\n" if lines_in_msg > 0 or header in buf else "") + line1 + "\n" + line2

        if lines_in_msg + 1 > LINES_PER_MESSAGE or len(buf) + len(chunk) > 3500:
            flush()
            buf += line1 + "\n" + line2
            lines_in_msg = 1
        else:
            buf += chunk
            lines_in_msg += 1

        rank += 1

    flush()
    print("✅ 텔레그램 전송 완료")

# ─────────────────────────────────────────────────────────────
# 7) 엔트리포인트
# ─────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(KST)

    # 전일 세션 랭킹 범위
    start_kst, end_kst = previous_kst_session_bounds(now_utc)
    since_ms, until_ms = to_ms(start_kst), to_ms(end_kst)

    # 트렌드용 기준 05:00
    base_5am_kst = latest_5am_kst_at_or_before(now_utc)

    # "최초 실행" 자동 판별: 분이 00/30이 아니면 최초 스타일로 간주
    first_run_style = (now_kst.minute % 30 != 0)

    ex = create_mexc_swap()
    markets = ex.markets
    symbols_raw = load_watchlist(WATCHLIST_PATH)

    valid_syms, invalid_syms = [], []
    for raw in symbols_raw:
        resolved = resolve_symbol_for_mexc(raw, markets)
        (valid_syms if resolved else invalid_syms).append(resolved or raw)

    if invalid_syms:
        print(f"[경고] MEXC 미지원/포맷 불일치 {len(invalid_syms)}개: {invalid_syms[:10]}{' …' if len(invalid_syms)>10 else ''}")

    # 랭킹 계산
    results: List[Dict] = []
    for s in valid_syms:
        r = compute_session_performance(ex, s, since_ms, until_ms, TIMEFRAME)
        if r:
            results.append(r)
        time.sleep(max(0.15, getattr(ex, "rateLimit", 200) / 1000.0))
    results.sort(key=lambda x: x["pct"], reverse=True)

    # 30분 연속 구간 Δ 계산 (4칸)
    trend_map: Dict[str, List[float]] = {}
    for s in valid_syms:
        deltas = last_n_interval_deltas_30m(ex, s, base_5am_kst, now_utc, TREND_COUNT)
        # 델타가 4칸 미만이면 앞쪽을 0으로 채우지 않고, 있는 만큼만 출력(요청 취지대로)
        trend_map[s] = deltas
        time.sleep(max(0.1, getattr(ex, "rateLimit", 200) / 1000.0))

    # 전송
    day_label = start_kst.strftime("%Y-%m-%d")
    send_ranked_messages(day_label, results[:TOP_N], trend_map, first_run_style, now_kst)

if __name__ == "__main__":
    main()
