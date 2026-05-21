# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What This Project Does

A GitHub Actions-based stock stop-loss alert bot. It runs on a schedule after Taiwan (TW) and US market close on weekdays, reads a portfolio from Google Sheets, fetches price history via yfinance, and sends Telegram messages when holdings break below their 10-day or 20-day moving averages.

## Running the Script

```bash
# Install dependencies
pip install -r requirements.txt

# Run for Taiwan market
python check_stocks.py TW

# Run for US market
python check_stocks.py US
```

The script requires four environment variables at runtime:
- `TELEGRAM_TOKEN` — Telegram bot token
- `TELEGRAM_CHAT_ID` — Telegram chat/channel ID
- `SPREADSHEET_ID` — Google Sheets document ID
- `GOOGLE_CREDENTIALS` — Service account JSON as a single-line string

## Architecture

Everything lives in `check_stocks.py`. The flow is:

1. **`get_portfolio()`** — authenticates with Google Sheets via a service account and returns all rows from the first worksheet.
2. **`get_stock_data(symbol)`** — fetches 3 months of daily OHLCV history from Yahoo Finance; returns `None` if fewer than 20 trading days are available.
3. **`check_stop_loss(row)`** — computes MA10/MA20 from closing prices and returns an alert dict if the current price is below either average, or `None` if the holding is safe.
4. **`main()`** — filters the portfolio to the requested market, calls `check_stop_loss` for each holding, then sends a single aggregated Telegram message.

## Stop-Loss Logic

| Condition | Level | Action |
|-----------|-------|--------|
| Current price < MA20 | `FULL` | Sell all shares |
| Current price < MA10 (but ≥ MA20) | `HALF` | Sell half shares |

## Google Sheets Schema

The spreadsheet's first worksheet must have these column headers (Traditional Chinese):

| Header | Meaning |
|--------|---------|
| 市場 | Market (`TW` or `US`) |
| 代碼 | Ticker code |
| 名稱 | Display name (optional) |
| 成本均價 | Average cost per share |
| 持股數量 | Number of shares held |

## Symbol Formatting

Taiwan stocks automatically get `.TW` appended (e.g., `2330` → `2330.TW`). US tickers are passed through as-is.

## CI / GitHub Actions

`.github/workflows/stock_check.yml` defines two jobs:

- **`check-tw`** — runs at `05:35 UTC` Mon–Fri (13:35 Taiwan time, after TWSE close), passing `TW` to the script.
- **`check-us`** — runs at `21:05 UTC` Mon–Fri (after NYSE/NASDAQ close in both EDT and EST), passing `US` to the script.

Both jobs can also be triggered manually via `workflow_dispatch` with a `market` input of `TW`, `US`, or `ALL`. All four secrets (`TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`, `SPREADSHEET_ID`, `GOOGLE_CREDENTIALS`) must be set in the repository's GitHub Secrets.
