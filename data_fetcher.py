"""
台股資料抓取模組（限速 + 重試版）
────────────────────────────────────────────
Fugle 免費方案有速率限制（~1-2 req/sec）
用 Semaphore 控制並發，加重試機制
────────────────────────────────────────────
"""

import os
import time
import threading
import requests
import pandas as pd
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")
FUGLE_BASE = "https://api.fugle.tw/marketdata/v1.0/stock"

# 限制同時最多 2 個 Fugle 請求（避免 rate limit）
_fugle_semaphore = threading.Semaphore(2)


class DataFetcher:

    def __init__(self):
        self.fugle_key = os.environ.get("FUGLE_API_KEY", "")
        self._session  = requests.Session()
        self._session.headers.update({
            "X-API-KEY":  self.fugle_key,
            "Accept":     "application/json",
            "User-Agent": "StockBot/1.0",
        })

    # ─────────────────────────────────────────
    # 對外介面
    # ─────────────────────────────────────────
    def get_history(self, ticker: str, days: int = 40) -> pd.DataFrame | None:
        stock_id = ticker.replace(".TW", "").replace(".tw", "")

        if self.fugle_key:
            df = self._fugle_with_retry(stock_id, days)
            if df is not None and not df.empty:
                return df
            print(f"  [Fetcher] Fugle 失敗 {stock_id}，改用 yfinance")

        return self._yfinance(ticker, days)

    # ─────────────────────────────────────────
    # Fugle：限速 + 重試
    # ─────────────────────────────────────────
    def _fugle_with_retry(self, stock_id: str, days: int,
                          retries: int = 3) -> pd.DataFrame | None:
        for attempt in range(retries):
            with _fugle_semaphore:          # 同時最多 2 個請求
                df = self._fugle_call(stock_id, days)

            if df is not None:
                return df

            if attempt < retries - 1:
                wait = 2.0 * (attempt + 1)  # 2s → 4s → 6s
                print(f"  [Fetcher] {stock_id} retry {attempt+1}，等 {wait}s")
                time.sleep(wait)
        return None

    def _fugle_call(self, stock_id: str, days: int) -> pd.DataFrame | None:
        try:
            end   = datetime.now(TZ).strftime("%Y-%m-%d")
            start = (datetime.now(TZ) - timedelta(days=days + 30)).strftime("%Y-%m-%d")

            resp = self._session.get(
                f"{FUGLE_BASE}/historical/candles/{stock_id}",
                params={"startDate": start, "endDate": end,
                        "fields": "open,high,low,close,volume"},
                timeout=12,
            )

            # 印出完整回應 key，方便 Railway log 除錯
            if resp.status_code == 200:
                data    = resp.json()
                candles = data.get("candles") or data.get("data") or []

                if not candles:
                    print(f"  [Fetcher] Fugle {stock_id} 空資料，keys={list(data.keys())[:5]}")
                    return None

                rows = []
                for c in candles:
                    try:
                        rows.append({
                            "Date":   pd.to_datetime(c.get("date") or c.get("Date", "")),
                            "Open":   float(c.get("open")   or 0),
                            "High":   float(c.get("high")   or 0),
                            "Low":    float(c.get("low")    or 0),
                            "Close":  float(c.get("close")  or 0),
                            "Volume": int(c.get("volume")   or 0),
                        })
                    except Exception:
                        continue

                if not rows:
                    return None

                df = pd.DataFrame(rows).set_index("Date").sort_index()
                print(f"  [Fetcher] OK {stock_id}: {len(df)} 筆")
                return df.tail(days)

            elif resp.status_code == 429:
                print(f"  [Fetcher] 429 rate limit {stock_id}")
                time.sleep(3)
                return None

            elif resp.status_code == 403:
                print(f"  [Fetcher] 403 {stock_id}: {resp.text[:100]}")
                return None

            else:
                print(f"  [Fetcher] HTTP {resp.status_code} {stock_id}: {resp.text[:100]}")
                return None

        except Exception as e:
            print(f"  [Fetcher] 例外 {stock_id}: {e}")
            return None

    # ─────────────────────────────────────────
    # yfinance 備援
    # ─────────────────────────────────────────
    def _yfinance(self, ticker: str, days: int) -> pd.DataFrame | None:
        try:
            import yfinance as yf
            df = yf.Ticker(ticker).history(period=f"{min(days+15,60)}d", timeout=15)
            if not df.empty:
                print(f"  [Fetcher] yfinance OK {ticker}: {len(df)} 筆")
                return df.tail(days)
        except Exception as e:
            print(f"  [Fetcher] yfinance 失敗 {ticker}: {str(e)[:50]}")
        return None