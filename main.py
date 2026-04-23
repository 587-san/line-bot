import os
import requests
import yfinance as yf
import xml.etree.ElementTree as ET
from urllib.parse import quote

from dotenv import load_dotenv
from flask import Flask, request, abort

from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration,
    ApiClient,
    MessagingApi,
    ReplyMessageRequest,
    PushMessageRequest,
    TextMessage,
    ImageMessage,
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent, FollowEvent

from dividend_calendar import DividendCalendar
from chart_generator import ChartGenerator

try:
    import anthropic
except Exception:
    anthropic = None


load_dotenv()

LINE_CHANNEL_ACCESS_TOKEN = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
LINE_CHANNEL_SECRET = os.getenv("LINE_CHANNEL_SECRET", "").strip()

TARGET_USER_ID = os.getenv("TARGET_USER_ID", "").strip()
PUSH_TARGET_USER_IDS_RAW = os.getenv("push_target_user_ids", "").strip()

ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "").strip()
ANTHROPIC_MODEL = os.getenv("ANTHROPIC_MODEL", "claude-3-5-haiku-latest").strip()

if not LINE_CHANNEL_ACCESS_TOKEN or not LINE_CHANNEL_SECRET:
    raise ValueError("請先設定 LINE_CHANNEL_ACCESS_TOKEN 與 LINE_CHANNEL_SECRET")


def parse_push_target_user_ids() -> list[str]:
    ids = []

    if TARGET_USER_ID:
        ids.append(TARGET_USER_ID)

    if PUSH_TARGET_USER_IDS_RAW:
        ids.extend([x.strip() for x in PUSH_TARGET_USER_IDS_RAW.split(",") if x.strip()])

    seen = set()
    result = []
    for uid in ids:
        if uid not in seen:
            seen.add(uid)
            result.append(uid)

    return result


PUSH_TARGET_USER_IDS = parse_push_target_user_ids()

print("TARGET_USER_ID =", repr(TARGET_USER_ID))
print("push_target_user_ids raw =", repr(PUSH_TARGET_USER_IDS_RAW))
print("PUSH_TARGET_USER_IDS =", PUSH_TARGET_USER_IDS)
print("ANTHROPIC_API_KEY exists =", bool(ANTHROPIC_API_KEY))

app = Flask(__name__)

configuration = Configuration(access_token=LINE_CHANNEL_ACCESS_TOKEN)
handler = WebhookHandler(LINE_CHANNEL_SECRET)


STOCK_ALIAS = {
    "鴻海": ("2317.TW", "鴻海"),
    "2317": ("2317.TW", "鴻海"),
    "2317.tw": ("2317.TW", "鴻海"),

    "台積電": ("2330.TW", "台積電"),
    "2330": ("2330.TW", "台積電"),
    "2330.tw": ("2330.TW", "台積電"),
    "tsmc": ("2330.TW", "台積電"),

    "聯發科": ("2454.TW", "聯發科"),
    "2454": ("2454.TW", "聯發科"),
    "2454.tw": ("2454.TW", "聯發科"),
    "mediatek": ("2454.TW", "聯發科"),
}

NEWS_QUERY_ALIAS = {
    "鴻海": '"鴻海" OR "Foxconn" OR "2317"',
    "台積電": '"台積電" OR "TSMC" OR "2330"',
    "聯發科": '"聯發科" OR "MediaTek" OR "2454"',
}


def normalize_stock_keyword(text: str):
    key = text.strip().lower()
    return STOCK_ALIAS.get(key)


def reply_text(reply_token: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=[TextMessage(text=text[:5000])]
            )
        )


def reply_messages(reply_token: str, messages: list):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.reply_message(
            ReplyMessageRequest(
                reply_token=reply_token,
                messages=messages
            )
        )


def push_text(to_user_id: str, text: str):
    with ApiClient(configuration) as api_client:
        line_bot_api = MessagingApi(api_client)
        line_bot_api.push_message(
            PushMessageRequest(
                to=to_user_id,
                messages=[TextMessage(text=text[:5000])]
            )
        )


def push_text_to_all(user_ids: list[str], text: str):
    for uid in user_ids:
        push_text(uid, text)


def get_help_text() -> str:
    return (
        "可用指令：\n"
        "1. 功能\n"
        "2. ping\n"
        "3. 我的ID\n"
        "4. 今年股利\n"
        "5. 新聞 鴻海\n"
        "6. 新聞 台積電\n"
        "7. 新聞 聯發科\n"
        "8. 三雄\n"
        "9. 推播名單\n"
        "10. 推播測試"
    )


def fetch_news_rss(keyword: str, limit: int = 5) -> list[dict]:
    stock = normalize_stock_keyword(keyword)

    if stock:
        _ticker, name = stock
        if name in NEWS_QUERY_ALIAS:
            query = f'({NEWS_QUERY_ALIAS[name]}) (台股 OR 股票 OR 財報 OR 法說會 OR 半導體)'
        else:
            query = f'"{name}" 台股 OR 股票'
    else:
        query = f'"{keyword}" 台股 OR 股票'

    q = quote(query)
    url = f"https://news.google.com/rss/search?q={q}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"

    resp = requests.get(url, timeout=15)
    resp.raise_for_status()

    root = ET.fromstring(resp.content)
    items = root.findall(".//item")

    news = []
    for item in items[:limit]:
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        pub_date = (item.findtext("pubDate") or "").strip()
        news.append({
            "title": title,
            "link": link,
            "pub_date": pub_date,
        })
    return news


def summarize_news(keyword: str, news_items: list[dict]) -> str:
    if not news_items:
        return f"目前找不到「{keyword}」的近期新聞"

    headlines = "\n".join([f"{i+1}. {item['title']}" for i, item in enumerate(news_items)])

    if not ANTHROPIC_API_KEY or anthropic is None:
        return (
            f"【{keyword} 新聞重點】\n"
            f"{headlines}\n\n"
            "未設定 ANTHROPIC_API_KEY，先顯示新聞標題。"
        )

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

    prompt = f"""
你是台股資訊摘要助手。
請根據以下新聞標題，用繁體中文整理：
1. 3點重點摘要
2. 市場可能關注什麼
3. 不要亂編，僅依標題合理整理
4. 控制在 180 字內

公司：{keyword}

新聞標題：
{headlines}
"""

    msg = client.messages.create(
        model=ANTHROPIC_MODEL,
        max_tokens=300,
        temperature=0.2,
        messages=[{"role": "user", "content": prompt}]
    )

    summary_text = ""
    for block in msg.content:
        if getattr(block, "type", "") == "text":
            summary_text += block.text

    return f"【{keyword} AI 新聞摘要】\n{summary_text.strip()}"


def fetch_comparison_data() -> dict:
    tickers = ["2317.TW", "2330.TW", "2454.TW"]
    result = {}

    for ticker in tickers:
        df = yf.download(
            ticker,
            period="3mo",
            interval="1d",
            progress=False,
            auto_adjust=False
        )

        if df.empty:
            continue

        close_data = df["Close"]
        volume_data = df["Volume"]

        if hasattr(close_data, "iloc") and getattr(close_data, "ndim", 1) == 2:
            close_data = close_data.iloc[:, 0]

        if hasattr(volume_data, "iloc") and getattr(volume_data, "ndim", 1) == 2:
            volume_data = volume_data.iloc[:, 0]

        closes = close_data.dropna().tolist()
        volumes = volume_data.fillna(0).tolist()
        dates = df.index.to_pydatetime().tolist()

        if len(closes) < 2:
            continue

        change_pct = ((closes[-1] - closes[-2]) / closes[-2]) * 100

        result[ticker] = {
            "closes": closes,
            "volumes": volumes,
            "dates": dates,
            "change_pct": change_pct,
        }

    return result


@app.route("/", methods=["GET"])
def home():
    return "LINE bot is running", 200


@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)

    print("=== webhook received ===")
    print(body)

    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        print("invalid signature")
        abort(400)
    except Exception as e:
        app.logger.exception("handle webhook error: %s", e)
        return "Internal Error", 500

    return "OK", 200


@handler.add(FollowEvent)
def handle_follow(event):
    user_id = getattr(event.source, "user_id", None)
    print("FOLLOW user_id =", user_id)
    reply_text(event.reply_token, "歡迎加入！\n輸入「功能」查看可用指令。")


@handler.add(MessageEvent, message=TextMessageContent)
def handle_text_message(event):
    text = (event.message.text or "").strip()
    user_id = getattr(event.source, "user_id", None)

    print("MESSAGE user_id =", user_id)
    print("TEXT =", text)

    if text in ["功能", "help", "Help", "HELP"]:
        reply_text(event.reply_token, get_help_text())
        return

    if text == "ping":
        reply_text(event.reply_token, "pong")
        return

    if text == "我的ID":
        if user_id:
            reply_text(event.reply_token, f"你的 LINE userId：\n{user_id}")
        else:
            reply_text(event.reply_token, "目前拿不到 userId")
        return

    if text in ["今年股利", "股利", "股利總覽"]:
        cal = DividendCalendar()
        reply_text(event.reply_token, cal.format_2026_payouts_for_line())
        return

    if text in ["新聞 鴻海", "鴻海新聞"]:
        keyword = "鴻海"
        try:
            news_items = fetch_news_rss(keyword, limit=5)
            summary = summarize_news(keyword, news_items)
            reply_text(event.reply_token, summary)
        except Exception as e:
            app.logger.exception("news error: %s", e)
            reply_text(event.reply_token, f"新聞查詢失敗：{e}")
        return

    if text in ["新聞 台積電", "台積電新聞"]:
        keyword = "台積電"
        try:
            news_items = fetch_news_rss(keyword, limit=5)
            summary = summarize_news(keyword, news_items)
            reply_text(event.reply_token, summary)
        except Exception as e:
            app.logger.exception("news error: %s", e)
            reply_text(event.reply_token, f"新聞查詢失敗：{e}")
        return

    if text in ["新聞 聯發科", "聯發科新聞"]:
        keyword = "聯發科"
        try:
            news_items = fetch_news_rss(keyword, limit=5)
            summary = summarize_news(keyword, news_items)
            reply_text(event.reply_token, summary)
        except Exception as e:
            app.logger.exception("news error: %s", e)
            reply_text(event.reply_token, f"新聞查詢失敗：{e}")
        return

    # 保留通用格式：新聞 XXX
    if text.startswith("新聞 "):
        keyword = text.replace("新聞", "", 1).strip()
        if not keyword:
            reply_text(event.reply_token, "請輸入：新聞 鴻海")
            return

        try:
            news_items = fetch_news_rss(keyword, limit=5)
            summary = summarize_news(keyword, news_items)
            reply_text(event.reply_token, summary)
        except Exception as e:
            app.logger.exception("news error: %s", e)
            reply_text(event.reply_token, f"新聞查詢失敗：{e}")
        return

    if text in ["三雄", "圖表", "比較圖"]:
        try:
            data = fetch_comparison_data()
            if not data:
                reply_text(event.reply_token, "目前抓不到股價資料，請稍後再試。")
                return

            chart = ChartGenerator()
            image_url = chart.generate_comparison_chart(data)

            reply_messages(
                event.reply_token,
                [
                    TextMessage(text="這是台股三雄近期比較圖"),
                    ImageMessage(
                        original_content_url=image_url,
                        preview_image_url=image_url
                    )
                ]
            )
        except Exception as e:
            app.logger.exception("chart error: %s", e)
            reply_text(event.reply_token, f"產生圖表失敗：{e}")
        return

    if text == "推播名單":
        if not PUSH_TARGET_USER_IDS:
            reply_text(event.reply_token, "目前沒有可用的推播對象")
        else:
            reply_text(event.reply_token, "目前推播對象：\n" + "\n".join(PUSH_TARGET_USER_IDS))
        return

    if text == "推播測試":
        if not PUSH_TARGET_USER_IDS:
            reply_text(event.reply_token, "尚未設定 TARGET_USER_ID 或 push_target_user_ids")
            return

        try:
            push_text_to_all(PUSH_TARGET_USER_IDS, "這是一則推播測試訊息")
            reply_text(event.reply_token, f"已送出推播測試，共 {len(PUSH_TARGET_USER_IDS)} 個對象")
        except Exception as e:
            app.logger.exception("push error: %s", e)
            reply_text(event.reply_token, f"推播失敗：{e}")
        return

    reply_text(
        event.reply_token,
        "目前支援的指令：\n"
        "功能\nping\n我的ID\n今年股利\n新聞 鴻海\n新聞 台積電\n新聞 聯發科\n三雄\n推播名單\n推播測試"
    )


if __name__ == "__main__":
    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)