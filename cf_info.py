"""用 Cloudflare API Token 查出 Account ID 與現有 Worker 名稱

設定自動部署需要這兩項,但它們只在 Cloudflare 後台看得到。既然 Token
已經放進 GitHub Secrets,就直接從 Actions 問出來,不必再登入後台翻。

環境變數:CLOUDFLARE_API_TOKEN
"""
import json
import os
import sys
import urllib.error
import urllib.request

API = 'https://api.cloudflare.com/client/v4/'


def api(path):
    req = urllib.request.Request(
        API + path,
        headers={'Authorization': f"Bearer {os.environ['CLOUDFLARE_API_TOKEN']}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:          # 權限不足時也要看得到原因
        try:
            return json.load(e)
        except Exception:
            return {'success': False, 'errors': [f'HTTP {e.code}']}
    except Exception as e:                       # 連不上時給訊息,不要噴堆疊
        return {'success': False, 'errors': [str(e)]}


def main():
    print('── Token 是否有效 ──')
    v = api('user/tokens/verify')
    print('  success:', v.get('success'),
          '| status:', (v.get('result') or {}).get('status'),
          '| errors:', v.get('errors'))

    print('\n── 帳號 ──')
    acc = api('accounts')
    if not acc.get('success'):
        print('  查不到帳號清單,Token 可能缺 Account Settings:Read 權限')
        print('  errors:', acc.get('errors'))
        return 1
    ids = []
    for a in acc.get('result', []):
        ids.append(a['id'])
        print(f"  名稱 = {a['name']}")
        print(f"  Account ID = {a['id']}      ← 這個填進 CLOUDFLARE_ACCOUNT_ID")

    print('\n── Worker 清單 ──')
    for aid in ids:
        scripts = api(f'accounts/{aid}/workers/scripts')
        if not scripts.get('success'):
            print(f'  帳號 {aid}: 查不到,errors: {scripts.get("errors")}')
            continue
        rows = scripts.get('result') or []
        if not rows:
            print(f'  帳號 {aid}: 這個帳號底下沒有 Worker')
        for s in rows:
            print(f"  worker 名稱 = {s['id']}"
                  f"   最後修改 {str(s.get('modified_on'))[:19]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
