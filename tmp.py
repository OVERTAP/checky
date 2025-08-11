# -*- coding: utf-8 -*-
"""
MEXC 세션 랭킹봇 (GitHub Actions 단발 실행용)
- 랭킹: 전일 🇰🇷KST 세션(05:00→04:59) '세션 저점→(이후)고점' 상승률
- 트렌드: 가장 가까운 05:00(KST) 기준으로 30분 간격 등락률 리스트(슬래시 구분)
- 대상: watchlist.json 전 종목 (모두 랭킹 정렬 후 출력)
- 전송: 텔레그램
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
TOP_N              = int(os.getenv("TOP_N", "999999"))  # 전체 출력
TREND_STEP_MIN     = int(os.getenv("TREND_STEP_MIN", "30"))  # 30분 간격
TREND_POINTS       = int(os.getenv("TREND_POINTS", "6"))     # 0.00% 포함 N개 포인트
LINES_PER_MESSAGE  = int(os.getenv("LINES_PER_MESSAGE", "999999"))  # 메시지 분할(랭킹 줄 단위)

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
# 3) MEXC 거래소 + 워치리스트
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
    highz = [(ts, high) for ts, _, high, _, _, _ in rows]

    low_ts,  low_price  = min(lows,  key=lambda x: x[1])
    after_low           = [row for row in rows if row[0] >= low_ts]
    highs_after_low     = [(ts, high) for ts, _, high, _, _, _ in after_low]
    high_ts, high_price = max(highs_after_low, key=lambda x: x[1])

    pct = 0.0
    if low_price > 0 and high_price >= low_price:
        pct = (high_price - low_price) / low_price * 100.0

    return {"symbol": symbol, "pct": pct, "low": low_price, "high": high_price, "low_ts": low_ts, "high_ts": high_ts}

# ─────────────────────────────────────────────────────────────
# 5) 05:00 기준 라이브 트렌드(30분 간격)
# ─────────────────────────────────────────────────────────────
def sample_live_trend_percentages(ex, symbol: str, base_5am_kst: datetime, now_utc: datetime,
                                  step_min: int, max_points: int) -> List[float]:
    """
    05:00 KST 기준 등락률을 30분 간격으로 샘플링(0.00 포함 최대 N개).
    기준가격: 05:00 이후 첫 캔들의 'open' (5m)
    각 포인트: 해당 시각 이전 마지막 캔들의 'close'
    """
    # 5m로 충분히 커버
    start_ms = to_ms(base_5am_kst)
    # 넉넉히 1000개 제한
    try:
        ohlcv = ex.fetch_ohlcv(symbol, timeframe="5m", since=start_ms, limit=1000)
    except Exception as e:
        print(f"[트렌드 실패] {symbol} - {e}")
        return []

    if not ohlcv:
        return []

    # 기준가(05:00 이후 첫 캔들의 open)
    base_rows = [r for r in ohlcv if r[0] >= start_ms]
    if not base_rows:
        return []
    base_open = base_rows[0][1]
    if not base_open or base_open <= 0:
        return []

    # 경계 시각 생성 (0, 30, 60, ... 분)
    now_kst = now_utc.astimezone(KST)
    # 최대 포인트 수 보장: 0% 포함해서 max_points개
    # 실제 경계 개수 계산
    elapsed_min = max(0, int((now_kst - base_5am_kst).total_seconds() // 60))
    steps = min(max_points - 1, elapsed_min // step_min)
    boundaries = [base_5am_kst + timedelta(minutes=step_min * i) for i in range(0, steps + 1)]

    # 각 경계의 직전(이하) 캔들 close로 등락률 계산
    pct_list = []
    idx = 0
    for b in boundaries:
        b_ms = to_ms(b)
        # ohlcv는 시간 오름차순. 포인터 전진 탐색
        while idx + 1 < len(ohlcv) and ohlcv[idx + 1][0] <= b_ms:
            idx += 1
        close_price = ohlcv[idx][4] if idx < len(ohlcv) else None
        if not close_price or close_price <= 0:
            pct_list.append(0.0 if len(pct_list) == 0 else pct_list[-1])
        else:
            pct_list.append((close_price - base_open) / base_open * 100.0)

    # 0.00% 보장
    if pct_list:
        pct_list[0] = 0.0
    return pct_list

# ─────────────────────────────────────────────────────────────
# 6) 메시지 포맷 + 분할 전송
# ─────────────────────────────────────────────────────────────
def format_block_header(day_label: str) -> str:
    return f"📈 {day_label} 세션(05:00→04:59) 상승률 순위\n(05:00 대비 라이브 변동: 30분 간격, 시간 표기 없음)\n"

def format_one_line(rank: int, symbol: str, pct: float) -> str:
    flair = "🔥" if pct >= 10 else "⚡️"
    if rank == 1:
        return f"🥇   {symbol} {flair}  {pct:.2f}%"
    elif rank == 2:
        return f"🥈   {symbol} {flair}  {pct:.2f}%"
    elif rank == 3:
        return f"🥉   {symbol} {flair}  {pct:.2f}%"
    else:
        return f"{rank}.  {symbol} {flair}  {pct:.2f}%"

def format_trend_line(pcts: List[float]) -> str:
    if not pcts:
        return "      -"
    parts = [f"{x:.2f}%" for x in pcts]
    return "      " + " / ".join(parts)

def send_ranked_messages(day_label: str, ranked: List[Dict], trends: Dict[str, List[float]]) -> None:
    """
    텔레그램 4096자 제한 고려해서 안전 분할 전송.
    기준: 대략 3500자 근처에서 잘라 보냄 + LINES_PER_MESSAGE 제한도 적용.
    """
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
        line1 = format_one_line(rank, item["symbol"], item["pct"])
        line2 = format_trend_line(trends.get(item["symbol"], []))
        chunk = ("\n\n" if lines_in_msg > 0 or header in buf else "") + line1 + "\n" + line2

        # 줄 수 또는 길이 기준 초과 시 플러시
        if lines_in_msg + 1 > LINES_PER_MESSAGE or len(buf) + len(chunk) > 3500:
            flush()
            # 새 메시지에 바로 추가
            buf += line1 + "\n" + line2
            lines_in_msg = 1
        else:
            buf += chunk
            lines_in_msg += 1

        rank += 1

    # 마지막 플러시
    flush()
    print("✅ 텔레그램 전송 완료")

# ─────────────────────────────────────────────────────────────
# 7) 엔트리포인트
# ─────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    # 랭킹용: 전일 세션
    start_kst, end_kst = previous_kst_session_bounds(now_utc)
    since_ms, until_ms = to_ms(start_kst), to_ms(end_kst)
    # 트렌드용: 가장 가까운 05:00
    base_5am_kst = latest_5am_kst_at_or_before(now_utc)

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

    # 트렌드 계산(30분 간격, N개)
    trends: Dict[str, List[float]] = {}
    for s in valid_syms:
        pcts = sample_live_trend_percentages(
            ex, s, base_5am_kst, now_utc, step_min=TREND_STEP_MIN, max_points=TREND_POINTS
        )
        trends[s] = pcts
        time.sleep(max(0.1, getattr(ex, "rateLimit", 200) / 1000.0))

    # 전송 (분할 처리)
    day_label = start_kst.strftime("%Y-%m-%d")
    send_ranked_messages(day_label, results[:TOP_N], trends)

if __name__ == "__main__":
    main()
