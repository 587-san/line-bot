"""
鴻海 + 台積電 + 聯發科 LINE Bot
每天早上 8:30 自動推播三雄晨報圖表
"""

import os
import threading
from flask import Flask, request, abort
from linebot.v3 import WebhookHandler
from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi,
    ReplyMessageRequest, PushMessageRequest,
    TextMessage
)
from linebot.v3.webhooks import MessageEvent, TextMessageContent

from stock_analyzer import StockAnalyzer, WATCH_LIST
from scheduler import setup_scheduler
from dividend_calendar import DividendCalendar
from news_monitor import NewsMonitor
from stock_screener import StockScreener

dividend_cal = DividendCalendar()
news_mon     = NewsMonitor()
screener     = StockScreener()
from line_messenger import LineMessenger
from chart_generator import ChartGenerator
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)

configuration = Configuration(access_token=os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
handler       = WebhookHandler(os.environ["LINE_CHANNEL_SECRET"])

analyzer  = StockAnalyzer()
messenger = LineMessenger(os.environ["LINE_CHANNEL_ACCESS_TOKEN"])
charter   = ChartGenerator()


# ─────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────
@app.route("/callback", methods=["POST"])
def callback():
    signature = request.headers.get("X-Line-Signature", "")
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return "OK"


@handler.add(MessageEvent, message=TextMessageContent)
def handle_message(event):
    text = event.message.text.strip()

    with ApiClient(configuration) as api_client:
        api = MessagingApi(api_client)

        # ── 指令路由 ──
        if text in ["鴻海", "2317"]:
            reply_stock(api, event, "2317.TW")

        elif text in ["台積電", "2330"]:
            reply_stock(api, event, "2330.TW")

        elif text in ["聯發科", "2454"]:
            reply_stock(api, event, "2454.TW")

        elif text in ["三雄", "比較", "圖"]:
            reply_comparison_chart(api, event)

        elif text in ["選股", "推薦", "潛力股", "未來5天", "看好"]:
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🔍 掃描台股潛力股中，約需 30 秒，完成後推播...")]))

            def _screen():
                result   = screener.screen_top5()
                msg_text = screener.format_for_line(result)
                with ApiClient(configuration) as ac:
                    MessagingApi(ac).push_message(PushMessageRequest(
                        to=event.source.user_id,
                        messages=[TextMessage(text=msg_text)]))
            threading.Thread(target=_screen, daemon=True).start()

        elif text in ["外部", "總經", "情報", "美股", "新聞分析"]:
            # 產出需要時間，先 reply 再背景產出 push
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="🌐 分析外部市場情報中，約 10 秒後推播...")]))

            def _gen_intel():
                intel    = news_mon.get_market_intelligence()
                msg_text = news_mon.format_for_line(intel)
                with ApiClient(configuration) as ac:
                    MessagingApi(ac).push_message(PushMessageRequest(
                        to=event.source.user_id,
                        messages=[TextMessage(text=msg_text)]))
            threading.Thread(target=_gen_intel, daemon=True).start()

        elif text in ["股利", "除息", "行事曆", "配息"]:
            msg = dividend_cal.format_upcoming_for_line(days=90)
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]))

        elif text.startswith("股利"):
            ticker = parse_stock_from_text(text)
            msg = dividend_cal.format_stock_events(ticker)
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=msg)]))

        elif text == "量":
            reply_volume_summary(api, event)

        elif text.startswith("週線"):
            # 週線 2317 / 週線 台積電
            ticker = parse_stock_from_text(text)
            report = analyzer.get_weekly_report(ticker)
            flex   = messenger.build_flex_message(report)
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token, messages=[flex]))

        elif text.startswith("新聞"):
            ticker = parse_stock_from_text(text)
            news   = analyzer.get_news_summary(ticker)
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=news)]))

        elif text == "幫助":
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text=HELP_TEXT)]))

        else:
            # 讓使用者知道可以輸入什麼
            api.reply_message(ReplyMessageRequest(
                reply_token=event.reply_token,
                messages=[TextMessage(text="輸入「幫助」查看所有指令 🤖")]))


# ─────────────────────────────────────────
# 指令處理 helpers
# ─────────────────────────────────────────
def reply_stock(api, event, ticker: str):
    """單股 Flex 卡片"""
    report = analyzer.get_daily_report(ticker)
    flex   = messenger.build_flex_message(report)
    api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token, messages=[flex]))


def reply_comparison_chart(api, event):
    """三雄比較圖 + 文字摘要"""
    # 先回覆「準備中」避免 webhook 超時
    api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text="📊 產出三雄比較圖中，請稍候 5 秒...")]))

    # 背景產圖後 push
    def _gen_and_push():
        stocks_data = analyzer.get_all_stocks_data()
        img_url_or_path = charter.generate_comparison_chart(stocks_data)

        summaries = analyzer.get_multi_summary()
        text_msg  = messenger.build_multi_summary_text(summaries)
        img_msg   = messenger.build_image_message(img_url_or_path)

        target_id = event.source.user_id
        with ApiClient(configuration) as ac:
            MessagingApi(ac).push_message(PushMessageRequest(
                to=target_id, messages=[img_msg, text_msg]))

    threading.Thread(target=_gen_and_push, daemon=True).start()


def reply_volume_summary(api, event):
    """三股量能快報（文字）"""
    summaries = analyzer.get_multi_summary()
    text = messenger.build_multi_summary_text(summaries)
    api.reply_message(ReplyMessageRequest(
        reply_token=event.reply_token,
        messages=[TextMessage(text=text)]))


def parse_stock_from_text(text: str) -> str:
    """從指令文字解析 ticker"""
    mapping = {
        "鴻海": "2317.TW", "2317": "2317.TW",
        "台積電": "2330.TW", "2330": "2330.TW",
        "聯發科": "2454.TW", "2454": "2454.TW",
    }
    for key, val in mapping.items():
        if key in text:
            return val
    return "2317.TW"  # 預設鴻海


HELP_TEXT = (
    "📊 台股三雄 Bot 完整指令\n\n"
    "─── 單股行情 ───\n"
    "🔹 鴻海 / 台積電 / 聯發科\n\n"
    "─── 比較 & 圖表 ───\n"
    "🔹 三雄 / 圖 → 三股比較圖\n"
    "🔹 量 → 量能快報\n"
    "🔹 週線 鴻海 → 近5日走勢\n\n"
    "─── 選股 ───\n"
    "🔹 選股 / 推薦 / 潛力股\n"
    "   → 掃描50檔台股，AI推薦\n"
    "     未來5天最具潛力的5檔\n\n"
    "─── 外部情報 ───\n"
    "🔹 外部 / 總經 / 情報\n"
    "   → NASDAQ/SOX/VIX + 新聞AI分析\n"
    "🔹 新聞 台積電 → 個股新聞摘要\n\n"
    "─── 股利行事曆 ───\n"
    "🔹 股利 / 除息 → 三雄股利總覽\n\n"
    "─── 自動推播 ───\n"
    "⏰ 08:30 三雄晨報圖表\n"
    "⏰ 08:31 股利日期提醒\n"
    "⏰ 08:45 外部情報 + AI分析\n"
    "🔍 09:00 每週一次選股推薦\n"
    "🔔 09:00–13:35 盤中訊號掃描\n"
    "📋 14:00 收盤法人動向\n"
    "🇺🇸 21:00 美股盤中情報"
)


# ─────────────────────────────────────────
# 啟動
# ─────────────────────────────────────────
if __name__ == "__main__":
    sched = setup_scheduler(analyzer, messenger, charter)
    sched.start()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)