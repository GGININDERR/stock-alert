"""OKX USDT 永續合約 — 1H 多頭排列 + 量比放大掃描

CLI 用法:
  python scan_bull.py                 # classic:多頭排列 + 量比,推播 Telegram
  python scan_bull.py --mode early    # 壓縮後突破:先橫盤、帶寬收窄,剛站上箱頂
  python scan_bull.py --mode breakout # 剛啟動:1h/4h/24h 都在動且貼著前高
  python scan_bull.py --mode chase    # 追高順勢:已在噴,供觀察回踩(門檻最嚴)
  python scan_bull.py --always        # 0 檔時也回報一則,確認系統活著
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
import time
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
WORKERS = 6          # 併發執行緒:太高會被 OKX 限流,整批 K 線抓不到
RETRY_GAP = 0.3      # 第二輪補抓時每檔間隔(秒)

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

def get(url, tries=4):
    """帶重試的 JSON 取得,失敗回 None

    OKX 會對密集請求限流,立刻重試同樣會被擋,所以每次重試前遞增等待
    (0.5 / 1 / 2 秒),讓限流窗口過去。
    """
    for attempt in range(tries):
        try:
            req = urllib.request.Request(url, headers=UA)
            return json.load(urllib.request.urlopen(req, timeout=20))
        except Exception:
            if attempt < tries - 1:
                time.sleep(0.5 * 2 ** attempt)
    return None


def ma(vals, n):
    """n 期簡單移動平均;資料不足回 None"""
    return sum(vals[-n:]) / n if len(vals) >= n else None


def rsi(c, n=14):
    """Wilder RSI。0-100,高於 50 代表近期漲多於跌"""
    if len(c) <= n:
        return None
    gains = losses = 0.0
    for i in range(1, n + 1):                # 前 n 根先取簡單平均
        d = c[i] - c[i - 1]
        gains += max(d, 0)
        losses += max(-d, 0)
    ag, al = gains / n, losses / n
    for i in range(n + 1, len(c)):           # 之後用 Wilder 平滑
        d = c[i] - c[i - 1]
        ag = (ag * (n - 1) + max(d, 0)) / n
        al = (al * (n - 1) + max(-d, 0)) / n
    if al == 0:
        return 100.0
    return 100 - 100 / (1 + ag / al)


def ema(vals, n):
    """指數移動平均,回傳整條序列"""
    k = 2 / (n + 1)
    out = [vals[0]]
    for x in vals[1:]:
        out.append(x * k + out[-1] * (1 - k))
    return out


def macd_state(c):
    """回傳 (DIF - DEA) 的柱狀值:大於 0 代表 MACD 在訊號線上方"""
    if len(c) < 35:
        return None
    f, s = ema(c, 12), ema(c, 26)
    dif = [a - b for a, b in zip(f, s)]
    dea = ema(dif, 9)
    return dif[-1] - dea[-1]


def bb_width(c, n=20):
    """布林帶寬 (上軌-下軌)/中軌,衡量波動壓縮程度"""
    if len(c) < n:
        return None
    win = c[-n:]
    mid = sum(win) / n
    var = sum((x - mid) ** 2 for x in win) / n
    return (4 * var ** 0.5) / mid if mid else None


def bbw_percentile(c, look=90, n=20):
    """目前帶寬在近 look 根裡的百分位。越小代表壓縮得越緊"""
    if len(c) < look + n:
        return None
    series = [bb_width(c[:len(c) - i], n) for i in range(look)]
    series = [x for x in series if x is not None]
    if not series:
        return None
    cur = series[0]
    return sum(1 for x in series if x <= cur) / len(series)


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
            # 成交額要用 volCcy24h(基礎幣顆數)乘價格。vol24h 對永續合約
            # 的單位是「張數」,每張面值(ctVal)各幣不同,拿來乘價格算出的
            # 不是美元金額,門檻會變成每個幣鬆緊不一。
            turn = float(d['volCcy24h']) * last  # 24h 成交額(USDT)
        except (KeyError, ValueError):
            continue
        if turn < min_turnover or op <= 0:
            continue
        out.append((inst, last, (last / op - 1) * 100, turn))
    return out


# ───────────────────────── 第二層:算指標 ─────────────────────────

def measure(item, bar):
    """抓 K 線並算出指標。回傳 (狀態, 結果)

    狀態分三種,分開計數才知道漏掉的是「該補抓」還是「本來就算不了」:
      ok    — 算出指標
      fail  — 抓不到資料(多半是被限流),值得第二輪補抓
      short — K 棒不足 120 根(通常是新上市的幣),補抓也沒用
    """
    inst, _last, ch24, turn = item
    r = get(OKX_CANDLES.format(inst=inst, bar=bar))
    if not r or r.get('code') != '0':
        return 'fail', None
    if len(r.get('data', [])) < 130:
        return 'short', None

    rows = list(reversed(r['data']))     # API 回傳新到舊,反轉成舊到新
    closed = rows[:-1]                   # 關鍵:丟掉當下未收盤那根
    c = [float(k[4]) for k in closed]    # 收盤價序列
    v = [float(k[5]) for k in closed]    # 成交量序列
    h = [float(k[2]) for k in closed]    # 最高價序列
    lo = [float(k[3]) for k in closed]   # 最低價序列

    m20, m60, m120 = ma(c, 20), ma(c, 60), ma(c, 120)
    if not m120:                         # K 棒不足 120 根,跳過
        return 'short', None

    base = sum(v[-21:-1]) / 20           # 前 20 根均量(不含自己,避免稀釋)
    volr = v[-1] / base if base else 0

    prev20 = ma(c[:-1], 20)              # 前一根的 MA20,用來看均線方向
    m20_prev20 = ma(c[:-20], 20)         # 20 根前的 MA20
    m60_prev20 = ma(c[:-20], 60)         # 20 根前的 MA60

    hh24 = max(h[-25:-1])                # 前 24 根的最高點(不含自己)
    hh48 = max(h[-49:-1]) if len(h) >= 49 else hh24
    box_hi = max(h[-25:-1])              # 箱頂
    box_lo = min(lo[-25:-1])             # 箱底

    return 'ok', dict(
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
        rsi=rsi(c, 14),
        macd=macd_state(c),                       # >0 = MACD 在訊號線上方
        ma20_up=bool(prev20 and m20 > prev20),    # MA20 是否向上
        above_mid=c[-1] > m60 and c[-1] > m120,   # 價格站上中長期均線
        # MA20 由下往上穿越 MA60(比較 20 根前的相對位置)
        ma_cross=bool(m20_prev20 and m60_prev20
                      and m20 > m60 and m20_prev20 <= m60_prev20),
        # 距離前高:0 代表剛好在前高,正數是已突破,負數是還差多少 %
        dist_hi24=(c[-1] / hh24 - 1) * 100,
        dist_hi48=(c[-1] / hh48 - 1) * 100,
        # 近 24 根的箱體振幅(高低差),越小代表盤整越緊
        box_amp=(box_hi / box_lo - 1) * 100 if box_lo else None,
        # 帶寬百分位要排除最新那根:突破本身就會把帶寬撐開,含進去會讓
        # 「突破前是否處於壓縮」這件事永遠測不出來。
        bbw_pct=bbw_percentile(c[:-1]),
    )


def scan(items, bar):
    """兩輪掃描:併發跑一輪,失敗的再單執行緒慢慢補

    限流是「短時間內請求太密集」造成的,補抓時剩下的量已經很少,
    單執行緒加間隔幾乎都拿得回來。回傳 (結果清單, 統計)
    """
    res, retry, short = [], [], 0
    with cf.ThreadPoolExecutor(WORKERS) as ex:
        for item, (status, r) in zip(items, ex.map(lambda x: measure(x, bar), items)):
            if status == 'ok':
                res.append(r)
            elif status == 'short':
                short += 1
            else:
                retry.append(item)

    first_round = len(res)
    recovered = 0
    for item in retry:                   # 第二輪:序列補抓,每檔間隔避免再被擋
        time.sleep(RETRY_GAP)
        status, r = measure(item, bar)
        if status == 'ok':
            res.append(r)
            recovered += 1
        elif status == 'short':
            short += 1

    stats = dict(first=first_round, recovered=recovered, short=short,
                 lost=len(items) - len(res) - short)   # 兩輪都沒拿到的
    return res, stats


# ───────────────────────── 第三層:篩選 ─────────────────────────

def between(x, lo, hi):
    """區間判斷,x 為 None 時視為不符合"""
    return x is not None and lo <= x <= hi


def pick_classic(res, opt):
    """原始版:均線多頭排列 + 量比放大 + 有動能"""
    key = 'bear' if opt.short else 'bull'
    hits = [x for x in res
            if x[key]
            and x['volr'] > opt.volr
            and (abs(x['ch1']) > opt.ch1 or abs(x['ch4']) > opt.ch4)]
    if opt.max_dist is not None:
        hits = [x for x in hits if abs(x['dist20']) < opt.max_dist]
    return hits


def conds_early(opt):
    """壓縮後突破:先橫盤、帶寬收窄,價格站上箱頂

    抓的是「還沒噴」的位置,所以不看漲幅,改看盤整結構是否被打破。
    """
    return [
        (f"量比>{opt.volr:g}", lambda x: x['volr'] > opt.volr),
        (f"成交額≥{opt.turn/1e6:g}M", lambda x: x['turn'] >= opt.turn),
        (f"箱體<{opt.box_amp:g}%", lambda x: between(x['box_amp'], 0, opt.box_amp)),
        (f"帶寬≤{opt.bbw_pct:g}", lambda x: x['bbw_pct'] is not None
         and x['bbw_pct'] <= opt.bbw_pct),
        ("站上箱頂", lambda x: x['dist_hi24'] > -0.5),   # 剛突破或差 0.5% 內
        ("均線轉多", lambda x: x['ma_cross'] or x['above_mid']),
        ("RSI 50-70", lambda x: between(x['rsi'], 50, 70)),
    ]


def conds_breakout(opt):
    """剛啟動:三個時間尺度都在動,且貼著前高,但還沒噴完"""
    return [
        ("多頭排列", lambda x: x['bull']),
        ("MA20向上", lambda x: x['ma20_up']),
        (f"量比>{opt.volr:g}", lambda x: x['volr'] > opt.volr),
        (f"成交額≥{opt.turn/1e6:g}M", lambda x: x['turn'] >= opt.turn),
        ("1h +1~10%", lambda x: between(x['ch1'], 1, 10)),
        ("4h +3~20%", lambda x: between(x['ch4'], 3, 20)),
        ("24h +5~40%", lambda x: between(x['ch24'], 5, 40)),
        ("距前高<2%", lambda x: x['dist_hi48'] >= -2),
        ("RSI 50-78", lambda x: between(x['rsi'], 50, 78)),
    ]


def conds_chase(opt):
    """追高順勢:已經在噴,只挑量能夠猛、貼著前高的,供觀察回踩"""
    return [
        ("多頭排列", lambda x: x['bull']),
        (f"量比>{max(opt.volr, 3):g}", lambda x: x['volr'] > max(opt.volr, 3)),
        (f"成交額≥{opt.turn/1e6:g}M", lambda x: x['turn'] >= opt.turn),
        ("1h +3~12%", lambda x: between(x['ch1'], 3, 12)),
        ("24h +20~80%", lambda x: between(x['ch24'], 20, 80)),
        ("距前高<2%", lambda x: x['dist_hi48'] >= -2),
        ("RSI 65-85", lambda x: between(x['rsi'], 65, 85)),
    ]


def diagnose(res, opt):
    """每個條件各自有幾檔通過,用來判斷是哪一關把標的殺光

    只看單一條件的通過數(不是漏斗),因為漏斗的順序會影響數字,
    單獨計數才看得出哪個門檻本身開得太小。
    """
    conds = COND_SETS.get(opt.mode)
    if not conds:
        return ''
    parts = [f"{name} {sum(1 for x in res if fn(x))}" for name, fn in conds(opt)]
    return ' ｜ '.join(parts)


COND_SETS = {
    'early': conds_early,
    'breakout': conds_breakout,
    'chase': conds_chase,
}


def by_conds(res, opt):
    """所有條件都成立才留下"""
    conds = COND_SETS[opt.mode](opt)
    return [x for x in res if all(fn(x) for _, fn in conds)]


MODES = {
    'classic': pick_classic,
    'early': by_conds,
    'breakout': by_conds,
    'chase': by_conds,
}

MODE_DESC = {
    'classic': '多頭排列 + 量比',
    'early': '壓縮後突破',
    'breakout': '剛啟動',
    'chase': '追高順勢',
}

# 各模式的預設門檻(未在命令列指定時採用)
MODE_DEFAULTS = {
    'classic': dict(turn=DEF_TURNOVER, volr=2.0),
    'early': dict(turn=1e6, volr=2.0),
    'breakout': dict(turn=2e6, volr=2.0),
    'chase': dict(turn=5e6, volr=3.0),
}


def pick(res, opt):
    hits = MODES[opt.mode](res, opt)
    hits.sort(key=lambda x: -x['volr'])           # 量比大的排前面
    return hits


# ───────────────────────── 輸出 ─────────────────────────

def line_text(x):
    return (f"{x['sym']:10} 價{x['last']:<12.6g} "
            f"1h{x['ch1']:+6.2f}% 4h{x['ch4']:+6.2f}% 12h{x['ch12']:+6.2f}% "
            f"24h{x['ch24']:+6.2f}% 離MA20{x['dist20']:+5.2f}% "
            f"量比{x['volr']:.2f} 24h額{x['turn']/1e6:.2f}M")


def verdict(x):
    """依量比與乖離給判讀,對應 PDF 第六節那張表。回傳 (標籤, 說明)"""
    d = abs(x['dist20'])
    if d >= HOT_DIST:
        return '🔴 末端加速', '乖離過大,追高性價比差,建議等回踩 MA20 附近'
    if x['volr'] >= 4 and d < 10:
        return '🟢 剛啟動', '量能猛、位階乾淨,這是最理想的進場位置'
    if x['volr'] >= 4:
        return '🟡 放量但偏高', '資金介入明顯,但已離短均一段,分批比一次進好'
    if d < 10:
        return '🟢 溫和啟動', '量能剛放大、位階乾淨,可留意後續是否續攻'
    return '🟡 觀察', '多頭整理中還沒噴,先放觀察名單'


def thin_warn(x):
    """成交額太小的提醒門檻:1M USDT 以下滑價風險明顯"""
    return x['turn'] < 1e6


def build_empty_message(scanned, opt):
    """0 檔時的簡短回報,讓你知道系統活著(--always 才會用到)"""
    now = datetime.now(TPE).strftime('%Y-%m-%d %H:%M')
    label = '空頭排列' if opt.short else MODE_DESC[opt.mode]
    return (f"😴 <b>OKX {opt.bar} {label} 掃描</b>\n"
            f"{now} (台北)  掃描 {scanned} 檔,<b>本輪無標的</b>\n"
            f"<i>條件未同時成立,維持觀望。</i>")


def build_message(hits, scanned, opt):
    """組 Telegram 訊息(HTML):數據 + 逐檔判讀 + 本輪重點"""
    now = datetime.now(TPE).strftime('%Y-%m-%d %H:%M')
    label = '空頭排列' if opt.short else MODE_DESC[opt.mode]
    head = (f"📊 <b>OKX {opt.bar} {label} 掃描</b>\n"
            f"{now} (台北)  掃描 {scanned} 檔,符合 <b>{len(hits)}</b> 檔\n"
            f"<i>依量比由大到小排,量比越大代表資金介入越猛</i>\n")

    blocks = []
    for i, x in enumerate(hits, 1):
        tag, why = verdict(x)
        thin = ('\n⚠️ <i>24h 成交額不足 1M,掛單簿薄,進出滑價會吃掉利潤</i>'
                if thin_warn(x) else '')
        blocks.append(
            f"\n<b>{i}. {x['sym']}</b>  {x['last']:.6g}  {tag}\n"
            f"量比 <b>{x['volr']:.2f}</b>ｘ ｜ 離MA20 {x['dist20']:+.2f}%\n"
            f"1h {x['ch1']:+.2f}% ｜ 4h {x['ch4']:+.2f}% ｜ "
            f"12h {x['ch12']:+.2f}% ｜ 24h {x['ch24']:+.2f}%\n"
            f"24h 成交額 {x['turn']/1e6:.2f}M ｜ RSI {x['rsi']:.0f} ｜ "
            f"距前高 {x['dist_hi48']:+.2f}%\n"
            f"→ <i>{why}</i>{thin}\n"
        )

    # 本輪重點:優先挑「量比夠大且乖離乾淨」的,沒有就退回量比最大那檔
    clean = [x for x in hits if abs(x['dist20']) < 10]
    best = clean[0] if clean else hits[0]
    hot = [x['sym'] for x in hits if abs(x['dist20']) >= HOT_DIST]
    summary = (f"\n<b>本輪重點</b>\n"
               f"最值得看的是 <b>{best['sym']}</b>:量比 {best['volr']:.2f}ｘ、"
               f"離 MA20 {best['dist20']:+.2f}%,"
               f"{'量能與位階都在合理範圍' if abs(best['dist20']) < 10 else '但位階已偏高'}。")
    if hot:
        summary += f"\n{'、'.join(hot)} 已進入末端加速區(乖離 ≥{HOT_DIST:g}%),不建議追。"
    else:
        summary += f"\n本輪沒有標的乖離超過 {HOT_DIST:g}%,未觸發追高警示。"

    tail = ("\n\n<i>技術面篩選,只看價格與成交量,不看基本面與消息面,非投資建議。"
            "訊號來自 OKX,下單請以你交易所的實際報價與掛單簿為準;"
            "高槓桿下止損設定比選標的更重要。</i>")
    return head + ''.join(blocks) + summary + tail


# ───────────────────────── main ─────────────────────────

def parse_args(argv):
    p = argparse.ArgumentParser(description='OKX 1H 多頭排列 + 量比掃描')
    p.add_argument('--mode', default='classic', choices=sorted(MODES),
                   help='篩選模式,預設 classic')
    p.add_argument('--bar', default='1H', help='K 線級別,預設 1H(可用 4H)')
    p.add_argument('--turn', type=float, default=None,
                   help='24h 成交額下限 USDT,未指定則用該模式預設值')
    p.add_argument('--volr', type=float, default=None,
                   help='量比門檻,未指定則用該模式預設值')
    p.add_argument('--box-amp', type=float, default=20.0,
                   help='early 模式:近 24 根箱體振幅上限 %%,預設 20')
    p.add_argument('--bbw-pct', type=float, default=0.35,
                   help='early 模式:布林帶寬百分位上限,預設 0.35(近 90 根最窄的三成五)')
    p.add_argument('--ch1', type=float, default=DEF_CH1, help='1 根 K 棒漲幅門檻 %%')
    p.add_argument('--ch4', type=float, default=DEF_CH4, help='4 根 K 棒漲幅門檻 %%')
    p.add_argument('--max-dist', type=float, default=None,
                   help='離 MA20 乖離上限 %%,超過就排除(避開追高)')
    p.add_argument('--short', action='store_true', help='改掃空頭排列')
    p.add_argument('--always', action='store_true',
                   help='0 檔時也發一則「本輪無標的」,預設沒標的就靜默')
    p.add_argument('--dry', action='store_true', help='只印螢幕,不發 Telegram')
    p.add_argument('--json', default='bull_hits.json', help='結果輸出路徑')
    opt = p.parse_args(argv)
    for k, val in MODE_DEFAULTS[opt.mode].items():   # 未指定的門檻用模式預設
        if getattr(opt, k) is None:
            setattr(opt, k, val)
    return opt


def main(argv=None):
    opt = parse_args(argv if argv is not None else sys.argv[1:])

    items = candidates(opt.turn)
    print(f'候選 {len(items)} 檔')

    res, st = scan(items, opt.bar)
    hits = pick(res, opt)
    print(f"第一輪取得 {st['first']},補抓 {st['recovered']},"
          f"K棒不足 {st['short']},仍未取得 {st['lost']}")
    print(f'掃描 {len(res)} 檔,符合 {len(hits)} 檔')
    detail = diagnose(res, opt)          # 各條件單獨的通過數,用來調門檻
    if detail:
        print('各條件通過數:', detail)
    for x in hits:
        print(line_text(x))

    if opt.json:
        with open(opt.json, 'w', encoding='utf-8') as f:
            json.dump(hits, f, ensure_ascii=False, indent=2)

    # 有標的就發;沒標的預設靜默,--always 時改發一則簡短回報
    if not opt.dry:
        if hits:
            send_telegram(build_message(hits, len(res), opt))
        elif opt.always:
            send_telegram(build_empty_message(len(res), opt))

    return 0


if __name__ == '__main__':
    sys.exit(main())
