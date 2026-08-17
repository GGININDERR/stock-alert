"""Telegram webhook 的裝設與檢查

指令的即時路徑是 Telegram → Cloudflare Worker → 觸發 workflow,前提是 bot 的
webhook 指向 Worker。tg_poll.py 收信時會解除 webhook(getUpdates 與 webhook
互斥),所以每次用完備援路徑,或第一次部署 Worker,都要用這支裝回去。

沒有回應時先跑 --info:pending_update_count 一直漲、last_error_message 有東西,
就是 webhook 這一段有問題,而不是掃描腳本的錯。

CLI 用法:
  python tg_webhook.py --info     # 看現在掛在哪、有沒有錯誤
  python tg_webhook.py --set      # 指向 WORKER_URL(帶 secret_token)
  python tg_webhook.py --delete   # 解除(要改用 tg_poll.py 收信時)

環境變數:
  TELEGRAM_TOKEN   Bot Token
  WORKER_URL       Worker 網址,例如 https://xxx.workers.dev
  WEBHOOK_SECRET   與 Worker 的 WEBHOOK_SECRET 相同的字串
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

TOKEN = os.environ.get('TELEGRAM_TOKEN', '')
WORKER_URL = os.environ.get('WORKER_URL', '').strip().rstrip('/')
SECRET = os.environ.get('WEBHOOK_SECRET', '')


def api(method, **params):
    url = f'https://api.telegram.org/bot{TOKEN}/{method}'
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


def show_info():
    r = api('getWebhookInfo')
    if not r.get('ok'):
        print('查詢失敗:', r.get('description'))
        return 1
    d = r.get('result', {})
    url = d.get('url') or '(沒有掛 webhook,目前是收信模式)'
    print('webhook 網址        :', url)
    print('待處理訊息數        :', d.get('pending_update_count', 0))
    if d.get('last_error_message'):
        # Telegram 只在這裡講失敗原因,Worker 的 log 看不到被拒絕的請求
        print('最後一次錯誤        :', d['last_error_message'])
        print('  (401 多半是 WEBHOOK_SECRET 兩邊沒對上;'
              '5xx 是 Worker 自己噴錯)')
    return 0


def set_hook():
    if not WORKER_URL:
        print('WORKER_URL 沒設定,不知道要指到哪')
        return 1
    if not SECRET:
        print('WEBHOOK_SECRET 沒設定;沒有它任何人都能對 Worker 灌假指令')
        return 1
    # drop_pending_updates:裝回去時把積壓的舊指令丟掉,否則會一次全部重跑
    r = api('setWebhook', url=WORKER_URL, secret_token=SECRET,
            drop_pending_updates='true',
            allowed_updates=json.dumps(['message']))
    if not r.get('ok'):
        print('設定失敗:', r.get('description'))
        return 1
    print('已指向', WORKER_URL)
    return show_info()


def delete_hook():
    r = api('deleteWebhook')
    if not r.get('ok'):
        print('解除失敗:', r.get('description'))
        return 1
    print('已解除,接下來要靠 tg_poll.py 收信才會有回應')
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description='Telegram webhook 裝設與檢查')
    g = p.add_mutually_exclusive_group()
    g.add_argument('--set', action='store_true', help='指向 WORKER_URL')
    g.add_argument('--delete', action='store_true', help='解除 webhook')
    g.add_argument('--info', action='store_true', help='只看現況(預設)')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not TOKEN:
        print('TELEGRAM_TOKEN 沒設定')
        return 1
    if opt.set:
        return set_hook()
    if opt.delete:
        return delete_hook()
    return show_info()


if __name__ == '__main__':
    sys.exit(main())
