# -*- coding: utf-8 -*-
"""
세션 랭킹봇 (KST 05:00 ~ 익일 04:59 기준)
- watchlist.json 심볼 리스트를 기준으로 전일 세션의 '세션 저점 이후 고점' 상승률을 계산
- 텔레그램으로 순위표 전송
- GitHub Actions 등 비대화형 환경에서 실행되도록 설계
requirements: ccxt, requests, python-dotenv (로컬 개발 시)
"""

import os
import json
import time
import math
import requests
from datetime import datetime, timedelta, timezone

try:
    from dotenv import load_dotenv
    load_dotenv()  # 로컬 개발 환경에서만 .env 사용, CI에서는 GitHub Secrets 사용
except Exception:
    pass

import ccxt

# ─────────────────────────────────────────────────────────────
# 0) 환경 변수
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
WATCHLIST_PATH = os.getenv("WATCHLIST_PATH", "watchlist.json")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")  # 5분봉 기준 (세션 내 저·고 탐색에 충분)
TOP_N = int(os.getenv("TOP_N", "50"))     # 상위 N개 출력 (과하면 메시지 길이 제한 주의)

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("⚠️ TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 환경 변수가 비어 있습니다.")

# ─────────────────────────────────────────────────────────────
# 1) 유틸: 시간대 & 세션 계산 (KST 기준 05:00 ~ 익일 04:59:59)
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

def previous_kst_session_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    """
    현재(UTC) 기준 '마지막으로 완전히 끝난 KST 세션'의 [start_kst, end_kst] 반환
    세션: 05:00:00 ~ 익일 04:59:59
    """
    now_kst = now_utc.astimezone(KST)
    # 오늘 KST 05:00
    today_kst_5 = now_kst.replace(hour=5, minute=0, second=0, microsecond=0)
    if now_kst >= today_kst_5:
        # 현재가 05:00 이후라면, 전일 05:00 ~ 오늘 04:59:59 세션이 '완료된' 마지막 세션
        start_kst = today_kst_5 - timedelta(days=1)
        end_kst = today_kst_5 - timedelta(seconds=1)
    else:
        # 현재가 05:00 이전이라면, 이틀 전 05:00 ~ 어제 04:59:59가 마지막 세션
        start_kst = today_kst_5 - timedelta(days=2)
        end_kst = today_kst_5 - timedelta(days=1, seconds= -1)  # 어제 04:59:59
    return start_kst, end_kst

def to_ms(dt: datetime) -> int:
    return int(dt.astimezone(timezone.utc).timestamp() * 1000)

# ─────────────────────────────────────────────────────────────
# 2) 텔레그램
# ─────────────────────────────────────────────────────────────
def send_telegram(text: str) -> None:
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    data = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "Markdown"
    }
    try:
        r = requests.post(url, data=data, timeout=15)
        if r.status_code != 200:
            print(f"[텔레그램 오류] {r.text}")
    except Exception as e:
        print(f"[텔레그램 전송 실패] {e}")

# ─────────────────────────────────────────────────────────────
# 3) 심볼 로드
# ─────────────────────────────────────────────────────────────
def load_watchlist(path: str) -> list[str]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    if not isinstance(data, list):
        raise ValueError("watchlist.json 포맷이 리스트가 아닙니다.")
    return [s.strip() for s in data if isinstance(s, str) and s.strip()]

# ─────────────────────────────────────────────────────────────
# 4) 메인 로직: 세션 내 저점 이후 고점 상승률
# ─────────────────────────────────────────────────────────────
def compute_session_performance(exchange: ccxt.binance, symbol: str, since_ms: int, until_ms: int) -> dict | None:
    """
    symbol의 세션(UTC ms: since_ms~until_ms)에서
    1) 세션 내 최저가(low)의 시점 찾고
    2) 그 이후 구간의 최고가(high) 찾음
    3) pct = (high - low)/low * 100 계산
    반환: {'symbol': str, 'low': float, 'high': float, 'pct': float} 또는 None(데이터 부족)
    """
    try:
        # session 길이(24h) 대비 5m 캔들 288개 → 여유로 600개
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=since_ms, limit=800)
    except Exception as e:
        print(f"[OHLCV 실패] {symbol} - {e}")
        return None

    if not ohlcv:
        return None

    # 세션 범위 내로 필터링
    rows = [row for row in ohlcv if since_ms <= row[0] <= until_ms]
    if len(rows) < 2:
        return None

    # 세션 최저가와 그 시점
    lows = [(ts, low) for ts, _, _, low, _, _ in rows]
    low_ts, low_price = min(lows, key=lambda x: x[1])

    # '저점 이후'의 최고가
    after_low = [row for row in rows if row[0] >= low_ts]
    highs = [(ts, high) for ts, _, high, _, _, _ in after_low]
    high_ts, high_price = max(highs, key=lambda x: x[1])

    if low_price <= 0 or high_price <= 0 or high_price < low_price:
        # 상승이 없으면 0%로 처리
        pct = 0.0
    else:
        pct = (high_price - low_price) / low_price * 100.0

    return {
        "symbol": symbol,
        "low": low_price,
        "high": high_price,
        "pct": pct,
        "low_ts": low_ts,
        "high_ts": high_ts
    }

# ─────────────────────────────────────────────────────────────
# 5) 메시지 포맷
# ─────────────────────────────────────────────────────────────
def format_message(results: list[dict], start_kst: datetime, end_kst: datetime) -> str:
    header = f"📊 세션(🇰🇷KST) {start_kst.strftime('%Y-%m-%d %H:%M')} → {end_kst.strftime('%Y-%m-%d %H:%M')}\n" \
             f"세션 저점 → 이후 고점 상승률 *TOP {min(TOP_N, len(results))}*\n"

    body_lines = []
    for i, r in enumerate(results[:TOP_N], start=1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        flair = "🚀" if r['pct'] >= 20 else "🔥" if r['pct'] >= 10 else "⚡️"
        body_lines.append(f"{medal} {r['symbol']}: {r['pct']:.2f}% {flair}")

    msg = header + "\n".join(body_lines) if body_lines else header + "_데이터가 없어요_"
    # 텔레그램 메시지 최대 길이(약 4096) 대비 안전
    if len(msg) > 3500:
        msg = msg[:3490] + "\n…(생략)"
    return msg

# ─────────────────────────────────────────────────────────────
# 6) 엔트리포인트
# ─────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    start_kst, end_kst = previous_kst_session_bounds(now_utc)
    since_ms = to_ms(start_kst)
    until_ms = to_ms(end_kst)

    # 바이낸스 선물
    exchange = ccxt.binance({
        "enableRateLimit": True,
        "options": {
            "defaultType": "future",
        },
    })
    exchange.load_markets()

    symbols = load_watchlist(WATCHLIST_PATH)
    # 입력 심볼이 거래소에 존재하는지 필터
    valid_syms = [s for s in symbols if s in exchange.symbols]
    invalid_syms = [s for s in symbols if s not in exchange.symbols]
    if invalid_syms:
        print(f"[경고] 거래소에 없는 심볼 {len(invalid_syms)}개: {invalid_syms[:10]}{' …' if len(invalid_syms)>10 else ''}")

    results = []
    for s in valid_syms:
        r = compute_session_performance(exchange, s, since_ms, until_ms)
        if r is not None:
            results.append(r)
        # 레이트리밋 안전 지연
        time.sleep(exchange.rateLimit / 1000.0 if getattr(exchange, 'rateLimit', 0) else 0.2)

    # 정렬
    results.sort(key=lambda x: x['pct'], reverse=True)
    message = format_message(results, start_kst, end_kst)
    print(message)
    send_telegram(message)
    print("✅ 메시지 전송 완료")

if __name__ == "__main__":
    main()
