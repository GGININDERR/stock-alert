"""台股日線突破掃描 — 零股小資金版

CLI 用法:
  python scan_tw.py                      # 突破模式,收盤後跑,推播 Telegram
  python scan_tw.py --mode early         # 壓縮後突破:先橫盤、帶寬收窄,剛站上箱頂
  python scan_tw.py --dry                # 只印螢幕,不發 Telegram
  python scan_tw.py --capital 1000       # 起手資金,決定買得起幾股與成本佔比
  python scan_tw.py --fee-min 20         # 券商單筆低消,預設 1(零股低消 1 元的券商)
  python scan_tw.py --symbols 2330,2454  # 跳過全市場掃描,只看指定幾檔
  python scan_tw.py --otc                # 一併掃上櫃(預設只掃上市)

環境變數:
  TELEGRAM_TOKEN, TELEGRAM_CHAT_ID   (--dry 時可不設)

為什麼分兩層抓資料:
  第一層用證交所/櫃買的 OpenAPI,一次請求拿到全市場「今天」的收盤與
  成交金額,先把 1800 多檔砍到剩幾十檔。第二層才用 yfinance 抓這幾十
  檔的歷史日線算指標。反過來做(全市場都抓歷史)會慢上兩個數量級,
  而且一定被限流。

出場規則沿用 exits.py,跟加密貨幣那邊共用同一份 —— 差別只在參數:
台股零股來回成本至少 0.5%(見 costs_tw.py),所以停損距離的下限拉到
3%,比 scan_bull 的 1% 寬。

零股的現實限制,寫在這裡免得之後忘記為什麼條件開得這麼嚴:
  盤中零股是集合競價、每分鐘撮合一次,不是逐筆成交,追價一定有延遲;
  零股買賣價差常常 1~2 檔,冷門股更誇張。所以流動性門檻(--turn)不是
  可有可無的參數,它決定訊號能不能真的成交。

免責:純技術面篩選,只看價格與成交量,不構成投資建議。
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

import costs_tw
import exits
from scan_bull import (TPE, atr, bbw_percentile, get, ma, macd_state, rsi,
                       send_telegram)

TWSE_DAY_ALL = 'https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL'
TWSE_PUNISH = 'https://openapi.twse.com.tw/v1/announcement/punish'
TPEX_DAY_ALL = ('https://www.tpex.org.tw/openapi/v1/'
                'tpex_mainboard_daily_close_quotes')

# 預設門檻
DEF_TURNOVER = 1e8      # 當日成交金額下限(元):零股掛得動的最低標準
DEF_PRICE_LO = 20.0     # 股價下限:太低的多半是雞蛋水餃股
DEF_PRICE_HI = 300.0    # 股價上限:1000 元至少要買得到 3 股才談得上部位
DEF_VOLR = 2.0          # 量比門檻:當日量 ÷ 前 20 日均量
DEF_MIN_RISK = 3.0      # 最小停損距離(%),見模組說明
DEF_COST_MULT = 3.0     # 停損距離至少要是來回成本的幾倍
DEF_MAX_COST = 1.5      # 來回成本佔投入金額的上限(%)
HOT_DIST = 10.0         # 離 MA20 超過這個 % 視為追高,標紅
HIST_DAYS = 400         # 抓幾天歷史:算 MA120 + 帶寬百分位要夠長
MIN_BARS = 130          # K 棒不足這個數就不算(新上市股)
CHUNK = 40              # yfinance 一批抓幾檔


# ───────────────────────── 第一層:全市場快照 ─────────────────────────

def field(row, *names):
    """從 OpenAPI 的一列取欄位,容忍欄位名稱不同

    證交所與櫃買的欄位命名不一致(ClosingPrice vs Close),而且改版過
    幾次。與其寫死一組名字,不如列出候選逐一試,少一個欄位就少一檔,
    不會整批掛掉。
    """
    for n in names:
        if n in row and row[n] not in (None, '', '--'):
            return row[n]
    return None


def num(x):
    """OpenAPI 的數字是帶逗號的字串,轉不動就回 None"""
    if x is None:
        return None
    try:
        return float(str(x).replace(',', '').replace('+', '').strip())
    except ValueError:
        return None


def parse_rows(rows, market):
    """把 OpenAPI 的原始列轉成統一格式,轉不出來的直接丟掉"""
    out = []
    for r in rows or []:
        if not isinstance(r, dict):
            continue
        code = field(r, 'Code', 'SecuritiesCompanyCode', 'CompanyCode')
        close = num(field(r, 'ClosingPrice', 'Close'))
        value = num(field(r, 'TradeValue', 'TransactionAmount', 'TradeAmount'))
        high = num(field(r, 'HighestPrice', 'High'))
        low = num(field(r, 'LowestPrice', 'Low'))
        if not code or not close or close <= 0 or value is None:
            continue
        out.append(dict(code=str(code).strip(), market=market,
                        name=str(field(r, 'Name', 'CompanyName') or '').strip(),
                        close=close, value=value, high=high, low=low))
    return out


def snapshot(otc=False):
    """全市場當日收盤快照。上櫃抓不到不算錯,少一半標的但還能跑"""
    rows = parse_rows(get(TWSE_DAY_ALL), 'TW')
    if not rows:
        raise SystemExit('證交所 OpenAPI 取得失敗,無法取得全市場快照')
    if otc:
        otc_rows = parse_rows(get(TPEX_DAY_ALL), 'TWO')
        if otc_rows:
            rows += otc_rows
        else:
            print('警告:櫃買 OpenAPI 取不到,本輪只掃上市', file=sys.stderr)
    return rows


def punished():
    """處置股代號集合。處置期間會被改成人工撮合(每 5~20 分鐘一次),
    零股更難成交,直接排除。抓不到就回空集合,不擋住整輪掃描。
    """
    out = set()
    for r in get(TWSE_PUNISH) or []:
        if isinstance(r, dict):
            code = field(r, 'Code', 'SecuritiesCompanyCode', '證券代號')
            if code:
                out.add(str(code).strip())
    return out


def is_common_stock(code):
    """只留普通股:4 碼數字,排除 00 開頭的 ETF 與 5~6 碼的權證/特別股

    ETF 不會有「突破前高 + 爆量」這種型態(成分股分散,波動被平均掉),
    放進來只會稀釋訊號。要做 ETF 是另一套策略。
    """
    return len(code) == 4 and code.isdigit() and not code.startswith('00')


def limit_up(row):
    """當日漲停鎖死:收盤 = 最高價,且漲幅接近 10%

    追這種隔天多半開高走低,而且零股根本掛不到。沒有前一日收盤價可比,
    改用「收在最高價」加上振幅判斷,寧可錯殺。
    """
    if not row.get('high') or not row.get('low'):
        return False
    return (row['close'] >= row['high']
            and row['low'] > 0
            and row['high'] / row['low'] - 1 >= 0.09)


def candidates(rows, opt, skip=()):
    """第一層篩選:流動性、股價區間、可交易性

    這一層砍掉的是「就算訊號再漂亮也不該買」的標的,跟技術面無關。
    """
    out = []
    for r in rows:
        if r['code'] in skip:
            continue
        if not opt.include_etf and not is_common_stock(r['code']):
            continue
        if r['value'] < opt.turn:
            continue
        if not opt.price_lo <= r['close'] <= opt.price_hi:
            continue
        if limit_up(r):
            continue
        if costs_tw.shares_for(r['close'], opt.capital) < 1:
            continue
        out.append(r)
    return out


# ───────────────────────── 第二層:歷史日線與指標 ─────────────────────────

def yf_symbol(row):
    return f"{row['code']}.{'TWO' if row['market'] == 'TWO' else 'TW'}"


def fetch_history(rows, days=HIST_DAYS):
    """用 yfinance 批次抓歷史日線,回傳 {yf代號: [(o,h,l,c,v,日期), ...]}

    分批是為了避開 Yahoo 對單次請求的檔數限制;一批掛掉只損失那批,
    其他批照常。
    """
    import yfinance as yf   # 只有真的要抓資料時才需要,匯入放在函式裡

    start = (datetime.now(TPE) - timedelta(days=days)).strftime('%Y-%m-%d')
    out = {}
    syms = [yf_symbol(r) for r in rows]
    for i in range(0, len(syms), CHUNK):
        chunk = syms[i:i + CHUNK]
        try:
            df = yf.download(chunk, start=start, group_by='ticker',
                             auto_adjust=False, progress=False, threads=True)
        except Exception as e:
            print(f'警告:第 {i // CHUNK + 1} 批抓取失敗 {e!r}', file=sys.stderr)
            continue
        for sym in chunk:
            bars = extract(df, sym, len(chunk))
            if bars:
                out[sym] = bars
    return out


def extract(df, sym, batch):
    """從 yfinance 的 DataFrame 取出單檔的 K 棒序列

    批次抓回來是 MultiIndex 欄位(代號, 欄位),只抓一檔時卻是單層,
    兩種都要吃。缺值的列直接丟掉 —— 台股停牌日 Yahoo 會補 NaN。
    """
    try:
        sub = df[sym] if batch > 1 else df
    except (KeyError, TypeError):
        return []
    try:
        sub = sub.dropna(subset=['Close', 'High', 'Low'])
    except (KeyError, TypeError):
        return []
    bars = []
    for ts, r in sub.iterrows():
        try:
            c, h, lo = float(r['Close']), float(r['High']), float(r['Low'])
            v = float(r['Volume'])
        except (KeyError, TypeError, ValueError):
            continue
        if c > 0 and h > 0 and lo > 0:
            bars.append((h, lo, c, v, ts))
    return bars


def measure(row, bars):
    """算指標。回傳與 scan_bull.measure 同構的 dict,exits.py 才能共用

    欄位名稱刻意跟 scan_bull 一致(last / high / low / atr / ma20 /
    ma20_prev / box_hi),這樣 exits.open_position 與 plan_text 不用改。
    只是這裡一根 K 棒是一天,不是一小時。
    """
    if len(bars) < MIN_BARS:
        return None
    h = [b[0] for b in bars]
    lo = [b[1] for b in bars]
    c = [b[2] for b in bars]
    v = [b[3] for b in bars]

    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    if not m120:
        return None

    base = sum(v[-21:-1]) / 20          # 前 20 日均量(不含當日,避免稀釋)
    volr = v[-1] / base if base else 0
    prev20 = ma(c[:-1], 20)
    box_hi = max(h[-21:-1])             # 前 20 日高點 = 箱頂
    box_lo = min(lo[-21:-1])

    return dict(
        sym=row['code'],
        name=row['name'],
        market=row['market'],
        ts=int(bars[-1][4].timestamp() * 1000),
        last=c[-1],
        high=h[-1], low=lo[-1],
        atr=atr(h, lo, c),
        ma20=m20, ma20_prev=prev20,
        box_hi=box_hi, box_lo=box_lo,
        bull=c[-1] > m20 > m60 > m120,
        volr=volr,
        turn=row['value'],
        ch1=(c[-1] / c[-2] - 1) * 100,           # 當日漲幅
        ch5=(c[-1] / c[-6] - 1) * 100,           # 一週
        ch20=(c[-1] / c[-21] - 1) * 100,         # 一個月
        dist20=(c[-1] / m20 - 1) * 100,
        rsi=rsi(c, 14),
        macd=macd_state(c),
        ma20_up=bool(prev20 and m20 > prev20),
        above_mid=c[-1] > m60 and c[-1] > m120,
        # 距前高:0 是剛好在箱頂,正數代表已突破
        dist_hi=(c[-1] / box_hi - 1) * 100 if box_hi else None,
        box_amp=(box_hi / box_lo - 1) * 100 if box_lo else None,
        bbw_pct=bbw_percentile(c[:-1]),
    )


# ───────────────────────── 第三層:篩選條件 ─────────────────────────

def between(x, lo, hi):
    return x is not None and lo <= x <= hi


def conds_breakout(opt):
    """突破:創 20 日新高 + 爆量 + 多頭排列

    台股個股日線的漲幅尺度比加密貨幣小得多,所以區間跟 scan_bull 的
    breakout 不同:單日 +2~9% 已經是大量,超過 9% 接近漲停不追。
    """
    return [
        ("多頭排列", lambda x: x['bull']),
        ("MA20向上", lambda x: x['ma20_up']),
        (f"量比>{opt.volr:g}", lambda x: x['volr'] > opt.volr),
        ("創20日新高", lambda x: x['dist_hi'] is not None and x['dist_hi'] > 0),
        ("日漲2~9%", lambda x: between(x['ch1'], 2, 9)),
        ("月漲<40%", lambda x: between(x['ch20'], -100, 40)),
        ("RSI 50-80", lambda x: between(x['rsi'], 50, 80)),
    ]


def conds_early(opt):
    """壓縮後突破:先橫盤、帶寬收窄,剛站上箱頂

    抓的是還沒噴的位置,所以不看漲幅,改看盤整結構是否被打破。
    對零股特別有意義:還沒噴的標的價差小、追價成本低。
    """
    return [
        (f"量比>{opt.volr:g}", lambda x: x['volr'] > opt.volr),
        (f"箱體<{opt.box_amp:g}%", lambda x: between(x['box_amp'], 0, opt.box_amp)),
        (f"帶寬≤{opt.bbw_pct:g}", lambda x: x['bbw_pct'] is not None
         and x['bbw_pct'] <= opt.bbw_pct),
        ("站上箱頂", lambda x: x['dist_hi'] is not None and x['dist_hi'] > -0.5),
        ("均線轉多", lambda x: x['above_mid']),
        ("RSI 50-75", lambda x: between(x['rsi'], 50, 75)),
    ]


COND_SETS = {'breakout': conds_breakout, 'early': conds_early}

MODE_DESC = {'breakout': '突破前高', 'early': '壓縮後突破'}

MODE_DEFAULTS = {
    'breakout': dict(volr=2.0),
    'early': dict(volr=1.5),      # 壓縮盤整本來就量縮,門檻跟著放低
}


def diagnose(res, opt):
    """每個條件各自有幾檔通過,用來判斷是哪一關把標的殺光"""
    conds = COND_SETS[opt.mode](opt)
    return ' ｜ '.join(f"{name} {sum(1 for x in res if fn(x))}"
                      for name, fn in conds)


def cost_ok(x, opt):
    """成本關:來回成本佔比不能太高,且停損要比成本寬得夠多

    這是零股小資金最容易死的地方,所以獨立成一關並且回傳原因,
    好在診斷裡看到「訊號是被成本砍掉的,不是被技術面砍掉的」。
    """
    be = costs_tw.breakeven_pct(x['last'], opt.capital, opt.discount,
                                opt.fee_min)
    if be is None:
        return False, '買不起 1 股'
    if be > opt.max_cost:
        return False, f'成本 {be:.2f}% > {opt.max_cost:g}%'
    r = exits.risk_pct(exits.open_position(x))
    if r is None:
        return False, '算不出停損'
    if r < opt.min_risk:
        return False, f'停損 {r:.2f}% < {opt.min_risk:g}%'
    if r < be * opt.cost_mult:
        return False, f'停損 {r:.2f}% 不到成本的 {opt.cost_mult:g} 倍'
    return True, ''


def pick(res, opt):
    conds = COND_SETS[opt.mode](opt)
    hits = [x for x in res if all(fn(x) for _, fn in conds)]
    if opt.max_dist is not None:
        hits = [x for x in hits if abs(x['dist20']) < opt.max_dist]

    kept, dropped = [], []
    for x in hits:
        ok, why = cost_ok(x, opt)
        (kept if ok else dropped).append(x if ok else (x['sym'], why))
    kept.sort(key=lambda x: -x['volr'])
    return kept, dropped


# ───────────────────────── 輸出 ─────────────────────────

def verdict(x):
    """依量比與乖離給判讀。回傳 (標籤, 說明)"""
    d = abs(x['dist20'])
    if d >= HOT_DIST:
        return '🔴 乖離過大', '離月線太遠,零股追價會更貴,建議等回踩 MA20'
    if x['volr'] >= 4 and d < 6:
        return '🟢 剛啟動', '量能猛、位階乾淨,這是最理想的進場位置'
    if x['volr'] >= 4:
        return '🟡 放量但偏高', '資金介入明顯,但已離月線一段,分批比一次進好'
    if d < 6:
        return '🟢 溫和啟動', '量能剛放大、位階乾淨,可留意後續是否續攻'
    return '🟡 觀察', '整理中還沒噴,先放觀察名單'


def line_text(x):
    return (f"{x['sym']:6} {x['name'][:6]:8} 收{x['last']:<8.2f} "
            f"日{x['ch1']:+6.2f}% 週{x['ch5']:+6.2f}% 月{x['ch20']:+6.2f}% "
            f"離MA20{x['dist20']:+5.2f}% 量比{x['volr']:.2f} "
            f"成交額{x['turn'] / 1e8:.2f}億")


def build_message(hits, scanned, opt):
    """組 Telegram 訊息(HTML):成本 + 數據 + 出場計畫"""
    now = datetime.now(TPE).strftime('%Y-%m-%d %H:%M')
    head = (f"📈 <b>台股日線 {MODE_DESC[opt.mode]} 掃描</b>\n"
            f"{now} (台北)  掃描 {scanned} 檔,符合 <b>{len(hits)}</b> 檔\n"
            f"<i>本金 {opt.capital:.0f} 元 ｜ 手續費 {opt.discount:g} 折抵、"
            f"低消 {opt.fee_min:g} 元 ｜ 依量比排序</i>\n")

    blocks = []
    for i, x in enumerate(hits, 1):
        tag, why = verdict(x)
        plan = exits.plan_text(exits.open_position(x))
        cost = costs_tw.cost_text(x['last'], opt.capital, opt.discount,
                                  opt.fee_min)
        blocks.append(
            f"\n<b>{i}. {x['sym']} {x['name']}</b>  {x['last']:.2f}  {tag}\n"
            f"量比 <b>{x['volr']:.2f}</b>ｘ ｜ 離MA20 {x['dist20']:+.2f}% ｜ "
            f"距箱頂 {x['dist_hi']:+.2f}%\n"
            f"日 {x['ch1']:+.2f}% ｜ 週 {x['ch5']:+.2f}% ｜ 月 {x['ch20']:+.2f}%\n"
            f"成交額 {x['turn'] / 1e8:.2f} 億 ｜ RSI {x['rsi']:.0f}\n"
            f"💰 {cost}\n"
            f"→ <i>{why}</i>\n"
            f"{plan}\n"
        )

    tail = ("\n<i>零股是每分鐘集合競價,不是逐筆成交,掛單請用限價並預留 1~2 檔;"
            "訊號以收盤價計算,隔天開盤跳空就重新評估,不要照舊價追。\n"
            "技術面篩選,不看基本面與消息面,非投資建議。</i>")
    return head + ''.join(blocks) + tail


def build_empty_message(scanned, opt):
    now = datetime.now(TPE).strftime('%Y-%m-%d %H:%M')
    return (f"😴 <b>台股日線 {MODE_DESC[opt.mode]} 掃描</b>\n"
            f"{now} (台北)  掃描 {scanned} 檔,<b>本輪無標的</b>\n"
            f"<i>條件未同時成立,維持觀望。</i>")


# ───────────────────────── 訊號記錄(紙上交易) ─────────────────────────

def record_signals(path, hits, opt):
    """把命中的訊號附加到 jsonl,之後回頭算成效

    1000 元本金一次只買得起一檔,真正能學到東西的是這份紙上紀錄:
    訊號照記,實單只挑其中最好的一筆,累積夠多筆才有勝率可談。
    """
    seen = set()
    if os.path.exists(path):
        with open(path, encoding='utf-8') as f:
            for line in f:
                try:
                    d = json.loads(line)
                    seen.add((d['mode'], d['sym'], d['ts']))
                except (ValueError, KeyError):
                    continue

    added = 0
    with open(path, 'a', encoding='utf-8') as f:
        for x in hits:
            key = (opt.mode, x['sym'], x['ts'])
            if key in seen:
                continue
            pos = exits.open_position(x)
            tag, _ = verdict(x)
            f.write(json.dumps({
                'mode': opt.mode, 'sym': x['sym'], 'name': x['name'],
                'market': x['market'], 'ts': x['ts'],
                'time': datetime.fromtimestamp(x['ts'] / 1000, TPE).isoformat(),
                'entry': x['last'], 'volr': round(x['volr'], 2),
                'turn': round(x['turn']),
                'rsi': round(x['rsi'], 1) if x['rsi'] else None,
                'dist20': round(x['dist20'], 2), 'ch20': round(x['ch20'], 2),
                'tag': tag,
                'stop': pos['stop'], 'box_hi': pos['box_hi'], 'atr': pos['atr'],
                'hot': pos['hot'],
                'shares': costs_tw.shares_for(x['last'], opt.capital),
                'cost_pct': round(costs_tw.breakeven_pct(
                    x['last'], opt.capital, opt.discount, opt.fee_min), 3),
            }, ensure_ascii=False) + '\n')
            added += 1
    print(f'記錄 {added} 筆訊號 → {path}')


# ───────────────────────── main ─────────────────────────

def parse_args(argv):
    p = argparse.ArgumentParser(description='台股日線突破掃描(零股小資金版)')
    p.add_argument('--mode', default='breakout', choices=sorted(COND_SETS))
    p.add_argument('--capital', type=float, default=costs_tw.DEF_CAPITAL,
                   help='起手資金(元),預設 1000')
    p.add_argument('--fee-min', type=float, default=costs_tw.DEF_FEE_MIN,
                   help='券商單筆低消(元),預設 1;低消 20 元的券商請填 20')
    p.add_argument('--discount', type=float, default=costs_tw.DEF_DISCOUNT,
                   help='手續費折扣,預設 0.6(六折)')
    p.add_argument('--max-cost', type=float, default=DEF_MAX_COST,
                   help='來回成本佔投入金額上限 %%,超過就不出訊號')
    p.add_argument('--cost-mult', type=float, default=DEF_COST_MULT,
                   help='停損距離至少要是來回成本的幾倍,預設 3')
    p.add_argument('--turn', type=float, default=DEF_TURNOVER,
                   help='當日成交金額下限(元),預設 1e8')
    p.add_argument('--price-lo', type=float, default=DEF_PRICE_LO)
    p.add_argument('--price-hi', type=float, default=DEF_PRICE_HI)
    p.add_argument('--volr', type=float, default=None,
                   help='量比門檻,未指定則用該模式預設值')
    p.add_argument('--box-amp', type=float, default=15.0,
                   help='early 模式:近 20 日箱體振幅上限 %%')
    p.add_argument('--bbw-pct', type=float, default=0.35,
                   help='early 模式:布林帶寬百分位上限')
    p.add_argument('--max-dist', type=float, default=None,
                   help='離 MA20 乖離上限 %%,超過就排除(避開追高)')
    p.add_argument('--min-risk', type=float, default=DEF_MIN_RISK,
                   help='最小停損距離 %%,預設 3(台股零股的成本門檻)')
    p.add_argument('--otc', action='store_true', help='一併掃上櫃')
    p.add_argument('--include-etf', action='store_true',
                   help='把 ETF 與非四碼普通股也納入')
    p.add_argument('--symbols', default=None,
                   help='只看這幾檔(逗號分隔),跳過全市場流動性篩選')
    p.add_argument('--limit', type=int, default=150,
                   help='第二層最多抓幾檔歷史(依成交金額由大到小取)')
    p.add_argument('--always', action='store_true',
                   help='0 檔時也發一則「本輪無標的」')
    p.add_argument('--dry', action='store_true', help='只印螢幕,不發 Telegram')
    p.add_argument('--json', default='tw_hits.json', help='結果輸出路徑')
    p.add_argument('--record', default=None,
                   help='把命中的訊號附加到這個 jsonl(紙上追蹤用)')
    opt = p.parse_args(argv)
    for k, val in MODE_DEFAULTS[opt.mode].items():
        if getattr(opt, k) is None:
            setattr(opt, k, val)
    return opt


def main(argv=None):
    opt = parse_args(argv if argv is not None else sys.argv[1:])

    rows = snapshot(opt.otc)
    print(f'全市場 {len(rows)} 檔')

    if opt.symbols:
        want = {s.strip() for s in opt.symbols.split(',') if s.strip()}
        cands = [r for r in rows if r['code'] in want]
    else:
        cands = candidates(rows, opt, skip=punished())
        cands.sort(key=lambda r: -r['value'])
        cands = cands[:opt.limit]
    print(f'候選 {len(cands)} 檔(流動性 / 股價 / 可交易性)')
    if not cands:
        return 0

    hist = fetch_history(cands)
    res = []
    for r in cands:
        bars = hist.get(yf_symbol(r))
        m = measure(r, bars) if bars else None
        if m:
            res.append(m)
    print(f'取得歷史 {len(res)} 檔')

    hits, dropped = pick(res, opt)
    print('各條件通過數:', diagnose(res, opt))
    if dropped:
        print('被成本關砍掉:', ' ｜ '.join(f'{s} {w}' for s, w in dropped))
    print(f'符合 {len(hits)} 檔')
    for x in hits:
        print(line_text(x))

    if opt.json:
        with open(opt.json, 'w', encoding='utf-8') as f:
            json.dump(hits, f, ensure_ascii=False, indent=2)

    if opt.record and hits:
        record_signals(opt.record, hits, opt)

    if not opt.dry:
        if hits:
            send_telegram(build_message(hits, len(res), opt))
        elif opt.always:
            send_telegram(build_empty_message(len(res), opt))
    return 0


if __name__ == '__main__':
    sys.exit(main())
