# -*- coding: utf-8 -*-
"""
MEXC 세션 랭킹봇 (GitHub Actions 단발 실행)
- 랭킹: 전일 🇰🇷KST 세션(05:00→04:59) '세션 저점→(이후)고점' 상승률
- 30분 구간 표기(4칸):
  * Δ(연속 30분 구간 수익률) + range%(각 구간 고저폭/시가)
  * 최초(FIRST_RUN=true):   📈 0% | Δ | Δ | Δ | HH시
  * 이후(FIRST_RUN=false): 📈 Δ | Δ | Δ | Δ
  * |Δ| ≥ 2.00% → 상승 🚀 / 하락 💥
- 출력 심볼: 'ETH/USDT:USDT' → 'ETH' 처럼 심플
- 대상: watchlist.json 전 종목
- 전송: 텔레그램(4096자 제한 대비 분할)
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

# ── 환경 변수 ────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID   = os.getenv("TELEGRAM_CHAT_ID")
WATCHLIST_PATH     = os.getenv("WATCHLIST_PATH", "watchlist.json")
TIMEFRAME          = os.getenv("TIMEFRAME", "5m")
TOP_N              = int(os.getenv("TOP_N", "999999"))
TREND_COUNT        = int(os.getenv("TREND_COUNT", "4"))            # 4칸
LINES_PER_MESSAGE  = int(os.getenv("LINES_PER_MESSAGE", "9999"))   # 랭킹 줄 기준 분할
DELTA_EMOJI_THRESH = float(os.getenv("DELTA_EMOJI_THRESH", "2.0")) # 2.00%
FIRST_RUN          = os.getenv("FIRST_RUN", "false").lower() in ("1", "true", "yes")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비었어요.")

# ── 시간 유틸 ────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

def previous_kst_session_bounds(now_utc: datetime) -> Tuple[datetime, datetime]:
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
    now_kst  = now_utc.astimezone(KST)
    today_05 = now_kst.replace(hour=5, minute=0, second=0, microsecond=0)
    return today_05 if now_kst >= today_05 else (today_05 - timedelta(days=1))

def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)

# ── 텔레그램 ────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    url  = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text}
    try:
        r = requests.post(url, data=data, timeout=20)
        if r.status_code != 200:
            print(f"[텔레그램 오류] {r.text}")
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

# ── MEXC + 워치리스트 ───────────────────────────────────────
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

def pretty_symbol(sym: str) -> str:
    if "/USDT:USDT" in sym: return sym.split("/")[0]
    if "/USDT" in sym:      return sym.split("/")[0]
    if sym.endswith("_USDT"): return sym[:-5]
    if sym.endswith("-USDT-SWAP"): return sym[:-10]
    return sym

# ── 전일 세션 랭킹(저→고) ──────────────────────────────────
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

# ── 5m→30m 집계 & Δ/range% 계산 ────────────────────────────
def aggregate_to_30m(ohlcv_5m: List[List[float]]) -> List[List[float]]:
    # return: [ [ts30, open, high, low, close, volume], ... ]
    buckets: Dict[int, List[float]] = {}
    for ts, o, h, l, c, v in ohlcv_5m:
        k = ts - (ts % (30*60*1000))
        if k not in buckets:
            buckets[k] = [o, h, l, c, v]
        else:
            bo, bh, bl, bc, bv = buckets[k]
            buckets[k] = [bo, max(bh, h), min(bl, l), c, bv + v]
    out = []
    for k in sorted(buckets.keys()):
        o,h,l,c,v = buckets[k]
        out.append([k, o, h, l, c, v])
    return out

def last_n_deltas_and_ranges_30m(ex, symbol: str, base_5am_kst: datetime, now_utc: datetime, n: int) -> Tuple[List[float], List[float]]:
    start_ms = to_ms(base_5am_kst)
    try:
        ohlcv_5m = ex.fetch_ohlcv(symbol, timeframe="5m", since=start_ms, limit=1000)
    except Exception as e:
        print(f"[트렌드 실패] {symbol} - {e}")
        return [], []
    if not ohlcv_5m:
        return [], []

    c30 = aggregate_to_30m(ohlcv_5m)
    if len(c30) < 2:
        return [], []

    # 현재 시각을 넘지 않는 30분 경계까지만 사용
    now_kst = now_utc.astimezone(KST)
    valid = [row for row in c30 if row[0] <= to_ms(now_kst)]
    if len(valid) < 2:
        return [], []

    deltas, ranges = [], []
    for i in range(1, len(valid)):
        _, o, h, l, c, _ = valid[i]
        _, _, _, _, prev_c, _ = valid[i-1]

        delta = (c - prev_c) / prev_c * 100.0 if prev_c else 0.0
        rng   = (h - l) / o * 100.0 if o else 0.0

        deltas.append(delta)
        ranges.append(rng)

    return deltas[-n:], ranges[-n:]

# ── 메시지 포맷 & 전송 ─────────────────────────────────────
def format_block_header(day_label: str) -> str:
    return f"📈 {day_label} 세션(05:00→04:59) 상승률 순위\n"

def format_rank_line(rank: int, symbol: str, pct: float) -> str:
    name = pretty_symbol(symbol)
    if rank == 1:   return f"🥇 {name} ({pct:.2f}%)"
    if rank == 2:   return f"🥈 {name} ({pct:.2f}%)"
    if rank == 3:   return f"🥉 {name} ({pct:.2f}%)"
    return f"{rank}. {name} ({pct:.2f}%)"

def _fmt_delta(v: float) -> str:
    s = f"{v:+.2f}%"
    if v >= DELTA_EMOJI_THRESH:  return f"{s} 🚀"
    if v <= -DELTA_EMOJI_THRESH: return f"{s} 💥"
    return s

def format_delta_line(deltas: List[float], first_run: bool, now_kst: datetime) -> str:
    if not deltas: return "📈  -"
    parts = [_fmt_delta(v) for v in deltas]
    if first_run:
        show3 = parts[-3:] if len(parts) >= 3 else parts
        hour_label = f"{now_kst.strftime('%H')}시"
        return "📈  " + " | ".join(["0%"] + show3 + [hour_label])
    return "📈  " + " | ".join(parts[-TREND_COUNT:])

def format_range_line(ranges: List[float]) -> str:
    if not ranges: return "🌊  -"
    parts = [f"{v:.2f}%" for v in ranges[-TREND_COUNT:]]
    return "🌊  " + " | ".join(parts)

def send_ranked_messages(day_label: str, ranked: List[Dict], trend_map: Dict[str, Tuple[List[float], List[float]]], first_run: bool, now_kst: datetime) -> None:
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
        sym = item["symbol"]
        deltas, ranges = trend_map.get(sym, ([], []))

        line1 = format_rank_line(rank, sym, item["pct"])
        line2 = format_delta_line(deltas, first_run, now_kst)
        line3 = format_range_line(ranges)
        chunk = ("\n\n" if lines_in_msg > 0 or header in buf else "") + line1 + "\n" + line2 + "\n" + line3

        # 길이/줄수 제한 처리
        if lines_in_msg + 1 > LINES_PER_MESSAGE or len(buf) + len(chunk) > 3500:
            flush()
            buf += line1 + "\n" + line2 + "\n" + line3
            lines_in_msg = 1
        else:
            buf += chunk
            lines_in_msg += 1

        rank += 1

    flush()
    print("✅ 텔레그램 전송 완료")

# ── 엔트리포인트 ───────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    now_kst = now_utc.astimezone(KST)

    # 전일 세션 랭킹 범위
    start_kst, end_kst = previous_kst_session_bounds(now_utc)
    since_ms, until_ms = to_ms(start_kst), to_ms(end_kst)

    # 트렌드 기준 05:00
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

    # 랭킹
    results: List[Dict] = []
    for s in valid_syms:
        r = compute_session_performance(ex, s, since_ms, until_ms, TIMEFRAME)
        if r: results.append(r)
        time.sleep(max(0.15, getattr(ex, "rateLimit", 200) / 1000.0))
    results.sort(key=lambda x: x["pct"], reverse=True)

    # Δ + range% (마지막 4칸)
    trend_map: Dict[str, Tuple[List[float], List[float]]] = {}
    for s in valid_syms:
        deltas, ranges = last_n_deltas_and_ranges_30m(ex, s, base_5am_kst, now_utc, TREND_COUNT)
        trend_map[s] = (deltas, ranges)
        time.sleep(max(0.1, getattr(ex, "rateLimit", 200) / 1000.0))

    # 전송
    day_label = start_kst.strftime("%Y-%m-%d")
    send_ranked_messages(day_label, results[:TOP_N], trend_map, FIRST_RUN, now_kst)

if __name__ == "__main__":
    main()
