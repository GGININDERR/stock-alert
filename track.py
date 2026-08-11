"""紙上交易追蹤:回頭計算每個訊號之後的漲跌,並統計成效

掃描器只負責找標的,不知道找得準不準。這支程式把 scan_bull.py 記下的
訊號拿回來,對照事後的 K 線,算出 4/12/24 小時後的報酬,累積成可以回答
「這套條件到底行不行」的數據。

CLI 用法:
  python track.py                    # 補算報酬並印出統計
  python track.py --telegram         # 統計順便推播 Telegram
  python track.py --file signals.jsonl

報酬定義:以訊號那根 K 棒的收盤價為進場價,N 小時後那根 K 棒的收盤價為
出場價。沒有停損、沒有手續費,是這套條件的「原始訊號品質」上限;實際
交易會比這個差。
"""
import argparse
import json
import os
import sys
from collections import defaultdict
from datetime import datetime

from scan_bull import OKX_CANDLES, TPE, get, send_telegram

HORIZONS = (4, 12, 24)          # 事後幾小時
HOUR_MS = 3600_000
FEE_PCT = 0.1                   # 來回手續費約 0.1%,估算淨值時扣掉


def load(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue
    return out


def save(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def closes_by_ts(sym):
    """抓該幣近 200 根 1H K 線,回傳 {時間戳: 收盤價}"""
    r = get(OKX_CANDLES.format(inst=f'{sym}-USDT-SWAP', bar='1H'))
    if not r or r.get('code') != '0':
        return None
    return {int(k[0]): float(k[4]) for k in r['data']}


def fill(rows, now_ms):
    """補算還沒算過、且時間已經到的報酬。回傳補了幾筆"""
    pending = defaultdict(list)
    for r in rows:
        for h in HORIZONS:
            key = f'ret{h}'
            if key not in r and now_ms >= r['ts'] + h * HOUR_MS:
                pending[r['sym']].append(r)
                break

    filled = 0
    for sym, items in pending.items():
        table = closes_by_ts(sym)
        if not table:
            continue
        for r in items:
            for h in HORIZONS:
                key = f'ret{h}'
                if key in r or now_ms < r['ts'] + h * HOUR_MS:
                    continue
                px = table.get(r['ts'] + h * HOUR_MS)
                if px:
                    r[key] = round((px / r['entry'] - 1) * 100, 2)
                    filled += 1
    return filled


def stats(vals):
    """回傳 (筆數, 平均, 中位數, 勝率%)"""
    if not vals:
        return 0, None, None, None
    s = sorted(vals)
    n = len(s)
    med = s[n // 2] if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2
    return n, sum(s) / n, med, 100 * sum(1 for x in s if x > 0) / n


def group_report(rows, keyfn, title):
    """依 keyfn 分組,列出各組 24h 的表現"""
    groups = defaultdict(list)
    for r in rows:
        if 'ret24' in r:
            groups[keyfn(r)].append(r['ret24'])
    if not groups:
        return []
    lines = [f'\n{title}(24h):']
    for k in sorted(groups, key=lambda k: -len(groups[k])):
        n, avg, med, win = stats(groups[k])
        if n:
            lines.append(f'  {str(k):14} {n:3} 筆  平均 {avg:+6.2f}%  '
                         f'中位 {med:+6.2f}%  勝率 {win:5.1f}%')
    return lines


def volr_bucket(r):
    v = r.get('volr') or 0
    if v >= 10:
        return '量比 ≥10'
    if v >= 5:
        return '量比 5-10'
    if v >= 3:
        return '量比 3-5'
    return '量比 2-3'


def report(rows):
    lines = []
    done = [r for r in rows if 'ret24' in r]
    lines.append(f'訊號 {len(rows)} 筆,已滿 24 小時 {len(done)} 筆')
    if rows:
        first = min(r['time'] for r in rows)[:16].replace('T', ' ')
        last = max(r['time'] for r in rows)[:16].replace('T', ' ')
        lines.append(f'期間 {first} ~ {last} (台北)')

    lines.append('\n整體表現:')
    for h in HORIZONS:
        n, avg, med, win = stats([r[f'ret{h}'] for r in rows if f'ret{h}' in r])
        if n:
            net = avg - FEE_PCT
            lines.append(f'  {h:2}h 後  {n:3} 筆  平均 {avg:+6.2f}%  '
                         f'中位 {med:+6.2f}%  勝率 {win:5.1f}%  '
                         f'(扣 {FEE_PCT}% 成本後 {net:+.2f}%)')
        else:
            lines.append(f'  {h:2}h 後  尚無資料')

    lines += group_report(rows, lambda r: r['mode'], '依模式')
    lines += group_report(rows, volr_bucket, '依量比')
    lines += group_report(rows, lambda r: r.get('tag', '—'), '依判讀標籤')
    return '\n'.join(lines)


def build_telegram(text, rows):
    done = sum(1 for r in rows if 'ret24' in r)
    head = '📈 <b>訊號追蹤週報</b>\n'
    warn = ''
    if done < 30:
        warn = (f'\n\n⚠️ <i>目前只有 {done} 筆滿 24 小時的樣本,數量太少,'
                f'這些數字還不足以下結論。建議累積 30 筆以上再看。</i>')
    return f'{head}<pre>{text}</pre>{warn}'


def main(argv=None):
    p = argparse.ArgumentParser(description='紙上交易追蹤與成效統計')
    p.add_argument('--file', default='signals.jsonl', help='訊號記錄檔')
    p.add_argument('--telegram', action='store_true', help='把統計推播出去')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])

    rows = load(opt.file)
    if not rows:
        print('尚無訊號記錄')
        return 0

    now_ms = int(datetime.now(TPE).timestamp() * 1000)
    filled = fill(rows, now_ms)
    if filled:
        save(opt.file, rows)
    print(f'補算 {filled} 個報酬值')

    text = report(rows)
    print(text)

    if opt.telegram:
        send_telegram(build_telegram(text, rows))
    return 0


if __name__ == '__main__':
    sys.exit(main())
