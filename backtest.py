"""歷史回測:用過去的 K 線檢驗篩選條件到底賺不賺錢

紙上交易要等兩週才有樣本,回測幾分鐘就能跑完過去幾個月。做法是把時間
倒回去,對每一根歷史 K 棒重算一次當下的指標,套用同一組篩選條件,再看
往後 4/12/24 小時的漲跌。

關鍵:篩選條件直接沿用 scan_bull 的 conds_*,不另外抄一份,否則回測驗證
的就不是實際在跑的東西。指標則改為一次算完整條序列(live 版只算最後一
根),兩者的一致性由 test_backtest.py 驗證。

CLI 用法:
  python backtest.py --mode breakout            # 回測 breakout,預設 60 檔
  python backtest.py --mode early --top 100     # 取成交額前 100 檔
  python backtest.py --mode breakout --bars 2000  # 每檔取更長的歷史
  python backtest.py --sweep volr 1.5 2 3 4     # 掃描量比門檻的影響
  python backtest.py --mode breakout --telegram # 結果推播

限制:
- 進出場都用收盤價,無停損、無滑價;手續費以固定 0.1% 估算後另列一欄
- 只納入目前仍在交易的幣種,已下架的看不到(倖存者偏差,結果偏樂觀)
- 24h 漲幅在 live 版取自即時報價,回測改用 24 根 K 棒前的收盤價,兩者
  在跨日時會有些微差異
"""
import argparse
import json
import statistics
import sys
import time
from collections import defaultdict

import scan_bull as sb

HORIZONS = (4, 12, 24)
FEE_PCT = 0.1
HISTORY = ('https://www.okx.com/api/v5/market/history-candles'
           '?instId={inst}&bar={bar}&limit=100{after}')


# ───────────────────────── 取歷史 K 線 ─────────────────────────

def history(inst, bar, bars):
    """往回翻頁抓 K 線,回傳舊到新的 list;不足或失敗回 None"""
    rows, after = [], ''
    while len(rows) < bars:
        r = sb.get(HISTORY.format(inst=inst, bar=bar, after=after))
        if not r or r.get('code') != '0' or not r.get('data'):
            break
        page = r['data']                      # 新到舊
        rows.extend(page)
        after = f'&after={page[-1][0]}'       # 下一頁要更舊的
        if len(page) < 100:
            break
        time.sleep(0.1)                       # 放慢一點,避免被限流
    if len(rows) < 200:
        return None
    return list(reversed(rows))               # 轉成舊到新


# ───────────────────────── 指標(整條序列) ─────────────────────────

def rolling_mean(xs, n):
    """回傳與 xs 等長的移動平均,前 n-1 個為 None"""
    out, run = [None] * len(xs), 0.0
    for i, x in enumerate(xs):
        run += x
        if i >= n:
            run -= xs[i - n]
        if i >= n - 1:
            out[i] = run / n
    return out


def rolling_std(xs, n):
    out, s, s2 = [None] * len(xs), 0.0, 0.0
    for i, x in enumerate(xs):
        s += x
        s2 += x * x
        if i >= n:
            s -= xs[i - n]
            s2 -= xs[i - n] ** 2
        if i >= n - 1:
            var = max(s2 / n - (s / n) ** 2, 0.0)
            out[i] = var ** 0.5
    return out


def rsi_series(c, n=14):
    """Wilder RSI,與 scan_bull.rsi 同一套平滑方式"""
    out = [None] * len(c)
    if len(c) <= n:
        return out
    gains = sum(max(c[i] - c[i - 1], 0) for i in range(1, n + 1))
    losses = sum(max(c[i - 1] - c[i], 0) for i in range(1, n + 1))
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    for i in range(n + 1, len(c)):
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
        out[i] = 100.0 if al == 0 else 100 - 100 / (1 + ag / al)
    return out


def ema_series(xs, n):
    k = 2 / (n + 1)
    out = [xs[0]]
    for x in xs[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def macd_series(c):
    if len(c) < 35:
        return [None] * len(c)
    f, s = ema_series(c, 12), ema_series(c, 26)
    dif = [a - b for a, b in zip(f, s)]
    dea = ema_series(dif, 9)
    return [d - e for d, e in zip(dif, dea)]


def bbw_series(c, n=20):
    """布林帶寬 (上-下)/中 = 4σ/中軌,與 scan_bull.bb_width 同定義"""
    mid = rolling_mean(c, n)
    sd = rolling_std(c, n)
    return [None if m in (None, 0) or s is None else 4 * s / m
            for m, s in zip(mid, sd)]


def bbw_pct_series(bbw, look=90):
    """帶寬在近 look 根中的百分位;live 版排除最新那根,這裡對齊為看前一根"""
    out = [None] * len(bbw)
    for i in range(len(bbw)):
        j = i - 1                              # 排除當根(突破本身會撐開帶寬)
        if j < look - 1 or bbw[j] is None:
            continue
        win = [x for x in bbw[j - look + 1:j + 1] if x is not None]
        if len(win) < look // 2:
            continue
        out[i] = sum(1 for x in win if x <= bbw[j]) / len(win)
    return out


def rolling_max(xs, n, offset=1):
    """前 n 根的最大值(不含當根);offset=1 代表看 i-n..i-1"""
    out = [None] * len(xs)
    for i in range(len(xs)):
        lo = i - n - offset + 1
        if lo < 0:
            continue
        out[i] = max(xs[lo:i - offset + 1])
    return out


def rolling_min(xs, n, offset=1):
    out = [None] * len(xs)
    for i in range(len(xs)):
        lo = i - n - offset + 1
        if lo < 0:
            continue
        out[i] = min(xs[lo:i - offset + 1])
    return out


def series(sym, rows):
    """把 K 線轉成每根一個 dict,欄位與 scan_bull.measure 的輸出一致"""
    ts = [int(k[0]) for k in rows]
    c = [float(k[4]) for k in rows]
    h = [float(k[2]) for k in rows]
    lo = [float(k[3]) for k in rows]
    v = [float(k[5]) for k in rows]
    # index 7 是以計價幣(USDT)計的成交額,拿來估 24h 成交額
    quote = [float(k[7]) if len(k) > 7 else 0.0 for k in rows]

    m20, m60, m120 = (rolling_mean(c, 20), rolling_mean(c, 60),
                      rolling_mean(c, 120))
    rsi = rsi_series(c)
    macd = macd_series(c)
    bbw = bbw_series(c)
    bbwp = bbw_pct_series(bbw)
    hh24, hh48 = rolling_max(h, 24), rolling_max(h, 48)
    box_hi, box_lo = rolling_max(h, 24), rolling_min(lo, 24)
    vbase = rolling_mean(v, 20)                # 前 20 根均量:取 i-1 的值

    out = []
    for i in range(len(rows)):
        if i < 121 or m120[i] is None or vbase[i - 1] in (None, 0):
            out.append(None)
            continue
        turn24 = sum(quote[max(0, i - 23):i + 1])
        out.append(dict(
            sym=sym, ts=ts[i], last=c[i],
            bull=c[i] > m20[i] > m60[i] > m120[i],
            bear=c[i] < m20[i] < m60[i] < m120[i],
            volr=v[i] / vbase[i - 1],
            turn=turn24,
            ch1=(c[i] / c[i - 1] - 1) * 100,
            ch4=(c[i] / c[i - 4] - 1) * 100,
            ch12=(c[i] / c[i - 12] - 1) * 100,
            ch24=(c[i] / c[i - 24] - 1) * 100,
            dist20=(c[i] / m20[i] - 1) * 100,
            rsi=rsi[i], macd=macd[i],
            ma20_up=bool(m20[i - 1] and m20[i] > m20[i - 1]),
            above_mid=c[i] > m60[i] and c[i] > m120[i],
            ma_cross=bool(m20[i - 20] and m60[i - 20]
                          and m20[i] > m60[i] and m20[i - 20] <= m60[i - 20]),
            dist_hi24=(c[i] / hh24[i] - 1) * 100 if hh24[i] else 0.0,
            dist_hi48=(c[i] / hh48[i] - 1) * 100 if hh48[i] else 0.0,
            box_amp=(box_hi[i] / box_lo[i] - 1) * 100 if box_lo[i] else None,
            bbw_pct=bbwp[i],
        ))
    return out, c


# ───────────────────────── 回測 ─────────────────────────

def run_one(sym, rows, opt):
    """回傳這一檔的所有命中訊號(含事後報酬)"""
    bars, c = series(sym, rows)
    conds = sb.COND_SETS[opt.mode](opt) if opt.mode != 'classic' else None
    hits = []
    for i, x in enumerate(bars):
        if x is None:
            continue
        if conds is not None:
            ok = all(fn(x) for _, fn in conds)
        else:                                   # classic 用原本的組合條件
            ok = (x['bull'] and x['volr'] > opt.volr
                  and (abs(x['ch1']) > opt.ch1 or abs(x['ch4']) > opt.ch4)
                  and x['turn'] >= opt.turn)
        if not ok:
            continue
        if opt.max_dist is not None and abs(x['dist20']) >= opt.max_dist:
            continue
        rec = {'sym': sym, 'ts': x['ts'], 'volr': x['volr'],
               'dist20': x['dist20'], 'entry': c[i]}
        for hz in HORIZONS:                     # 事後報酬,不足長度就略過
            if i + hz < len(c):
                rec[f'ret{hz}'] = (c[i + hz] / c[i] - 1) * 100
        hits.append(rec)
    return hits


def summarize(hits, title):
    lines = [title]
    if not hits:
        return lines + ['  沒有任何訊號']
    for hz in HORIZONS:
        vals = [h[f'ret{hz}'] for h in hits if f'ret{hz}' in h]
        if not vals:
            continue
        win = 100 * sum(1 for v in vals if v > 0) / len(vals)
        lines.append(
            f'  {hz:2}h  {len(vals):5} 筆  平均 {statistics.mean(vals):+6.2f}%  '
            f'中位 {statistics.median(vals):+6.2f}%  勝率 {win:5.1f}%  '
            f'扣成本後 {statistics.mean(vals) - FEE_PCT:+6.2f}%')
    return lines


def by_bucket(hits):
    groups = defaultdict(list)
    for h in hits:
        if 'ret24' not in h:
            continue
        v = h['volr']
        k = ('量比 ≥10' if v >= 10 else '量比 5-10' if v >= 5
             else '量比 3-5' if v >= 3 else '量比 2-3')
        groups[k].append(h['ret24'])
    lines = ['\n依量比分組(24h):']
    for k in sorted(groups, key=lambda k: -len(groups[k])):
        vals = groups[k]
        win = 100 * sum(1 for v in vals if v > 0) / len(vals)
        lines.append(f'  {k:10} {len(vals):5} 筆  '
                     f'平均 {statistics.mean(vals):+6.2f}%  勝率 {win:5.1f}%')
    return lines


def main(argv=None):
    p = argparse.ArgumentParser(description='歷史回測')
    p.add_argument('--mode', default='breakout', choices=sorted(sb.MODES))
    p.add_argument('--bar', default='1H')
    p.add_argument('--top', type=int, default=60, help='取成交額前幾檔,預設 60')
    p.add_argument('--bars', type=int, default=1000, help='每檔取幾根 K 棒')
    p.add_argument('--turn', type=float, default=None)
    p.add_argument('--volr', type=float, default=None)
    p.add_argument('--box-amp', type=float, default=20.0)
    p.add_argument('--bbw-pct', type=float, default=0.35)
    p.add_argument('--ch1', type=float, default=sb.DEF_CH1)
    p.add_argument('--ch4', type=float, default=sb.DEF_CH4)
    p.add_argument('--max-dist', type=float, default=None)
    p.add_argument('--short', action='store_true')
    p.add_argument('--sweep', nargs='+', default=None,
                   help='掃描某個門檻,例如:--sweep volr 1.5 2 3 4')
    p.add_argument('--telegram', action='store_true')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])
    for k, val in sb.MODE_DEFAULTS[opt.mode].items():
        if getattr(opt, k) is None:
            setattr(opt, k, val)

    items = sb.candidates(opt.turn)
    items.sort(key=lambda x: -x[3])
    items = items[:opt.top]
    print(f'回測 {len(items)} 檔,每檔 {opt.bars} 根 {opt.bar} K 棒')

    data = {}
    for n, it in enumerate(items, 1):
        rows = history(it[0], opt.bar, opt.bars)
        if rows:
            data[it[0].replace('-USDT-SWAP', '')] = rows
        if n % 10 == 0:
            print(f'  已取得 {len(data)}/{n}')
    print(f'成功取得 {len(data)} 檔的歷史資料')

    def evaluate(o):
        out = []
        for sym, rows in data.items():
            out.extend(run_one(sym, rows, o))
        return out

    report = []
    if opt.sweep:
        field, values = opt.sweep[0], [float(v) for v in opt.sweep[1:]]
        report.append(f'掃描 {field}:{values}(模式 {opt.mode})')
        for v in values:
            setattr(opt, field.replace('-', '_'), v)
            hits = evaluate(opt)
            report += summarize(hits, f'\n{field}={v:g}')
    else:
        hits = evaluate(opt)
        span = ''
        if hits:
            lo = min(h['ts'] for h in hits) / 1000
            hi = max(h['ts'] for h in hits) / 1000
            span = (f"  期間 {time.strftime('%Y-%m-%d', time.gmtime(lo))}"
                    f" ~ {time.strftime('%Y-%m-%d', time.gmtime(hi))}")
        report += summarize(hits, f'模式 {opt.mode}{span}')
        report += by_bucket(hits)

    text = '\n'.join(report)
    print('\n' + text)
    with open('backtest_result.json', 'w', encoding='utf-8') as f:
        json.dump({'mode': opt.mode, 'text': text}, f, ensure_ascii=False)
    if opt.telegram:
        sb.send_telegram(f'🧪 <b>歷史回測</b>\n<pre>{text}</pre>')
    return 0


if __name__ == '__main__':
    sys.exit(main())
