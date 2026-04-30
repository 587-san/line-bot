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
import json
import requests
import feedparser
import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime
from zoneinfo import ZoneInfo
import anthropic

TZ = ZoneInfo("Asia/Taipei")

# ═══════════════════════════════════════════════════════
# 股票基本資料庫（產品 + 競爭地位）
# 每季更新一次即可
# ═══════════════════════════════════════════════════════
STOCK_PROFILES = {
    "2317.TW": {
        "name":     "鴻海",
        "products": [
            "AI 伺服器（GB200 NVL72 機架式系統，NVIDIA 最大組裝夥伴）",
            "iPhone 代工（全球最大蘋果製造商，佔鴻海營收約 50%）",
            "電動車（MIH 平台，與 Stellantis / 裕隆合作）",
            "雲端基礎設施（液冷散熱、電源管理模組）",
        ],
        "competitors": ["緯創", "廣達", "英業達"],
        "sector":   "電子製造服務（EMS）",
        "catalysts": ["NVIDIA GB300 新訂單", "iPhone 17 出貨季", "美國德州廠投產"],
        "risks":    ["美中關稅對 iPhone 組裝衝擊", "AI 伺服器毛利率稀釋", "鄭州廠勞動成本上升"],
    },
    "2330.TW": {
        "name":     "台積電",
        "products": [
            "3nm / 2nm 先進製程（蘋果、NVIDIA、AMD 最大客戶）",
            "CoWoS 先進封裝（AI 晶片唯一量產供應商，產能嚴重供不應求）",
            "SoIC 3D 堆疊（下世代異質整合）",
            "成熟製程（28nm 車用、工業 IoT）",
        ],
        "competitors": ["三星代工", "Intel Foundry（IFS）"],
        "sector":   "半導體代工",
        "catalysts": ["CoWoS 月產能提升", "N2 良率確認", "美國鳳凰城廠量產", "日本熊本廠"],
        "risks":    ["地緣政治台海風險", "美國出口管制升級", "先進製程資本支出過高"],
    },
    "2454.TW": {
        "name":     "聯發科",
        "products": [
            "天璣 9400 旗艦 SoC（三星 3nm，搭載 AI 引擎）",
            "Dimensity Auto 車用晶片（L2+ 自駕、座艙運算）",
            "Wi-Fi 7 / 5G 網通晶片（全球市佔第一）",
            "ASIC 客製晶片服務（AI 邊緣推理）",
        ],
        "competitors": ["高通 Snapdragon", "蘋果 A 系列（直接競爭旗艦機）"],
        "sector":   "IC 設計（行動通訊）",
        "catalysts": ["三星 Galaxy S26 採用天璣", "車用晶片進入大量出貨", "AI 手機換機潮"],
        "risks":    ["高通旗艦機市佔反攻", "中國手機市場需求疲軟", "3nm 良率爬坡"],
    },
    "2382.TW": {
        "name":     "廣達",
        "products": [
            "AI 伺服器機架（Meta、Microsoft、Google 主要供應商）",
            "筆電 ODM（Dell、HP、Lenovo）",
            "雲端基礎設施（交換器、儲存系統）",
            "醫療設備（血液透析機、醫療影像 AI）",
        ],
        "competitors": ["緯創", "英業達", "鴻海"],
        "sector":   "電子製造服務（EMS）",
        "catalysts": ["Meta AI 資本支出持續擴大", "Microsoft Azure 擴產", "GB300 訂單"],
        "risks":    ["AI 伺服器集中少數客戶", "筆電市場持續低迷", "關稅衝擊"],
    },
    "6669.TW": {
        "name":     "緯穎",
        "products": [
            "超大規模資料中心客製伺服器（Microsoft Azure 直供）",
            "AI 加速運算節點（HGX H100 / H200 系統）",
            "液冷散熱伺服器（直接液冷 DLC）",
            "儲存系統（JBODs、Flash Array）",
        ],
        "competitors": ["鴻海次品牌 Ingrasys", "廣達 QCT"],
        "sector":   "雲端資料中心硬體",
        "catalysts": ["Azure 資本支出 FY2025 達歷史高", "GB300 機架訂單", "液冷訂單放量"],
        "risks":    ["微軟單一客戶集中度 >80%", "AI 伺服器庫存調整風險", "關稅影響美國廠佈局"],
    },
    "2308.TW": {
        "name":     "台達電",
        "products": [
            "資料中心電源（AI 伺服器電源供應器全球第一）",
            "工業自動化（馬達驅動器、機器人控制器）",
            "EV 充電樁（全球前三大 DC 快充供應商）",
            "再生能源逆變器（太陽能、儲能系統）",
        ],
        "competitors": ["Vertiv", "Eaton", "ABB（工業）"],
        "sector":   "電力電子 / 工業自動化",
        "catalysts": ["AI 資料中心電力需求爆發", "EV 充電基礎建設補貼", "工廠自動化訂單"],
        "risks":    ["匯率損失（美元走強）", "EV 充電市場競爭加劇", "中國工業需求疲軟"],
    },
    "2379.TW": {
        "name":     "瑞昱",
        "products": [
            "乙太網路控制器（PC、伺服器 NIC，全球第一）",
            "Wi-Fi 7 / Bluetooth 複合晶片",
            "音效晶片（ALC 系列，主機板標配）",
            "交換器晶片（10G/25G/100G 資料中心用）",
        ],
        "competitors": ["Broadcom（高階）", "Marvell", "英特爾網路部門"],
        "sector":   "IC 設計（網路通訊）",
        "catalysts": ["AI 伺服器 NIC 升級到 100G", "Wi-Fi 7 路由器換機潮", "PCIe 5.0 NIC 出貨"],
        "risks":    ["AI 伺服器 NIC 被 NVIDIA ConnectX 搶單", "PC 市場復甦慢", "庫存去化"],
    },
    "3034.TW": {
        "name":     "聯詠",
        "products": [
            "顯示驅動 IC（DDIC，三星 / LGD OLED 面板主供應商）",
            "觸控控制器（TDDI，整合觸控與驅動）",
            "汽車顯示 IC（車載娛樂、儀表板）",
            "AMOLED 驅動（摺疊螢幕手機）",
        ],
        "competitors": ["天鈺科技", "Novatek 自身競爭格局穩固"],
        "sector":   "IC 設計（顯示驅動）",
        "catalysts": ["OLED 手機滲透率持續提升", "車載顯示器需求成長", "摺疊螢幕放量"],
        "risks":    ["面板廠資本支出削減", "中國本土 DDIC 競爭", "高端 OLED 集中三星一客戶"],
    },
    "2345.TW": {
        "name":     "智邦",
        "products": [
            "企業網路交換器（100G/400G，思科的最大 ODM 夥伴）",
            "AI 資料中心交換器（800G 正進入量產）",
            "白牌交換器（超大規模雲端商直購）",
            "網路安全設備（防火牆、SD-WAN 裝置）",
        ],
        "competitors": ["Celestica", "Edgecore（正文科技子公司）"],
        "sector":   "網通設備製造",
        "catalysts": ["AI 資料中心 800G 切換加速", "Cisco 外包比重提高", "白牌訂單增加"],
        "risks":    ["Cisco 自製化政策風險", "400G→800G 過渡期庫存調整", "關稅影響美國客戶採購"],
    },
    "3711.TW": {
        "name":     "日月光投控",
        "products": [
            "先進封裝（Fan-out、Flip-chip、SiP）",
            "CoWoS 競爭封裝（台積電 CoWoS 替代方案）",
            "半導體測試（全球前三大 IC 測試商）",
            "KGD 已知良品晶粒（HBM 配套測試）",
        ],
        "competitors": ["安靠", "長電科技（中）"],
        "sector":   "半導體封測",
        "catalysts": ["HBM4 封測訂單", "AI 晶片 SiP 需求", "汽車晶片封測成長"],
        "risks":    ["台積電 CoWoS 自留訂單擴大", "中國封測廠低價競爭", "先進封裝產能投資過重"],
    },
}

# 尚未建立詳細 profile 的股票，用通用格式
GENERIC_PROFILES = {
    "2303.TW": ("聯電", "成熟製程代工（28nm/40nm，車用/工業/MCU為主）", "半導體代工"),
    "2408.TW": ("南亞科", "DRAM 記憶體（PC/伺服器用 DDR4/DDR5）", "記憶體"),
    "2337.TW": ("旺宏", "NOR Flash 記憶體（汽車、工業 IoT 最大供應商）", "記憶體"),
    "1590.TW": ("亞德客", "氣壓自動化元件（工廠、半導體設備氣缸）", "工業自動化"),
    "6415.TW": ("矽力-KY", "類比 IC / 電源管理 IC（消費電子、AI 邊緣裝置）", "IC 設計"),
    "2207.TW": ("和泰車", "台灣 Toyota 代理商 + Lexus，附汽車金融", "汽車銷售"),
    "2412.TW": ("中華電", "台灣最大電信商，5G + 固網寬頻 + IDC 機房", "電信"),
    "2882.TW": ("國泰金", "國泰人壽（最大壽險）+ 國泰世華銀行", "金融保險"),
    "2886.TW": ("兆豐金", "兆豐銀行（外匯第一）+ 兆豐壽險", "金融銀行"),
    "1301.TW": ("台塑", "PVC / 乙烯塑膠原料，石化上游", "石化"),
    "2002.TW": ("中鋼", "台灣最大鋼鐵廠，熱軋/冷軋/鍍鋅鋼板", "鋼鐵"),
    "9910.TW": ("豐泰", "耐吉（Nike）最大鞋底代工商", "製造"),
    "2353.TW": ("緯創", "筆電 ODM + 伺服器（Dell/Apple/HP）", "電子製造"),
    "4904.TW": ("遠傳", "台灣第二大電信商 + momo 購物網", "電信"),
    "2882.TW": ("國泰金", "壽險 + 銀行 + 證券 金融控股", "金融"),
}


class StockScreener:

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # ═══════════════════════════════════════════════════════
    # 主入口
    # ═══════════════════════════════════════════════════════
    def screen_top5(self) -> dict:
        all_tickers = list(STOCK_PROFILES.keys()) + list(GENERIC_PROFILES.keys())
        # 去重
        all_tickers = list(dict.fromkeys(all_tickers))

        print(f"[選股 Layer1] 量化初篩 {len(all_tickers)} 檔...")
        scored = []
        for ticker in all_tickers:
            try:
                r = self._quantitative_score(ticker)
                if r:
                    scored.append(r)
                time.sleep(0.25)
            except Exception as e:
                print(f"  ⚠️ {ticker}: {e}")

        if not scored:
            return {"top5": [], "ai_analysis": "資料取得失敗，請稍後再試", "scanned_at": ""}

        scored.sort(key=lambda x: x["quant_score"], reverse=True)
        top10 = scored[:10]
        print(f"[選股 Layer1] 量化完成，前10名：{[s['name'] for s in top10]}")

        # Layer 2+3: 抓新聞 + 產品深度分析
        print(f"[選股 Layer2+3] 深度分析中...")
        enriched = []
        for s in top10:
            try:
                news   = self._fetch_stock_news(s["ticker"], s["name"])
                profile = self._get_profile(s["ticker"])
                s["news"]    = news
                s["profile"] = profile
                enriched.append(s)
                time.sleep(0.5)
            except Exception as e:
                s["news"]    = []
                s["profile"] = {}
                enriched.append(s)

        # Layer 4: Claude 抽絲剝繭 → 評級
        print(f"[選股 Layer4] Claude 深度評級中...")
        result = self._claude_deep_grade(enriched)

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

        t  = yf.Ticker(ticker)
        df = t.history(period="40d")
        if df.empty or len(df) < 10:
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
    # Layer 3：抓個股新聞
    # ═══════════════════════════════════════════════════════
    def _fetch_stock_news(self, ticker: str, name: str) -> list[dict]:
        items = []
        stock_id = ticker.replace(".TW", "")

        # Google News（中文優先）
        queries = [
            f"{name} 股票 財報",
            f"{name} 訂單 營收",
            f"{stock_id} {name}",
        ]
        for q in queries[:2]:
            try:
                url  = f"https://news.google.com/rss/search?q={requests.utils.quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:
                    title   = entry.get("title", "")
                    summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:150]
                    items.append({
                        "title":   title,
                        "summary": summary,
                        "source":  "Google 新聞",
                        "time":    entry.get("published", ""),
                    })
            except Exception:
                pass

        # yfinance 內建新聞
        try:
            news = yf.Ticker(ticker).get_news(count=4)
            for n in (news or []):
                content = n.get("content", {})
                title   = content.get("title", n.get("title", ""))
                summary = content.get("summary", "")[:150]
                if title:
                    items.append({
                        "title":   title,
                        "summary": summary,
                        "source":  "Yahoo Finance",
                        "time":    content.get("pubDate", ""),
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

        return unique[:6]

    # ═══════════════════════════════════════════════════════
    # Layer 4：Claude 深度評級
    # ═══════════════════════════════════════════════════════
    def _claude_deep_grade(self, stocks: list) -> dict:
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

        prompt = f"""You are an elite Taiwan equity analyst conducting deep due diligence.
Today: {today}

ANALYSIS MISSION: From the 10 pre-screened stocks below, identify the TOP 5 with the highest expected return over the next 5 trading days.

ANALYSIS PROTOCOL (strictly follow this order):
1. PRODUCT ANALYSIS (English): For each stock, analyze product cycle position, demand drivers, and competitive moat
2. NEWS DISSECTION (English): Extract signal from noise in recent news — separate fundamental changes from noise
3. CROSS-VALIDATION (English): Does the price/volume action confirm or contradict the fundamental story?
4. GRADE ASSIGNMENT: Based on above, assign grade A+/A/A-/B+/B/B-/C
5. FINAL SELECTION: Pick Top 5 and explain conviction level
6. TRANSLATE ALL CONCLUSIONS to Traditional Chinese

GRADING RUBRIC:
  A+: Strong momentum + positive catalyst + news confirms → Highest conviction buy
  A:  Good setup + fundamental support
  A-: Solid but 1 concern (valuation OR news OR technicals)
  B+: Decent setup, watching for confirmation
  B:  Mixed signals, needs more evidence
  B-: Some positives but risks outweigh
  C:  Avoid for next 5 days

PRE-SCREENED DATA:
{stock_blocks}

OUTPUT FORMAT (entirely in Traditional Chinese):

📊 台股深度選股報告
📅 {today}  掃描 {len(stocks)} 檔（初篩自 {len(STOCK_PROFILES)+len(GENERIC_PROFILES)} 檔）

══════════════════════════
🏆 未來5日推薦 Top 5
══════════════════════════

[依序列出5檔，每檔格式如下]

▋ #排名  股票名稱（代號）  ${price}  評級：X
   ─────────────────────
   📦 產品與業務分析
   （說明核心產品、市場地位、目前在產業週期的位置，3-4句）
   
   📰 新聞深度解讀
   （逐一解析重要新聞的實質意義，區分利多/利空/雜訊，2-3句）
   
   📈 技術面 × 基本面交叉驗證
   （量價行為是否與基本面故事吻合？1-2句）
   
   🎯 5日目標區間：$XXX – $XXX
   ⚡ 主要催化劑：（最可能在5日內發酵的1-2件事）
   ⚠️ 關鍵風險：（1句，若此事發生則停損）

══════════════════════════
📋 未入選 Top5 說明
══════════════════════════
（簡述其餘5檔未入選原因，各1句）

══════════════════════════
💡 本次選股核心主題
══════════════════════════
（1-2句，說明這5檔的共同邏輯或市場主線）

⚠️ 以上為 AI 量化 + 基本面輔助分析，不構成任何投資建議，請自行判斷風險"""

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=2000,
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
