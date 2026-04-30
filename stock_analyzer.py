"""
股票分析模組 v2
- 鴻海 / 台積電 / 聯發科 三股並行分析
- 每日成交量 / 金額 / 量比 / RSI
- TWSE 法人買賣超
- AI 新聞摘要（Claude）
"""

import os
import requests
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
import anthropic
from dotenv import load_dotenv
load_dotenv()

WATCH_LIST = {
    "2317.TW": "鴻海",
    "2330.TW": "台積電",
    "2454.TW": "聯發科",
}

TWSE_ID_MAP = {
    "2317.TW": "2317",
    "2330.TW": "2330",
    "2454.TW": "2454",
}


class StockAnalyzer:

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    def get_all_stocks_data(self) -> dict:
        result = {}
        for ticker in WATCH_LIST:
            try:
                df = yf.Ticker(ticker).history(period="30d")
                if df.empty:
                    continue
                closes  = df["Close"].tolist()
                volumes = df["Volume"].tolist()
                dates   = df.index.tolist()
                pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if len(closes) >= 2 else 0
                result[ticker] = {
                    "closes":     closes,
                    "volumes":    volumes,
                    "dates":      dates,
                    "change_pct": round(pct, 2),
                    "df":         df,
                }
            except Exception as e:
                print(f"取得 {ticker} 資料失敗: {e}")
        return result

    def get_daily_report(self, ticker: str = "2317.TW") -> dict:
        stock = yf.Ticker(ticker)
        df    = stock.history(period="30d")

        if df.empty:
            return {"error": f"無法取得 {ticker} 資料"}

        latest = df.iloc[-1]
        prev   = df.iloc[-2]

        price       = latest["Close"]
        volume      = latest["Volume"]
        prev_close  = prev["Close"]

        change      = price - prev_close
        change_pct  = (change / prev_close) * 100
        amount_yi   = volume * price / 1e8

        vol_5d_avg  = df["Volume"].iloc[-6:-1].mean()
        vol_ratio   = volume / vol_5d_avg if vol_5d_avg > 0 else 1.0

        df["MA5"]   = df["Close"].rolling(5).mean()
        df["MA20"]  = df["Close"].rolling(20).mean()

        rsi = self._calc_rsi(df["Close"])
        vol_signal = self._volume_signal(vol_ratio, change_pct)
        institutional = self._get_twse_institutional(TWSE_ID_MAP.get(ticker, ""))

        return {
            "ticker":         ticker,
            "name":           WATCH_LIST.get(ticker, ticker),
            "date":           datetime.now().strftime("%Y/%m/%d"),
            "price":          round(price, 2),
            "open":           round(latest["Open"], 2),
            "high":           round(latest["High"], 2),
            "low":            round(latest["Low"], 2),
            "change":         round(change, 2),
            "change_pct":     round(change_pct, 2),
            "volume_k":       int(volume / 1000),
            "amount_yi":      round(amount_yi, 2),
            "vol_ratio":      round(vol_ratio, 2),
            "vol_5d_avg_k":   int(vol_5d_avg / 1000),
            "ma5":            round(df["MA5"].iloc[-1], 2),
            "ma20":           round(df["MA20"].iloc[-1], 2),
            "rsi":            round(rsi, 1),
            "volume_signal":  vol_signal,
            "institutional":  institutional,
        }

    def get_multi_summary(self) -> list:
        summaries = []
        for ticker in WATCH_LIST:
            try:
                r = self.get_daily_report(ticker)
                if "error" not in r:
                    summaries.append(r)
            except Exception as e:
                print(f"摘要失敗 {ticker}: {e}")
        return summaries

    def get_weekly_report(self, ticker: str = "2317.TW") -> dict:
        stock = yf.Ticker(ticker)
        df    = stock.history(period="15d")

        if df.empty:
            return {"error": "無法取得資料"}

        df = df.tail(6)
        rows = []
        for i in range(1, len(df)):
            row  = df.iloc[i]
            prev = df.iloc[i-1]
            pct  = (row["Close"] - prev["Close"]) / prev["Close"] * 100
            rows.append({
                "date":       df.index[i].strftime("%m/%d"),
                "close":      round(row["Close"], 2),
                "volume_k":   int(row["Volume"] / 1000),
                "change_pct": round(pct, 2),
            })

        return {
            "type":   "weekly",
            "ticker": ticker,
            "name":   WATCH_LIST.get(ticker, ticker),
            "rows":   rows,
        }

    def get_news_summary(self, ticker: str = "2317.TW") -> str:
        stock = yf.Ticker(ticker)
        news  = stock.news

        if not news:
            return f"目前無最新新聞"

        headlines = "\n".join([
            f"- {n.get('content', {}).get('title', n.get('title', ''))}"
            for n in news[:6]
        ])

        name = WATCH_LIST.get(ticker, ticker)
        msg = self.client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=500,
            messages=[{
                "role": "user",
                "content": (
                    f"以下是{name}最新新聞標題（可能含英文，請全部翻譯成繁體中文後整理）。\n"
                    f"請用**繁體中文**整理成3個重點，每點一行，加emoji，簡潔有力，總長不超過180字。\n"
                    f"英文新聞標題必須先翻譯再摘要，所有輸出全程繁體中文：\n\n"
                    f"{headlines}"
                )
            }]
        )
        return f"📰 {name} 最新動態\n\n{msg.content[0].text}"

    def _get_twse_institutional(self, stock_id: str) -> dict:
        if not stock_id:
            return {}
        try:
            url  = "https://openapi.twse.com.tw/v1/fund/MI_QFIIS"
            resp = requests.get(url, timeout=8)
            if resp.status_code != 200:
                return {}
            for row in resp.json():
                if row.get("Code") == stock_id:
                    def to_int(key):
                        v = row.get(key, "0") or "0"
                        return int(v.replace(",", "").replace("+", ""))
                    foreign = to_int("Foreign_Investor_Net_Buy_Sell")
                    invest  = to_int("Investment_Trust_Net_Buy_Sell")
                    dealer  = to_int("Dealer_Net_Buy_Sell")
                    return {"foreign": foreign, "invest": invest,
                            "dealer": dealer, "total": foreign + invest + dealer}
        except Exception as e:
            print(f"TWSE API 錯誤: {e}")
        return {}

    def _volume_signal(self, vol_ratio: float, change_pct: float) -> str:
        if vol_ratio >= 2.0 and change_pct > 2:   return "🚀 爆量大漲 - 強勢訊號"
        if vol_ratio >= 2.0 and change_pct < -2:  return "⚠️ 爆量大跌 - 注意風險"
        if vol_ratio >= 1.5 and change_pct > 0:   return "📈 放量上漲 - 多方積極"
        if vol_ratio >= 1.5 and change_pct < 0:   return "📉 放量下跌 - 留意壓力"
        if vol_ratio < 0.6:                        return "😴 縮量整理 - 觀望氣氛"
        return "➡️ 量能正常"

    def _calc_rsi(self, prices: pd.Series, period: int = 14) -> float:
        delta    = prices.diff()
        gain     = delta.clip(lower=0)
        loss     = -delta.clip(upper=0)
        avg_gain = gain.rolling(period).mean()
        avg_loss = loss.rolling(period).mean()
        rs       = avg_gain / avg_loss.replace(0, float("nan"))
        rsi      = 100 - (100 / (1 + rs))
        val      = rsi.iloc[-1]
        return float(val) if not pd.isna(val) else 50.0