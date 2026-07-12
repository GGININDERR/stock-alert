# -*- coding: utf-8 -*-
"""
Trip.com 機加酒抓取模組。

⚠️ 重要:
- Trip.com 沒有公開 API,機加酒頁面為 JS 動態載入且有反爬蟲機制。
- 頁面結構(CSS selector)會不定期改版,「本檔是全專案最需要維護的地方」。
- 僅供個人低頻使用,請勿高頻抓取。

回傳格式:list[dict],每筆為一個方案(Package),欄位見 Package 說明。
若尚未接上真實 selector,可用環境變數 TRIP_MOCK=1 產生假資料方便測通知/篩選流程。
"""
import os
import re
from datetime import datetime


# ---- 方案資料結構(dict 欄位)----
# {
#   "id":            唯一識別(用來記錄已通知,避免重複)
#   "標題":          飯店/方案名稱
#   "總價":          整數,台幣
#   "飯店星級":      float
#   "去程出發":      "HH:MM"
#   "回程出發":      "HH:MM"
#   "航空公司":      str
#   "連結":          方案網址
# }


def _mock_packages(cond):
    """假資料,供尚未接上真實 selector 時測試整條流程。"""
    return [
        {
            "id": f"mock-{cond['目的地城市']}-A",
            "標題": "新宿華盛頓酒店 4★",
            "總價": 46800,
            "飯店星級": 4.0,
            "去程出發": "08:45",
            "回程出發": "18:20",
            "航空公司": "長榮航空",
            "連結": "https://www.trip.com/",
        },
        {
            "id": f"mock-{cond['目的地城市']}-B",
            "標題": "澀谷東急膠囊旅館",
            "總價": 31000,
            "飯店星級": 2.5,
            "去程出發": "14:10",
            "回程出發": "10:00",
            "航空公司": "星宇航空",
            "連結": "https://www.trip.com/",
        },
        {
            "id": f"mock-{cond['目的地城市']}-C",
            "標題": "池袋大都會大飯店 5★",
            "總價": 52000,
            "飯店星級": 5.0,
            "去程出發": "09:30",
            "回程出發": "16:40",
            "航空公司": "中華航空",
            "連結": "https://www.trip.com/",
        },
    ]


def _build_search_url(cond):
    """
    組出 Trip.com 機加酒搜尋網址。
    ⚠️ 這個 URL 格式是示意,實際參數需你打開 Trip.com 機加酒搜尋頁後,
       從網址列複製真實格式回來替換。
    """
    return (
        "https://hk.trip.com/flights-hotels/"
        f"?dcity={cond['出發城市']}&acity={cond['目的地城市']}"
        f"&ddate={cond['出發日']}&rdate={cond['回程日']}"
        f"&adult={cond.get('大人數', 2)}"
    )


def fetch_packages(cond):
    """抓取單一搜尋條件的所有方案。回傳 list[dict]。"""
    if os.getenv("TRIP_MOCK") == "1":
        return _mock_packages(cond)

    # ---- 真實抓取(Playwright)----
    from playwright.sync_api import sync_playwright

    url = _build_search_url(cond)
    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            locale="zh-TW",
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0 Safari/537.36"
            ),
        )
        page.goto(url, wait_until="networkidle", timeout=60000)

        # === 除錯模式 ===
        # 第一次在能連到 Trip.com 的電腦上跑,請設環境變數 TRIP_DEBUG=1。
        # 會把整頁 HTML 存成 trip_debug.html,把這個檔貼給 Claude,
        # 就能精準對上下面的 selector。
        if os.getenv("TRIP_DEBUG") == "1":
            debug_path = os.path.join(os.path.dirname(__file__), "trip_debug.html")
            with open(debug_path, "w", encoding="utf-8") as f:
                f.write(page.content())
            print(f"[scraper] 已存 HTML 供除錯:{debug_path}")

        # ⚠️ 以下 selector 為示意,務必用瀏覽器開發者工具或上面的除錯 HTML 確認實際 class。
        cards = page.query_selector_all("[data-testid='package-card']")
        for card in cards:
            try:
                results.append(_parse_card(card))
            except Exception as e:  # noqa: BLE001 單筆解析失敗不影響整體
                print(f"[scraper] 解析單筆失敗: {e}")
        browser.close()
    return results


def _parse_card(card):
    """把一張方案卡片的 DOM 解析成 dict。selector 需依實際頁面調整。"""
    def _text(sel):
        el = card.query_selector(sel)
        return el.inner_text().strip() if el else ""

    price_txt = _text(".package-price")
    price = int(re.sub(r"[^\d]", "", price_txt) or 0)
    title = _text(".hotel-name")

    star_txt = _text(".hotel-star")
    star_m = re.search(r"[\d.]+", star_txt)
    star = float(star_m.group()) if star_m else 0.0

    link_el = card.query_selector("a")
    link = link_el.get_attribute("href") if link_el else ""
    if link and link.startswith("/"):
        link = "https://hk.trip.com" + link

    return {
        "id": f"{title}-{price}-{_text('.depart-time')}",
        "標題": title,
        "總價": price,
        "飯店星級": star,
        "去程出發": _text(".depart-time") or "00:00",
        "回程出發": _text(".return-time") or "00:00",
        "航空公司": _text(".airline"),
        "連結": link,
    }
