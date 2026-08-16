"""出場規則 — 回測與實盤共用的唯一一份

掃描器只回答「進不進場」,這支回答「什麼時候該走」。規則寫在這裡而不是
各自實作,理由跟 backtest 沿用 scan_bull.conds_* 一樣:兩邊若各寫一份,
回測驗證的就不是實際會通知你出場的那套邏輯。

三類出場,依優先序:
  1. 停損     — 棒內最低價碰到停損價就算出場(不等收盤)
  2. 訊號失效 — 進場理由消失(跌回箱頂 / MA20 轉弱 / RSI 由熱轉弱),
                收盤才判定,避免被棒內雜訊掃出去
  3. 時間出場 — 抱滿 N 根還沒被觸發就平倉,資金卡住也是成本

停損價的算法:以 ATR 為主、結構(訊號那根 K 棒的低點)為輔,取兩者中
「離進場價較遠」的那個。純結構停損常常貼價格太近,一根雜訊就被掃掉;
用 ATR 當下限可以確保停損至少有一個波動幅度的呼吸空間。

這裡只做停損與失效出場,沒有停利與移動停利。
"""
from collections import namedtuple

DEF_STOP_ATR = 1.5      # 停損距離:進場價 - N × ATR(14)
DEF_MAX_BARS = 24       # 時間出場:抱滿幾根 K 棒
RSI_HOT = 70            # RSI 曾經衝到這之上,才算「過熱」
RSI_COLD = 50           # 過熱後跌破這條,視為動能熄火

Cfg = namedtuple('Cfg', 'stop_atr max_bars invalidate')
Cfg.__new__.__defaults__ = (DEF_STOP_ATR, DEF_MAX_BARS, True)

# 出場原因,同時當作 Telegram 顯示文字
STOP = '停損'
BOX = '跌回箱頂'
MA20 = 'MA20 轉弱'
RSI = 'RSI 轉弱'
TIME = '時間出場'

REASONS = (STOP, BOX, MA20, RSI, TIME)


def cfg_from(opt):
    """從 argparse 的 Namespace 取出出場設定"""
    return Cfg(stop_atr=getattr(opt, 'stop_atr', DEF_STOP_ATR),
               max_bars=getattr(opt, 'max_bars', DEF_MAX_BARS),
               invalidate=not getattr(opt, 'no_invalidate', False))


def stop_price(bar, cfg=Cfg()):
    """算停損價;ATR 算不出來(K 棒不足)時退回純結構停損,再不行回 None

    bar 是 scan_bull.measure() 或 backtest.series() 產出的那個 dict,
    兩邊欄位一致,所以這個函式對實盤與回測都適用。
    """
    entry = bar['last']
    cands = []
    if bar.get('atr'):
        cands.append(entry - cfg.stop_atr * bar['atr'])
    if bar.get('low'):
        cands.append(bar['low'])
    if not cands:
        return None
    stop = min(cands)                 # 取較遠的那個 = 較寬的停損
    return stop if 0 < stop < entry else None


def open_position(bar, cfg=Cfg()):
    """把一根命中的 K 棒轉成持倉狀態

    box_hi 記的是進場當下的箱頂(前 24 根高點)。突破後它就是這筆單的
    地板:收盤跌回去代表突破失敗,理由消失了。

    但只有「收盤真的站上箱頂」才適用。chase 與 breakout 允許收盤在前高
    下方 2% 就進場,那種單進場當下就已經在箱頂之下,拿它當地板會在下一
    根立刻誤判出場 —— 這類單就不設這條,交給停損與 MA20 管。
    """
    box_hi = bar.get('box_hi')
    if not box_hi or bar['last'] <= box_hi:
        box_hi = None
    return {
        'sym': bar['sym'],
        'entry_ts': bar['ts'],
        'entry': bar['last'],
        'stop': stop_price(bar, cfg),
        'box_hi': box_hi,
        'atr': bar.get('atr'),
        'hot': (bar.get('rsi') or 0) >= RSI_HOT,   # 進場時就已過熱
        'bars': 0,
    }


def risk_pct(pos):
    """停損距離佔進場價的百分比,也就是這筆單的 1R"""
    if not pos.get('stop') or not pos.get('entry'):
        return None
    return (1 - pos['stop'] / pos['entry']) * 100


def step(pos, bar, cfg=Cfg()):
    """吃進下一根 K 棒,回傳 (原因, 出場價);還不用走就回 None

    會就地更新 pos 的 bars 與 hot,所以同一筆持倉要按時間順序餵。
    """
    pos['bars'] += 1

    # 1. 停損:用棒內最低價判定。收盤價早就跌破卻當作沒事,回測會比實際好看
    if pos.get('stop') and bar['low'] <= pos['stop']:
        # 整根都在停損之下代表跳空,停損價根本成交不到,保守改用收盤價
        px = pos['stop'] if bar['high'] >= pos['stop'] else bar['last']
        return STOP, px

    if (bar.get('rsi') or 0) >= RSI_HOT:
        pos['hot'] = True

    # 2. 訊號失效:收盤價判定
    if cfg.invalidate:
        close = bar['last']
        if pos.get('box_hi') and close < pos['box_hi']:
            return BOX, close
        ma, prev = bar.get('ma20'), bar.get('ma20_prev')
        if ma and prev and ma < prev and close < ma:
            return MA20, close
        if pos['hot'] and bar.get('rsi') is not None and bar['rsi'] < RSI_COLD:
            return RSI, close

    # 3. 時間出場
    if pos['bars'] >= cfg.max_bars:
        return TIME, bar['last']
    return None


def plan_text(pos):
    """進場推播用的出場計畫一行字"""
    if not pos.get('stop'):
        return '出場計畫:ATR 算不出來,無法給停損價,建議略過'
    r = risk_pct(pos)
    box = (f"、收盤跌破箱頂 {pos['box_hi']:.6g}" if pos.get('box_hi') else '')
    return (f"停損 <b>{pos['stop']:.6g}</b>(-{r:.2f}%){box};"
            f"或 MA20 轉弱、RSI 由熱轉弱即出場")
