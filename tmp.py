# -*- coding: utf-8 -*-
"""
세션 랭킹봇 (KST 05:00 ~ 익일 04:59 기준)
- watchlist.json 심볼 리스트를 기준으로 '세션 저점 → (그 이후) 고점' 상승률 계산
- 텔레그램으로 순위표 전송
- GitHub Actions/CI에서 비대화형 실행 가능
- 451(지역 제한) 발생 시 텔레그램으로 사유 알리고 정상 종료(워크플로 녹색 유지) 옵션 포함
requirements: ccxt, requests, python-dotenv(로컬)
"""

import os
import json
import time
import requests
from datetime import datetime, timedelta, timezone
from typing import List, Dict, Optional

try:
    # 로컬 개발용 (.env). CI에서는 GitHub Secrets 사용
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import ccxt
from ccxt.base.errors import ExchangeNotAvailable

# ─────────────────────────────────────────────────────────────
# 0) 환경 변수
# ─────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")

WATCHLIST_PATH = os.getenv("WATCHLIST_PATH", "watchlist.json")
TIMEFRAME = os.getenv("TIMEFRAME", "5m")     # 세션 탐색용 기본 5분봉
TOP_N = int(os.getenv("TOP_N", "50"))        # 전송 순위 개수
EXCHANGE = os.getenv("EXCHANGE", "binanceusdm")  # 기본: 바이낸스 USD-M 선물

# 451시 워크플로 실패 대신 안내만 하고 성공 처리할지 여부
SOFT_FAIL_451 = os.getenv("SOFT_FAIL_451", "true").lower() in ("1", "true", "yes")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
    raise RuntimeError("⚠️ 환경변수 TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 비었어요.")

# ─────────────────────────────────────────────────────────────
# 1) KST 세션 계산 (05:00 ~ 익일 04:59:59)
# ─────────────────────────────────────────────────────────────
KST = timezone(timedelta(hours=9))

def previous_kst_session_bounds(now_utc: datetime) -> tuple[datetime, datetime]:
    """
    현재(UTC) 기준으로 '완료된' 마지막 KST 세션 구간 반환.
    세션: KST 05:00:00 ~ 익일 04:59:59
    """
    now_kst = now_utc.astimezone(KST)
    today_05 = now_kst.replace(hour=5, minute=0, second=0, microsecond=0)
    if now_kst >= today_05:
        start_kst = today_05 - timedelta(days=1)
        end_kst = today_05 - timedelta(seconds=1)  # 어제 05:00 ~ 오늘 04:59:59
    else:
        start_kst = today_05 - timedelta(days=2)
        end_kst = today_05 - timedelta(days=1, seconds=-1)  # 그제 05:00 ~ 어제 04:59:59
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
# 4) 거래소 생성 (451 우회 시도: binanceusdm 우선)
# ─────────────────────────────────────────────────────────────
def create_exchange():
    """
    EXCHANGE 환경변수에 따라 거래소 생성.
    - binanceusdm: fapi(USD-M) 전용 엔드포인트 사용 → api.binance.com(spot) 호출 최소화
    - bybit: 대안 거래소
    - (fallback) binance + defaultType=future
    """
    if EXCHANGE.lower() == "binanceusdm":
        ex = ccxt.binanceusdm({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
    elif EXCHANGE.lower() == "bybit":
        ex = ccxt.bybit({"enableRateLimit": True})
    else:
        ex = ccxt.binance({
            "enableRateLimit": True,
            "options": {"defaultType": "future"},
        })
    return ex

# ─────────────────────────────────────────────────────────────
# 5) 세션 퍼포먼스 계산
# ─────────────────────────────────────────────────────────────
def compute_session_performance(exchange, symbol: str, since_ms: int, until_ms: int) -> Optional[Dict]:
    """
    세션 내에서:
      1) 최저가(low) 시점을 찾고
      2) 그 이후 최고가(high) 찾은 뒤
      3) pct = (high - low) / low * 100
    """
    try:
        # 24h 세션 기준 5m 캔들 288개 → 여유 limit
        ohlcv = exchange.fetch_ohlcv(symbol, timeframe=TIMEFRAME, since=since_ms, limit=800)
    except Exception as e:
        print(f"[OHLCV 실패] {symbol} - {e}")
        return None

    if not ohlcv:
        return None

    rows = [row for row in ohlcv if since_ms <= row[0] <= until_ms]
    if len(rows) < 2:
        return None

    # (ts, low), (ts, high) 뽑기
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
# 6) 메시지 포맷
# ─────────────────────────────────────────────────────────────
def format_message(results: List[Dict], start_kst: datetime, end_kst: datetime) -> str:
    header = (
        f"📊 세션(🇰🇷KST) {start_kst.strftime('%Y-%m-%d %H:%M')} → {end_kst.strftime('%Y-%m-%d %H:%M')}\n"
        f"세션 저점 → 이후 고점 상승률 *TOP {min(TOP_N, len(results))}*\n"
    )
    lines = []
    for i, r in enumerate(results[:TOP_N], start=1):
        medal = "🥇" if i == 1 else "🥈" if i == 2 else "🥉" if i == 3 else f"{i}."
        flair = "🚀" if r["pct"] >= 20 else "🔥" if r["pct"] >= 10 else "⚡️"
        lines.append(f"{medal} {r['symbol']}: {r['pct']:.2f}% {flair}")

    msg = header + ("\n".join(lines) if lines else "_데이터가 없어요_")
    # 텔레그램 길이 보호
    if len(msg) > 3500:
        msg = msg[:3490] + "\n…(생략)"
    return msg

# ─────────────────────────────────────────────────────────────
# 7) 엔트리포인트
# ─────────────────────────────────────────────────────────────
def main():
    now_utc = datetime.now(timezone.utc)
    start_kst, end_kst = previous_kst_session_bounds(now_utc)
    since_ms, until_ms = to_ms(start_kst), to_ms(end_kst)

    exchange = create_exchange()

    # 451 등으로 load_markets 실패 시 처리
    try:
        exchange.load_markets()
    except ExchangeNotAvailable as e:
        txt = str(e)
        if "451" in txt and SOFT_FAIL_451:
            note = "ℹ️ 지역 제한(451)로 거래소 접속이 차단되어 이번 실행을 건너뜁니다.\n" \
                   "• 해결 옵션: 셀프호스티드 러너 / 프록시 / EXCHANGE=bybit"
            print(note)
            try:
                send_telegram(note)
            finally:
                return  # 정상 종료(액션 녹색)
        raise

    symbols = load_watchlist(WATCHLIST_PATH)

    # 거래소에 있는 심볼만 필터
    valid_syms = [s for s in symbols if s in exchange.symbols]
    invalid_syms = [s for s in symbols if s not in exchange.symbols]
    if invalid_syms:
        print(f"[경고] 미지원 심볼 {len(invalid_syms)}개: {invalid_syms[:10]}{' …' if len(invalid_syms)>10 else ''}")

    results: List[Dict] = []
    for s in valid_syms:
        r = compute_session_performance(exchange, s, since_ms, until_ms)
        if r:
            results.append(r)
        # 레이트리밋 배려
        delay = getattr(exchange, "rateLimit", 200) / 1000.0
        time.sleep(max(0.15, delay))

    results.sort(key=lambda x: x["pct"], reverse=True)
    msg = format_message(results, start_kst, end_kst)
    print(msg)
    send_telegram(msg)
    print("✅ 메시지 전송 완료")

if __name__ == "__main__":
    try:
        main()
    except ExchangeNotAvailable as e:
        # 451이 아닌 경우 등 치명적일 땐 실패로 남기기
        if "451" in str(e) and SOFT_FAIL_451:
            print("ℹ️ 451 감지, 안내 후 정상 종료.")
        else:
            raise
