"""
新聞監控模組
────────────────────────────────────────────────────────────
抓取會影響台股三雄的所有外部訊息：

【總體/地緣政治】
  - 川普 Truth Social / Twitter → 關稅、對中政策、科技出口管制
  - Fed 利率決策
  - 美中貿易戰

【半導體/科技產業】
  - NVIDIA / AMD 財報與展望
  - ASML 訂單（影響台積電）
  - 蘋果供應鏈（影響鴻海）
  - CHIPS Act 補貼進展

【市場指數】
  - NASDAQ、SOX（費城半導體）、VIX
  - USD/TWD 匯率

來源：
  - Alpha Vantage News Sentiment（免費 25 次/天）
  - NewsAPI（免費 100 次/天）
  - Google News RSS（免費，無限制）
  - yfinance built-in news
────────────────────────────────────────────────────────────
"""

import os
import re
import feedparser
import requests
import yfinance as yf
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
import anthropic

TZ = ZoneInfo("Asia/Taipei")

# ── 關鍵字清單：這些詞出現在新聞裡，Claude 要重點分析 ──
HIGH_IMPACT_KEYWORDS = [
    # 政治/關稅
    "tariff", "trade war", "export control", "chip ban", "sanction",
    "Trump", "Biden", "白宮", "關稅", "出口管制", "制裁",
    # 聯準會
    "Fed", "Federal Reserve", "interest rate", "rate cut", "rate hike",
    "FOMC", "Powell", "降息", "升息", "聯準會",
    # 半導體
    "TSMC", "NVIDIA", "ASML", "Samsung", "Intel", "AMD",
    "台積電", "輝達", "半導體", "晶圓", "先進封裝",
    # 鴻海相關
    "Foxconn", "Apple", "iPhone", "AI server", "鴻海", "蘋果", "AI伺服器",
    # 聯發科相關
    "MediaTek", "Qualcomm", "smartphone chip", "聯發科", "手機晶片",
    # 市場
    "NASDAQ", "SOX", "semiconductor index", "VIX", "recession",
    "科技股", "費城半導體",
]

# ── Google News RSS 查詢（不需 API key，Railway 上直接可用）──
# 優先查中文新聞，再補英文重大事件
GOOGLE_NEWS_QUERIES = [
    # 台股個股（中文）
    "台積電 半導體 股票",
    "鴻海 AI伺服器 股票",
    "聯發科 手機晶片 股票",
    # 外部衝擊（中文）
    "川普 關稅 台灣 半導體",
    "美國 聯準會 降息 科技股",
    "美中 晶片 出口管制",
    # 英文重大事件（補充）
    "Trump tariff semiconductor Taiwan",
    "Fed interest rate tech stocks",
    "NVIDIA AI server demand",
]


class NewsMonitor:

    def __init__(self):
        self.client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
        self.newsapi_key = os.environ.get("NEWSAPI_KEY", "")
        self.alphavantage_key = os.environ.get("ALPHAVANTAGE_KEY", "")

    # ─────────────────────────────────────────
    # 主入口：抓所有新聞並 AI 分析
    # ─────────────────────────────────────────
    def get_market_intelligence(self) -> dict:
        """
        回傳：
        {
          "macro": {...},          # 總經指標
          "news_items": [...],     # 原始新聞列表
          "ai_analysis": "...",    # Claude 綜合分析文字
          "impact_scores": {...},  # 各股影響分數 -3~+3
        }
        """
        # 1. 總經指標
        macro = self._get_macro_indicators()

        # 2. 抓多來源新聞
        news_items = []
        news_items.extend(self._fetch_google_news())
        news_items.extend(self._fetch_newsapi())
        news_items.extend(self._fetch_alphavantage_news())
        news_items.extend(self._fetch_yfinance_news())

        # 去重（依標題前 40 字）
        seen = set()
        unique_news = []
        for n in news_items:
            key = n["title"][:40]
            if key not in seen:
                seen.add(key)
                unique_news.append(n)

        # 只保留 24 小時內的新聞
        recent = self._filter_recent(unique_news, hours=24)

        # 3. AI 分析
        ai_result = self._claude_analyze(macro, recent)

        return {
            "macro":         macro,
            "news_items":    recent[:15],   # 最多回傳 15 則
            "ai_analysis":   ai_result["analysis"],
            "impact_scores": ai_result["scores"],
            "fetched_at":    datetime.now(TZ).strftime("%Y/%m/%d %H:%M"),
        }

    # ─────────────────────────────────────────
    # 總經指標
    # ─────────────────────────────────────────
    def _get_macro_indicators(self) -> dict:
        """
        抓 NASDAQ / SOX / VIX / USD-TWD 即時值
        用 yfinance，Railway 上完全可用
        """
        tickers = {
            "nasdaq":  "^IXIC",
            "sox":     "^SOX",
            "vix":     "^VIX",
            "usd_twd": "TWD=X",
            "us10y":   "^TNX",    # 美債10年期殖利率
        }

        result = {}
        for name, symbol in tickers.items():
            try:
                t  = yf.Ticker(symbol)
                df = t.history(period="2d")
                if df.empty:
                    result[name] = {"price": None, "change_pct": None}
                    continue
                price    = df["Close"].iloc[-1]
                prev     = df["Close"].iloc[-2]
                chg_pct  = (price - prev) / prev * 100
                result[name] = {
                    "price":      round(price, 2),
                    "change_pct": round(chg_pct, 2),
                }
            except Exception as e:
                result[name] = {"price": None, "change_pct": None}

        return result

    # ─────────────────────────────────────────
    # Google News RSS（免費、不需 key）
    # ─────────────────────────────────────────
    def _fetch_google_news(self) -> list[dict]:
        items = []
        for query in GOOGLE_NEWS_QUERIES:
            try:
                q   = requests.utils.quote(query)
                # hl=zh-TW gl=TW 優先抓繁體中文台灣新聞
                url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"
                feed = feedparser.parse(url)
                for entry in feed.entries[:3]:
                    title   = entry.get("title", "")
                    summary = entry.get("summary", "")
                    # 去除 HTML 標籤
                    summary = re.sub(r"<[^>]+>", "", summary)[:200]
                    items.append({
                        "title":   title,
                        "source":  "Google 新聞",
                        "url":     entry.get("link", ""),
                        "time":    entry.get("published", ""),
                        "summary": summary,
                    })
            except Exception as e:
                print(f"Google 新聞抓取失敗 ({query}): {e}")
        return items

    # ─────────────────────────────────────────
    # NewsAPI（免費 100 次/天，需申請 key）
    # ─────────────────────────────────────────
    def _fetch_newsapi(self) -> list[dict]:
        if not self.newsapi_key:
            return []

        queries = [
            "台積電 OR 鴻海 OR 聯發科 半導體",
            "川普 關稅 台灣 晶片",
            "聯準會 利率 科技股",
        ]
        items = []
        since = (datetime.utcnow() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ")

        for q in queries[:3]:
            try:
                resp = requests.get(
                    "https://newsapi.org/v2/everything",
                    params={
                        "q":        q,
                        "from":     since,
                        "sortBy":   "publishedAt",
                        "pageSize": 5,
                        "language": "zh",        # 優先中文新聞
                        "apiKey":   self.newsapi_key,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    articles = resp.json().get("articles", [])
                    # 若中文無結果，補抓英文
                    if not articles:
                        resp2 = requests.get(
                            "https://newsapi.org/v2/everything",
                            params={
                                "q":        q.replace("台積電", "TSMC").replace("鴻海", "Foxconn")
                                            .replace("聯發科", "MediaTek").replace("川普", "Trump")
                                            .replace("關稅", "tariff").replace("聯準會", "Federal Reserve")
                                            .replace("利率", "interest rate").replace("晶片", "chip")
                                            .replace("台灣", "Taiwan").replace("科技股", "tech stocks")
                                            .replace("半導體", "semiconductor"),
                                "from":     since,
                                "sortBy":   "publishedAt",
                                "pageSize": 5,
                                "apiKey":   self.newsapi_key,
                            },
                            timeout=10,
                        )
                        if resp2.status_code == 200:
                            articles = resp2.json().get("articles", [])

                    for art in articles:
                        items.append({
                            "title":   art.get("title", ""),
                            "source":  art.get("source", {}).get("name", "NewsAPI"),
                            "url":     art.get("url", ""),
                            "time":    art.get("publishedAt", ""),
                            "summary": art.get("description", "")[:200],
                        })
            except Exception as e:
                print(f"NewsAPI 錯誤: {e}")

        return items

    # ─────────────────────────────────────────
    # Alpha Vantage 新聞情緒（免費 25 次/天）
    # ─────────────────────────────────────────
    def _fetch_alphavantage_news(self) -> list[dict]:
        if not self.alphavantage_key:
            return []

        items = []
        # TSM = 台積電 ADR；FOXCONN 無美股，用 AAPL 代替關注供應鏈
        for ticker_us in ["TSM", "NVDA", "AAPL"]:
            try:
                resp = requests.get(
                    "https://www.alphavantage.co/query",
                    params={
                        "function": "NEWS_SENTIMENT",
                        "tickers":  ticker_us,
                        "limit":    5,
                        "apikey":   self.alphavantage_key,
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    for art in data.get("feed", []):
                        sentiment = art.get("overall_sentiment_label", "")
                        items.append({
                            "title":     art.get("title", ""),
                            "source":    art.get("source", "Alpha Vantage"),
                            "url":       art.get("url", ""),
                            "time":      art.get("time_published", ""),
                            "summary":   art.get("summary", "")[:200],
                            "sentiment": sentiment,   # Bearish/Neutral/Bullish
                        })
            except Exception as e:
                print(f"Alpha Vantage 錯誤 ({ticker_us}): {e}")

        return items

    # ─────────────────────────────────────────
    # yfinance 內建新聞
    # ─────────────────────────────────────────
    def _fetch_yfinance_news(self) -> list[dict]:
        items = []
        for ticker in ["2317.TW", "2330.TW", "2454.TW"]:
            try:
                news = yf.Ticker(ticker).get_news(count=5)
                for n in news:
                    content = n.get("content", {})
                    title   = content.get("title", n.get("title", ""))
                    summary = content.get("summary", "")
                    url     = content.get("canonicalUrl", {}).get("url", n.get("link", ""))
                    items.append({
                        "title":   title,
                        "source":  "Yahoo Finance",
                        "url":     url,
                        "time":    content.get("pubDate", ""),
                        "summary": summary[:200],
                    })
            except Exception as e:
                print(f"yfinance news 錯誤 ({ticker}): {e}")
        return items

    # ─────────────────────────────────────────
    # 篩選 24 小時內的新聞
    # ─────────────────────────────────────────
    def _filter_recent(self, items: list[dict], hours: int = 24) -> list[dict]:
        """保留最近 N 小時的新聞，無法解析時間的也保留"""
        cutoff = datetime.utcnow() - timedelta(hours=hours)
        recent = []
        for item in items:
            t_str = item.get("time", "")
            try:
                # Alpha Vantage 格式：20250101T120000
                if re.match(r"\d{8}T\d{6}", t_str):
                    dt = datetime.strptime(t_str, "%Y%m%dT%H%M%S")
                    if dt >= cutoff:
                        recent.append(item)
                    continue
                # RFC 2822 / ISO 格式
                from email.utils import parsedate_to_datetime
                dt = parsedate_to_datetime(t_str)
                if dt.replace(tzinfo=None) >= cutoff:
                    recent.append(item)
            except Exception:
                recent.append(item)   # 無法解析時間 → 保留
        return recent

    # ─────────────────────────────────────────
    # Claude AI 分析
    # ─────────────────────────────────────────
    def _claude_analyze(self, macro: dict, news: list[dict]) -> dict:
        """用 Claude 分析新聞對三股的影響，回傳分析文字 + 影響分數"""

        # 整理 macro 文字
        def fmt(d, label):
            p = d.get("price")
            c = d.get("change_pct")
            if p is None:
                return f"{label}：無資料"
            sign = "▲" if c >= 0 else "▼"
            return f"{label}：{p} ({sign}{abs(c):.1f}%)"

        macro_text = "\n".join([
            fmt(macro.get("nasdaq",  {}), "那斯達克 NASDAQ"),
            fmt(macro.get("sox",     {}), "費城半導體 SOX"),
            fmt(macro.get("vix",     {}), "恐慌指數 VIX"),
            fmt(macro.get("usd_twd", {}), "美元兌新台幣"),
            fmt(macro.get("us10y",   {}), "美債10年期殖利率"),
        ])

        # 整理新聞文字（最多15則）
        news_text = ""
        for i, n in enumerate(news[:15], 1):
            sentiment = n.get("sentiment", "")
            # 翻譯情緒標籤
            sent_map = {"Bullish": "偏多", "Bearish": "偏空",
                        "Somewhat-Bullish": "小幅偏多", "Somewhat-Bearish": "小幅偏空",
                        "Neutral": "中性"}
            sent_str = f"[{sent_map.get(sentiment, sentiment)}]" if sentiment else ""
            news_text += (
                f"{i}. {sent_str} {n['title']}\n"
                f"   來源：{n['source']}\n"
                f"   摘要：{n.get('summary','')[:100]}\n\n"
            )

        if not news_text:
            news_text = "目前無最新新聞"

        prompt = f"""You are a senior Taiwan stock market analyst. Analyze the following macro data and news, then provide conclusions in Traditional Chinese.

ANALYSIS RULES:
- Step 1: Analyze all source material in English first (preserves accuracy of financial/political nuance)
- Step 2: Translate and present ALL conclusions in Traditional Chinese
- For English news: understand in English, output conclusions in Chinese
- Never lose precision by pre-translating before analyzing

【MACRO INDICATORS】
{macro_text}

【LATEST NEWS (past 24 hours)】
{news_text}

Now analyze and output ENTIRELY in Traditional Chinese:

1. 📊 總體環境評估
   NASDAQ／費城半導體／VIX／新台幣匯率對台股科技股的整體影響（2-3句，含方向判斷）

2. 🔥 重大事件 Top 3
   最可能影響台股的3則新聞，每則包含：
   - 標題（翻譯成中文）
   - 影響方向：🟢 利多 ／ 🔴 利空 ／ ⚪ 中性
   - 核心影響邏輯（1句，精準）

3. 📈 個股影響分析
   • 鴻海 (2317)：[方向] — 原因（含供應鏈、AI伺服器、關稅等具體邏輯）
   • 台積電 (2330)：[方向] — 原因（含先進製程、美日廠、客戶需求等）
   • 聯發科 (2454)：[方向] — 原因（含手機晶片、AI邊緣運算、競爭格局等）

4. ⚠️ 最大尾部風險（1句，最需警惕的黑天鵝）

最後一行輸出JSON（僅此一行）：
{{"2317": 分數, "2330": 分數, "2454": 分數}}
分數：+3強烈利多 +2利多 +1小利多 0中性 -1小利空 -2利空 -3強烈利空

⚠️ 以上為AI輔助分析，不構成任何投資建議"""

        try:
            msg = self.client.messages.create(
                model="claude-sonnet-4-6",
                max_tokens=800,
                messages=[{"role": "user", "content": prompt}]
            )
            full_text = msg.content[0].text

            # 解析影響分數
            import json
            scores = {"2317": 0, "2330": 0, "2454": 0}
            json_match = re.search(r'\{[^}]*"2317"[^}]*\}', full_text)
            if json_match:
                try:
                    scores = json.loads(json_match.group())
                except Exception:
                    pass

            # 移除最後一行 JSON，只保留分析文字
            analysis = re.sub(r'\n?\{[^}]*"2317"[^}]*\}\s*$', "", full_text).strip()

            return {"analysis": analysis, "scores": scores}

        except Exception as e:
            print(f"Claude 分析失敗: {e}")
            return {
                "analysis": "⚠️ 新聞分析暫時無法使用",
                "scores":   {"2317": 0, "2330": 0, "2454": 0},
            }

    # ─────────────────────────────────────────
    # 格式化為 LINE 推播文字
    # ─────────────────────────────────────────
    def format_for_line(self, intel: dict) -> str:
        macro    = intel.get("macro", {})
        analysis = intel.get("ai_analysis", "")
        scores   = intel.get("impact_scores", {})
        now      = intel.get("fetched_at", "")

        def score_icon(s):
            if s >= 2:   return "🚀 強烈利多"
            if s == 1:   return "📈 小利多"
            if s == -1:  return "📉 小利空"
            if s <= -2:  return "🔻 強烈利空"
            return "➡️ 中性"

        def macro_line(key, label, unit=""):
            d = macro.get(key, {})
            p = d.get("price")
            c = d.get("change_pct")
            if p is None:
                return f"{label}：無資料"
            sign  = "▲" if c >= 0 else "▼"
            color = "🔴" if c >= 0 else "🟢"
            return f"{color} {label}：{p}{unit}（{sign}{abs(c):.1f}%）"

        header = (
            f"🌐 外部市場情報  {now}\n\n"
            f"【總經指標】\n"
            f"{macro_line('nasdaq',  '那斯達克')}\n"
            f"{macro_line('sox',     '費城半導體')}\n"
            f"{macro_line('vix',     '恐慌指數 VIX')}\n"
            f"{macro_line('usd_twd', '美元/新台幣')}\n"
            f"{macro_line('us10y',   '美債10年', '%')}\n\n"
            f"【AI 影響評估】\n"
            f"鴻海  {score_icon(scores.get('2317', 0))}\n"
            f"台積電 {score_icon(scores.get('2330', 0))}\n"
            f"聯發科 {score_icon(scores.get('2454', 0))}\n\n"
        )

        return header + analysis + "\n\n⚠️ 以上為 AI 輔助分析，不構成投資建議"
