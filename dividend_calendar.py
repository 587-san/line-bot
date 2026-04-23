from dataclasses import dataclass
from datetime import date
from typing import Optional


@dataclass
class DividendItem:
    company: str
    ticker: str
    item_type: str
    event_date: Optional[date]
    amount: Optional[float]
    note: str = ""


class DividendCalendar:
    def __init__(self):
        self.items = self._load_2026_data()

    def _load_2026_data(self) -> list[DividendItem]:
        return [
            # 鴻海（官方已確認：現金股利 7.2 元、股東會 2026/05/29；除息/發放待公告）
            DividendItem("鴻海", "2317.TW", "股東會", date(2026, 5, 29), None, "2025 年度盈餘分配相關股東會"),
            DividendItem("鴻海", "2317.TW", "現金股利", None, 7.2, "每股現金股利 7.2 元"),
            DividendItem("鴻海", "2317.TW", "除息日", None, None, "官方尚未公告"),
            DividendItem("鴻海", "2317.TW", "發放日", None, None, "官方尚未公告"),

            # 台積電（官方 Latest Dividend）
            DividendItem("台積電", "2330.TW", "發放日", date(2026, 1, 8), 5.00001118, "2Q25 現金股利"),
            DividendItem("台積電", "2330.TW", "發放日", date(2026, 4, 9), 6.00003573, "3Q25 現金股利"),
            DividendItem("台積電", "2330.TW", "除息日", date(2026, 6, 11), 6.00, "4Q25 除息"),
            DividendItem("台積電", "2330.TW", "發放日", date(2026, 7, 9), 6.00, "4Q25 現金股利"),

            # 聯發科（官方 IR events + 股利資訊）
            DividendItem("聯發科", "2454.TW", "發放日", date(2026, 1, 30), 29.0, "2025 上半年度現金股利"),
            DividendItem("聯發科", "2454.TW", "股東會", date(2026, 5, 29), None, "年度股東常會"),
            DividendItem("聯發科", "2454.TW", "除息日", date(2026, 7, 7), 25.0, "2025 下半年度除息"),
            DividendItem("聯發科", "2454.TW", "發放日", date(2026, 7, 31), 25.0, "2025 下半年度現金股利"),
        ]

    def format_2026_payouts_for_line(self) -> str:
        lines = [
            "📅 2026 今年股利總覽",
            "",
            "【鴻海 2317】",
            "• 每股現金股利：7.2 元",
            "• 股東會：2026/05/29",
            "• 除息日：官方尚未公告",
            "• 發放日：官方尚未公告",
            "",
            "【台積電 2330】",
            "• 2026/01/08：已發放 5.00001118 元（2Q25）",
            "• 2026/04/09：已發放 6.00003573 元（3Q25）",
            "• 2026/06/11：除息 6.00 元（4Q25）",
            "• 2026/07/09：預計發放 6.00 元（4Q25）",
            "",
            "【聯發科 2454】",
            "• 2026/01/30：已發放 29 元（2025 上半年度）",
            "• 2026/05/29：股東會",
            "• 2026/07/07：除息 25 元（2025 下半年度）",
            "• 2026/07/31：預計發放 25 元（2025 下半年度）",
            "",
            "⚠️ 以公司官網 / 公開資訊觀測站最新公告為準",
        ]
        return "\n".join(lines)