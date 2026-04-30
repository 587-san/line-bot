"""
台股資料抓取模組
────────────────────────────────────────────
主要來源：富果 Fugle Marketdata API（台灣本土，Railway 穩定可用）
備援來源：yfinance（本機測試用）

Fugle 免費方案：
  - 每日 5000 次請求，完全夠用
  - 申請：developer.fugle.tw → 免費註冊 → 取得 API Key
  - 填入 .env：FUGLE_API_KEY=xxxxx
────────────────────────────────────────────
"""

import os
import time
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Asia/Taipei")

FUGLE_BASE = "https://api.fugle.tw/marketdata/v1.0/stock"


class DataFetcher:
    """
    統一資料抓取介面
    screener / stock_analyzer 都透過這個 class 取資料
    """

    def __init__(self):
        self.fugle_key = os.environ.get("FUGLE_API_KEY", "")
        self._session  = requests.Session()
        self._session.headers.update({
            "X-API-KEY": self.fugle_key,
            "Accept": "application/json",
        })

    # ─────────────────────────────────────────
    # 取得 K 線歷史資料（回傳 DataFrame）
    # ─────────────────────────────────────────
    def get_history(self, ticker: str, days: int = 40) -> pd.DataFrame:
        """
        ticker: '2317.TW' 或 '2317'
        回傳: DataFrame with columns [Open, High, Low, Close, Volume]
        """
        stock_id = ticker.replace(".TW", "").replace(".tw", "")

        # 優先用 Fugle
        if self.fugle_key:
            df = self._fugle_candles(stock_id, days)
            if df is not None and not df.empty:
                return df

        # 備援：yfinance（本機測試 OK，Railway 可能失敗）
        return self._yfinance_history(ticker, days)

    # ─────────────────────────────────────────
    # 取得即時報價（今日）
    # ─────────────────────────────────────────
    def get_quote(self, ticker: str) -> dict:
        """
        回傳今日即時報價 dict:
        {price, open, high, low, volume, change, change_pct}
        """
        stock_id = ticker.replace(".TW", "")

        if self.fugle_key:
            q = self._fugle_quote(stock_id)
            if q:
                return q

        # 備援：從歷史資料取最後一天
        df = self._yfinance_history(ticker, 5)
        if df is not None and not df.empty:
            latest = df.iloc[-1]
            prev   = df.iloc[-2] if len(df) >= 2 else latest
            change = latest["Close"] - prev["Close"]
            return {
                "price":      round(latest["Close"], 2),
                "open":       round(latest["Open"], 2),
                "high":       round(latest["High"], 2),
                "low":        round(latest["Low"], 2),
                "volume":     int(latest["Volume"]),
                "change":     round(change, 2),
                "change_pct": round(change / prev["Close"] * 100, 2),
            }
        return {}

    # ─────────────────────────────────────────
    # 全市場當日收盤資料（批次，選股用）
    # ─────────────────────────────────────────
    def get_batch_quotes(self, tickers: list[str]) -> dict[str, dict]:
        """
        批次取得多檔股票今日報價
        回傳: {ticker: {price, change_pct, volume, ...}}
        """
        result = {}
        for ticker in tickers:
            try:
                q = self.get_quote(ticker)
                if q:
                    result[ticker] = q
                time.sleep(0.15)   # 避免 Fugle rate limit
            except Exception as e:
                print(f"  get_quote {ticker}: {e}")
        return result

    # ─────────────────────────────────────────
    # Fugle：K 線歷史
    # ─────────────────────────────────────────
    def _fugle_candles(self, stock_id: str, days: int) -> pd.DataFrame | None:
        """
        Fugle historical candles API
        GET /marketdata/v1.0/stock/historical/candles/{symbolId}
        """
        try:
            end   = datetime.now(TZ).strftime("%Y-%m-%d")
            start = (datetime.now(TZ) - timedelta(days=days + 20)).strftime("%Y-%m-%d")

            resp = self._session.get(
                f"{FUGLE_BASE}/historical/candles/{stock_id}",
                params={
                    "startDate": start,
                    "endDate":   end,
                    "fields":    "open,high,low,close,volume",
                },
                timeout=10,
            )

            if resp.status_code == 200:
                data = resp.json()
                candles = data.get("data", data.get("candles", []))
                if not candles:
                    return None

                rows = []
                for c in candles:
                    rows.append({
                        "Date":   pd.to_datetime(c.get("date", c.get("Date", ""))),
                        "Open":   float(c.get("open",  c.get("Open",  0))),
                        "High":   float(c.get("high",  c.get("High",  0))),
                        "Low":    float(c.get("low",   c.get("Low",   0))),
                        "Close":  float(c.get("close", c.get("Close", 0))),
                        "Volume": int(c.get("volume",  c.get("Volume", 0))),
                    })

                df = pd.DataFrame(rows)
                df = df.set_index("Date").sort_index()
                return df.tail(days)

            elif resp.status_code == 403:
                print(f"Fugle 403：API Key 未設定或權限不足")
            else:
                print(f"Fugle candles {stock_id}: HTTP {resp.status_code}")

        except Exception as e:
            print(f"Fugle candles {stock_id}: {e}")

        return None

    # ─────────────────────────────────────────
    # Fugle：即時報價
    # ─────────────────────────────────────────
    def _fugle_quote(self, stock_id: str) -> dict | None:
        """
        Fugle intraday quote API
        GET /marketdata/v1.0/stock/intraday/quote/{symbolId}
        """
        try:
            resp = self._session.get(
                f"{FUGLE_BASE}/intraday/quote/{stock_id}",
                timeout=8,
            )

            if resp.status_code == 200:
                d = resp.json()
                # Fugle quote 欄位
                price  = float(d.get("closePrice") or d.get("lastPrice") or 0)
                ref    = float(d.get("referencePrice") or price)
                change = price - ref

                return {
                    "price":      round(price, 2),
                    "open":       round(float(d.get("openPrice") or price), 2),
                    "high":       round(float(d.get("highPrice")  or price), 2),
                    "low":        round(float(d.get("lowPrice")   or price), 2),
                    "volume":     int(d.get("totalVolume") or 0),
                    "change":     round(change, 2),
                    "change_pct": round(change / ref * 100, 2) if ref else 0,
                }
        except Exception as e:
            print(f"Fugle quote {stock_id}: {e}")

        return None

    # ─────────────────────────────────────────
    # yfinance 備援
    # ─────────────────────────────────────────
    def _yfinance_history(self, ticker: str, days: int) -> pd.DataFrame | None:
        try:
            import yfinance as yf
            period = f"{min(days + 10, 60)}d"
            df = yf.Ticker(ticker).history(period=period, timeout=12)
            if not df.empty:
                return df.tail(days)
        except Exception as e:
            print(f"yfinance {ticker}: {e}")
        return None