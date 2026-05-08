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

MARKET = sys.argv[1] if len(sys.argv) > 1 else 'TW'


def send_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    resp = requests.post(url, json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': message,
        'parse_mode': 'HTML'
    })
    return resp.ok


def get_portfolio():
    creds_dict = json.loads(GOOGLE_CREDENTIALS)
    creds = Credentials.from_service_account_info(
        creds_dict,
        scopes=['https://www.googleapis.com/auth/spreadsheets.readonly']
    )
    gc = gspread.authorize(creds)
    sh = gc.open_by_key(SPREADSHEET_ID)
    ws = sh.get_worksheet(0)
    return ws.get_all_records()


def get_stock_data(symbol):
    ticker = yf.Ticker(symbol)
    hist = ticker.history(period='3mo')
    if hist.empty or len(hist) < 20:
        return None
    return hist


def check_stop_loss(row):
    market = str(row.get('市場', '')).strip()
    code = str(row.get('代碼', '')).strip()
    name = str(row.get('名稱', '')).strip() or code
    cost = float(row.get('成本均價', 0) or 0)
    shares = int(row.get('持股數量', 0) or 0)

    if not code or shares == 0 or cost == 0:
        return None

    symbol = f"{code}.TW" if market == 'TW' else code
    hist = get_stock_data(symbol)
    if hist is None:
        return None, code, "資料不足（需至少 20 個交易日）"

    current_price = float(hist['Close'].iloc[-1])
    ma10 = float(hist['Close'].tail(10).mean())
    ma20 = float(hist['Close'].tail(20).mean())
    pnl_pct = (current_price - cost) / cost * 100

    base = {
        'name': name, 'code': code, 'market': market,
        'current': current_price, 'ma10': ma10, 'ma20': ma20,
        'cost': cost, 'shares': shares, 'pnl_pct': pnl_pct
    }

    if current_price < ma20:
        return {**base, 'level': 'FULL', 'sell_shares': shares}, code, None
    elif current_price < ma10:
        return {**base, 'level': 'HALF', 'sell_shares': shares // 2}, code, None

    return None, code, None


def fmt_price(price, market):
    return f"NT${price:.2f}" if market == 'TW' else f"${price:.2f}"


def format_alert(a):
    emoji = '🔴' if a['level'] == 'FULL' else '🟡'
    reason = '跌破 20日線 → 全部賣出' if a['level'] == 'FULL' else '跌破 10日線 → 賣出一半'
    pnl_sign = '+' if a['pnl_pct'] >= 0 else ''
    p = lambda v: fmt_price(v, a['market'])

    return (
        f"{emoji} <b>{a['name']} ({a['code']})</b>\n"
        f"原因：{reason}\n"
        f"現價：{p(a['current'])}  |  10日均：{p(a['ma10'])}  |  20日均：{p(a['ma20'])}\n"
        f"成本：{p(a['cost'])}  |  損益：{pnl_sign}{a['pnl_pct']:.1f}%\n"
        f"👉 建議賣出 <b>{a['sell_shares']} 股</b>"
    )


def main():
    market_label = '台股' if MARKET == 'TW' else '美股'
    now = datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')

    try:
        portfolio = get_portfolio()
    except Exception as e:
        send_telegram(f"⚠️ 讀取 Google Sheets 失敗：{e}")
        return

    holdings = [
        r for r in portfolio
        if str(r.get('市場', '')).strip() == MARKET
        and int(r.get('持股數量', 0) or 0) > 0
    ]

    if not holdings:
        send_telegram(f"ℹ️ [{market_label}] 目前無 {MARKET} 持股，略過檢查。")
        return

    alerts = []
    errors = []

    for row in holdings:
        try:
            result, code, err = check_stop_loss(row)
            if err:
                errors.append(f"{code}：{err}")
            elif result:
                alerts.append(result)
        except Exception as e:
            code = str(row.get('代碼', '?'))
            errors.append(f"{code}：{e}")

    if alerts:
        msg = f"⚠️ <b>停損警報 [{market_label}] {now}</b>\n\n"
        msg += "\n\n".join(format_alert(a) for a in alerts)
        if errors:
            msg += f"\n\n⚠️ 以下標的檢查失敗：\n" + "\n".join(errors)
        send_telegram(msg)
    else:
        ok_count = len(holdings) - len(errors)
        msg = f"✅ <b>[{market_label}收盤] {now}</b>\n{ok_count} 檔持股均在均線之上，無需動作。"
        if errors:
            msg += f"\n\n⚠️ 以下標的檢查失敗：\n" + "\n".join(errors)
        send_telegram(msg)


if __name__ == '__main__':
    main()
