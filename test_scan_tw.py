"""驗證台股掃描的成本模型、欄位解析與篩選邏輯

外部資料(證交所 OpenAPI、yfinance)在測試裡不連線,改餵假資料:
會變的是他們的欄位名稱與可用性,不會變的是我們算成本、算指標、下判斷
的那段邏輯,這支測的就是那段。

用法:python test_scan_tw.py
"""
import math
import sys

import costs_tw as ct
import exits as ex
import scan_tw as st

FAIL = []


def check(cond, msg):
    if not cond:
        FAIL.append(msg)
        print(f'  ✗ {msg}')
    return cond


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


# ───────────────────────── 成本模型 ─────────────────────────

def test_costs():
    print('成本模型')
    # 1000 元買 100 元的股票 = 10 股,投入 1000 元
    check(ct.shares_for(100, 1000) == 10, 'shares_for: 1000/100 應為 10 股')
    check(ct.shares_for(300, 1000) == 3, 'shares_for: 無條件捨去')
    check(ct.shares_for(1200, 1000) == 0, 'shares_for: 買不起要回 0')
    check(ct.breakeven_pct(1200, 1000) is None, '買不起 1 股要回 None')

    # 低消 1 元:手續費 1000×0.001425×0.6 = 0.855 < 1,兩邊都吃低消
    # 來回 = 1 + 1 + 1000×0.003 = 5 元 → 0.5%
    be = ct.breakeven_pct(100, 1000, discount=0.6, fee_min=1.0)
    check(close(be, 0.5), f'低消 1 元的來回成本應為 0.5%,實際 {be}')

    # 低消 20 元:20 + 20 + 3 = 43 元 → 4.3%,這就是不能用一般券商的理由
    be20 = ct.breakeven_pct(100, 1000, discount=0.6, fee_min=20.0)
    check(close(be20, 4.3), f'低消 20 元的來回成本應為 4.3%,實際 {be20}')

    # 金額大到低消不再生效時,成本應趨近 0.1425%×0.6×2 + 0.3% = 0.471%
    be_big = ct.breakeven_pct(100, 1_000_000, discount=0.6, fee_min=1.0)
    check(close(be_big, 0.1425 * 0.6 * 2 + 0.3, 1e-9),
          f'大額時成本應為費率本身,實際 {be_big}')

    # 買不滿本金時要以「投入金額」計算,不是本金:
    # 1000 元買 300 元的股票只能買 3 股 = 900 元
    be_odd = ct.breakeven_pct(300, 1000, discount=0.6, fee_min=1.0)
    expect = (2 * max(900 * 0.001425 * 0.6, 1) + 900 * 0.003) / 900 * 100
    check(close(be_odd, expect), f'零頭應以投入金額計算,實際 {be_odd}')

    # ETF 證交稅只有 0.1%,成本應該更低
    check(ct.breakeven_pct(100, 1000, etf=True)
          < ct.breakeven_pct(100, 1000, etf=False), 'ETF 稅率應較低')


# ───────────────────────── OpenAPI 欄位解析 ─────────────────────────

TWSE_ROW = {'Code': '2330', 'Name': '台積電', 'TradeVolume': '30,000,000',
            'TradeValue': '29,000,000,000', 'OpeningPrice': '960',
            'HighestPrice': '975', 'LowestPrice': '958', 'ClosingPrice': '970',
            'Change': '+10', 'Transaction': '50,000'}

TPEX_ROW = {'SecuritiesCompanyCode': '6488', 'CompanyName': '環球晶',
            'Close': '450', 'High': '455', 'Low': '445',
            'TransactionAmount': '2,000,000,000'}


def test_parse():
    print('OpenAPI 欄位解析')
    tw = st.parse_rows([TWSE_ROW], 'TW')
    check(len(tw) == 1 and tw[0]['code'] == '2330', '證交所欄位應解析成功')
    check(close(tw[0]['close'], 970) and close(tw[0]['value'], 2.9e10),
          '帶逗號的數字要轉成 float')

    otc = st.parse_rows([TPEX_ROW], 'TWO')
    check(len(otc) == 1 and close(otc[0]['close'], 450),
          '櫃買的另一組欄位名稱也要吃得下')
    check(otc[0]['market'] == 'TWO', '市場別要標對,yfinance 後綴才會對')

    # 壞資料不能讓整批掛掉
    bad = st.parse_rows([{'Code': '9999', 'ClosingPrice': '--'},
                         {'no': 'fields'}, 'not a dict', TWSE_ROW], 'TW')
    check(len(bad) == 1, f'壞資料應被跳過,只留 1 筆,實際 {len(bad)}')

    check(st.yf_symbol({'code': '2330', 'market': 'TW'}) == '2330.TW',
          '上市後綴應為 .TW')
    check(st.yf_symbol({'code': '6488', 'market': 'TWO'}) == '6488.TWO',
          '上櫃後綴應為 .TWO')


# ───────────────────────── 第一層候選篩選 ─────────────────────────

def row(code, close_px, value, high=None, low=None):
    return dict(code=code, market='TW', name='X', close=close_px, value=value,
                high=high if high is not None else close_px * 1.01,
                low=low if low is not None else close_px * 0.99)


def test_candidates():
    print('候選篩選')
    opt = st.parse_args(['--dry'])
    rows = [
        row('2330', 100, 5e9),                    # 正常
        row('0050', 190, 5e9),                    # ETF,預設排除
        row('12345', 100, 5e9),                   # 權證/特別股,排除
        row('1111', 100, 1e6),                    # 成交額不足
        row('2222', 5, 5e9),                      # 股價太低
        row('3333', 800, 5e9),                    # 股價太高
        row('4444', 100, 5e9, high=100, low=90),  # 漲停鎖死(收在最高)
        row('5555', 100, 5e9),                    # 正常,但在處置股名單
    ]
    got = {r['code'] for r in st.candidates(rows, opt, skip={'5555'})}
    check(got == {'2330'}, f'只有 2330 該留下,實際 {sorted(got)}')

    opt_etf = st.parse_args(['--dry', '--include-etf'])
    got2 = {r['code'] for r in st.candidates(rows, opt_etf)}
    check('0050' in got2, '--include-etf 時 ETF 要進得來')

    # 本金買不起 1 股的要擋掉,即使其他條件都過
    opt_small = st.parse_args(['--dry', '--capital', '50'])
    got3 = {r['code'] for r in st.candidates(rows, opt_small)}
    check(got3 == set(), f'本金 50 元買不起任何一檔,實際 {sorted(got3)}')


# ───────────────────────── 指標與條件 ─────────────────────────

def make_bars(n=200, breakout=True):
    """做一段先橫盤、最後一根放量突破的日線

    用固定的規則產生而不是亂數:要測的是「這個型態會不會被抓出來」,
    每次都一樣才有辦法斷言。
    """
    class TS:
        def __init__(self, t):
            self._t = t

        def timestamp(self):
            return self._t

    bars, base = [], 100.0
    for i in range(n):
        base *= 1.0004                        # 緩步走高,讓均線呈多頭排列
        # 疊一段起伏:全是紅 K 的假資料 RSI 會逼近 100,反而過不了
        # 「RSI 50-80」這關,測到的就不是真實型態
        price = base * (1 + 0.02 * math.sin(i / 4.0))
        wig = 0.004 * math.sin(i / 3.0)
        h, lo, c = price * (1 + abs(wig) + 0.002), price * (1 - abs(wig)), price
        bars.append((h, lo, c, 1000.0, TS(1_700_000_000 + i * 86400)))
    if breakout:
        last = bars[-1][2] * 1.04             # 最後一根 +4% 且量放大 4 倍
        bars[-1] = (last * 1.002, bars[-1][2], last, 4000.0, bars[-1][4])
    return bars


def test_measure():
    print('指標計算')
    m = st.measure(row('2330', 0, 5e9), make_bars())
    check(m is not None, 'measure 應算得出來')
    if not m:
        return
    check(m['bull'], '緩步走高應判定為多頭排列')
    check(close(m['volr'], 4.0), f"量比應為 4,實際 {m['volr']}")
    check(m['ch1'] > 4, f"當日漲幅應約 4%,實際 {m['ch1']}")
    check(m['dist_hi'] > 0, '最後一根該創 20 日新高')
    check(m['ma20_up'], 'MA20 應向上')
    check(m['ts'] == (1_700_000_000 + 199 * 86400) * 1000,
          f"ts 應換算成毫秒,實際 {m['ts']}")

    # K 棒不足的新股要回 None,不能拿半截資料算指標
    check(st.measure(row('9999', 0, 5e9), make_bars(50)) is None,
          'K 棒不足應回 None')

    # 指標欄位要跟 exits.py 的期待對得上,否則出場計畫會算不出來
    pos = ex.open_position(m)
    check(pos['stop'] is not None, 'exits 應算得出停損價')
    check(pos['box_hi'] is not None, '收盤站上箱頂時 box_hi 應保留')


def test_extract():
    """yfinance 的回傳形狀:批次是 MultiIndex,單檔是單層,兩種都要吃"""
    print('yfinance 解析')
    import pandas as pd

    idx = pd.to_datetime(['2026-08-18', '2026-08-19', '2026-08-20'])
    cols = ['Open', 'High', 'Low', 'Close', 'Volume']
    data = [[10, 11, 9, 10.5, 1000], [10.5, 12, 10, 11, 2000],
            [11, 13, 10.5, 12, 3000]]
    single = pd.DataFrame(data, index=idx, columns=cols)
    check(len(st.extract(single, '2330.TW', 1)) == 3, '單檔形狀應解析出 3 根')

    multi = pd.concat({'2330.TW': single, '2317.TW': single}, axis=1)
    bars = st.extract(multi, '2330.TW', 2)
    check(len(bars) == 3, f'批次形狀應解析出 3 根,實際 {len(bars)}')
    check(bars and close(bars[-1][2], 12), '收盤價應取 Close 欄')
    check(st.extract(multi, '9999.TW', 2) == [], '不存在的代號應回空,不能拋例外')

    # 停牌日 Yahoo 會補 NaN,不能拿去算指標
    holed = single.copy()
    holed.loc[idx[1], 'Close'] = float('nan')
    check(len(st.extract(holed, '2330.TW', 1)) == 2, 'NaN 的列應被丟掉')


def test_conds():
    print('篩選條件')
    opt = st.parse_args(['--dry'])
    m = st.measure(row('2330', 0, 5e9), make_bars())
    hits, dropped = st.pick([m], opt)
    check(len(hits) == 1, f'這個型態該被 breakout 抓到,實際 {len(hits)} 檔;'
                          f'各條件:{st.diagnose([m], opt)};成本關:{dropped}')

    # 橫盤沒突破的不該中
    flat = st.measure(row('2330', 0, 5e9), make_bars(breakout=False))
    hits2, _ = st.pick([flat], opt)
    check(len(hits2) == 0, '沒突破不該出訊號')


def test_cost_gate():
    print('成本關')
    m = st.measure(row('2330', 0, 5e9), make_bars())

    # 低消 20 元:成本 4%+,遠超過 --max-cost,應該被擋掉並說明原因
    opt20 = st.parse_args(['--dry', '--fee-min', '20'])
    ok, why = st.cost_ok(m, opt20)
    check(not ok and '成本' in why, f'低消 20 元應被成本關擋下,實際 {ok} {why}')

    # 停損距離不夠寬的也要擋:把停損拉到只差 0.5%
    opt1 = st.parse_args(['--dry'])
    narrow = dict(m, atr=m['last'] * 0.001, low=m['last'] * 0.995)
    ok2, why2 = st.cost_ok(narrow, opt1)
    check(not ok2 and '停損' in why2, f'停損過窄應被擋下,實際 {ok2} {why2}')

    ok3, _ = st.cost_ok(m, opt1)
    check(ok3, '低消 1 元且停損夠寬時應該通過')


def test_message():
    print('訊息組裝')
    opt = st.parse_args(['--dry'])
    m = st.measure(row('2330', 0, 5e9), make_bars())
    m['name'] = '台積電'
    msg = st.build_message([m], 1, opt)
    check('台積電' in msg and '停損' in msg and '可買' in msg,
          '推播內容應含名稱、停損與可買股數')
    check(msg.count('<b>') == msg.count('</b>'), 'HTML 標籤要成對')
    check('本輪無標的' in st.build_empty_message(0, opt), '空訊息應可組出')


def test_pipeline():
    """整條路走一次:快照 → 候選 → 指標 → 篩選 → 記錄,全程不連線

    各段單獨測過不代表串起來會動,參數名稱打錯、回傳形狀對不上這類問題
    只有整條跑一次才看得出來。
    """
    print('整體流程')
    import tempfile

    real_get, real_hist = st.get, st.fetch_history
    st.get = lambda url, tries=4: (
        [dict(TWSE_ROW, ClosingPrice='100', TradeValue='5000000000')]
        if 'STOCK_DAY' in url else [])
    st.fetch_history = lambda rows, days=st.HIST_DAYS: {'2330.TW': make_bars()}
    try:
        with tempfile.TemporaryDirectory() as d:
            rec = f'{d}/signals_tw.jsonl'
            rc = st.main(['--dry', '--json', f'{d}/hits.json', '--record', rec])
            check(rc == 0, 'main 應正常結束')
            import json
            with open(rec, encoding='utf-8') as f:
                rows = [json.loads(x) for x in f if x.strip()]
            check(len(rows) == 1, f'應記錄 1 筆訊號,實際 {len(rows)}')
            check(rows and rows[0]['stop'] and rows[0]['shares'] >= 1,
                  '訊號要帶停損價與可買股數')
            # 同一根重複跑不該記兩次
            st.main(['--dry', '--json', f'{d}/hits.json', '--record', rec])
            with open(rec, encoding='utf-8') as f:
                again = sum(1 for x in f if x.strip())
            check(again == 1, f'同一根重複掃描不該重複記錄,實際 {again} 筆')
    finally:
        st.get, st.fetch_history = real_get, real_hist


def main():
    for fn in (test_costs, test_parse, test_candidates, test_measure,
               test_extract, test_conds, test_pipeline, test_cost_gate, test_message):
        fn()
    if FAIL:
        print(f'\n❌ {len(FAIL)} 項未通過')
        return 1
    print('\n✅ 全部通過')
    return 0


if __name__ == '__main__':
    sys.exit(main())
