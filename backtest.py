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
  python backtest.py --sweep stop_atr 1 1.5 2 3 # 掃描停損寬度
  python backtest.py --no-invalidate            # 只留停損,看失效出場有沒有加分
  python backtest.py --mode breakout --telegram # 結果推播

出場規則沿用 exits.py(停損 + 訊號失效 + 時間出場),與實盤 positions.py
是同一份程式碼。報表會同時列出「固定時間出場」與「帶停損出場」兩組數字,
前者是舊口徑,留著當對照。

限制:
- 無滑價;手續費以固定 0.1% 估算後另列一欄
- 停損以棒內最低價判定,但 1H K 棒內的走勢看不到,實際可能更差
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

import exits as ex
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


def atr_series(h, lo, c, n=14):
    """Wilder ATR 的整條序列,與 scan_bull.atr 同一套平滑方式"""
    out = [None] * len(c)
    if len(c) <= n:
        return out
    tr = [0.0] + [max(h[i] - lo[i], abs(h[i] - c[i - 1]), abs(lo[i] - c[i - 1]))
                  for i in range(1, len(c))]
    a = sum(tr[1:n + 1]) / n
    out[n] = a
    for i in range(n + 1, len(c)):
        a = (a * (n - 1) + tr[i]) / n
        out[i] = a
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
    atr = atr_series(h, lo, c)

    out = []
    for i in range(len(rows)):
        if i < 121 or m120[i] is None or vbase[i - 1] in (None, 0):
            out.append(None)
            continue
        turn24 = sum(quote[max(0, i - 23):i + 1])
        out.append(dict(
            sym=sym, ts=ts[i], last=c[i],
            high=h[i], low=lo[i],
            atr=atr[i],
            ma20=m20[i], ma20_prev=m20[i - 1],
            box_hi=box_hi[i], box_lo=box_lo[i],
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

def simulate(bars, i, cfg):
    """從第 i 根進場,逐根套用 exits 的規則,回傳出場結果

    逐根走而不是直接取 N 小時後的收盤價,是因為停損會在中途觸發:先跌到
    停損再拉回來的那些單,固定時間出場會記成賺錢,實際上早就被掃出場了。
    走到資料尾端還沒出場的回 None(尚未結束的單不該計入統計)。
    """
    pos = ex.open_position(bars[i], cfg)
    # 1R 要在進場當下記下來:停損之後會被保本與移動停利往上推,
    # 事後再算 risk_pct 得到的是被推高後的距離,不是這筆單的風險
    risk = ex.risk_pct(pos)
    for j in range(i + 1, len(bars)):
        b = bars[j]
        if b is None:
            continue
        hit = ex.step(pos, b, cfg)
        if hit:
            reason, px = hit
            # 報酬要把分批出掉的那半算進去,不能只看最後一筆的價格
            return dict(exit_reason=reason, exit_ts=b['ts'],
                        exit_ret=ex.total_ret(pos, px),
                        scaled=bool(pos['partials']),
                        held=pos['bars'], risk=risk)
    return None


def run_one(sym, rows, opt):
    """回傳這一檔的所有命中訊號(含事後報酬)"""
    bars, c = series(sym, rows)
    cfg = ex.cfg_from(opt)
    conds = sb.COND_SETS[opt.mode](opt) if opt.mode != 'classic' else None
    hits = []
    for i, x in enumerate(bars):
        if x is None:
            continue
        # live 版在第一層就用成交額篩掉候選,回測用當下的滾動 24h 成交額
        # 對應,否則等於少了一道門檻
        if x['turn'] < opt.turn:
            continue
        if conds is not None:
            ok = all(fn(x) for _, fn in conds)
        else:                                   # classic 用原本的組合條件
            ok = (x['bull'] and x['volr'] > opt.volr
                  and (abs(x['ch1']) > opt.ch1 or abs(x['ch4']) > opt.ch4))
        if not ok:
            continue
        if opt.max_dist is not None and abs(x['dist20']) >= opt.max_dist:
            continue
        # 停損距離太窄的單直接不出訊號:ATR 趨近於零時(波動死掉、或成交
        # 稀疏的代幣化股票在美股收盤後)停損會貼在進場價上,一個跳動就被
        # 掃掉,而手續費來回就 0.2%,這種單的期望值必為負
        if opt.min_risk:
            r = ex.risk_pct(ex.open_position(x, cfg))
            if r is None or r < opt.min_risk:
                continue
        rec = {'sym': sym, 'ts': x['ts'], 'volr': x['volr'],
               'dist20': x['dist20'], 'entry': c[i]}
        for hz in HORIZONS:                     # 事後報酬,不足長度就略過
            if i + hz < len(c):
                rec[f'ret{hz}'] = (c[i + hz] / c[i] - 1) * 100
        # 固定時間報酬保留下來當對照組:有停損之後差多少,一眼看得出來
        got = simulate(bars, i, cfg)
        if got:
            rec.update(got)
        hits.append(rec)
    return hits


MIN_N = 20        # 少於這個筆數的分組不足以下結論,標記出來


def line_for(vals, label):
    """一列統計;樣本太少會標註,避免把雜訊當結論"""
    if not vals:
        return f'  {label}  無資料'
    win = 100 * sum(1 for v in vals if v > 0) / len(vals)
    warn = '  ⚠樣本不足' if len(vals) < MIN_N else ''
    return (f'  {label}  {len(vals):5} 筆  平均 {statistics.mean(vals):+6.2f}%  '
            f'中位 {statistics.median(vals):+6.2f}%  勝率 {win:5.1f}%  '
            f'扣成本後 {statistics.mean(vals) - FEE_PCT:+6.2f}%{warn}')


def summarize(hits, title):
    lines = [title]
    if not hits:
        return lines + ['  沒有任何訊號']
    lines.append('  ── 固定時間出場(無停損,舊版口徑)──')
    for hz in HORIZONS:
        vals = [h[f'ret{hz}'] for h in hits if f'ret{hz}' in h]
        if vals:
            lines.append(line_for(vals, f'{hz:2}h'))
    lines += with_exits(hits)
    return lines


def with_exits(hits):
    """帶停損與失效出場的績效,以及是被哪一種規則請出場的

    出場原因的分布比報酬還重要:停損佔比過高代表停損太緊、時間出場佔比
    過高代表規則根本沒在作用,兩種都要調參數而不是調心情。
    """
    done = [h for h in hits if 'exit_ret' in h]
    if not done:
        return ['  ── 帶停損出場:沒有已結束的單 ──']
    vals = [h['exit_ret'] for h in done]
    lines = ['  ── 帶停損 + 停利 + 失效出場 ──', line_for(vals, '實際')]

    risks = [h['risk'] for h in done if h.get('risk')]
    held = [h['held'] for h in done]
    if risks:
        # 期望值以 R 計價,才不會被幾檔高波動幣的大百分比帶著跑
        rs = [h['exit_ret'] / h['risk'] for h in done if h.get('risk')]
        lines.append(f"  平均停損距離 {statistics.mean(risks):.2f}%  "
                     f"平均期望值 {statistics.mean(rs):+.2f}R  "
                     f"平均持有 {statistics.mean(held):.1f} 根")
    scaled = [h for h in done if h.get('scaled')]
    if scaled:
        # 到得了第一目標的比例,決定停利有沒有在作用
        lines.append(f"  觸及第一目標 {len(scaled)} 筆 "
                     f"({100 * len(scaled) / len(done):.1f}%)  "
                     f"平均 {statistics.mean([h['exit_ret'] for h in scaled]):+.2f}%")

    counts = defaultdict(list)
    for h in done:
        counts[h['exit_reason']].append(h['exit_ret'])
    lines.append('  出場原因:')
    for r in ex.REASONS:
        if r in counts:
            v = counts[r]
            share = 100 * len(v) / len(done)
            lines.append(f'    {r:6} {len(v):5} 筆 ({share:4.1f}%)  '
                         f'平均 {statistics.mean(v):+6.2f}%')
    return lines


def by_period(hits, parts=3):
    """把期間切成幾段分別看:只有某一段有效的話,代表撐不起結論"""
    done = sorted((h for h in hits if 'ret24' in h), key=lambda h: h['ts'])
    if len(done) < parts * 5:
        return ['\n分期間(24h):樣本太少,不切分']
    size = len(done) // parts
    lines = ['\n分期間(24h):']
    for i in range(parts):
        seg = done[i * size:(i + 1) * size] if i < parts - 1 else done[i * size:]
        lo = time.strftime('%m-%d', time.gmtime(seg[0]['ts'] / 1000))
        hi = time.strftime('%m-%d', time.gmtime(seg[-1]['ts'] / 1000))
        lines.append(line_for([h['ret24'] for h in seg], f'{lo}~{hi}'))
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
        lines.append(line_for(groups[k], f'{k:10}'))
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
    p.add_argument('--min-risk', type=float, default=ex.DEF_MIN_RISK,
                   help='最小停損距離(%%),低於此值的訊號直接不出;0=不設限。'
                        '預設跟 live 掃描同一個值,回測的才是實際會發的訊號')
    p.add_argument('--short', action='store_true')
    p.add_argument('--stop-atr', type=float, default=ex.DEF_STOP_ATR,
                   help=f'停損距離 = 進場價 - N×ATR(14),預設 {ex.DEF_STOP_ATR}')
    p.add_argument('--max-bars', type=int, default=ex.DEF_MAX_BARS,
                   help=f'時間出場:抱滿幾根 K 棒,預設 {ex.DEF_MAX_BARS}')
    p.add_argument('--no-invalidate', action='store_true',
                   help='只留停損與時間出場,關掉訊號失效出場(用來看它有沒有加分)')
    p.add_argument('--tp1-r', type=float, default=ex.DEF_TP1_R,
                   help=f'第一目標:獲利達 N 個 R 出一部分,預設 {ex.DEF_TP1_R}')
    p.add_argument('--tp1-frac', type=float, default=ex.DEF_TP1_FRAC,
                   help=f'到第一目標時出掉的比例,預設 {ex.DEF_TP1_FRAC}')
    p.add_argument('--trail-atr', type=float, default=ex.DEF_TRAIL_ATR,
                   help=f'移動停利:最高價 - N×ATR,預設 {ex.DEF_TRAIL_ATR};0 = 關閉')
    p.add_argument('--no-tp', action='store_true',
                   help='關掉停利與移動停利,只留停損(用來看停利有沒有加分)')
    p.add_argument('--sweep', nargs='+', default=None,
                   help='掃描某個門檻,例如:--sweep volr 1.5 2 3 4')
    p.add_argument('--full', action='store_true',
                   help='一次跑完:三種模式比較 + 量比掃描 + 分期間檢驗')
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

    def with_mode(mode):
        """複製一份設定並套用該模式的預設門檻"""
        o = argparse.Namespace(**vars(opt))
        o.mode = mode
        for k, val in sb.MODE_DEFAULTS[mode].items():
            setattr(o, k, val)
        return o

    report = []
    if opt.full:
        report.append(f'資料:{len(data)} 檔 × 最多 {opt.bars} 根 {opt.bar}')
        best = {}
        for mode in ('breakout', 'early', 'chase'):
            o = with_mode(mode)
            hits = evaluate(o)
            report += summarize(hits, f'\n【模式 {mode}】'
                                      f'(成交額≥{o.turn/1e6:g}M 量比>{o.volr:g})')
            report += by_period(hits)
            best[mode] = hits
        report.append('\n【量比門檻掃描:breakout,只看 24h】')
        o = with_mode('breakout')
        for v in (2, 3, 5, 8):
            o.volr = v
            hits = evaluate(o)
            vals = [h['ret24'] for h in hits if 'ret24' in h]
            report.append(line_for(vals, f'量比>{v}'))
        report.append('\n【停損寬度掃描:breakout,實際出場口徑】')
        o = with_mode('breakout')
        for k in (1.0, 1.5, 2.0, 3.0):
            o.stop_atr = k
            vals = [h['exit_ret'] for h in evaluate(o) if 'exit_ret' in h]
            report.append(line_for(vals, f'{k:g}×ATR'))

        # 停利到底有沒有加分,要跟「只有停損」的版本比才知道
        report.append('\n【停利設定比較:breakout】')
        for label, kw in (('關掉停利', {'no_tp': True}),
                          ('1R 出半 + 2ATR 跟', {}),
                          ('1R 出半 + 3ATR 跟', {'trail_atr': 3.0}),
                          ('1.5R 出半 + 2ATR 跟', {'tp1_r': 1.5}),
                          ('1R 全出', {'tp1_frac': 1.0})):
            o = with_mode('breakout')
            o.no_tp = False
            for k, v in kw.items():
                setattr(o, k, v)
            vals = [h['exit_ret'] for h in evaluate(o) if 'exit_ret' in h]
            report.append(line_for(vals, f'{label:16}'))
        report += by_bucket(best['breakout'])
        report.append('\n⚠️ 無滑價、看不到已下架的幣,結果偏樂觀;'
                      '樣本<20 的分組不足以下結論。')
    elif opt.sweep:
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
