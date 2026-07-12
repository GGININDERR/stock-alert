# 機加酒自動化篩選(Trip.com + LINE)

指定日期 / 條件,自動抓 Trip.com 機加酒方案,符合就用 LINE 推播給你。

## 檔案結構
| 檔案 | 作用 |
|------|------|
| `config.json` | 你的搜尋條件(改這裡就好,不用動程式) |
| `scraper.py`  | 抓 Trip.com。**最需要維護**,頁面改版時改這裡 |
| `filters.py`  | 篩選邏輯(價格、星級、時間、關鍵字) |
| `notify.py`   | LINE Messaging API 推播 |
| `main.py`     | 主流程 |
| `seen.json`   | 已通知記錄,自動產生,避免重複通知 |

## 快速測試(不用抓真網站)
```bash
pip install -r ../requirements.txt
TRIP_MOCK=1 python -m trip_alert.main
```
會用假資料跑完整條「抓取→篩選→通知」流程。沒設 LINE 金鑰時,通知會印在畫面上。

## 正式使用步驟
1. **LINE Bot**:到 [LINE Developers](https://developers.line.biz/) 建一個 Messaging API channel,
   取得 `Channel access token`;把 bot 加為好友後取得你的 `userId`。
2. 在 GitHub repo 的 **Settings → Secrets** 設定:
   - `LINE_CHANNEL_TOKEN`
   - `LINE_USER_ID`
3. **對 selector(只需做一次)**:在**能連到 Trip.com 的電腦**上跑
   ```bash
   TRIP_DEBUG=1 python -m trip_alert.main
   ```
   會產生 `trip_alert/trip_debug.html`。把這個檔貼給 Claude,
   即可精準填好 `scraper.py` 的 `_build_search_url` 與 `_parse_card`。
4. 改 `config.json` 成你要的日期與條件。
5. GitHub Actions 會依 `trip_check.yml` 的排程自動執行;也可到 Actions 頁面手動 **Run workflow**。

## 注意
- LINE Notify 已停止服務,故改用 Messaging API。
- Trip.com 無公開 API,請低頻使用,僅供個人。
