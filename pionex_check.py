"""派網可行性探測 — 掃描器的標的,派網到底有沒有?

接派網之前要先回答一個前提問題:掃描器掃的是 OKX 的 USDT 永續合約,
標的常是小幣;派網不一定有,有也可能只有現貨。對不上的話,後面接帳戶
查詢或下單都沒有意義。

這支只讀公開資料,**不需要 API Key**,所以放心跑。

符號命名兩邊不一定一致 —— 已知派網的代幣化美股會加 X 尾綴(NBISX、
WDCX、ARMX),而掃描器記的是 NBIS、WDC。所以比對時會試幾種變體,
免得把「命名不同」誤判成「沒上架」。

CLI 用法:
  python pionex_check.py            # 比對 signals.jsonl 出現過的所有幣種
  python pionex_check.py --json out.json   # 順便存一份結果
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

UA = {'User-Agent': 'Mozilla/5.0'}

# 派網公開端點的候選路徑。文件連不上(egress 被擋),所以逐一試,
# 哪個回得出東西就用哪個 —— 順便把結果印出來,下次就不用猜了。
CANDIDATES = [
    'https://api.pionex.com/api/v1/common/symbols',
    'https://api.pionex.com/api/v1/market/symbols',
    'https://api.pionex.com/api/v1/common/symbols?type=PERP',
    'https://api.pionex.com/api/v1/futures/common/symbols',
]


def get(url, timeout=20):
    try:
        req = urllib.request.Request(url, headers=UA)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.load(r), None
    except urllib.error.HTTPError as e:
        return None, f'HTTP {e.code}'
    except Exception as e:
        return None, str(e)[:80]


def walk_symbols(obj, out):
    """從任意結構的回應裡撈出看起來像交易對的字串

    不知道回應長什麼樣,與其猜欄位名寫死,不如把整棵樹走一遍找
    'symbol' / 'symbolName' 這類鍵。多撈一點無妨,反正之後要比對。
    """
    if isinstance(obj, dict):
        for k, v in obj.items():
            if k.lower() in ('symbol', 'symbolname', 'name', 'instid') \
                    and isinstance(v, str):
                out.add(v.upper())
            else:
                walk_symbols(v, out)
    elif isinstance(obj, list):
        for x in obj:
            walk_symbols(x, out)
    return out


def base_of(sym):
    """把交易對還原成基礎幣:BTC_USDT_PERP → BTC"""
    s = sym.upper()
    for sep in ('_', '-', '/'):
        s = s.replace(sep, ' ')
    parts = [p for p in s.split() if p not in ('USDT', 'PERP', 'SWAP', 'USD')]
    return parts[0] if parts else s


def variants(sym):
    """同一個幣可能的寫法 —— 派網的代幣化美股會多一個 X 尾綴"""
    out = {sym, sym + 'X'}
    if sym.endswith('X') and len(sym) > 2:
        out.add(sym[:-1])
    return out


def our_symbols(path):
    syms = set()
    if not os.path.exists(path):
        return syms
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    syms.add(json.loads(line)['sym'].upper())
                except (ValueError, KeyError):
                    continue
    return syms


def main(argv=None):
    p = argparse.ArgumentParser(description='派網標的覆蓋率探測(不需金鑰)')
    p.add_argument('--file', default='signals.jsonl')
    p.add_argument('--json', default=None, help='把結果另存一份')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])

    print('=== 探測派網公開端點 ===')
    found, used = set(), None
    for url in CANDIDATES:
        data, err = get(url)
        if err:
            print(f'  ✗ {url}\n      {err}')
            continue
        got = walk_symbols(data, set())
        print(f'  ✓ {url}\n      撈到 {len(got)} 個代號')
        if got:
            found |= got
            used = used or url

    if not found:
        print('\n派網所有候選端點都拿不到資料 —— 可能是路徑不對或需要金鑰。')
        print('請把 https://www.pionex.com/docs/api-docs 的公開端點路徑給我,'
              '我改掉 CANDIDATES 再跑一次。')
        return 1

    bases = {base_of(s) for s in found}
    ours = our_symbols(opt.file)
    print(f'\n=== 比對 ===')
    print(f'派網代號 {len(found)} 個 → 基礎幣 {len(bases)} 種')
    print(f'掃描器出過訊號的幣種:{len(ours)} 種')

    hit, miss = [], []
    for s in sorted(ours):
        (hit if variants(s) & bases else miss).append(s)
    rate = 100 * len(hit) / len(ours) if ours else 0
    print(f'\n對得上:{len(hit)}/{len(ours)}  ({rate:.0f}%)')
    print(f'  {" ".join(hit)}')
    print(f'\n對不上:{len(miss)}')
    print(f'  {" ".join(miss)}')

    if opt.json:
        with open(opt.json, 'w', encoding='utf-8') as f:
            json.dump({'endpoint': used, 'pionex_bases': sorted(bases),
                       'matched': hit, 'missing': miss, 'rate': rate},
                      f, ensure_ascii=False, indent=2)
        print(f'\n已存 {opt.json}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
