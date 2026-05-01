"""
台股深度選股模組（10分鐘深度版）
────────────────────────────────────────────────────────
Layer 1  量化初篩（35檔）→ 淘汰弱勢，留 Top 15
Layer 2  產品週期分析（Top 15）→ 現在在哪個景氣位置
Layer 3  Google新聞逐條解讀（Top 15）→ 訊號 vs 雜訊
Layer 4  Claude 抽絲剝繭（英文思考 → 繁中輸出）→ 精選 Top 5
         評級：A+ / A / A- / B+ / B / B- / C
────────────────────────────────────────────────────────
"""

import os
import re
import time
import urllib.parse
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
# 監控清單（35檔，涵蓋AI/半導體/網通/電源/記憶體/封測）
# ═══════════════════════════════════════════════════════
STOCK_PROFILES = {
    # ── AI 伺服器供應鏈 ──
    "2317.TW": {
        "name": "鴻海", "sector": "AI伺服器／電子代工",
        "products": [
            "GB200 NVL72 AI伺服器機架（NVIDIA最大組裝商，全球市佔約40%）",
            "iPhone代工（全球最大蘋果製造商，佔鴻海營收約50%）",
            "MIH電動車平台（與Stellantis/裕隆合作）",
            "液冷散熱模組、電源管理系統",
        ],
        "competitors": ["廣達", "緯創", "英業達"],
        "catalysts": ["NVIDIA GB300/B300新訂單確認", "iPhone 17出貨旺季", "美國德州廠量產", "EV新車款發表"],
        "risks": ["美中關稅衝擊iPhone組裝成本", "AI伺服器毛利率被稀釋至6%以下", "鄭州廠勞動成本上升"],
    },
    "2382.TW": {
        "name": "廣達", "sector": "AI伺服器／雲端硬體",
        "products": [
            "AI伺服器機架系統（Meta、Microsoft Azure、Google主要供應商）",
            "QCT品牌AI運算節點（HGX H100/H200）",
            "筆電ODM（Dell、HP、Lenovo）",
            "醫療設備（血液透析機、醫療影像AI系統）",
        ],
        "competitors": ["鴻海", "緯創", "英業達"],
        "catalysts": ["Meta 2026資本支出650億美元計畫", "GB300 NVL72訂單放量", "Azure擴產"],
        "risks": ["AI伺服器集中少數雲端客戶", "筆電市場持續低迷", "美國關稅轉嫁壓力"],
    },
    "6669.TW": {
        "name": "緯穎", "sector": "超大規模資料中心",
        "products": [
            "Microsoft Azure客製AI伺服器（直接供應，客戶佔比>80%）",
            "液冷散熱AI運算節點（DLC直接液冷）",
            "All-Flash儲存陣列（NVMe SSD）",
            "Maia 100 AI加速器搭載系統（微軟自研晶片）",
        ],
        "competitors": ["鴻海旗下Ingrasys", "廣達QCT"],
        "catalysts": ["Azure FY2026資本支出歷史新高", "GB300機架出貨", "液冷訂單翻倍"],
        "risks": ["微軟單一客戶集中度極高", "AI伺服器需求放緩風險", "匯率影響（美元計價）"],
    },
    "2353.TW": {
        "name": "緯創", "sector": "AI伺服器／筆電ODM",
        "products": [
            "AI伺服器（蘋果Mac Pro代工、Dell/HP伺服器）",
            "筆電ODM（Apple MacBook Pro/Air最大代工商）",
            "工業電腦、醫療設備",
        ],
        "competitors": ["廣達", "鴻海", "英業達"],
        "catalysts": ["MacBook Air M4換機潮", "AI PC滲透率提升", "伺服器AI升級"],
        "risks": ["蘋果自製化風險", "筆電市場週期性低谷"],
    },
    # ── 半導體 ──
    "2330.TW": {
        "name": "台積電", "sector": "半導體代工",
        "products": [
            "3nm/2nm先進製程（蘋果A系列、NVIDIA Blackwell、AMD MI系列）",
            "CoWoS先進封裝（AI晶片唯一量產供應商，月產能持續爬坡）",
            "SoIC 3D異質整合（下世代HPC封裝）",
            "成熟製程28nm（車用、工業IoT）",
        ],
        "competitors": ["三星代工（良率落後）", "Intel Foundry（IFS虧損中）"],
        "catalysts": ["CoWoS月產能從7萬擴至10萬片", "N2良率突破65%確認", "鳳凰城廠N4量產", "日本熊本廠N6量產"],
        "risks": ["地緣政治台海風險溢價", "美國《晶片法》補貼條件限制", "先進封裝資本支出過重拖累ROE"],
    },
    "2454.TW": {
        "name": "聯發科", "sector": "IC設計／行動晶片",
        "products": [
            "天璣9400旗艦SoC（三星3nm製程，內建AI APU算力最強）",
            "Dimensity Auto車用晶片（L2+自駕、座艙多螢運算）",
            "Wi-Fi 7/Bluetooth 5.4複合晶片（全球市佔第一）",
            "ASIC客製AI邊緣推理晶片（新業務）",
        ],
        "competitors": ["高通Snapdragon 8 Elite（旗艦機主要對手）", "蘋果A18（直接競爭iPhone）"],
        "catalysts": ["三星Galaxy S26/小米15 Pro採用天璣確認", "車用晶片Q3進入大量出貨", "AI On-Device手機換機潮加速"],
        "risks": ["高通以台積電3nm反攻旗艦市場", "中國安卓手機市場需求疲軟", "3nm良率爬坡成本壓力"],
    },
    "2303.TW": {
        "name": "聯電", "sector": "成熟製程代工",
        "products": [
            "28nm/22nm製程（車用MCU、工業控制器、IoT主力節點）",
            "40nm/65nm（電源管理IC、觸控晶片）",
            "UMC Japan 12吋廠（車用嵌入式快閃記憶體）",
        ],
        "competitors": ["台積電（下一節點）", "中芯國際（中國市場）", "格芯GlobalFoundries"],
        "catalysts": ["車用晶片去庫存完成、新訂單回補", "日本廠車用eFlash需求", "AI Edge裝置28nm需求"],
        "risks": ["28nm中國本土廠（中芯）低價競爭", "車用客戶庫存調整尚未結束", "產能利用率偏低"],
    },
    "3711.TW": {
        "name": "日月光投控", "sector": "半導體封測",
        "products": [
            "先進封裝Fan-out WLP、Flip Chip（手機/AI SoC）",
            "SiP系統級封裝（Apple Watch、AirPods）",
            "HBM記憶體配套測試（CoWoS前後段）",
            "汽車晶片封測（SiP車用模組）",
        ],
        "competitors": ["安靠（Amkor）", "長電科技（中國）"],
        "catalysts": ["HBM4封測量產訂單", "AI晶片SiP需求爆發", "車用SiP模組出貨成長"],
        "risks": ["台積電CoWoS自留訂單持續擴大", "中國封測廠低價搶成熟封裝", "先進封裝設備投資回收期長"],
    },
    "2337.TW": {
        "name": "旺宏", "sector": "記憶體（NOR Flash）",
        "products": [
            "NOR Flash（汽車ECU韌體儲存，全球第一大供應商）",
            "工業級NOR Flash（工業IoT、網路設備）",
            "3D NAND（開發中，尚未量產）",
        ],
        "competitors": ["Winbond（華邦電）", "GigaDevice（中國）"],
        "catalysts": ["汽車NOR Flash去庫存完成、ASP回升", "ADAS/自駕需求帶動NOR容量升級", "MCU搭配需求"],
        "risks": ["電動車銷售放緩影響車用NOR出貨", "中國GigaDevice低價競爭成熟品", "3D NAND轉型投資風險"],
    },
    "2408.TW": {
        "name": "南亞科", "sector": "DRAM記憶體",
        "products": [
            "DDR4/DDR5 DRAM（PC、伺服器、工業用）",
            "LPDDR4X（行動裝置，量少）",
            "利基型DRAM（車用、工業用低功耗）",
        ],
        "competitors": ["三星（市佔45%）", "SK海力士（市佔30%）", "美光（市佔25%）"],
        "catalysts": ["DDR5伺服器滲透率突破60%", "AI伺服器DRAM需求帶動ASP上漲", "PC換機週期"],
        "risks": ["三星DRAM供給過剩壓價", "AI主流記憶體轉向HBM（南亞科無HBM）", "DRAM週期谷底延長"],
    },
    # ── 網通/電源 ──
    "2308.TW": {
        "name": "台達電", "sector": "電源／工業自動化",
        "products": [
            "AI資料中心電源供應器PSU（全球市佔第一，GB200配套）",
            "工業伺服馬達驅動器（台灣第一、亞洲前三）",
            "EV充電樁（直流快充，全球前三大供應商）",
            "太陽能逆變器、工業UPS不斷電系統",
        ],
        "competitors": ["Vertiv（IDC電源）", "Eaton", "ABB（工業）"],
        "catalysts": ["AI資料中心電力密度從40kW→130kW帶動PSU單價翻倍", "EV充電基礎建設補貼落地", "工廠自動化訂單回升"],
        "risks": ["美元走強侵蝕匯兌收益（台達海外營收佔80%）", "EV充電市場競爭白熱化", "中國工業自動化需求疲軟"],
    },
    "2379.TW": {
        "name": "瑞昱", "sector": "IC設計／網通",
        "products": [
            "乙太網路NIC控制器（PC/工作站/伺服器，全球市佔>60%）",
            "Wi-Fi 7/Bluetooth 5.4複合晶片（路由器、AP）",
            "2.5G/5G/10G Multi-Gig交換器晶片",
            "音效編解碼器（主機板ALC系列，幾乎壟斷）",
        ],
        "competitors": ["Broadcom（高階交換器）", "Marvell（伺服器NIC高端）", "Intel（網路事業部）"],
        "catalysts": ["AI伺服器NIC從10G升級到25G/100G（單價5倍）", "Wi-Fi 7家用路由換機潮", "PCIe 5.0 NIC大量出貨"],
        "risks": ["NVIDIA ConnectX搶走高階AI伺服器NIC份額", "PC市場復甦不如預期", "庫存去化進度落後"],
    },
    "2345.TW": {
        "name": "智邦", "sector": "網通設備製造",
        "products": [
            "AI資料中心400G/800G交換器（Cisco最大ODM夥伴）",
            "白牌交換器（超大規模雲端商直接採購，Meta/Microsoft）",
            "企業WiFi 6E/7 AP設備",
            "SD-WAN設備、網路安全裝置",
        ],
        "competitors": ["Celestica（加拿大）", "Edgecore正文科技"],
        "catalysts": ["AI資料中心800G切換進入爆發期", "Cisco ODM外包比重從35%升至50%", "白牌訂單取代品牌產品"],
        "risks": ["Cisco推動自製化策略風險", "400G→800G過渡期造成庫存調整", "美國關稅影響客戶採購決策"],
    },
    # ── 其他科技 ──
    "3034.TW": {
        "name": "聯詠", "sector": "IC設計／顯示驅動",
        "products": [
            "OLED顯示驅動IC DDIC（三星SDC、LG Display主要供應商）",
            "TDDI觸控顯示整合驅動IC（中階Android手機）",
            "車載顯示IC（儀表板、中控娛樂系統）",
            "摺疊螢幕高更新率DDIC（120Hz/144Hz）",
        ],
        "competitors": ["天鈺科技（Raydium）", "Novatek自身為市場領導者"],
        "catalysts": ["OLED手機全球滲透率突破50%", "三星Galaxy Z Fold7/Flip7摺疊機出貨", "車載顯示需求年增20%"],
        "risks": ["面板廠資本支出削減影響訂單", "中國本土DDIC廠商（天鈺等）低價競爭", "OLED高端客戶集中三星一家"],
    },
    "6415.TW": {
        "name": "矽力-KY", "sector": "IC設計／類比電源",
        "products": [
            "電源管理IC PMIC（手機、AI Edge裝置、IoT）",
            "DC-DC轉換器、LDO線性穩壓器",
            "車用電源管理IC（ADAS、車身控制）",
            "AI伺服器VRM電壓調節模組晶片",
        ],
        "competitors": ["德州儀器TI（高端）", "聖邦股份（中國）", "MPS（Monolithic Power）"],
        "catalysts": ["AI伺服器VRM晶片單台用量是PC的5倍", "車用PMIC進入量產期", "AI手機電源管理升級"],
        "risks": ["中國聖邦股份持續低價搶消費電子份額", "KY股結構（開曼設立）流動性折扣", "客戶集中度偏高"],
    },
    "1590.TW": {
        "name": "亞德客", "sector": "工業自動化／氣動元件",
        "products": [
            "氣壓缸、電磁閥（半導體廠、汽車廠自動化設備標配）",
            "電動缸、伺服驅動器（智慧製造轉型需求）",
            "半導體設備專用氣動元件（潔淨室規格）",
            "醫療設備氣動模組",
        ],
        "competitors": ["SMC（日本，市佔第一）", "費斯托Festo（德國）"],
        "catalysts": ["台灣半導體廠擴廠帶動設備需求", "中國製造業復甦訂單回升", "電動缸新產品放量"],
        "risks": ["中國自動化設備需求疲軟", "日圓升值壓縮SMC競爭力（但同樣壓縮亞德客相對優勢）", "全球製造業資本支出保守"],
    },
}

GENERIC_PROFILES = {
    "2303.TW": ("聯電",   "成熟製程代工28nm/40nm，車用/工業/MCU", "半導體代工"),
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

        # ── Layer 1：平行量化初篩 ──
        print(f"[Layer1] 平行掃描 {len(all_tickers)} 檔...")
        scored = []
        with ThreadPoolExecutor(max_workers=4) as pool:  # 配合 Fugle semaphore(2) 用4條線程
            futures = {pool.submit(self._quantitative_score, tk): tk for tk in all_tickers}
            for fut in as_completed(futures):
                try:
                    r = fut.result(timeout=30)
                    if r:
                        scored.append(r)
                except Exception as e:
                    print(f"  ⚠️ {futures[fut]}: {e}")

        if not scored:
            return {
                "top5": [], "ai_analysis": "⚠️ 股票資料暫時無法取得\n請 5 分鐘後再試",
                "scanned_at": datetime.now(TZ).strftime("%Y/%m/%d %H:%M"), "total_scanned": 0,
            }

        scored.sort(key=lambda x: x["quant_score"], reverse=True)
        top15 = scored[:15]
        print(f"[Layer1] {time.time()-t0:.1f}s  Top15：{[s['name'] for s in top15]}")

        # ── Layer 2+3：平行抓新聞（Top15 同時）──
        print(f"[Layer2+3] 平行抓新聞 + 產品分析...")

        def _enrich(s):
            s["news"]    = self._fetch_stock_news(s["ticker"], s["name"])
            s["profile"] = self._get_profile(s["ticker"])
            return s

        enriched = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [pool.submit(_enrich, s) for s in top15]
            for fut in as_completed(futures):
                try:
                    enriched.append(fut.result(timeout=40))
                except Exception as e:
                    print(f"  ⚠️ enrich: {e}")

        enriched.sort(key=lambda x: x["quant_score"], reverse=True)
        print(f"[Layer2+3] {time.time()-t0:.1f}s  完成")

        # ── Layer 4：Claude 深度評級 ──
        print(f"[Layer4] Claude 深度分析中...")
        result = self._claude_deep_grade(enriched, scored_count=len(scored))
        print(f"[Layer4] {time.time()-t0:.1f}s  完成")

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
    # Layer 3：抓個股新聞（Google News RSS，每股最多8則）
    # ═══════════════════════════════════════════════════════
    def _fetch_stock_news(self, ticker: str, name: str) -> list[dict]:
        items   = []
        stock_id = ticker.replace(".TW", "")
        # 三個角度查詢：基本面、訂單/財報、產業面
        queries = [
            f"{name} 股票 營收 財報",
            f"{name} 訂單 出貨 法說",
            f"{stock_id} {name}",
        ]

        for q in queries:
            try:
                url  = f"https://news.google.com/rss/search?q={urllib.parse.quote(q)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                feed = feedparser.parse(url)
                for entry in feed.entries[:4]:
                    title   = re.sub(r"<[^>]+>", "", entry.get("title", "")).strip()
                    summary = re.sub(r"<[^>]+>", "", entry.get("summary", ""))[:150].strip()
                    if title:
                        items.append({
                            "title":   title,
                            "summary": summary,
                            "time":    entry.get("published", ""),
                        })
            except Exception:
                pass

        # 去重 + 最多回傳8則
        seen, unique = set(), []
        for item in items:
            key = item["title"][:35]
            if key not in seen:
                seen.add(key)
                unique.append(item)

        return unique[:8]

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

        prompt = f"""You are a senior Taiwan equity research analyst at a top-tier fund. Conduct thorough due diligence on these {len(stocks)} pre-screened stocks.

Today: {today}

YOUR MANDATE: Select the TOP 5 stocks with highest probability of outperforming over the next 5 trading days. Dig deep — don't just repeat the data, INTERPRET it.

ANALYSIS FRAMEWORK (think in English, output Chinese):

Step 1 — PRODUCT CYCLE POSITION
For each stock: Where exactly is this company in its product/demand cycle? Is demand accelerating, peaking, or declining? What is the TAM and their capture rate? What differentiates them from competitors RIGHT NOW?

Step 2 — NEWS SIGNAL EXTRACTION  
Read every news headline carefully. Ask: Does this news change the earnings trajectory? Is it a one-time event or structural shift? What would a buy-side analyst think about this? Separate SIGNAL (changes fundamentals) from NOISE (PR/routine).

Step 3 — TECHNICAL × FUNDAMENTAL ALIGNMENT
Does the price/volume behavior confirm or contradict the fundamental story? High volume breakout + strong fundamental = conviction. Low volume drift + weak fundamental = avoid.

Step 4 — GRADE & RANK
Assign grade and rank all {len(stocks)} stocks. Then select Top 5 with written conviction.

GRADING SCALE:
A+ = Strong momentum + imminent catalyst + news confirms + technical breakout → Highest conviction
A  = Good fundamental setup + price confirming + no major near-term risks
A- = Solid setup but one concern (valuation stretched OR news mixed OR volume weak)
B+ = Interesting setup, waiting for one more confirmation signal
B  = Mixed — some positives but not enough conviction for 5-day hold
B- = More risks than opportunities near-term
C  = Clear underperformer for next 5 days, avoid

━━━━━ STOCK DATA ━━━━━
{stock_blocks}

━━━━━ OUTPUT (全程繁體中文) ━━━━━

📊 台股深度選股報告
📅 {today}  量化掃描 {scored_count} 檔 → 深度分析 {len(stocks)} 檔 → 精選 Top 5

{"═"*35}
🏆 未來5日精選 Top 5
{"═"*35}

[每檔按以下格式，依評級排序]

【#排名｜評級 X】股票名稱（代號）  現價 $XXX
─────────────────────────────
📦 產品週期分析
  現在位置：（此刻在景氣循環的哪個位置）
  核心優勢：（為何現在比競爭對手強）
  需求驅動：（未來6個月的需求來自哪裡）

📰 新聞深度解讀
  （逐條分析重要新聞，說明對EPS/訂單的實質影響）
  （明確區分：🟢利多 / 🔴利空 / ⚪雜訊）

📈 技術面驗證
  （量價關係是否與基本面吻合？一句話結論）

📊 量化數據
  現價 $XXX｜5日報酬 +X.X%｜量比 X.Xx｜RSI XX
  5日均線 $XXX｜20日均線 $XXX｜成交金額 XXX億

🎯 操作參數
  5日目標：$XXX – $XXX
  停損位：$XXX（跌破此價則出場）
  主要催化劑：（最可能在5天內發酵的1-2件事）

─────────────────────────────

{"═"*35}
📋 其餘 {len(stocks)-5} 檔評級與未入選原因
{"═"*35}
[每檔一行：評級 + 股名（代號）+ 未入選關鍵原因]

{"═"*35}
💡 本次市場主線
{"═"*35}
（這5檔的共同邏輯 + 當前大盤環境解讀，2-3句）

⚠️ 以上為AI量化+基本面深度輔助分析，不構成任何投資建議，請自行評估風險"""

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=3500,
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