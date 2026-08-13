"""驗證回測算出的指標與實際在跑的 scan_bull 一致

回測若和 live 版算得不一樣,驗證的就不是實際會發出的訊號。這支測試用
同一組 K 線餵給兩邊,比對最後一根的每個指標。

用法:python test_backtest.py
"""
import random
import sys

import backtest as bt
import scan_bull as sb

# live 版的 measure() 會丟掉最後一根(未收盤),回測沒有這個概念,
# 所以比對時餵給 measure 的資料要多一根當作「未收盤」。
TOL = 1e-6


def make_rows(n=300, seed=1):
    """做一段有漲有跌、量能不均的假 K 線(OKX 格式,新到舊)"""
    random.seed(seed)
    rows, price = [], 100.0
    for i in range(n):
        price *= 1 + random.uniform(-0.02, 0.022)
        high = price * (1 + random.uniform(0, 0.01))
        low = price * (1 - random.uniform(0, 0.01))
        vol = random.uniform(500, 3000) * (5 if i % 37 == 0 else 1)
        rows.append([str(1_700_000_000_000 + i * 3600_000), str(price),
                     str(high), str(low), str(price), str(vol),
                     str(vol), str(vol * price), '1'])
    return rows


def make_trend(n=400, seed=5):
    """做一段會觸發條件的資料:溫和上升 + 週期性回檔 + 不時放量

    隨機漫步幾乎不可能同時滿足多頭排列、量比、三個漲幅區間與 RSI,
    所以另外做一組帶趨勢的,確保「條件成立」那條路徑也被驗到。
    """
    random.seed(seed)
    rows, price = [], 50.0
    for i in range(n):
        phase = i % 20                         # 冷卻 → 起漲 → 放量突破
        spike = phase == 15
        if phase < 12:                         # 橫盤冷卻,把 RSI 與漲幅壓下來
            price *= 1 + random.uniform(-0.006, 0.005)
        elif phase < 15:
            price *= 1 + random.uniform(0.004, 0.012)
        elif spike:
            price *= 1.02                      # 放量帶價那根
        else:
            price *= 1 + random.uniform(-0.002, 0.006)
        vol = random.uniform(800, 1200) * (4.5 if spike else 1)
        high = price * (1 + random.uniform(0, 0.004))
        low = price * (1 - random.uniform(0, 0.004))
        rows.append([str(1_700_000_000_000 + i * 3600_000), str(price),
                     str(high), str(low), str(price), str(vol),
                     str(vol), str(vol * price), '1'])
    return rows


def compare(rows):
    """回傳 (live 結果, 回測最後一根結果)"""
    sb.get = lambda url, tries=4: {'code': '0', 'data': list(reversed(rows))}
    ch24 = (float(rows[-2][4]) / float(rows[-26][4]) - 1) * 100
    status, live = sb.measure(('X-USDT-SWAP', 1, ch24, 1e7), '1H')
    assert status == 'ok', status

    # measure 丟掉最後一根,所以回測要比對的是倒數第二根
    bars, _ = bt.series('X', rows)
    back = bars[len(rows) - 2]
    return live, back


def sweep_bars(rows, sample=25):
    """逐根比對:對多個時間點各跑一次 live measure,與回測同一根對照

    只比最後一根容易矇混過去(剛好都不通過)。抽樣多根、並統計條件成立
    的次數,才能確認兩邊在「會發訊號」的那些根上也一致。
    """
    bars, _ = bt.series('X', rows)
    idxs = range(150, len(rows) - 1, max(1, (len(rows) - 151) // sample))
    modes = ('early', 'breakout', 'chase')
    opts = {m: sb.parse_args(['--mode', m, '--dry']) for m in modes}
    mismatch, hits = 0, {m: 0 for m in modes}

    for i in idxs:
        sub = rows[:i + 2]                      # 多一根當作未收盤
        sb.get = lambda url, tries=4, s=sub: {'code': '0',
                                              'data': list(reversed(s))}
        # live 的 24h 漲幅來自即時報價,不是 K 線;測試要餵等價的值,
        # 否則比到的是測試設定的差異,不是程式的差異
        ch24 = (float(sub[i][4]) / float(sub[i - 24][4]) - 1) * 100
        status, live = sb.measure(('X-USDT-SWAP', 1, ch24, 1e7), '1H')
        if status != 'ok':
            continue
        back = bars[i]
        for f in ('volr', 'rsi', 'dist20', 'bbw_pct', 'box_amp', 'dist_hi48'):
            a, b = live.get(f), back.get(f)
            if a is None or b is None:
                if a is not b:
                    mismatch += 1
            elif abs(a - b) > 1e-6:
                mismatch += 1
                print(f'  第 {i} 根 {f}: live={a:.6g} 回測={b:.6g}')
        for m in modes:
            conds = sb.COND_SETS[m](opts[m])
            la = all(fn(live) for _, fn in conds)
            ba = all(fn(back) for _, fn in conds)
            if la != ba:
                mismatch += 1
                print(f'  第 {i} 根 {m} 判定不一致:live={la} 回測={ba}')
            if la:
                hits[m] += 1
    return mismatch, hits, len(list(idxs))


def main():
    rows = make_rows()
    live, back = compare(rows)

    fields = ['ts', 'last', 'bull', 'bear', 'volr', 'ch1', 'ch4', 'ch12', 'ch24',
              'dist20', 'rsi', 'ma20_up', 'above_mid', 'ma_cross',
              'dist_hi24', 'dist_hi48', 'box_amp', 'bbw_pct']
    bad = []
    print(f"{'欄位':12} {'live':>14} {'回測':>14}")
    for f in fields:
        a, b = live.get(f), back.get(f)
        if isinstance(a, bool) or isinstance(b, bool):
            same = a == b
            sa, sbv = str(a), str(b)
        elif a is None or b is None:
            same = a is b
            sa, sbv = str(a), str(b)
        else:
            same = abs(a - b) <= max(TOL, abs(a) * 1e-9)
            sa, sbv = f'{a:.6g}', f'{b:.6g}'
        print(f"{f:12} {sa:>14} {sbv:>14} {'' if same else '  ✗ 不一致'}")
        if not same:
            bad.append(f)

    # macd 只比較正負號(回測與 live 的 EMA 起點相同,值應該也相同)
    if (live['macd'] > 0) != (back['macd'] > 0):
        bad.append('macd 正負號')

    # 同一根 K 棒,兩邊套用同一組條件應得到相同判定
    for mode in ('early', 'breakout', 'chase'):
        opt = sb.parse_args(['--mode', mode, '--dry'])
        conds = sb.COND_SETS[mode](opt)
        la = all(fn(live) for _, fn in conds)
        ba = all(fn(back) for _, fn in conds)
        print(f'{mode:12} 條件判定 live={la} 回測={ba}'
              f"{'' if la == ba else '  ✗ 不一致'}")
        if la != ba:
            bad.append(f'{mode} 判定')

    # 逐根比對:換幾組隨機資料,確保有條件成立的案例也一致
    total_hits = {'early': 0, 'breakout': 0, 'chase': 0}
    checked = 0
    # 帶趨勢的資料逐根比(不抽樣),否則會漏掉稀疏的爆量根
    datasets = [(make_rows(400, s), 60) for s in (1, 2, 3)]
    datasets += [(make_trend(400, s), 400) for s in (5, 6, 7)]
    for rows_i, sample in datasets:
        mism, hits, n = sweep_bars(rows_i, sample=sample)
        checked += n
        for k, v in hits.items():
            total_hits[k] += v
        if mism:
            bad.append(f'逐根比對 {mism} 處不一致')

    print(f'\n逐根比對 {checked} 根,條件成立次數:{total_hits}')
    if sum(total_hits.values()) == 0:
        print('⚠️ 測試資料完全沒有觸發任何條件,判定一致性等於沒驗到')
        bad.append('無正向案例')

    if bad:
        print('\n不一致的欄位:', bad)
        return 1
    print('\n全部一致 ✓')
    return 0


if __name__ == '__main__':
    sys.exit(main())
