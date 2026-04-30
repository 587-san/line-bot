"""
即時監控排程模組 (APScheduler 版)
────────────────────────────────
每日 08:00   → 快取清除
每日 08:30   → 三雄晨報（圖表 + 量能）
每日 08:31   → 股利事件提醒
每日 08:45   → 外部市場情報（總經 + 新聞 AI 分析）
平日盤中     → 每 5 分鐘掃描訊號
每日 14:00   → 收盤法人買賣超推播
每日 21:00   → 美股盤前情報（NASDAQ/SOX/新聞）
"""

import os
import re
import json
from datetime import datetime, date
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from dividend_calendar import DividendCalendar
from news_monitor import NewsMonitor
from stock_screener import StockScreener

dividend_cal = DividendCalendar()
news_mon     = NewsMonitor()
screener_obj = StockScreener()

TZ = ZoneInfo("Asia/Taipei")

# ── 用字典記錄「今日已推播的訊號」，避免重複通知 ──
# 若有 Redis，換成 _SignalCache(use_redis=True)
_today_signals: dict = {}


class SignalCache:
    """防止同訊號當日重複推播"""

    def __init__(self):
        self._cache: dict[str, str] = {}   # key: ticker+signal, val: date str

    def already_sent(self, ticker: str, signal: str) -> bool:
        key = f"{ticker}:{signal}"
        return self._cache.get(key) == str(date.today())

    def mark_sent(self, ticker: str, signal: str):
        key = f"{ticker}:{signal}"
        self._cache[key] = str(date.today())

    def reset_daily(self):
        """每天 08:00 清除前一日紀錄"""
        self._cache.clear()


signal_cache = SignalCache()


def setup_scheduler(analyzer, messenger, charter) -> BackgroundScheduler:
    """
    初始化並回傳 APScheduler 實例。
    在 main.py 中呼叫：
        sched = setup_scheduler(analyzer, messenger, charter)
        sched.start()
    """
    sched = BackgroundScheduler(timezone=TZ)

    # ① 每天 08:00 清除訊號快取
    sched.add_job(
        signal_cache.reset_daily,
        CronTrigger(hour=8, minute=0, timezone=TZ),
        id="daily_reset",
    )

    # ② 每天 08:30 晨報（三雄圖表）
    sched.add_job(
        lambda: morning_report(analyzer, messenger, charter),
        CronTrigger(hour=8, minute=30, timezone=TZ),
        id="morning_report",
    )

    # ③ 平日 09:00–13:30 每 5 分鐘掃描訊號
    sched.add_job(
        lambda: intraday_scan(analyzer, messenger),
        IntervalTrigger(minutes=5, timezone=TZ),
        id="intraday_scan",
    )

    # ④ 每天 14:00 收盤法人推播
    sched.add_job(
        lambda: close_report(analyzer, messenger),
        CronTrigger(hour=14, minute=0, timezone=TZ),
        id="close_report",
    )

    # ⑤ 每天 08:31 股利提醒（晨報後立刻檢查）
    sched.add_job(
        lambda: dividend_alert(messenger),
        CronTrigger(hour=8, minute=31, timezone=TZ),
        id="dividend_alert",
    )

    # ⑥ 每天 08:45 外部市場情報
    sched.add_job(
        lambda: market_intelligence_report(messenger),
        CronTrigger(hour=8, minute=45, timezone=TZ),
        id="market_intelligence",
    )

    # ⑦ 每週一 09:05 選股推薦（開盤後讓量價確認）
    sched.add_job(
        lambda: weekly_stock_pick(messenger),
        CronTrigger(day_of_week="mon", hour=9, minute=5, timezone=TZ),
        id="weekly_stock_pick",
    )

    # ⑧ 每天 21:00 美股盤前情報
    sched.add_job(
        lambda: us_market_report(messenger),
        CronTrigger(hour=21, minute=0, timezone=TZ),
        id="us_market_report",
    )

    return sched


# ─────────────────────────────────────────
# ① 每日晨報
# ─────────────────────────────────────────
def morning_report(analyzer, messenger, charter):
    print(f"[{_now()}] ⏰ 晨報推播中...")
    targets = _get_targets()
    if not targets:
        return

    stocks_data     = analyzer.get_all_stocks_data()
    img_url_or_path = charter.generate_comparison_chart(stocks_data)
    summaries       = analyzer.get_multi_summary()

    img_msg  = messenger.build_image_message(img_url_or_path)
    text_msg = messenger.build_morning_report_text(summaries)

    for t in targets:
        try:
            messenger.push_message(t, img_msg)
            messenger.push_message(t, text_msg)
        except Exception as e:
            print(f"  ❌ {t}: {e}")

    print(f"[{_now()}] ✅ 晨報完成")


# ─────────────────────────────────────────
# ② 盤中訊號掃描
# ─────────────────────────────────────────
def intraday_scan(analyzer, messenger):
    """只在平日 9:00–13:35 跑，其他時間跳過"""
    now = datetime.now(TZ)
    if now.weekday() >= 5:          # 週六日
        return
    if not (9 <= now.hour < 13 or (now.hour == 13 and now.minute <= 35)):
        return

    targets = _get_targets()
    if not targets:
        return

    print(f"[{_now()}] 🔍 盤中掃描...")

    from stock_analyzer import WATCH_LIST
    for ticker in WATCH_LIST:
        try:
            r = analyzer.get_daily_report(ticker)
            if "error" in r:
                continue

            alerts = _detect_signals(r)
            for signal_key, msg in alerts:
                if signal_cache.already_sent(ticker, signal_key):
                    continue
                for t in targets:
                    try:
                        messenger.push_message(t, messenger.build_text_message(msg))
                    except Exception as e:
                        print(f"  ❌ push {t}: {e}")
                signal_cache.mark_sent(ticker, signal_key)
                print(f"  🔔 {ticker} 訊號推播：{signal_key}")

        except Exception as e:
            print(f"  ⚠️ {ticker} 掃描錯誤: {e}")


def _detect_signals(r: dict) -> list[tuple[str, str]]:
    """
    分析單股資料，回傳 [(signal_key, message_text), ...]
    signal_key 用於去重（同訊號當日只推一次）
    """
    alerts = []
    name   = r["name"]
    price  = r["price"]
    pct    = r["change_pct"]
    vr     = r["vol_ratio"]
    rsi    = r["rsi"]
    ma5    = r["ma5"]
    ma20   = r["ma20"]

    # 爆量大漲
    if vr >= 2.0 and pct > 2:
        alerts.append((
            "explosive_up",
            f"🚀【爆量大漲警報】{name}\n"
            f"股價 ${price}  漲幅 +{pct}%\n"
            f"量比 {vr}x（今日量是5日均量的 {vr} 倍）\n"
            f"成交金額 {r['amount_yi']} 億元\n"
            f"⚠️ 僅供參考，非投資建議"
        ))

    # 爆量大跌
    if vr >= 2.0 and pct < -2:
        alerts.append((
            "explosive_down",
            f"⚠️【爆量大跌警報】{name}\n"
            f"股價 ${price}  跌幅 {pct}%\n"
            f"量比 {vr}x  成交金額 {r['amount_yi']} 億元\n"
            f"⚠️ 僅供參考，非投資建議"
        ))

    # RSI 超買
    if rsi > 72:
        alerts.append((
            "rsi_overbought",
            f"📈【RSI 超買】{name}\n"
            f"RSI(14) = {rsi}（超過 70 警戒線）\n"
            f"股價 ${price}  獲利了結壓力增加\n"
            f"⚠️ 僅供參考，非投資建議"
        ))

    # RSI 超賣
    if rsi < 28:
        alerts.append((
            "rsi_oversold",
            f"🟢【RSI 超賣】{name}\n"
            f"RSI(14) = {rsi}（低於 30 超賣區）\n"
            f"股價 ${price}  留意反彈機會\n"
            f"⚠️ 僅供參考，非投資建議"
        ))

    # 突破 MA20（收盤站上 MA20 且之前在下方）
    if price > ma20 and (price - ma20) / ma20 < 0.01 and pct > 0:
        alerts.append((
            "break_ma20",
            f"📊【突破 MA20】{name}\n"
            f"股價 ${price} 站上 20日均線 {ma20}\n"
            f"中期趨勢轉強訊號\n"
            f"⚠️ 僅供參考，非投資建議"
        ))

    # 成交金額異常（鴻海 > 30 億，台積電 > 200 億，聯發科 > 50 億）
    amount_threshold = {
        "鴻海":  30,
        "台積電": 200,
        "聯發科": 50,
    }
    threshold = amount_threshold.get(r["name"], 30)
    if r["amount_yi"] > threshold:
        alerts.append((
            "high_amount",
            f"💰【成交金額異常】{name}\n"
            f"成交金額 {r['amount_yi']} 億元（超過 {threshold} 億警戒線）\n"
            f"股價 ${price}  量比 {vr}x\n"
            f"⚠️ 僅供參考，非投資建議"
        ))

    return alerts


# ─────────────────────────────────────────
# ④ 股利日期提醒
# ─────────────────────────────────────────
def dividend_alert(messenger):
    """
    每天早上 08:31 檢查股利事件
    提前 14、7、3、1 天各推播一次
    """
    targets = _get_targets()
    if not targets:
        return

    alert_events = dividend_cal.get_alert_events(advance_days=[14, 7, 3, 1])
    if not alert_events:
        return

    print(f"[{_now()}] 📅 股利提醒推播（{len(alert_events)} 個事件）")

    for e in alert_events:
        icon = {"除息日": "✂️", "停止過戶": "🔒",
                "發放日": "💰", "股東常會": "🏛️"}.get(e.event_type, "📌")

        urgency = {1: "⚠️ 明天就到！", 3: "🔔 3 天後",
                   7: "📌 7 天後", 14: "📅 14 天後"}.get(e.days_until, "")

        amt_str = f"\n💵 股利金額：${e.amount} 元" if e.amount else ""

        msg_text = (
            f"{icon} {urgency}【{e.event_type}提醒】\n"
            f"\n"
            f"📌 {e.name}（{e.ticker.replace('.TW','')}）\n"
            f"📆 日期：{e.event_date.strftime('%Y/%m/%d')}"
            f"{amt_str}\n"
            f"\n"
            f"📝 {e.note}\n"
            f"\n"
            f"⚠️ 以公開資訊觀測站公告為準"
        )

        msg = messenger.build_text_message(msg_text)
        for t in targets:
            try:
                messenger.push_message(t, msg)
            except Exception as ex:
                print(f"  ❌ {t}: {ex}")


# ─────────────────────────────────────────
# ③ 收盤法人推播
# ─────────────────────────────────────────
def close_report(analyzer, messenger):
    now = datetime.now(TZ)
    if now.weekday() >= 5:
        return

    print(f"[{_now()}] 📋 收盤法人報告...")
    targets = _get_targets()
    if not targets:
        return

    from stock_analyzer import WATCH_LIST
    lines = [f"📋 {datetime.now(TZ).strftime('%Y/%m/%d')} 收盤法人動向\n"]

    for ticker in WATCH_LIST:
        try:
            r = analyzer.get_daily_report(ticker)
            inst = r.get("institutional", {})
            if not inst:
                continue
            total = inst.get("total", 0)
            sign  = "買超" if total >= 0 else "賣超"
            icon  = "📈" if total > 0 else ("📉" if total < 0 else "➡️")
            lines.append(
                f"{icon} {r['name']}  {sign} {abs(total):,} 張\n"
                f"   外資 {inst.get('foreign',0):+,}  投信 {inst.get('invest',0):+,}  自營 {inst.get('dealer',0):+,}"
            )
        except Exception as e:
            print(f"  ⚠️ {ticker}: {e}")

    lines.append("\n⚠️ 僅供參考，非投資建議")
    msg = messenger.build_text_message("\n".join(lines))

    for t in targets:
        try:
            messenger.push_message(t, msg)
        except Exception as e:
            print(f"  ❌ {t}: {e}")


# ─────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────
def _get_targets() -> list[str]:
    raw = os.environ.get("PUSH_TARGET_IDS", "")
    return [t.strip() for t in raw.split(",") if t.strip()]

def _now() -> str:
    return datetime.now(TZ).strftime("%H:%M:%S")


def weekly_stock_pick(messenger):
    """每週一 09:05 推播：掃描全市場，AI 推薦未來5天潛力股"""
    print(f"[{_now()}] 🔍 週一選股掃描中...")
    targets = _get_targets()
    if not targets:
        return
    try:
        result   = screener_obj.screen_top5()
        msg_text = screener_obj.format_for_line(result)
        msg      = messenger.build_text_message(msg_text)
        for t in targets:
            try:
                messenger.push_message(t, msg)
            except Exception as e:
                print(f"  ❌ {t}: {e}")
        print(f"[{_now()}] ✅ 週一選股推播完成")
    except Exception as e:
        print(f"[{_now()}] ⚠️ 選股失敗: {e}")


# ─────────────────────────────────────────
# ⑥ 外部市場情報（08:45 盤前）
# ─────────────────────────────────────────
def market_intelligence_report(messenger):
    """
    08:45 推播：
    - 總經指標（NASDAQ/SOX/VIX/USD-TWD）
    - 影響三雄的重大新聞 AI 分析
    - 個股影響評分 -3~+3
    """
    print(f"[{_now()}] 🌐 市場情報分析中...")
    targets = _get_targets()
    if not targets:
        return

    try:
        intel = news_mon.get_market_intelligence()
        msg_text = news_mon.format_for_line(intel)
        msg = messenger.build_text_message(msg_text)
        for t in targets:
            try:
                messenger.push_message(t, msg)
            except Exception as e:
                print(f"  ❌ {t}: {e}")
        print(f"[{_now()}] ✅ 市場情報推播完成")
    except Exception as e:
        print(f"[{_now()}] ⚠️ 市場情報失敗: {e}")


# ─────────────────────────────────────────
# ⑦ 美股盤前情報（21:00）
# ─────────────────────────────────────────
def us_market_report(messenger):
    """
    每天台灣時間 21:00（美東 09:00 開盤後約1小時）
    推播當日美股市場動態 + 對隔日台股的影響分析
    """
    now = datetime.now(TZ)
    # 週六日美股休市跳過
    if now.weekday() >= 5:
        return

    print(f"[{_now()}] 🇺🇸 美股情報分析中...")
    targets = _get_targets()
    if not targets:
        return

    try:
        intel = news_mon.get_market_intelligence()
        macro = intel.get("macro", {})
        scores = intel.get("impact_scores", {})

        def fmt(key, label, unit=""):
            d = macro.get(key, {})
            p, c = d.get("price"), d.get("change_pct")
            if p is None:
                return f"{label}: —"
            sign = "▲" if c >= 0 else "▼"
            color = "🔴" if c >= 0 else "🟢"
            return f"{color} {label}: {p}{unit} ({sign}{abs(c):.1f}%)"

        def score_bar(s):
            if s >= 2:  return "🚀 強烈利多"
            if s == 1:  return "📈 小利多"
            if s == -1: return "📉 小利空"
            if s <= -2: return "🔻 強烈利空"
            return "➡️ 中性"

        text = (
            f"🇺🇸 美股盤中動態  {now.strftime('%m/%d %H:%M')}\n\n"
            f"{fmt('nasdaq',  'NASDAQ')}\n"
            f"{fmt('sox',     'SOX半導體')}\n"
            f"{fmt('vix',     'VIX')}\n"
            f"{fmt('usd_twd', 'USD/TWD')}\n"
            f"{fmt('us10y',   '美債10Y', '%')}\n\n"
            f"【對明日台股影響預估】\n"
            f"鴻海  {score_bar(scores.get('2317', 0))}\n"
            f"台積電 {score_bar(scores.get('2330', 0))}\n"
            f"聯發科 {score_bar(scores.get('2454', 0))}\n\n"
            f"{intel.get('ai_analysis', '')}\n\n"
            f"⚠️ 僅供參考，非投資建議"
        )

        msg = messenger.build_text_message(text)
        for t in targets:
            try:
                messenger.push_message(t, msg)
            except Exception as e:
                print(f"  ❌ {t}: {e}")
        print(f"[{_now()}] ✅ 美股情報推播完成")
    except Exception as e:
        print(f"[{_now()}] ⚠️ 美股情報失敗: {e}")

