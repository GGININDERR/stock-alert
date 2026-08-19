"""Telegram 指令收信:Cloudflare Worker 掛掉時的備援路徑

平常的路徑是 Telegram → Cloudflare Worker → 觸發 workflow,幾秒內就有回應。
這支是同一張指令表的收信版,只在 Worker 出問題時手動跑。

為什麼不當主力:排程最密只能 5 分鐘,但 GitHub 實際平均 30 分鐘才跑一次
(量過最長 85 分鐘),而下面的 MAX_AGE_SEC 會略過太舊的訊息,等於有一半的
指令在被讀到之前就過期。tg_bot.yml 的排程因此停用,只留手動觸發。

getUpdates 與 webhook 互斥,所以收信會踩掉 Worker 那條路;要真的接手必須
明講 --takeover,收完再用 tg_webhook.py --set 把 webhook 裝回去。

CLI 用法:
  python tg_poll.py            # 收信並執行指令(webhook 還在就拒絕動作)
  python tg_poll.py --takeover # 解除 webhook 後接手收信
  python tg_poll.py --dry      # 只印出解析結果,不執行也不回訊息

環境變數:TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')
API = f'https://api.telegram.org/bot{TOKEN}/'

# 太舊的訊息不執行:第一次啟用時 Telegram 會把積壓的訊息一次倒出來,
# 沒有這道防線會把幾天前的指令全部重跑一遍。
MAX_AGE_SEC = 30 * 60

SCAN_MODES = {'': 'breakout', 'breakout': 'breakout', 'early': 'early',
              'chase': 'chase', 'classic': 'classic'}

SCAN_LABEL = {'breakout': '剛啟動掃描', 'early': '壓縮後突破掃描',
              'chase': '追高順勢掃描', 'classic': '寬鬆版掃描'}

# 股票指令 → check_stocks.py 的參數(對應原本 Worker 的指令表)
STOCK_SIMPLE = {
    '/check': ['check', 'ALL'], '/check_all': ['check', 'ALL'],
    '/check_tw': ['check', 'TW'], '/check_us': ['check', 'US'],
    '/list': ['list'], '/portfolio': ['list'], '/whoami': ['whoami'],
}
STOCK_MULTI = {'/price', '/add', '/del', '/delete', '/sold'}

HELP = """🤖 <b>Stark 停損機器人</b>  <i>(GitHub 版)</i>

<b>股票</b>
/check 或 /check_all — 台股 + 美股(停損 + 停利)
/check_tw — 只檢查台股
/check_us — 只檢查美股
/list — 持股清單 + 總損益
/price &lt;TW|US&gt; &lt;代碼&gt; — 查現價 + 均線
/add &lt;TW|US&gt; &lt;代碼&gt; &lt;成本&gt; &lt;股數&gt; [名稱]
/del &lt;TW|US&gt; &lt;代碼&gt;
/sold &lt;TW|US&gt; &lt;代碼&gt; &lt;股數&gt;

<b>幣圈掃描(OKX USDT 永續)</b>
/scan — 立刻掃一次(預設 breakout:剛啟動)
/scan early — 壓縮後突破,還沒噴的位置
/scan chase — 追高順勢,供觀察回踩
/scan classic — 最寬鬆的一組
/pos — 目前幣圈持倉:停損價、離停損多遠、是否逼近停損
/stats — 訊號追蹤統計(這些條件到底準不準)

<b>自動排程</b>
台股 13:35、美股 21:05 收盤後自動檢查持股。
幣圈每小時掃 breakout、每 4 小時掃 early,掃到才推播;
每天 09:15 推一次訊號追蹤統計。

⏰ <i>你現在走的是備援收信模式,回應會慢。要恢復即時,跑一次
Telegram Webhook workflow 的 action=set。</i>"""


def api(method, **params):
    url = API + method
    data = urllib.parse.urlencode(params).encode() if params else None
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                    timeout=30) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        try:
            return json.load(e)
        except Exception:
            return {'ok': False, 'error_code': e.code}
    except Exception as e:
        return {'ok': False, 'description': str(e)}


def send(text):
    return api('sendMessage', chat_id=CHAT_ID, text=text, parse_mode='HTML')


def updates(takeover=False):
    """取回未讀訊息;webhook 還掛著時預設不動它

    Telegram 的 webhook 與 getUpdates 互斥。Worker 佔著 webhook 時要收信就得
    先解除,但解除等於把即時那條路關掉——這種副作用不該順手發生,所以只有
    明講 --takeover 才做。
    """
    r = api('getUpdates', timeout=0, allowed_updates=json.dumps(['message']))
    if not r.get('ok') and r.get('error_code') == 409:
        if not takeover:
            print('webhook 仍掛著(Cloudflare Worker),指令由它處理,這裡不接手。\n'
                  '真的要改用收信模式請加 --takeover,事後記得 '
                  'python tg_webhook.py --set 裝回去。')
            return []
        print('解除 webhook,改由這裡收信(Worker 那條路會失效)')
        api('deleteWebhook')
        time.sleep(1)
        r = api('getUpdates', timeout=0, allowed_updates=json.dumps(['message']))
    if not r.get('ok'):
        print('取信失敗:', r.get('description'))
        return []
    return r.get('result', [])


def ack(last_id):
    """回報已讀:Telegram 會把 offset 之前的訊息刪掉,不會重送"""
    api('getUpdates', offset=last_id + 1, timeout=0)


def run(cmd):
    """執行腳本;各腳本會自己把結果推回 Telegram"""
    print('執行:', ' '.join(cmd))
    p = subprocess.run([sys.executable] + cmd, capture_output=True, text=True)
    print(p.stdout[-2000:] or '(無輸出)')
    if p.returncode != 0:
        print('stderr:', p.stderr[-1000:])
        send(f"❌ 指令執行失敗(exit {p.returncode}),詳情看 Actions log")
    return p.returncode


def handle(text, dry=False):
    """把一則訊息翻成要執行的動作。回傳描述字串供 log 檢視"""
    tokens = text.split()
    if not tokens:
        return None
    head = tokens[0].lower().split('@')[0]
    rest = tokens[1:]

    if head in ('/help', '/start'):
        if not dry:
            send(HELP)
        return 'help'

    if head == '/scan':
        key = (rest[0].lower() if rest else '')
        mode = SCAN_MODES.get(key)
        if not mode:
            if not dry:
                send(f"❌ 沒有「{rest[0]}」這個模式。可用:"
                     f"<code>breakout</code>(預設) <code>early</code> "
                     f"<code>chase</code> <code>classic</code>")
            return f'scan:unknown({key})'
        if not dry:
            send(f"⏳ 收到指令,執行 <b>{SCAN_LABEL[mode]}</b> 中...\n"
                 f"<i>沒掃到標的就不會再有訊息</i>")
            run(['scan_bull.py', '--mode', mode, '--record', 'signals.jsonl'])
        return f'scan:{mode}'

    if head in ('/pos', '/positions', '/holding'):
        if not dry:
            send('⏳ 收到指令,查詢<b>幣圈持倉狀態</b>中...')
            run(['positions.py', '--report'])
        return 'positions'

    if head == '/stats':
        if not dry:
            send('⏳ 收到指令,整理<b>訊號追蹤統計</b>中...')
            run(['track.py', '--telegram'])
        return 'stats'

    if head in STOCK_SIMPLE:
        args = STOCK_SIMPLE[head]
        if not dry:
            send(f"⏳ 收到指令,執行 <b>{head}</b> 中...")
            run(['check_stocks.py'] + args)
        return f'stock:{" ".join(args)}'

    if head == '/clear':
        if not any(a.upper() == 'CONFIRM' for a in rest):
            if not dry:
                send('⚠️ <b>這會刪掉全部持股</b>,無法復原。\n'
                     '確定的話請打:<code>/clear CONFIRM</code>')
            return 'clear:需確認'
        if not dry:
            run(['check_stocks.py', 'clear', 'CONFIRM'])
        return 'clear:confirmed'

    if head in STOCK_MULTI:
        if len(rest) < 2:
            if not dry:
                send(f"❌ <code>{head}</code> 參數不足,打 /help 看用法")
            return f'{head}:參數不足'
        if not dry:
            send(f"⏳ 收到指令,執行 <b>{head} {' '.join(rest)}</b> 中...")
            run(['check_stocks.py', head[1:]] + rest)
        return f'stock:{head[1:]} {" ".join(rest)}'

    # 以 / 開頭但不認得的指令要回話。默默吃掉的話,打錯字或用到還沒
    # 上線的指令都會像機器人掛了一樣(而且訊息已被 ack,重跑也救不回)。
    if head.startswith('/'):
        if not dry:
            send(f"❓ 沒有 <code>{head}</code> 這個指令,打 /help 看清單")
        return f'unknown:{head}'

    return None                      # 一般聊天訊息,忽略


def main(argv=None):
    p = argparse.ArgumentParser(description='Telegram 指令收信')
    p.add_argument('--dry', action='store_true', help='只解析不執行')
    p.add_argument('--takeover', action='store_true',
                   help='webhook 還掛著時解除它,由這裡接手收信')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not TOKEN or not CHAT_ID:
        print('TELEGRAM_TOKEN / TELEGRAM_CHAT_ID 沒設定')
        return 1

    ups = updates(opt.takeover)
    print(f'收到 {len(ups)} 則訊息')
    now, last_id, handled = time.time(), None, 0

    for u in ups:
        last_id = u['update_id']
        msg = u.get('message') or {}
        if str(msg.get('chat', {}).get('id')) != str(CHAT_ID):
            continue                                   # 只認自己的對話
        if now - msg.get('date', 0) > MAX_AGE_SEC:
            print(f"略過過舊的訊息:{msg.get('text','')[:20]}")
            continue
        action = handle(msg.get('text', ''), opt.dry)
        if action:
            handled += 1
            print(f"  {msg.get('text','')[:30]} → {action}")

    if last_id is not None and not opt.dry:
        ack(last_id)                                   # 標記已讀,避免重複執行
    print(f'處理 {handled} 則指令')
    return 0


if __name__ == '__main__':
    sys.exit(main())
