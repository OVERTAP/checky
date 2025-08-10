# -*- coding: utf-8 -*-
"""
MEXC 최적화 세션 랭킹봇 (KST 05:00 ~ 익일 04:59 기준)
- watchlist.json의 심볼을 MEXC에서 지원되는 포맷으로 자동 정규화
- 전일 KST 세션에서 '저점 → (그 이후) 고점' 상승률 계산
- 텔레그램으로 랭킹 메시지 전송
requirements: ccxt, requests, python-dotenv(로컬)
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

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
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WATCHLIST_PATH = os.getenv("WATCHLIST_PATH", "watchlist.json")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")
TOP_N = int(os.getenv("TOP_N", "50"))

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("⚠️ 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비었어요.")

# ─────────────────────────────────────────────────────────────
# 1) KST 세션 계산
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

def previous_kst_session_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    now_kst = now_utc.astimezone(KST)
    today_05 = now_kst.replace(hour=5, minute=0, second=0, microsecond=0)
    if now_kst >= today_05:
        start_kst = today_05 - timedelta(days=1)
        end_kst = today_05 - timedelta(seconds=1)
    else:
        start_kst = today_05 - timedelta(days=2)
        end_kst = today_05 - timedelta(days=1, seconds=-1)
    return start_kst, end_kst

def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)

# ─────────────────────────────────────────────────────────────
# 2) 텔레그램
# ─────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {"chat_id": TELEGRAM_CHAT_ID, "text": text, "parse_mode": "Markdown"}
    try:
        r = requests.post(url, data=data, timeout=20)
        if r.status_code != 200:
            print(f"[텔레그램 오류] {r.text}")
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

# ─────────────────────────────────────────────────────────────
# 3) 심볼 로드
# ─────────────────────────────────────────────────────────────
def load_watchlist(path: str) -> List[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("watchlist.json 포맷은 리스트여야 해요.")
    return [s.strip() for s in data if isinstance(s, str) and s.strip()]

# ─────────────────────────────────────────────────────────────
# 4) MEXC 거래소 + 심볼 정규화
# ─────────────────────────────────────────────────────────────
def create_mexc_swap():
    ex = ccxt.mexc({
        "enableRateLimit": True,
        "options": {"defaultType": "swap"},
    })
    ex.load_markets()
    return ex

def resolve_symbol_for_mexc(raw: str, markets: Dict) -> Optional[str]:
    candidates = [
        raw,
        raw.replace("/USDT:USDT", "_USDT"),
        raw.replace("/USDT", "_USDT"),
        raw.replace("-USDT-SWAP", "_USDT"),
    ]
    seen, cand_unique = set(), []
    for c in candidates:
        if c not in seen:
            seen.add(c)
            cand_unique.append(c)
    for c in cand_unique:
        if c in markets:
            m = markets[c]
            if (m.get("type") == "swap") and (m.get("quote") == "USDT") and m.get("active", True):
                return c
    return None

# ─────────────────────────────────────────────────────────────
# 5) 세션 퍼포먼스 계산
# ─────────────────────────────────────────────────────────────
def compute_session_performance(exchange, symbol: str, since_ms: int, until_ms: int, timeframe: str) -> Optional[Dict]:
    try:
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=timeframe, since=since_ms, limit=800)
    except Exception as e:
        print(f"[OHLCV 실패] {symbol} - {e}")
        return None
    if not ohlcv:
        return None
    rows = [row for row in ohlcv if since_ms <= row[0] <= until_ms]
    if len(rows) < 2:
        return None

    lows = [(ts, low) for ts, _, _, low, _, _ in rows]
    low_ts, low_price = min(lows, key=lambda x: x[1])
    after_low = [row for row in rows if row[0] >= low_ts]
    highs = [(ts, high) for ts, _, high, _, _, _ in after_low]
    high_ts, high_price = max(highs, key=lambda x: x[1])

    pct = 0.0
    if low_price > 0 and high_price > 0 and high_price >= low_price:
        pct = (high_price - low_price) / low_price * 100.0

    return {
        "symbol": symbol,
        "low": low_price,
        "high": high_price,
        "pct": pct,
        "low_ts": low_ts,
        "high_ts": high_ts,
    }

# ─────────────────────────────────────────────────────────────
# 6) 메시지 포맷 (요청 포맷)
# ─────────────────────────────────────────────────────────────
def format_message(results: List[Dict], start_kst: datetime, end_kst: datetime) -> str:
    day_label = start_kst.strftime("%Y-%m-%d")
    header = f"📈 {day_label} 세션(05:00→04:59) 상승률 순위\n\n"
    lines = []
    for i, r in enumerate(results[:TOP_N], start=1):
        flair = "🔥" if r["pct"] >= 10 else "⚡️"
        if i == 1:
            line = f"🥇   {r['symbol']} {flair}  {r['pct']:.2f}%"
        elif i == 2:
            line = f"🥈   {r['symbol']} {flair}  {r['pct']:.2f}%"
        elif i == 3:
            line = f"🥉   {r['symbol']} {flair}  {r['pct']:.2f}%"
        else:
            line = f"{i}.  {r['symbol']} {flair}  {r['pct']:.2f}%"
        lines.append(line)
    return header + "\n\n".join(lines)

# ─────────────────────────────────────────────────────────────
# 7) 엔트리포인트 (Actions: 단발 실행)
# ─────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    start_kst, end_kst = previous_kst_session_bounds(now_utc)
    since_ms, until_ms = to_ms(start_kst), to_ms(end_kst)

    exchange = create_mexc_swap()
    markets = exchange.markets

    symbols_raw = load_watchlist(WATCHLIST_PATH)
    valid_syms, invalid_syms = [], []
    for raw in symbols_raw:
        resolved = resolve_symbol_for_mexc(raw, markets)
        if resolved:
            valid_syms.append(resolved)
        else:
            invalid_syms.append(raw)

    if invalid_syms:
        print(f"[경고] MEXC에서 지원 안 되거나 포맷 불일치 심볼 {len(invalid_syms)}개: {invalid_syms[:10]}{' …' if len(invalid_syms)>10 else ''}")

    results: List[Dict] = []
    for s in valid_syms:
        r = compute_session_performance(exchange, s, since_ms, until_ms, TIMEFRAME)
        if r:
            results.append(r)
        time.sleep(max(0.15, getattr(exchange, "rateLimit", 200) / 1000.0))

    results.sort(key=lambda x: x["pct"], reverse=True)
    msg = format_message(results, start_kst, end_kst)
    print(msg)
    send_telegram(msg)
    print("✅ 메시지 전송 완료")

if __name__ == "__main__":
    main()
