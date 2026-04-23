"""
圖表產生模組
- 產出台股三雄比較圖（鴻海 / 台積電 / 聯發科）
- 上傳到 Cloudinary 取得公開 URL（LINE 圖片訊息需要公開網址）
"""

import os
import io
import cloudinary
import cloudinary.uploader
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import numpy as np
import pandas as pd
from datetime import datetime


# 中文字型設定
plt.rcParams["font.family"] = [
    "Microsoft JhengHei",
    "Noto Sans CJK TC",
    "PingFang TC",
    "Heiti TC",
    "Arial Unicode MS",
    "sans-serif",
]
plt.rcParams["axes.unicode_minus"] = False


# 台股三雄設定
STOCKS = {
    "2317.TW": {"name": "鴻海\n(2317)", "color": "#E74C3C", "label_color": "#FF6B6B"},
    "2330.TW": {"name": "台積電\n(2330)", "color": "#3498DB", "label_color": "#74B9FF"},
    "2454.TW": {"name": "聯發科\n(2454)", "color": "#2ECC71", "label_color": "#55EFC4"},
}

# 自訂追蹤股票（可擴充）
CUSTOM_STOCKS = {}


class ChartGenerator:
    def __init__(self):
        cloudinary.config(
            cloud_name=os.environ["CLOUDINARY_CLOUD_NAME"],
            api_key=os.environ["CLOUDINARY_API_KEY"],
            api_secret=os.environ["CLOUDINARY_API_SECRET"],
            secure=True
        )

    # ─────────────────────────────────────────
    # 產出三雄比較圖
    # ─────────────────────────────────────────
    def generate_comparison_chart(self, stocks_data: dict) -> str:
        """
        stocks_data: {ticker: {'closes': [...], 'volumes': [...], 'dates': [...], 'change_pct': float}}
        回傳：圖片公開 URL 或本地路徑
        """
        tickers = list(stocks_data.keys())
        n_stocks = len(tickers)

        fig, axes = plt.subplots(2, n_stocks, figsize=(6 * n_stocks, 9))
        if n_stocks == 1:
            axes = [[axes[0]], [axes[1]]]
        fig.patch.set_facecolor("#0D0D1A")

        for idx, ticker in enumerate(tickers):
            d = stocks_data[ticker]
            cfg = STOCKS.get(ticker, {"name": ticker, "color": "#BDC3C7", "label_color": "#BDC3C7"})
            color = cfg["color"]
            name = cfg["name"]

            closes = d["closes"]
            volumes = d["volumes"]
            dates = d["dates"]
            pct = d["change_pct"]
            price = closes[-1]

            ax_p = axes[0][idx]
            ax_v = axes[1][idx]

            # ── 價格圖 ──
            ax_p.set_facecolor("#16213E")
            ax_p.plot(dates, closes, color=color, linewidth=2.2, zorder=3)
            ax_p.fill_between(dates, closes, min(closes) * 0.995, alpha=0.18, color=color, zorder=2)

            # 5日 / 20日均線
            if len(closes) >= 5:
                ma5 = pd.Series(closes).rolling(5).mean()
                ax_p.plot(
                    dates, ma5,
                    color="#F39C12", linewidth=1, linestyle="--", alpha=0.7,
                    label="5日均線"
                )
            if len(closes) >= 20:
                ma20 = pd.Series(closes).rolling(20).mean()
                ax_p.plot(
                    dates, ma20,
                    color="#9B59B6", linewidth=1, linestyle=":", alpha=0.7,
                    label="20日均線"
                )

            pct_sign = "▲" if pct >= 0 else "▼"
            ax_p.set_title(
                f"{name}\n${price:.1f}  {pct_sign}{abs(pct):.1f}%",
                color="white", fontsize=11, pad=10, fontweight="bold"
            )
            ax_p.tick_params(colors="#555", labelsize=7)
            ax_p.xaxis.set_major_formatter(mdates.DateFormatter("%m/%d"))
            ax_p.xaxis.set_major_locator(mdates.WeekdayLocator(byweekday=0))
            plt.setp(ax_p.get_xticklabels(), rotation=30, ha="right", color="#666")
            for sp in ax_p.spines.values():
                sp.set_color("#2C3E50")
            ax_p.yaxis.tick_right()
            ax_p.tick_params(axis="y", colors="#666", labelsize=7)

            if idx == 0 and len(closes) >= 5:
                ax_p.legend(
                    fontsize=6, loc="upper left",
                    facecolor="#0D0D1A", edgecolor="#333", labelcolor="#CCC"
                )

            # ── 成交量圖 ──
            ax_v.set_facecolor("#16213E")
            vol_m = [v / 1e6 for v in volumes]
            avg5 = np.mean(vol_m[-6:-1]) if len(vol_m) >= 6 else np.mean(vol_m)

            bar_colors = [color if i >= len(vol_m) - 5 else "#2C3E50" for i in range(len(vol_m))]
            ax_v.bar(range(len(vol_m)), vol_m, color=bar_colors, width=0.75, zorder=3)
            ax_v.axhline(
                avg5,
                color="#F39C12",
                linestyle="--",
                linewidth=1.3,
                alpha=0.85,
                label=f"5日均量 {avg5:.0f}百萬股"
            )

            # 今日量標註
            today_vol = vol_m[-1]
            ratio = today_vol / avg5 if avg5 > 0 else 1
            ratio_color = "#E74C3C" if ratio >= 1.5 else ("#27AE60" if ratio >= 1 else "#888")

            ax_v.annotate(
                f"{ratio:.1f}倍",
                xy=(len(vol_m) - 1, today_vol),
                xytext=(len(vol_m) - 1, today_vol * 1.08),
                ha="center",
                fontsize=8,
                color=ratio_color,
                fontweight="bold"
            )

            ax_v.set_title(
                f"成交量（百萬股）  5日均量={avg5:.0f}",
                color="#AAA",
                fontsize=9,
                pad=6
            )
            ax_v.tick_params(colors="#555", labelsize=7)
            for sp in ax_v.spines.values():
                sp.set_color("#2C3E50")
            ax_v.legend(
                fontsize=6,
                facecolor="#0D0D1A",
                edgecolor="#333",
                labelcolor="#F39C12"
            )

        today_str = datetime.now().strftime("%Y/%m/%d")
        fig.suptitle(
            f"台股三雄每日報告  {today_str}",
            color="white", fontsize=14, fontweight="bold", y=0.995
        )

        plt.tight_layout(rect=[0, 0, 1, 0.975])
        plt.subplots_adjust(hspace=0.4)

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=150, bbox_inches="tight", facecolor="#0D0D1A")
        plt.close()
        buf.seek(0)
        img_bytes = buf.read()

        url = self._upload_cloudinary(img_bytes, public_id="stock_comparison")
        if url:
            return url

        local_path = "/tmp/stock_chart.png"
        with open(local_path, "wb") as f:
            f.write(img_bytes)
        return local_path

    # ─────────────────────────────────────────
    # 產出單股 K 線圖（蠟燭圖）
    # ─────────────────────────────────────────
    def generate_candlestick(self, ticker: str, df: pd.DataFrame) -> str:
        """產出單股蠟燭圖，回傳 URL 或路徑"""
        cfg = STOCKS.get(ticker, {"name": ticker, "color": "#BDC3C7"})

        fig, (ax1, ax2) = plt.subplots(
            2, 1, figsize=(10, 7),
            gridspec_kw={"height_ratios": [3, 1]}
        )
        fig.patch.set_facecolor("#0D0D1A")

        df = df.tail(30)

        for ax in [ax1, ax2]:
            ax.set_facecolor("#16213E")
            for sp in ax.spines.values():
                sp.set_color("#2C3E50")

        # 蠟燭圖
        for i, (ts, row) in enumerate(df.iterrows()):
            o, h, l, c = row["Open"], row["High"], row["Low"], row["Close"]
            bull = c >= o
            body_color = "#E74C3C" if bull else "#27AE60"
            ax1.bar(i, abs(c - o), bottom=min(o, c), color=body_color, width=0.6, zorder=3)
            ax1.plot([i, i], [l, h], color=body_color, linewidth=1, zorder=3)

        closes = df["Close"].values
        if len(closes) >= 5:
            ma5 = pd.Series(closes).rolling(5).mean()
            ax1.plot(
                range(len(ma5)), ma5,
                color="#F39C12", linewidth=1.2, linestyle="--",
                label="5日均線"
            )
        if len(closes) >= 20:
            ma20 = pd.Series(closes).rolling(20).mean()
            ax1.plot(
                range(len(ma20)), ma20,
                color="#9B59B6", linewidth=1.2, linestyle=":",
                label="20日均線"
            )

        price = closes[-1]
        pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        ax1.set_title(
            f"{cfg['name']}  ${price:.1f}  ({'▲' if pct >= 0 else '▼'}{abs(pct):.1f}%)",
            color="white", fontsize=13, fontweight="bold"
        )
        ax1.tick_params(colors="#666", labelsize=8)
        ax1.yaxis.tick_right()
        ax1.legend(fontsize=8, facecolor="#0D0D1A", edgecolor="#333", labelcolor="#CCC")

        # 成交量
        volumes = df["Volume"].values / 1e6
        avg5v = np.mean(volumes[-6:-1]) if len(volumes) >= 6 else np.mean(volumes)
        v_colors = [
            "#E74C3C" if df["Close"].iloc[i] >= df["Open"].iloc[i] else "#27AE60"
            for i in range(len(df))
        ]
        ax2.bar(range(len(volumes)), volumes, color=v_colors, width=0.6, alpha=0.8)
        ax2.axhline(avg5v, color="#F39C12", linestyle="--", linewidth=1.2, alpha=0.8)
        ax2.set_title("成交量（百萬股）", color="#AAA", fontsize=9)
        ax2.tick_params(colors="#666", labelsize=7)

        plt.tight_layout()

        buf = io.BytesIO()
        plt.savefig(buf, format="png", dpi=130, bbox_inches="tight", facecolor="#0D0D1A")
        plt.close()
        buf.seek(0)
        img_bytes = buf.read()

        url = self._upload_cloudinary(img_bytes, public_id=f"candle_{ticker.replace('.', '_')}")
        if url:
            return url

        local_path = f"/tmp/{ticker}_candle.png"
        with open(local_path, "wb") as f:
            f.write(img_bytes)
        return local_path

    # ─────────────────────────────────────────
    # Cloudinary 上傳
    # ─────────────────────────────────────────
    def _upload_cloudinary(self, img_bytes: bytes, public_id: str = "stock_chart") -> str:
        """上傳圖片到 Cloudinary，回傳公開 HTTPS URL"""
        try:
            result = cloudinary.uploader.upload(
                img_bytes,
                public_id=public_id,
                folder="stock_linebot",
                overwrite=True,
                resource_type="image",
            )
            return result["secure_url"]
        except Exception as e:
            print(f"Cloudinary 上傳失敗: {e}")
        return ""