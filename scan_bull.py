"""OKX USDT 永續合約 — 1H 多頭排列 + 量比放大掃描

CLI 用法:
  python scan_bull.py                 # 用預設參數掃描並推播 Telegram
  python scan_bull.py --dry           # 只印在螢幕,不發 Telegram
  python scan_bull.py --volr 3        # 量比門檻改 3(訊號更少更精)
  python scan_bull.py --turn 5e6      # 只看 24h 成交額 500 萬美元以上的主流幣
  python scan_bull.py --max-dist 10   # 避開追高:離 MA20 乖離 10% 以上不列入
  python scan_bull.py --bar 4H        # 改看 4H 級別(MA 週期不用動)
  python scan_bull.py --short         # 改掃空頭排列(均線反向,量比條件不變)

環境變數:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (--dry 時可不設)

免責:純技術面篩選,只看價格與成交量,不構成投資建議。
"""
import argparse
import concurrent.futures as cf
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone

import requests

OKX_TICKERS = 'https://www.okx.com/api/v5/market/tickers?instType=SWAP'
OKX_CANDLES = 'https://www.okx.com/api/v5/market/candles?instId={inst}&bar={bar}&limit=200'
UA = {'User-Agent': 'Mozilla/5.0'}

# 預設門檻
DEF_TURNOVER = 3e5   # 24h 成交額下限(USDT):太薄的掛單簿容易出假訊號
DEF_VOLR = 2.0       # 量比門檻:最近一根量 ÷ 前 20 根均量
DEF_CH1 = 0.5        # 動能過濾:1h 漲幅(%)
DEF_CH4 = 2.0        # 動能過濾:4h 漲幅(%)
HOT_DIST = 15.0      # 離 MA20 超過這個 % 視為末端加速,通知標紅
WORKERS = 12         # 併發執行緒,約 30 秒掃完全市場

TPE = timezone(timedelta(hours=8))


# ───────────────────────── Telegram ─────────────────────────

def send_telegram(message):
    token = os.environ['TELEGRAM_TOKEN']
    chat_id = os.environ['TELEGRAM_CHAT_ID']
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    resp = requests.post(url, json={
        'chat_id': chat_id,
        'text': message,
        'parse_mode': 'HTML',
        'disable_web_page_preview': True,
    })
    return resp.ok


# ───────────────────────── 工具 ─────────────────────────

def get(url, tries=3):
    """帶重試的 JSON 取得,失敗回 None"""
    for _ in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=20))
        except Exception:
            pass
    return None


def ma(vals, n):
    """n 期簡單移動平均;資料不足回 None"""
    return sum(vals[-n:]) / n if len(vals) >= n else None


# ───────────────────────── 第一層:挑候選 ─────────────────────────

def candidates(min_turnover):
    """全市場報價 → 只留 USDT 本位永續,且 24h 成交額達標的"""
    tick = get(OKX_TICKERS)
    if not tick or tick.get('code') != '0':
        raise SystemExit('OKX 報價取得失敗')

    out = []
    for d in tick['data']:
        inst = d['instId']
        if not inst.endswith('-USDT-SWAP'):
            continue
        try:
            last = float(d['last'])
            op = float(d['open24h'])
            turn = float(d['vol24h']) * last     # 24h 成交額(USDT)
        except (KeyError, ValueError):
            continue
        if turn < min_turnover or op <= 0:
            continue
        out.append((inst, last, (last / op - 1) * 100, turn))
    return out


# ───────────────────────── 第二層:算指標 ─────────────────────────

def measure(item, bar):
    """抓 K 線並算出均線、量比、各時間尺度漲幅"""
    inst, _last, ch24, turn = item
    r = get(OKX_CANDLES.format(inst=inst, bar=bar))
    if not r or r.get('code') != '0' or len(r.get('data', [])) < 130:
        return None

    rows = list(reversed(r['data']))     # API 回傳新到舊,反轉成舊到新
    closed = rows[:-1]                   # 關鍵:丟掉當下未收盤那根
    c = [float(k[4]) for k in closed]    # 收盤價序列
    v = [float(k[5]) for k in closed]    # 成交量序列

    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    if not m120:                         # K 棒不足 120 根,跳過
        return None

    base = sum(v[-21:-1]) / 20           # 前 20 根均量(不含自己,避免稀釋)
    volr = v[-1] / base if base else 0

    return dict(
        sym=inst.replace('-USDT-SWAP', ''),
        last=c[-1],
        bull=c[-1] > m20 > m60 > m120,           # 多頭排列
        bear=c[-1] < m20 < m60 < m120,           # 空頭排列
        volr=volr,
        turn=turn,
        ch1=(c[-1] / c[-2] - 1) * 100,           # 1 根 K 棒漲幅
        ch4=(c[-1] / c[-5] - 1) * 100,           # 4 根 K 棒漲幅
        ch12=(c[-1] / c[-13] - 1) * 100,         # 12 根 K 棒漲幅
        ch24=ch24,                                # 24 小時漲幅
        dist20=(c[-1] / m20 - 1) * 100,          # 離 MA20 乖離
    )


def scan(items, bar):
    res = []
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for r in ex.map(lambda x: measure(x, bar), items):
            if r:
                res.append(r)
    return res


# ───────────────────────── 第三層:篩選 ─────────────────────────

def pick(res, opt):
    """三個條件同時成立:均線排列 + 量比放大 + 有動能"""
    key = 'bear' if opt.short else 'bull'
    hits = [x for x in res
            if x[key]
            and x['volr'] > opt.volr
            and (abs(x['ch1']) > opt.ch1 or abs(x['ch4']) > opt.ch4)]
    if opt.max_dist is not None:
        hits = [x for x in hits if abs(x['dist20']) < opt.max_dist]
    hits.sort(key=lambda x: -x['volr'])           # 量比大的排前面
    return hits


# ───────────────────────── 輸出 ─────────────────────────

def line_text(x):
    return (f"{x['sym']:10} 價{x['last']:<12.6g} "
            f"1h{x['ch1']:+6.2f}% 4h{x['ch4']:+6.2f}% 12h{x['ch12']:+6.2f}% "
            f"24h{x['ch24']:+6.2f}% 離MA20{x['dist20']:+5.2f}% "
            f"量比{x['volr']:.2f} 24h額{x['turn']/1e6:.2f}M")


def build_message(hits, scanned, opt):
    """組 Telegram 訊息(HTML);乖離過大會標紅提醒別追高"""
    now = datetime.now(TPE).strftime('%Y-%m-%d %H:%M')
    side = '空頭排列' if opt.short else '多頭排列'
    head = (f"📊 <b>OKX {opt.bar} {side} + 量比&gt;{opt.volr:g} 掃描</b>\n"
            f"{now} (台北)  掃描 {scanned} 檔,符合 <b>{len(hits)}</b> 檔\n")

    blocks = []
    for x in hits:
        hot = abs(x['dist20']) >= HOT_DIST
        flag = ' 🔴<i>末端加速,建議等回踩</i>' if hot else ''
        blocks.append(
            f"\n<b>{x['sym']}</b>  {x['last']:.6g}{flag}\n"
            f"量比 <b>{x['volr']:.2f}</b>ｘ ｜ 離MA20 {x['dist20']:+.2f}%\n"
            f"1h {x['ch1']:+.2f}% ｜ 4h {x['ch4']:+.2f}% ｜ "
            f"12h {x['ch12']:+.2f}% ｜ 24h {x['ch24']:+.2f}%\n"
            f"24h 成交額 {x['turn']/1e6:.2f}M"
        )
    tail = "\n\n<i>技術面篩選,非投資建議。訊號來自 OKX,下單請以你的交易所實際報價為準。</i>"
    return head + ''.join(blocks) + tail


# ───────────────────────── main ─────────────────────────

def parse_args(argv):
    p = argparse.ArgumentParser(description='OKX 1H 多頭排列 + 量比掃描')
    p.add_argument('--bar', default='1H', help='K 線級別,預設 1H(可用 4H)')
    p.add_argument('--turn', type=float, default=DEF_TURNOVER,
                   help=f'24h 成交額下限 USDT,預設 {DEF_TURNOVER:g}')
    p.add_argument('--volr', type=float, default=DEF_VOLR,
                   help=f'量比門檻,預設 {DEF_VOLR:g}')
    p.add_argument('--ch1', type=float, default=DEF_CH1, help='1 根 K 棒漲幅門檻 %%')
    p.add_argument('--ch4', type=float, default=DEF_CH4, help='4 根 K 棒漲幅門檻 %%')
    p.add_argument('--max-dist', type=float, default=None,
                   help='離 MA20 乖離上限 %%,超過就排除(避開追高)')
    p.add_argument('--short', action='store_true', help='改掃空頭排列')
    p.add_argument('--dry', action='store_true', help='只印螢幕,不發 Telegram')
    p.add_argument('--json', default='bull_hits.json', help='結果輸出路徑')
    return p.parse_args(argv)


def main(argv=None):
    opt = parse_args(argv if argv is not None else sys.argv[1:])

    items = candidates(opt.turn)
    print(f'候選 {len(items)} 檔')

    res = scan(items, opt.bar)
    hits = pick(res, opt)
    print(f'掃描 {len(res)} 檔,符合 {len(hits)} 檔')
    for x in hits:
        print(line_text(x))

    if opt.json:
        with open(opt.json, 'w', encoding='utf-8') as f:
            json.dump(hits, f, ensure_ascii=False, indent=2)

    # 沒有標的就靜默,不打擾
    if hits and not opt.dry:
        send_telegram(build_message(hits, len(res), opt))

    return 0


if __name__ == '__main__':
    sys.exit(main())
