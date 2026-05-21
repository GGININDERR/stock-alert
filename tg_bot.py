"""Telegram 指令接收器

支援的指令(只接受預設 TELEGRAM_CHAT_ID 的訊息):
  /check          → 檢查台股 + 美股
  /check_all      → 同上
  /check_tw       → 只檢查台股
  /check_us       → 只檢查美股
  /help, /start   → 顯示說明

由 GitHub Actions 定期輪詢,每次執行抓 TG 未讀訊息、解析指令、跑對應檢查。
"""
import os
import sys
import subprocess
import requests

TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
TELEGRAM_CHAT_ID = str(os.environ['TELEGRAM_CHAT_ID'])

API = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}"

HELP_TEXT = (
    "🤖 <b>Stark 停損機器人指令</b>\n\n"
    "/check 或 /check_all — 檢查台股 + 美股\n"
    "/check_tw — 只檢查台股\n"
    "/check_us — 只檢查美股\n"
    "/help — 顯示這則說明\n\n"
    "另外每個交易日收盤後會自動檢查並通知。"
)


def tg_send(text):
    requests.post(f"{API}/sendMessage", json={
        'chat_id': TELEGRAM_CHAT_ID,
        'text': text,
        'parse_mode': 'HTML',
    })


def get_updates():
    r = requests.get(f"{API}/getUpdates", params={'timeout': 0})
    return r.json().get('result', [])


def ack_updates(last_id):
    requests.get(f"{API}/getUpdates", params={'offset': last_id + 1})


def parse_command(text):
    cmd = text.strip().split('@')[0].lower()
    return {
        '/check': ('run', ['TW', 'US']),
        '/check_all': ('run', ['TW', 'US']),
        '/check_tw': ('run', ['TW']),
        '/check_us': ('run', ['US']),
        '/help': ('help', None),
        '/start': ('help', None),
    }.get(cmd)


def main():
    updates = get_updates()
    if not updates:
        return

    last_id = updates[-1]['update_id']
    actions = []  # 收集要做的事(去重)

    for u in updates:
        msg = u.get('message') or u.get('edited_message')
        if not msg:
            continue
        if str(msg.get('chat', {}).get('id')) != TELEGRAM_CHAT_ID:
            continue
        text = msg.get('text', '') or ''
        action = parse_command(text)
        if action:
            actions.append(action)

    ack_updates(last_id)

    if not actions:
        return

    markets_to_run = set()
    show_help = False
    for kind, payload in actions:
        if kind == 'run':
            markets_to_run.update(payload)
        elif kind == 'help':
            show_help = True

    if show_help:
        tg_send(HELP_TEXT)

    for market in sorted(markets_to_run):
        tg_send(f"⏳ 收到指令,開始檢查 <b>{market}</b> ...")
        subprocess.run(
            [sys.executable, 'check_stocks.py', market],
            check=False,
        )


if __name__ == '__main__':
    main()
