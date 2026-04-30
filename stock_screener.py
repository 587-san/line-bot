"""
台股深度選股模組
────────────────────────────────────────────────────────
分析層次（由淺到深）：

Layer 1  量化初篩（全市場 ~25 檔）
  → 動能 / 量能 / 技術面 / RSI 評分
  → 淘汰明顯弱勢，留下 Top 10

Layer 2  產品與業務深度分析（Top 10 每檔）
  → 主要產品線與市場地位
  → 產業景氣週期位置
  → 競爭對手比較

Layer 3  新聞抽絲剝繭（Top 10 每檔）
  → 抓取個股近期新聞（Google News RSS + yfinance）
  → 英文新聞：先理解原意再分析，最後翻譯結論
  → 正面 / 負面 / 中性事件歸因

Layer 4  Claude 綜合評級（Top 10 → 選出 Top 5）
  → 英文思考：產品週期 × 技術面 × 新聞情緒
  → 輸出繁體中文報告
  → 評級：A+ / A / A- / B+ / B / B- / C
────────────────────────────────────────────────────────
"""

import os
import re
import time
import requests
import feedparser
import pandas as pd
import numpy as np
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from zoneinfo import ZoneInfo
import anthropic

from data_fetcher import DataFetcher

TZ      = ZoneInfo("Asia/Taipei")
fetcher = DataFetcher()

# ═══════════════════════════════════════════════════════
# 核心監控清單（精選15檔，速度快 + 覆蓋主要產業）
# ═══════════════════════════════════════════════════════
STOCK_PROFILES = {
    "2317.TW": {
        "name": "鴻海", "sector": "AI伺服器/電子代工",
        "products": ["GB200 NVL72 AI伺服器（NVIDIA最大組裝商）", "iPhone代工（佔營收50%）", "MIH電動車平台"],
        "catalysts": ["NVIDIA GB300新訂單", "iPhone 17出貨", "美國德州廠"],
        "risks": ["美中關稅衝擊iPhone", "AI伺服器毛利稀釋"],
    },
    "2330.TW": {
        "name": "台積電", "sector": "半導體代工",
        "products": ["3nm/2nm先進製程（蘋果/NVIDIA/AMD）", "CoWoS先進封裝（AI唯一量產）", "N2良率爬坡"],
        "catalysts": ["CoWoS月產能提升", "N2量產確認", "鳳凰城廠"],
        "risks": ["地緣政治風險", "出口管制升級"],
    },
    "2454.TW": {
        "name": "聯發科", "sector": "IC設計/行動晶片",
        "products": ["天璣9400旗艦SoC（三星3nm）", "Wi-Fi 7/5G網通（市佔第一）", "車用Dimensity Auto"],
        "catalysts": ["三星S26採用天璣", "AI手機換機潮", "車用放量"],
        "risks": ["高通旗艦反攻", "中國手機需求疲軟"],
    },
    "2382.TW": {
        "name": "廣達", "sector": "AI伺服器/雲端",
        "products": ["AI伺服器機架（Meta/Microsoft/Google）", "筆電ODM", "醫療AI設備"],
        "catalysts": ["Meta AI資本支出擴大", "GB300訂單", "Azure擴產"],
        "risks": ["客戶集中度高", "筆電市場低迷"],
    },
    "6669.TW": {
        "name": "緯穎", "sector": "超大規模資料中心",
        "products": ["Microsoft Azure客製伺服器（直供）", "液冷散熱HPC", "儲存Flash Array"],
        "catalysts": ["Azure FY2025資本支出歷史高", "GB300機架", "液冷放量"],
        "risks": ["微軟單一客戶>80%", "庫存調整風險"],
    },
    "2308.TW": {
        "name": "台達電", "sector": "電源/工業自動化",
        "products": ["AI伺服器電源（全球第一）", "EV充電樁（全球前三）", "工業馬達驅動器"],
        "catalysts": ["AI資料中心電力需求爆發", "EV充電補貼"],
        "risks": ["匯率損失", "EV競爭加劇"],
    },
    "2379.TW": {
        "name": "瑞昱", "sector": "IC設計/網通",
        "products": ["乙太網路NIC（PC/伺服器，全球第一）", "Wi-Fi 7晶片", "100G交換器IC"],
        "catalysts": ["AI伺服器NIC升級100G", "Wi-Fi 7路由換機潮"],
        "risks": ["NVIDIA ConnectX搶單", "PC市場慢"],
    },
    "2345.TW": {
        "name": "智邦", "sector": "網通設備",
        "products": ["AI資料中心交換器800G（思科最大ODM）", "白牌交換器雲端直購", "防火牆設備"],
        "catalysts": ["800G切換加速", "Cisco外包比重提高"],
        "risks": ["Cisco自製化風險", "庫存調整"],
    },
    "3034.TW": {
        "name": "聯詠", "sector": "IC設計/顯示驅動",
        "products": ["OLED顯示驅動IC（三星/LGD主供應商）", "車載顯示IC", "摺疊螢幕DDIC"],
        "catalysts": ["OLED手機滲透率提升", "車載顯示成長", "摺疊螢幕放量"],
        "risks": ["面板廠砍資本支出", "中國本土競爭"],
    },
    "3711.TW": {
        "name": "日月光投控", "sector": "半導體封測",
        "products": ["先進封裝Fan-out/SiP", "HBM配套測試", "汽車晶片封測"],
        "catalysts": ["HBM4封測訂單", "AI晶片SiP需求"],
        "risks": ["台積電CoWoS自留擴大", "中國低價競爭"],
    },
}

GENERIC_PROFILES = {
    "2303.TW": ("聯電",   "成熟製程代工28nm/40nm，車用/工業/MCU", "半導體代工"),
    "2337.TW": ("旺宏",   "NOR Flash記憶體，車用IoT最大供應商",  "記憶體"),
    "1590.TW": ("亞德客", "氣壓自動化元件，工廠/半導體設備",      "工業自動化"),
    "6415.TW": ("矽力-KY","類比IC/電源管理IC，AI邊緣裝置",        "IC設計"),
    "2408.TW": ("南亞科", "DRAM記憶體 DDR4/DDR5 PC/伺服器用",      "記憶體"),
}


class StockScreener:

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ═══════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════
    def screen_top5(self) -> dict:
        all_tickers = list(STOCK_PROFILES.keys()) + list(GENERIC_PROFILES.keys())
        all_tickers = list(dict.fromkeys(all_tickers))
        t0 = time.time()

        # ── Layer 1：平行量化初篩（全部同時跑）──
        print(f"[選股 Layer1] 平行量化初篩 {len(all_tickers)} 檔...")
        scored = []
        with ThreadPoolExecutor(max_workers=6) as pool:
            futures = {pool.submit(self._quantitative_score, tk): tk for tk in all_tickers}
            for fut in as_completed(futures):
                try:
                    r = fut.result(timeout=15)
                    if r:
                        scored.append(r)
                except Exception as e:
                    print(f"  ⚠️ {futures[fut]}: {e}")

        if not scored:
            return {
                "top5": [],
                "ai_analysis": "⚠️ 股票資料暫時無法取得\n請 5 分鐘後再試，或傳「潛力股」重新觸發",
                "scanned_at": datetime.now(TZ).strftime("%Y/%m/%d %H:%M"),
                "total_scanned": 0,
            }

        scored.sort(key=lambda x: x["quant_score"], reverse=True)
        top5_candidates = scored[:5]   # 直接取 Top5，減少 Claude 負擔
        print(f"[Layer1] {time.time()-t0:.1f}s  Top5候選：{[s['name'] for s in top5_candidates]}")

        # ── Layer 2+3：平行抓新聞（Top5 同時抓）──
        print(f"[選股 Layer2+3] 平行抓新聞...")

        def _enrich(s):
            s["news"]    = self._fetch_stock_news(s["ticker"], s["name"])
            s["profile"] = self._get_profile(s["ticker"])
            return s

        enriched = []
        with ThreadPoolExecutor(max_workers=5) as pool:
            futures = [pool.submit(_enrich, s) for s in top5_candidates]
            for fut in as_completed(futures):
                try:
                    enriched.append(fut.result(timeout=20))
                except Exception as e:
                    print(f"  ⚠️ enrich: {e}")

        # 維持原始排序
        enriched.sort(key=lambda x: x["quant_score"], reverse=True)
        print(f"[Layer2+3] {time.time()-t0:.1f}s  新聞抓取完成")

        # ── Layer 4：Claude 深度分析（只分析 Top5）──
        print(f"[選股 Layer4] Claude 深度評級中...")
        result = self._claude_deep_grade(enriched, scored_count=len(scored))
        print(f"[Layer4] {time.time()-t0:.1f}s  分析完成")

        return {
            "top5":          result["top5"],
            "ai_analysis":   result["analysis"],
            "scanned_at":    datetime.now(TZ).strftime("%Y/%m/%d %H:%M"),
            "total_scanned": len(scored),
        }


    # ═══════════════════════════════════════════════════════
    # Layer 1：量化評分
    # ═══════════════════════════════════════════════════════
    def _quantitative_score(self, ticker: str) -> dict | None:
        name = (STOCK_PROFILES.get(ticker, {}).get("name")
                or GENERIC_PROFILES.get(ticker, ("",))[0]
                or ticker)

        df = fetcher.get_history(ticker, days=40)
        if df is None or df.empty or len(df) < 10:
            return None

        closes  = df["Close"].values
        volumes = df["Volume"].values
        price   = closes[-1]

        # 動能：近5日、近10日報酬
        ret5  = (closes[-1] - closes[-6])  / closes[-6]  * 100
        ret10 = (closes[-1] - closes[-11]) / closes[-11] * 100

        # 量比
        avg5v    = np.mean(volumes[-6:-1])
        vol_ratio = volumes[-1] / avg5v if avg5v > 0 else 1.0

        # 均線
        ma5  = np.mean(closes[-5:])
        ma20 = np.mean(closes[-20:])

        # RSI
        rsi = self._calc_rsi(pd.Series(closes))

        # 成交金額（億）
        amount_yi = (volumes[-1] * price) / 1e8

        # 評分
        m_score = (3 if ret5 > 5 else 2 if ret5 > 2 else 1 if ret5 > 0 else -1 if ret5 > -3 else -2)
        v_score = (3 if vol_ratio > 2.5 else 2 if vol_ratio > 1.5 else 1 if vol_ratio > 1.0 else 0)
        t_score = sum([price > ma5, ma5 > ma20, price > ma20])
        r_score = (2 if 40 <= rsi <= 65 else 1 if 30 <= rsi < 40 else 1 if 65 < rsi <= 72 else 0 if rsi > 72 else -1)

        return {
            "ticker":      ticker,
            "name":        name,
            "price":       round(price, 2),
            "change_pct":  round((closes[-1] - closes[-2]) / closes[-2] * 100, 2),
            "ret5d":       round(ret5, 2),
            "ret10d":      round(ret10, 2),
            "vol_ratio":   round(vol_ratio, 2),
            "rsi":         round(rsi, 1),
            "ma5":         round(ma5, 2),
            "ma20":        round(ma20, 2),
            "amount_yi":   round(amount_yi, 2),
            "quant_score": m_score + v_score + t_score + r_score,
            "scores":      {"momentum": m_score, "volume": v_score,
                            "technical": t_score, "rsi": r_score},
        }

    # ═══════════════════════════════════════════════════════
    # Layer 2：取得產品 Profile
    # ═══════════════════════════════════════════════════════
    def _get_profile(self, ticker: str) -> dict:
        if ticker in STOCK_PROFILES:
            return STOCK_PROFILES[ticker]
        if ticker in GENERIC_PROFILES:
            t = GENERIC_PROFILES[ticker]
            return {"name": t[0], "products": [t[1]], "sector": t[2],
                    "competitors": [], "catalysts": [], "risks": []}
        return {}

    # ═══════════════════════════════════════════════════════
    # Layer 3：抓個股新聞（只用 Google News RSS）
    # ═══════════════════════════════════════════════════════
    def _fetch_stock_news(self, ticker: str, name: str) -> list[dict]:
        import urllib.parse
        items   = []
        queries = [f"{name} 股票 營收", f"{name} 訂單 財報"]

        for q in queries:
            try:
                url  = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:
                    title   = re.sub(r"<[^>]+>", "", entry.get("title", ""))
                    summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:120]
                    if title:
                        items.append({
                            "title":   title,
                            "summary": summary,
                            "source":  "Google新聞",
                        })
            except Exception:
                pass

        # 去重
        seen, unique = set(), []
        for item in items:
            key = item["title"][:30]
            if key not in seen:
                seen.add(key)
                unique.append(item)

        return unique[:5]

    # ═══════════════════════════════════════════════════════
    # Layer 4：Claude 深度評級
    # ═══════════════════════════════════════════════════════
    def _claude_deep_grade(self, stocks: list, scored_count: int = 0) -> dict:
        total_stocks = len(STOCK_PROFILES) + len(GENERIC_PROFILES)
        today = datetime.now(TZ).strftime("%Y-%m-%d")

        # 為每檔整理完整資料
        stock_blocks = ""
        for i, s in enumerate(stocks, 1):
            profile = s.get("profile", {})
            news    = s.get("news", [])

            products_text = "\n    ".join(profile.get("products", ["N/A"]))
            competitors   = "、".join(profile.get("competitors", [])) or "N/A"
            catalysts     = "、".join(profile.get("catalysts", [])) or "N/A"
            risks         = "、".join(profile.get("risks", [])) or "N/A"

            news_text = ""
            for n in news:
                news_text += f"    • {n['title']}\n      {n['summary'][:80]}\n"
            if not news_text:
                news_text = "    （無最新新聞）\n"

            stock_blocks += f"""
━━━ #{i} {s['name']} ({s['ticker']}) ━━━
[Quantitative]
  Price: {s['price']}  5D Return: {s['ret5d']:+.1f}%  10D Return: {s['ret10d']:+.1f}%
  Vol Ratio: {s['vol_ratio']}x  RSI: {s['rsi']}  Amount: {s['amount_yi']}B TWD
  MA5: {s['ma5']}  MA20: {s['ma20']}
  Quant Score: {s['quant_score']} (M:{s['scores']['momentum']} V:{s['scores']['volume']} T:{s['scores']['technical']} R:{s['scores']['rsi']})

[Products & Business]
  Sector: {profile.get('sector','N/A')}
  Key Products:
    {products_text}
  Competitors: {competitors}
  Potential Catalysts: {catalysts}
  Key Risks: {risks}

[Recent News]
{news_text}
"""

        prompt = f"""You are a Taiwan equity analyst. Analyze these {len(stocks)} pre-screened stocks and output a deep report in Traditional Chinese.

Today: {today}

ANALYSIS STEPS (do in English first, output Chinese):
1. PRODUCT CYCLE: Where is each company in its product/demand cycle right now?
2. NEWS SIGNAL: What do the news headlines actually mean for earnings/orders? (signal vs noise)
3. TECHNICAL CONFIRM: Does volume/price action match the fundamental story?
4. GRADE: A+/A/A-/B+/B/B-/C based on 5-day outlook

PRE-SCREENED DATA:
{stock_blocks}

OUTPUT (全程繁體中文):

📊 台股選股報告｜{today}
掃描 {scored_count} 檔 → 精選前 {len(stocks)} 檔深度分析

{"═"*30}

依評級由高到低列出，每檔格式：

【評級 X】股票名稱（代號）$收盤價
📦 產品分析：核心產品現在在哪個週期？競爭優勢？（2-3句）
📰 新聞解讀：新聞對營收/訂單的實質影響（區分利多/利空/雜訊，2句）
📈 量價驗證：技術面與基本面是否吻合？（1句）
🎯 5日目標：$低點–$高點
⚡ 催化劑：最可能在5天內發酵的事
⚠️ 停損條件：若此事發生立刻出場

{"─"*25}

💡 本次主線：（這幾檔共同的市場邏輯，1句）

⚠️ AI輔助分析，不構成投資建議"""

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=1500,
                messages=[{"role": "user", "content": prompt}]
            )
            analysis = msg.content[0].text

            # 解析 Top5 tickers
            tickers_in_text = re.findall(r'\b(\d{4})\.TW\b', analysis)
            if not tickers_in_text:
                tickers_in_text = re.findall(r'（(\d{4})）', analysis)
            top5_data = []
            seen = set()
            for tk_id in tickers_in_text:
                tk = tk_id if ".TW" in tk_id else f"{tk_id}.TW"
                if tk not in seen:
                    seen.add(tk)
                    match = next((s for s in stocks if s["ticker"] == tk), None)
                    if match:
                        top5_data.append(match)
            if len(top5_data) < 5:
                top5_data = stocks[:5]

            return {"top5": top5_data[:5], "analysis": analysis}

        except Exception as e:
            print(f"Claude 深度評級失敗: {e}")
            return {
                "top5":     stocks[:5],
                "analysis": f"⚠️ AI 深度分析暫時無法使用\n\n量化前5名：\n" + "\n".join(
                    [f"{i+1}. {s['name']}（{s['ticker']}）量化分 {s['quant_score']}" for i, s in enumerate(stocks[:5])]
                ),
            }

    # ═══════════════════════════════════════════════════════
    # RSI(14)
    # ═══════════════════════════════════════════════════════
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

    # ═══════════════════════════════════════════════════════
    # LINE 格式化
    # ═══════════════════════════════════════════════════════
    def format_for_line(self, result: dict) -> str:
        return (
            f"🔍 掃描 {result.get('total_scanned','?')} 檔  "
            f"更新時間 {result.get('scanned_at','')}\n\n"
            + result.get("ai_analysis", "資料取得失敗")
        )