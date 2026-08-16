"""持倉監控:對已發出的訊號套用出場規則,該走就推播通知

scan_bull 只在進場時通知一次,之後就不管了。這支負責另一半:把 signals.jsonl
裡還沒出場的訊號當成持倉,每小時拿新的 K 線回來逐根套用 exits.py 的規則,
碰到停損或訊號失效就發 Telegram,並把出場結果寫回 signals.jsonl。

出場規則刻意不寫在這裡,而是 import exits —— 與 backtest.py 用的是同一份,
否則回測驗證的就不是實際會通知你的那套。

CLI 用法:
  python positions.py                # 檢查並推播出場通知
  python positions.py --dry          # 只印螢幕,不發 Telegram
  python positions.py --summary      # 順便推播目前持倉概況
  python positions.py --stop-atr 2   # 用不同的停損寬度(要與回測結論一致)

只處理有 stop 欄位的訊號。這個欄位是加上出場規則之後才開始記的,更早的
舊訊號沒有進場當下的 ATR 與箱頂,事後補算會用到未來資訊,所以直接跳過。
"""
import argparse
import json
import os
import sys
from datetime import datetime

import backtest as bt
import exits as ex
from scan_bull import OKX_CANDLES, TPE, get, send_telegram

MAX_AGE_BARS = 200          # 超過 K 線可查範圍的舊單,補不回來就標記結案


def load(path):
    rows = []
    if not os.path.exists(path):
        return rows
    with open(path, encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except ValueError:
                    continue
    return rows


def save(path, rows):
    with open(path, 'w', encoding='utf-8') as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + '\n')


def open_rows(rows):
    """還在場內的單:有停損價、且還沒記錄出場"""
    return [r for r in rows if r.get('stop') and not r.get('exit_reason')]


def bars_for(sym):
    """抓近 200 根 1H K 線,轉成與回測同一套欄位的 bar 序列

    直接用 backtest.series,指標的算法就不會有第二份。最後一根未收盤,
    跟 scan_bull.measure 一樣丟掉,否則會用還在跳動的價格判定出場。
    """
    r = get(OKX_CANDLES.format(inst=f'{sym}-USDT-SWAP', bar='1H'))
    if not r or r.get('code') != '0' or len(r.get('data', [])) < 130:
        return None
    rows = list(reversed(r['data']))[:-1]
    bars, _ = bt.series(sym, rows)
    return [b for b in bars if b]


def position_of(row):
    """把 signals.jsonl 的一列還原成 exits 認得的持倉狀態

    停損價與箱頂用記錄下來的值,不重算 —— 它們屬於進場當下的決定。

    分批、峰值、被抬高的停損這些過程狀態不從檔案還原,因為每輪都是從
    進場那根重新播一次,狀態自然會重建;存下來反而多一份會不同步的真相。
    """
    return {
        'sym': row['sym'], 'entry_ts': row['ts'], 'entry': row['entry'],
        'stop': row['stop'], 'box_hi': row.get('box_hi'),
        'atr': row.get('atr'), 'hot': bool(row.get('hot')),
        'bars': 0,
        'stop_kind': 'init', 'peak': row['entry'],
        'left': 1.0, 'partials': [],
    }


def check(rows, cfg):
    """逐檔重放 K 線,回傳 (剛出場的清單, 仍在場內的清單)

    每次都從進場那根重放到最新一根,而不是只看最新一根:排程有可能漏跑
    或延遲,只看最新一根會漏掉中間已經觸發的停損,把虧損拖大。
    """
    todo = open_rows(rows)
    closed, holding, scaled = [], [], []
    by_sym = {}
    for r in todo:
        by_sym.setdefault(r['sym'], []).append(r)

    for sym, items in by_sym.items():
        bars = bars_for(sym)
        if not bars:
            continue
        oldest = bars[0]['ts']
        for r in items:
            if r['ts'] < oldest:
                # 進場點已超出可查範圍,無法重放;標記結案免得每輪重試
                r['exit_reason'] = '資料過期'
                r['exit_ret'] = None
                closed.append(r)
                continue
            pos = position_of(r)
            pos['bars'] = 0
            hit = None
            for b in bars:
                if b['ts'] <= r['ts']:
                    continue
                hit = ex.step(pos, b, cfg)
                if hit:
                    reason, px = hit
                    r['exit_reason'] = reason
                    r['exit_ts'] = b['ts']
                    r['exit_time'] = datetime.fromtimestamp(
                        b['ts'] / 1000, TPE).isoformat()
                    r['exit_px'] = px
                    # 報酬要含分批出掉的那半,不能只看最後一筆的價格
                    r['exit_ret'] = round(ex.total_ret(pos, px), 2)
                    r['held'] = pos['bars']
                    if pos['partials']:
                        r['tp1_px'] = pos['partials'][0][1]
                    closed.append(r)
                    break

            if not hit:
                # 第一目標是分批出場,單子還在場內,但你得知道要動手減碼。
                # tp1_px 同時當作「已通知過」的標記,否則每輪都會再喊一次。
                if pos['partials'] and not r.get('tp1_px'):
                    _, tp_px, frac = pos['partials'][0]
                    r['tp1_px'] = tp_px
                    r['tp1_frac'] = frac
                    scaled.append(dict(r, new_stop=pos['stop']))

                # 在場內的單只做展示,不把現價寫回記錄檔(下一輪就過期了)
                last = bars[-1]['last']
                holding.append(dict(r, held=pos['bars'], now=last,
                                    stop=pos['stop'],   # 可能已被保本/移動停利抬高
                                    stop_kind=pos['stop_kind'],
                                    open_ret=round((last / r['entry'] - 1) * 100, 2)))
    return closed, holding, scaled


ICON = {ex.STOP: '🛑', ex.BE: '🟰', ex.TRAIL: '🏃', ex.TP1: '🎯',
        ex.BOX: '🔻', ex.MA20: '🔻', ex.RSI: '🔻', ex.TIME: '⏱'}


def build_message(closed, holding, scaled, show_summary):
    now = datetime.now(TPE).strftime('%Y-%m-%d %H:%M')
    head = f'本輪 <b>{len(closed)}</b> 筆出場'
    if scaled:
        head += f'、<b>{len(scaled)}</b> 筆到第一目標'
    lines = [f'🚪 <b>出場通知</b>\n{now} (台北)  {head}\n']

    # 分批減碼放前面:這是要你現在動手的事,已出場的只是回報
    for r in scaled:
        lines.append(
            f"\n🎯 <b>{r['sym']}</b> ({r['mode']}) 到第一目標 "
            f"{r['tp1_px']:.6g}(+{(r['tp1_px']/r['entry']-1)*100:.2f}%)\n"
            f"建議出 {r['tp1_frac']:.0%},剩下的停損拉到 "
            f"<b>{r['new_stop']:.6g}</b>,之後跟著最高價往上移\n")

    for r in closed:
        if r['exit_reason'] == '資料過期':
            continue
        ret = r['exit_ret']
        mark = '✅' if ret > 0 else '❌'
        lines.append(
            f"\n{ICON.get(r['exit_reason'], '•')} <b>{r['sym']}</b> "
            f"({r['mode']})  {mark} <b>{ret:+.2f}%</b>\n"
            f"進 {r['entry']:.6g} → 出 {r['exit_px']:.6g}  "
            f"持有 {r['held']} 根\n"
            f"原因:<i>{r['exit_reason']}</i>\n")

    if show_summary and holding:
        lines.append(f'\n<b>仍在場內 {len(holding)} 筆</b>')
        for r in sorted(holding, key=lambda x: -(x['open_ret'])):
            kind = {'breakeven': '(保本)', 'trail': '(移動)'}.get(
                r.get('stop_kind'), '')
            lines.append(
                f"\n• {r['sym']} {r['open_ret']:+.2f}%  "
                f"現價 {r['now']:.6g} ｜ 停損 {r['stop']:.6g}{kind} ｜ "
                f"{r['held']} 根")
        lines.append('\n')

    lines.append('\n<i>出場價為 K 棒收盤或停損價,未計滑價與手續費;'
                 '實際成交以你交易所為準。</i>')
    return ''.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description='持倉監控與出場推播')
    p.add_argument('--file', default='signals.jsonl', help='訊號記錄檔')
    p.add_argument('--stop-atr', type=float, default=ex.DEF_STOP_ATR)
    p.add_argument('--max-bars', type=int, default=ex.DEF_MAX_BARS)
    p.add_argument('--no-invalidate', action='store_true',
                   help='關掉訊號失效出場,只留停損與時間出場')
    p.add_argument('--tp1-r', type=float, default=ex.DEF_TP1_R)
    p.add_argument('--tp1-frac', type=float, default=ex.DEF_TP1_FRAC)
    p.add_argument('--trail-atr', type=float, default=ex.DEF_TRAIL_ATR)
    p.add_argument('--no-tp', action='store_true', help='關掉停利與移動停利')
    p.add_argument('--summary', action='store_true', help='推播時附上持倉概況')
    p.add_argument('--dry', action='store_true', help='只印螢幕,不發 Telegram')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])

    rows = load(opt.file)
    if not rows:
        print('尚無訊號記錄')
        return 0

    cfg = ex.cfg_from(opt)
    closed, holding, scaled = check(rows, cfg)
    if closed or scaled:
        save(opt.file, rows)

    print(f'在場內 {len(holding)} 筆,本輪出場 {len(closed)} 筆,'
          f'到第一目標 {len(scaled)} 筆')
    for r in closed:
        print(f"  {r['sym']:10} {r['exit_reason']:6} {r.get('exit_ret')}%")
    for r in scaled:
        print(f"  {r['sym']:10} 第一目標 {r['tp1_px']:.6g} → 減碼 {r['tp1_frac']:.0%}")
    for r in holding:
        print(f"  {r['sym']:10} 持有中 {r['open_ret']:+.2f}%  {r['held']} 根")

    # 沒有出場也沒有減碼就不吵你;--summary 時仍會報一次持倉概況
    fresh = [r for r in closed if r['exit_reason'] != '資料過期']
    if not opt.dry and (fresh or scaled or (opt.summary and holding)):
        send_telegram(build_message(fresh, holding, scaled, opt.summary))
    return 0


if __name__ == '__main__':
    sys.exit(main())
