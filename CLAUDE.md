# CLAUDE.md

Guidance for AI assistants working in this repository.

## What this is

A personal **stock stop-loss / take-profit alert bot** for Taiwan (TW) and US
markets. Holdings live in a Google Sheet; prices come from Yahoo Finance
(`yfinance`); alerts and command replies are pushed to **Telegram**. Users
control it by typing Telegram slash-commands, which are relayed through a
**Cloudflare Worker** that triggers a **GitHub Actions** workflow which runs the
Python program. The same workflow also runs on a schedule after each market
close.

Note: user-facing strings, comments, and this app's domain vocabulary are in
**Traditional Chinese**. Keep new user-facing text consistent with that.

## Architecture / data flow

```
Telegram user types /command
        │  (webhook)
        ▼
cloudflare_worker.js   ── validates secret + chat_id, ACKs "⏳ 收到指令"
        │  (workflow_dispatch via GitHub REST API, ref: main)
        ▼
.github/workflows/stock_check.yml   ── installs deps, dispatches command
        │
        ▼
check_stocks.py <cmd> <market> <args>
        │  reads/writes Google Sheet, fetches yfinance quotes
        ▼
Telegram sendMessage  ── result pushed back to the user
```

Scheduled runs skip the Worker entirely: cron in the workflow calls
`check_stocks.py check TW` / `check US` directly after each market close.

## Files

| File | Role |
|------|------|
| `check_stocks.py` | The whole bot. CLI dispatcher + all command logic (check, list, price, add, del, sold, whoami). |
| `cloudflare_worker.js` | Telegram webhook receiver. Parses slash-commands, ACKs, dispatches the GitHub workflow. Deployed on Cloudflare (not run in CI). |
| `.github/workflows/stock_check.yml` | Runs the bot on schedule and on `workflow_dispatch`. |
| `requirements.txt` | Python deps: `gspread`, `google-auth`, `yfinance`, `requests`. |

## `check_stocks.py` conventions

- **Entry point** is `main()` → dispatches `sys.argv[1:]`. First arg is the
  command; `TW`/`US`/`ALL` as the first arg is treated as `check` (backward
  compat), and no args defaults to `check TW`.
- **Every command reports via Telegram**, including errors — the top-level
  `try/except` in `main()` sends failures to TG rather than crashing silently.
  Preserve this: user feedback happens through `send_telegram`, not stdout.
- **Google Sheets access** goes through `get_worksheet(write=False)`. Read uses
  the read-only scope; mutating commands (`add`/`del`/`sold`) pass `write=True`.
  On write failure, show `_write_hint()` which tells the user to share the Sheet
  with the service-account email as Editor.
- **Sheet columns are Chinese header names**: `市場` (market), `代碼` (code),
  `名稱` (name), `成本均價` (avg cost), `持股數量` (shares), `買入日期` (buy date),
  `備註` (note). Rows are matched by (`市場`, `代碼`). Header row is row 1;
  `get_all_records()` data starts at sheet row 2.
- **Ticker resolution** (`fetch_history`): TW codes try `.TW` then `.TWO`
  suffixes; US codes are used as-is. Requires ≥20 trading days of history.
- **Alert logic** (`analyze_stock`), evaluated in priority order:
  stop-loss first, then take-profit.
  - `current < ma20` → SL_FULL (sell all)
  - `current < ma10` → SL_HALF (sell half)
  - `pnl_pct ≥ 50` (`TP_FULL_PCT`) → TP_FULL
  - `pnl_pct ≥ 20` (`TP_HALF_PCT`) → TP_HALF
  Thresholds are module constants near the top.
- **Telegram messages use `parse_mode: HTML`** — use `<b>`, `<code>`, `<a>`;
  escape `<`/`>`/`&` in dynamic text where needed.

## Commands (kept in sync across three places)

Adding or changing a command requires touching **all three**:
1. `check_stocks.py` — the `cmd_*` handler and its `main()` dispatch branch.
2. `cloudflare_worker.js` — `COMMAND_TABLE` (simple) or `MULTI_ARG_COMMANDS`
   (needs market + args), plus `HELP_TEXT` and `prettyLabel`.
3. `.github/workflows/stock_check.yml` — the `case "$CMD"` block if it needs
   special arg handling.

Current commands: `check [TW|US|ALL]`, `list`, `price <mkt> <code>`,
`add <mkt> <code> <cost> <shares> [name]`, `del <mkt> <code>`,
`sold <mkt> <code> <shares>`, `whoami`.

## Environment variables / secrets

`check_stocks.py` (GitHub Actions secrets): `TELEGRAM_TOKEN`,
`TELEGRAM_CHAT_ID`, `SPREADSHEET_ID`, `GOOGLE_CREDENTIALS` (service-account JSON
as a string).

`cloudflare_worker.js` (Cloudflare vars): `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID`,
`GITHUB_TOKEN` (PAT with Actions:Write), `GITHUB_REPO` (`GGININDERR/stock-alert`),
`WEBHOOK_SECRET` (matches Telegram webhook `secret_token`).

Never hardcode or commit any of these values.

## Development / testing

There is no test suite, linter, or build step. To exercise a command locally
you must supply the four env vars (a real Telegram bot + chat and a
service-account with access to the Sheet):

```bash
pip install -r requirements.txt
export TELEGRAM_TOKEN=... TELEGRAM_CHAT_ID=... SPREADSHEET_ID=... GOOGLE_CREDENTIALS='{...}'
python check_stocks.py price TW 2330      # quick read-only smoke test
python check_stocks.py list
```

`price` and `check` are read-only and safest for smoke-testing.
`add`/`del`/`sold` mutate the live Sheet.

To validate the workflow without the Worker, use the Actions "Run workflow"
(`workflow_dispatch`) button with `command` / `market` / `args` inputs.

## Conventions for changes

- Keep the single-file structure of `check_stocks.py`; group new logic under the
  existing section comment banners.
- Match the surrounding style: Chinese user-facing text and section comments,
  terse helpers, `send_telegram` for all output.
- The Cloudflare Worker is edge JS (`export default { async fetch }`) — no Node
  APIs, no npm deps.
