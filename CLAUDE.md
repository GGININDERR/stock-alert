# CLAUDE.md

本文件為 Claude Code（claude.ai/code）在此專案中操作時提供指引。

## 專案說明

基於 GitHub Actions 的股票停損警報機器人。每個交易日於台股與美股收盤後自動執行，從 Google Sheets 讀取持倉資料，透過 yfinance 抓取價格歷史，當持股跌破 10 日或 20 日均線時發送 Telegram 通知。

## 執行方式

```bash
# 安裝相依套件
pip install -r requirements.txt

# 執行台股檢查
python check_stocks.py TW

# 執行美股檢查
python check_stocks.py US
```

執行時需設定以下四個環境變數：
- `TELEGRAM_TOKEN` — Telegram 機器人 Token
- `TELEGRAM_CHAT_ID` — Telegram 聊天室／頻道 ID
- `SPREADSHEET_ID` — Google Sheets 文件 ID
- `GOOGLE_CREDENTIALS` — 服務帳號 JSON（需壓縮為單行字串）

## 架構說明

所有邏輯集中於 `check_stocks.py`，執行流程如下：

1. **`get_portfolio()`** — 透過服務帳號認證 Google Sheets，回傳第一個工作表的所有資料列。
2. **`get_stock_data(symbol)`** — 從 Yahoo Finance 抓取近 3 個月日線資料；若交易日不足 20 筆則回傳 `None`。
3. **`check_stop_loss(row)`** — 計算收盤價的 MA10／MA20，若當前價格跌破任一均線則回傳警報 dict，否則回傳 `None`。
4. **`main()`** — 依指定市場篩選持倉，逐一呼叫 `check_stop_loss`，最後彙整成單一 Telegram 訊息發送。

## 停損邏輯

| 條件 | 等級 | 動作 |
|------|------|------|
| 現價 < MA20 | `FULL` | 全部賣出 |
| 現價 < MA10（且 ≥ MA20） | `HALF` | 賣出一半 |

## Google Sheets 欄位格式

第一個工作表需包含以下欄位標題：

| 欄位 | 說明 |
|------|------|
| 市場 | 市場別（`TW` 或 `US`） |
| 代碼 | 股票代碼 |
| 名稱 | 顯示名稱（選填） |
| 成本均價 | 每股平均成本 |
| 持股數量 | 持有股數 |

## 股票代碼格式

台股代碼會自動加上 `.TW` 後綴（例如 `2330` → `2330.TW`）；美股代碼直接使用原始代碼。

## CI／GitHub Actions

`.github/workflows/stock_check.yml` 定義兩個 Job：

- **`check-tw`** — 每週一至五 `05:35 UTC`（台灣時間 13:35，台股收盤後）執行，傳入 `TW`。
- **`check-us`** — 每週一至五 `21:05 UTC`（涵蓋 EDT／EST 兩個時區的美股收盤後）執行，傳入 `US`。

兩個 Job 也可透過 `workflow_dispatch` 手動觸發，`market` 參數可設為 `TW`、`US` 或 `ALL`。四個 Secrets（`TELEGRAM_TOKEN`、`TELEGRAM_CHAT_ID`、`SPREADSHEET_ID`、`GOOGLE_CREDENTIALS`）須在 GitHub 儲存庫的 Secrets 中設定。
