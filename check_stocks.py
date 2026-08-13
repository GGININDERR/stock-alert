"""股票機器人主程式 — 多指令版本

CLI 用法:
  python check_stocks.py check TW              # 檢查台股停損/停利
  python check_stocks.py check US              # 檢查美股
  python check_stocks.py check ALL             # 兩個都查
  python check_stocks.py list                  # 列出全部持股 + 總損益
  python check_stocks.py price TW 2330         # 查單檔現價 + 均線
  python check_stocks.py price US NVDA
  python check_stocks.py add TW 2330 950 1000  # 新增持股(市場 代碼 成本 股數)
  python check_stocks.py del TW 2330           # 刪除持股
  python check_stocks.py sold TW 2330 500      # 賣出 N 股(自動扣減)
  python check_stocks.py clear ALL CONFIRM     # 清空全部持股(需 CONFIRM)

環境變數:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID, SPREADSHEET_ID, GOOGLE_CREDENTIALS
"""
import gspread
import yfinance as yf
import requests
import json
import os
import sys
from datetime import datetime
from google.oauth2.service_account import Credentials

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
SPREADSHEET_ID = os.environ['SPREADSHEET_ID']
GOOGLE_CREDENTIALS = os.environ['GOOGLE_CREDENTIALS']

# 停利門檻(% 報酬率)
TP_HALF_PCT = 20   # 漲 ≥ 20% → 建議賣出一半
TP_FULL_PCT = 50   # 漲 ≥ 50% → 建議全部獲利了結

SCOPE_READ = ['https://www.googleapis.com/auth/spreadsheets.readonly']
SCOPE_WRITE = ['https://www.googleapis.com/auth/spreadsheets']


# ───────────────────────── Telegram ─────────────────────────

def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    })
    return resp.ok


# ───────────────────────── Google Sheets ─────────────────────────

def get_worksheet(write=False):
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    scopes = SCOPE_WRITE if write else SCOPE_READ
    creds = Credentials.from_service_account_info(creds_dict, scopes=scopes)
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    return sh.get_worksheet(0)


def get_portfolio(write=False):
    return get_worksheet(write=write).get_all_records()


# ───────────────────────── yfinance ─────────────────────────

def get_stock_data(symbol):
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period='3mo')
    if hist.empty or len(hist) < 20:
        return None
    return hist


def fetch_history(market, code):
    if market == 'TW':
        for suffix in ('.TW', '.TWO'):
            hist = get_stock_data(f"{code}{suffix}")
            if hist is not None:
                return hist
        return None
    return get_stock_data(code)


def fmt_price(price, market):
    return f"NT${price:,.2f}" if market == 'TW' else f"${price:,.2f}"


# ───────────────────────── check (停損 + 停利) ─────────────────────────

def analyze_stock(row):
    """回傳:
       - (alert_dict, code, None) 有警報
       - (None,       code, error_msg) 資料不足
       - (None,       code, None) 無事
    """
    market = str(row.get('市場', '')).strip()
    code = str(row.get('代碼', '')).strip()
    name = str(row.get('名稱', '')).strip() or code
    cost = float(row.get('成本均價', 0) or 0)
    shares = int(row.get('持股數量', 0) or 0)

    if not code or shares == 0 or cost == 0:
        return None, code, None

    hist = fetch_history(market, code)
    if hist is None:
        return None, code, "資料不足（需至少 20 個交易日）"

    current = float(hist['Close'].iloc[-1])
    ma10 = float(hist['Close'].tail(10).mean())
    ma20 = float(hist['Close'].tail(20).mean())
    pnl_pct = (current - cost) / cost * 100

    base = {
        'name': name, 'code': code, 'market': market,
        'current': current, 'ma10': ma10, 'ma20': ma20,
        'cost': cost, 'shares': shares, 'pnl_pct': pnl_pct,
    }

    # 停損優先(跌破均線),停利後檢查
    if current < ma20:
        return {**base, 'level': 'SL_FULL', 'sell_shares': shares}, code, None
    if current < ma10:
        return {**base, 'level': 'SL_HALF', 'sell_shares': shares // 2}, code, None
    if pnl_pct >= TP_FULL_PCT:
        return {**base, 'level': 'TP_FULL', 'sell_shares': shares}, code, None
    if pnl_pct >= TP_HALF_PCT:
        return {**base, 'level': 'TP_HALF', 'sell_shares': shares // 2}, code, None

    return None, code, None


ALERT_META = {
    'SL_FULL': ('🔴', '跌破 20日線 → 全部停損'),
    'SL_HALF': ('🟡', '跌破 10日線 → 停損一半'),
    'TP_FULL': ('💎', f'獲利 ≥ {TP_FULL_PCT}% → 全部獲利了結'),
    'TP_HALF': ('🟢', f'獲利 ≥ {TP_HALF_PCT}% → 獲利了結一半'),
}


def format_alert(a):
    emoji, reason = ALERT_META[a['level']]
    pnl_sign = '+' if a['pnl_pct'] >= 0 else ''
    p = lambda v: fmt_price(v, a['market'])
    return (
        f"{emoji} <b>{a['name']} ({a['code']})</b>\n"
        f"原因：{reason}\n"
        f"現價：{p(a['current'])}  |  10日均：{p(a['ma10'])}  |  20日均：{p(a['ma20'])}\n"
        f"成本：{p(a['cost'])}  |  損益：{pnl_sign}{a['pnl_pct']:.1f}%\n"
        f"👉 建議{'賣出' if a['level'].startswith('SL') else '獲利了結'} <b>{a['sell_shares']} 股</b>"
    )


def cmd_check(market_arg):
    market_arg = (market_arg or 'ALL').upper()
    markets = ['TW', 'US'] if market_arg == 'ALL' else [market_arg]

    try:
        portfolio = get_portfolio()
    except Exception as e:
        send_telegram(f"⚠️ 讀取 Google Sheets 失敗：{e}")
        return

    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    for market in markets:
        label = '台股' if market == 'TW' else '美股'
        holdings = [
            r for r in portfolio
            if str(r.get('市場', '')).strip() == market
            and int(r.get('持股數量', 0) or 0) > 0
        ]

        if not holdings:
            send_telegram(f"ℹ️ [{label}] 目前無持股，略過檢查。")
            continue

        alerts, errors = [], []
        for row in holdings:
            try:
                result, code, err = analyze_stock(row)
                if err:
                    errors.append(f"{code}：{err}")
                elif result:
                    alerts.append(result)
            except Exception as e:
                code = str(row.get('代碼', '?'))
                errors.append(f"{code}：{e}")

        if alerts:
            msg = f"⚠️ <b>動作提醒 [{label}] {now}</b>\n\n"
            msg += "\n\n".join(format_alert(a) for a in alerts)
        else:
            ok = len(holdings) - len(errors)
            msg = f"✅ <b>[{label}收盤] {now}</b>\n{ok} 檔持股在安全區間，無需動作。"

        if errors:
            msg += "\n\n⚠️ 以下標的檢查失敗：\n" + "\n".join(errors)
        send_telegram(msg)


# ───────────────────────── list (持股清單) ─────────────────────────

def cmd_list():
    try:
        portfolio = get_portfolio()
    except Exception as e:
        send_telegram(f"⚠️ 讀取 Google Sheets 失敗：{e}")
        return

    holdings = [r for r in portfolio if int(r.get('持股數量', 0) or 0) > 0]
    if not holdings:
        send_telegram("ℹ️ 持股清單是空的。")
        return

    by_market = {'TW': [], 'US': []}
    totals = {'TW': {'cost': 0, 'value': 0}, 'US': {'cost': 0, 'value': 0}}

    for row in holdings:
        market = str(row.get('市場', '')).strip()
        code = str(row.get('代碼', '')).strip()
        name = str(row.get('名稱', '')).strip() or code
        cost = float(row.get('成本均價', 0))
        shares = int(row.get('持股數量', 0))

        hist = fetch_history(market, code)
        if hist is None:
            current = cost
            note = ' (無報價)'
        else:
            current = float(hist['Close'].iloc[-1])
            note = ''

        pnl_pct = (current - cost) / cost * 100 if cost else 0
        emoji = '🟢' if pnl_pct >= 0 else '🔴'
        sign = '+' if pnl_pct >= 0 else ''

        line = (
            f"{emoji} <b>{name} ({code})</b>{note}\n"
            f"  現:{fmt_price(current, market)}  成:{fmt_price(cost, market)}  "
            f"{shares:,} 股  {sign}{pnl_pct:.1f}%"
        )
        by_market.setdefault(market, []).append(line)
        totals[market]['cost'] += cost * shares
        totals[market]['value'] += current * shares

    parts = [f"📊 <b>持股清單</b>"]
    for m, label in [('TW', '台股'), ('US', '美股')]:
        if by_market[m]:
            parts.append(f"\n<b>【{label}】</b>")
            parts.extend(by_market[m])
            t = totals[m]
            if t['cost']:
                pnl = t['value'] - t['cost']
                pnl_pct = pnl / t['cost'] * 100
                sign = '+' if pnl >= 0 else ''
                parts.append(
                    f"  小計  成本:{fmt_price(t['cost'], m)}  "
                    f"市值:{fmt_price(t['value'], m)}  "
                    f"損益:{sign}{fmt_price(pnl, m)} ({sign}{pnl_pct:.1f}%)"
                )

    send_telegram("\n".join(parts))


# ───────────────────────── price (即時查價) ─────────────────────────

def cmd_price(market, code):
    market = (market or '').upper()
    code = (code or '').strip()
    if not market or not code:
        send_telegram("❌ 用法錯誤,範例:<code>/price TW 2330</code> 或 <code>/price US NVDA</code>")
        return
    if market not in ('TW', 'US'):
        send_telegram(f"❌ 市場只支援 TW 或 US,你傳的是「{market}」")
        return

    hist = fetch_history(market, code)
    if hist is None:
        send_telegram(f"❌ 查無 {market} {code}(可能代碼錯或資料不足)")
        return

    current = float(hist['Close'].iloc[-1])
    ma10 = float(hist['Close'].tail(10).mean())
    ma20 = float(hist['Close'].tail(20).mean())
    high20 = float(hist['High'].tail(20).max())
    low20 = float(hist['Low'].tail(20).min())
    change_pct = (current - float(hist['Close'].iloc[-2])) / float(hist['Close'].iloc[-2]) * 100
    sign = '+' if change_pct >= 0 else ''

    p = lambda v: fmt_price(v, market)
    msg = (
        f"💹 <b>{code}</b> ({market})\n"
        f"現價: {p(current)}  ({sign}{change_pct:.2f}%)\n"
        f"10日均: {p(ma10)}\n"
        f"20日均: {p(ma20)}\n"
        f"20日高: {p(high20)}\n"
        f"20日低: {p(low20)}"
    )
    send_telegram(msg)


# ───────────────────────── add / del / sold (持股管理) ─────────────────────────

def _service_account_email():
    try:
        return json.loads(GOOGLE_CREDENTIALS).get('client_email', '(未知)')
    except Exception:
        return '(讀取失敗)'


def _write_hint():
    email = _service_account_email()
    return (
        "⚠️ 寫入失敗,Google Sheets 沒給 Service Account 寫入權限。\n\n"
        "請打開你的持股 Google Sheet → 右上角「<b>共用</b>」按鈕 → 在新增使用者欄位貼入下面這個 email:\n\n"
        f"<code>{email}</code>\n\n"
        "→ 把權限從「檢視者」改成「<b>編輯者</b>」→ 傳送(不用發通知)。"
    )


def _find_row(ws, market, code):
    """回傳 (row_index 1-based, row_dict) 或 (None, None)"""
    records = ws.get_all_records()
    for idx, row in enumerate(records, start=2):  # 第 1 列是標題
        if (str(row.get('市場', '')).strip() == market
                and str(row.get('代碼', '')).strip() == str(code)):
            return idx, row
    return None, None


def _header_map(ws):
    """欄位名稱 → 欄位 index(1-based)"""
    headers = ws.row_values(1)
    return {h: i + 1 for i, h in enumerate(headers)}


def cmd_add(market, code, cost, shares, name=''):
    market = market.upper()
    if market not in ('TW', 'US'):
        send_telegram(f"❌ 市場只支援 TW 或 US"); return
    try:
        cost_f = float(cost)
        shares_i = int(shares)
    except ValueError:
        send_telegram("❌ 成本/股數格式錯誤"); return
    if cost_f <= 0 or shares_i <= 0:
        send_telegram("❌ 成本與股數須大於 0"); return

    try:
        ws = get_worksheet(write=True)
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}"); return

    # 已存在 → 提示用 /edit 或 /sold
    idx, existing = _find_row(ws, market, code)
    if idx:
        send_telegram(
            f"⚠️ {market} {code} 已在清單(現有 {existing.get('持股數量')} 股)。"
            f"想加碼請手動編輯,或先 /del 再 /add。"
        )
        return

    today = datetime.now().strftime('%Y-%m-%d')
    headers = ws.row_values(1)
    new_row = []
    for h in headers:
        val = {
            '市場': market, '代碼': code, '名稱': name or code,
            '成本均價': cost_f, '持股數量': shares_i,
            '買入日期': today, '備註': '',
        }.get(h, '')
        new_row.append(val)
    try:
        ws.append_row(new_row, value_input_option='USER_ENTERED')
        send_telegram(
            f"✅ 已新增 <b>{market} {code}</b>\n"
            f"成本:{cost_f}  股數:{shares_i:,}  日期:{today}"
        )
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}")


def cmd_del(market, code):
    market = market.upper()
    try:
        ws = get_worksheet(write=True)
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}"); return

    idx, row = _find_row(ws, market, code)
    if not idx:
        send_telegram(f"❌ 找不到 {market} {code}"); return
    try:
        ws.delete_rows(idx)
        send_telegram(
            f"🗑 已刪除 <b>{market} {code}</b> "
            f"({row.get('名稱', '')},原 {row.get('持股數量')} 股)"
        )
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}")


def cmd_clear(confirmed):
    """清空全部持股(只留標題列)。誤觸代價太大,一定要帶 CONFIRM。"""
    if not confirmed:
        send_telegram(
            "⚠️ <b>這會刪掉全部持股</b>,無法復原。\n"
            "確定的話請打:<code>/clear CONFIRM</code>"
        )
        return

    try:
        ws = get_worksheet(write=True)
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}"); return

    try:
        records = ws.get_all_records()
        if not records:
            send_telegram("📭 持股清單本來就是空的"); return
        # 刪之前先把還原用的 /add 指令留一份(TG + Actions log)
        backup = []
        for row in records:
            market = str(row.get('市場', '')).strip()
            code = str(row.get('代碼', '')).strip()
            name = str(row.get('名稱', '')).strip()
            backup.append(
                f"/add {market} {code} {row.get('成本均價')} "
                f"{row.get('持股數量')} {name}".strip()
            )
        print('=== 清空前備份 ===')
        for line in backup:
            print(line)

        ws.delete_rows(2, len(records) + 1)
        send_telegram(
            f"🗑 已清空全部持股(共 {len(records)} 檔),標題列保留。\n\n"
            "<b>還原用指令</b>(要復原就把下面貼回來):\n"
            "<code>" + '\n'.join(backup) + "</code>"
        )
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}")


def cmd_sold(market, code, n_shares):
    market = market.upper()
    try:
        n = int(n_shares)
    except ValueError:
        send_telegram("❌ 股數格式錯誤"); return
    if n <= 0:
        send_telegram("❌ 股數須大於 0"); return

    try:
        ws = get_worksheet(write=True)
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}"); return

    idx, row = _find_row(ws, market, code)
    if not idx:
        send_telegram(f"❌ 找不到 {market} {code}"); return

    current_shares = int(row.get('持股數量', 0) or 0)
    if n > current_shares:
        send_telegram(f"❌ 你只有 {current_shares} 股,不能賣 {n} 股"); return

    new_shares = current_shares - n
    hmap = _header_map(ws)
    shares_col = hmap.get('持股數量')

    try:
        if new_shares == 0:
            ws.delete_rows(idx)
            send_telegram(
                f"💰 賣出 <b>{market} {code}</b> {n:,} 股(全部出清,自動刪除)"
            )
        else:
            ws.update_cell(idx, shares_col, new_shares)
            send_telegram(
                f"💰 賣出 <b>{market} {code}</b> {n:,} 股\n"
                f"剩餘:{new_shares:,} 股"
            )
    except Exception as e:
        send_telegram(_write_hint() + f"\n\n錯誤:{e}")


# ───────────────────────── whoami (查 service account email) ─────────────────────────

def cmd_whoami():
    try:
        creds = json.loads(GOOGLE_CREDENTIALS)
        email = creds.get('client_email', '(無)')
        project = creds.get('project_id', '(無)')
        sheet_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"
        send_telegram(
            "🔑 <b>持股系統設定資訊</b>\n\n"
            f"<b>① 你的持股 Sheet:</b>\n"
            f'<a href="{sheet_url}">點這裡打開 Sheet</a>\n\n'
            f"<b>② Service Account Email:</b>\n"
            f"<code>{email}</code>\n"
            f"<i>(點上面那串可以一鍵複製)</i>\n\n"
            f"<b>Project:</b> <code>{project}</code>\n\n"
            "──────────\n"
            "🔧 <b>開放寫入步驟</b>(用 /add /del /sold /clear 必做):\n"
            "1. 點 ① 打開 Sheet\n"
            "2. 右上角藍色「共用」按鈕\n"
            "3. 貼上 ② 的 email,權限選「<b>編輯者</b>」\n"
            "4. 取消勾「通知人員」,送出"
        )
    except Exception as e:
        send_telegram(f"❌ 讀取設定失敗:{e}")


# ───────────────────────── 入口 ─────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        # 預設行為:檢查 TW(向後相容舊版排程)
        cmd_check('TW')
        return

    cmd = args[0].lower()
    rest = args[1:]

    try:
        if cmd == 'check':
            cmd_check(rest[0] if rest else 'ALL')
        elif cmd == 'list':
            cmd_list()
        elif cmd == 'whoami':
            cmd_whoami()
        elif cmd == 'price':
            if len(rest) < 2:
                send_telegram("❌ 用法:<code>/price TW 2330</code>")
                return
            cmd_price(rest[0], rest[1])
        elif cmd == 'add':
            if len(rest) < 4:
                send_telegram(
                    "❌ 用法:<code>/add TW 2330 950 1000 [名稱]</code>\n"
                    "(市場 代碼 成本 股數 [可選名稱])"
                ); return
            cmd_add(rest[0], rest[1], rest[2], rest[3],
                    ' '.join(rest[4:]) if len(rest) > 4 else '')
        elif cmd in ('del', 'delete'):
            if len(rest) < 2:
                send_telegram("❌ 用法:<code>/del TW 2330</code>"); return
            cmd_del(rest[0], rest[1])
        elif cmd == 'sold':
            if len(rest) < 3:
                send_telegram("❌ 用法:<code>/sold TW 2330 500</code>"); return
            cmd_sold(rest[0], rest[1], rest[2])
        elif cmd == 'clear':
            # rest 可能是 ['ALL', 'CONFIRM'] 或只有 ['CONFIRM']
            cmd_clear(any(a.upper() == 'CONFIRM' for a in rest))
        # 向後相容:第一個參數直接是 TW/US/ALL 時當 check
        elif cmd.upper() in ('TW', 'US', 'ALL'):
            cmd_check(cmd.upper())
        else:
            send_telegram(f"❌ 未知指令:{cmd}")
    except Exception as e:
        send_telegram(f"❌ 執行失敗:{type(e).__name__}: {e}")


if __name__ == '__main__':
    main()
