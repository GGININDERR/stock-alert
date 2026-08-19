"""出場規則 — 回測與實盤共用的唯一一份

掃描器只回答「進不進場」,這支回答「什麼時候該走」。規則寫在這裡而不是
各自實作,理由跟 backtest 沿用 scan_bull.conds_* 一樣:兩邊若各寫一份,
回測驗證的就不是實際會通知你出場的那套邏輯。

出場規則,依一根 K 棒內的判定順序:
  1. 停損     — 棒內最低價碰到停損價就出場(不等收盤)
  2. 第一目標 — 獲利達 1R 時出掉一半,剩下的把停損拉到成本價
  3. 移動停利 — 出過第一批之後,停損改跟著持倉最高價 - 2×ATR 往上抬
  4. 訊號失效 — 進場理由消失(跌回箱頂 / MA20 轉弱 / RSI 由熱轉弱),
                收盤才判定,避免被棒內雜訊掃出去
  5. 時間出場 — 抱滿 N 根還沒被觸發就平倉,資金卡住也是成本

停損價的算法:以 ATR 為主、結構(訊號那根 K 棒的低點)為輔,取兩者中
「離進場價較遠」的那個。純結構停損常常貼價格太近,一根雜訊就被掃掉;
用 ATR 當下限可以確保停損至少有一個波動幅度的呼吸空間。

停利不用固定目標價,而是把停損往上推:到 1R 出一半並移到成本價(這筆
單從此不會賠),之後改用移動停利跟著跑。所以「第一目標」以外的停利,
最後都是以停損的形式成交,只是名字不同(保本出場 / 移動停利)。

R 指的是這筆單的風險單位,也就是進場價到停損價的距離。用 R 計價才不會
被幾檔高波動幣的大百分比帶著跑。
"""
from collections import namedtuple

DEF_STOP_ATR = 1.5      # 停損距離:進場價 - N × ATR(14)
DEF_MIN_RISK = 1.0      # 最小停損距離(%)的通用值:低於此值的訊號直接不出
# 依據是算術而非回測:手續費來回約 0.2%,停損距離若只有零點幾 %(波動
# 死掉的標的、或代幣化股票在美股收盤後),一個跳動就掃掉,期望值必為負。
#
# 但這個值不能全模式共用。early 專挑「箱體收窄、帶寬壓縮」的標的,低波動
# 是它的進場條件而非缺陷,停損距離天生就窄(實盤中位數 1.0%,breakout 是
# 4.5%)。1.0% 會砍掉 early 近半訊號,所以各模式的值寫在 scan_bull
# .MODE_DEFAULTS,這裡只當沒有模式資訊時的退路。
DEF_MAX_BARS = 24       # 時間出場:抱滿幾根 K 棒
DEF_TP1_R = 1.0         # 第一目標:獲利達 N 個 R(R = 停損距離)
DEF_TP1_FRAC = 0.5      # 到第一目標時出掉的比例
DEF_TRAIL_ATR = 2.0     # 移動停利:持倉最高價 - N × ATR
RSI_HOT = 70            # RSI 曾經衝到這之上,才算「過熱」
RSI_COLD = 50           # 過熱後跌破這條,視為動能熄火

Cfg = namedtuple('Cfg', 'stop_atr max_bars invalidate tp1_r tp1_frac trail_atr')
Cfg.__new__.__defaults__ = (DEF_STOP_ATR, DEF_MAX_BARS, True,
                            DEF_TP1_R, DEF_TP1_FRAC, DEF_TRAIL_ATR)

# 出場原因,同時當作 Telegram 顯示文字
STOP = '停損'
TP1 = '第一目標'
BE = '保本出場'
TRAIL = '移動停利'
BOX = '跌回箱頂'
MA20 = 'MA20 轉弱'
RSI = 'RSI 轉弱'
TIME = '時間出場'

REASONS = (STOP, TP1, BE, TRAIL, BOX, MA20, RSI, TIME)

# 計畫用的圖示,與「已出場」的圖示刻意分開:🛑 是紅燈,擺在還沒發生的
# 計畫旁邊會讀成「這檔別碰」;🛡 講的是這個價格在保護你,語氣才對。
# 真的被掃出去時仍然用 🛑(見 positions.ICON),那時候紅燈是準確的。
PLAN_STOP = '🛡'
PLAN_TP = '🎯'

# 停損價是怎麼來的,決定最後那筆出場要掛哪一個名字
KIND_REASON = {'init': STOP, 'breakeven': BE, 'trail': TRAIL}


def cfg_from(opt):
    """從 argparse 的 Namespace 取出出場設定"""
    return Cfg(stop_atr=getattr(opt, 'stop_atr', DEF_STOP_ATR),
               max_bars=getattr(opt, 'max_bars', DEF_MAX_BARS),
               invalidate=not getattr(opt, 'no_invalidate', False),
               tp1_r=0.0 if getattr(opt, 'no_tp', False)
               else getattr(opt, 'tp1_r', DEF_TP1_R),
               tp1_frac=getattr(opt, 'tp1_frac', DEF_TP1_FRAC),
               trail_atr=getattr(opt, 'trail_atr', DEF_TRAIL_ATR))


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
        'stop_kind': 'init',        # 停損價的來源:init / breakeven / trail
        'peak': bar['last'],        # 持倉期間的最高價,移動停利用
        'left': 1.0,                # 還沒出掉的部位比例
        'partials': [],             # 已分批出場的 (原因, 價格, 比例)
    }


def risk_pct(pos):
    """停損距離佔進場價的百分比,也就是這筆單的 1R"""
    if not pos.get('stop') or not pos.get('entry'):
        return None
    return (1 - pos['stop'] / pos['entry']) * 100


def tp1_price(pos, cfg=Cfg()):
    """第一目標價 = 進場價 + N×R;沒有停損價就沒有 R,也就沒有目標"""
    if not pos.get('stop') or cfg.tp1_r <= 0:
        return None
    return pos['entry'] + cfg.tp1_r * (pos['entry'] - pos['stop'])


def total_ret(pos, exit_px):
    """把分批出場與最後出場加權成一個報酬率(%)"""
    realized = sum(frac * (px / pos['entry'] - 1)
                   for _, px, frac in pos.get('partials', []))
    realized += pos.get('left', 1.0) * (exit_px / pos['entry'] - 1)
    return realized * 100


def step(pos, bar, cfg=Cfg()):
    """吃進下一根 K 棒,回傳 (原因, 出場價);還不用走就回 None

    會就地更新 pos 的狀態(bars / hot / stop / 分批出場),所以同一筆持倉
    要按時間順序餵,而且只能餵一次。

    一根 K 棒內的先後順序看不出來,凡是同一根同時碰到停損與停利,一律
    當作先碰停損 —— 猜對自己有利的那個順序,回測就會比實際好看。
    """
    pos['bars'] += 1

    # 1. 停損:用棒內最低價判定。收盤價早就跌破卻當作沒事,回測會比實際好看
    #    停損價會被保本與移動停利往上推,所以這裡也是停利的出場點
    if pos.get('stop') and bar['low'] <= pos['stop']:
        # 整根都在停損之下代表跳空,停損價根本成交不到,保守改用收盤價
        px = pos['stop'] if bar['high'] >= pos['stop'] else bar['last']
        return KIND_REASON[pos.get('stop_kind', 'init')], px

    if (bar.get('rsi') or 0) >= RSI_HOT:
        pos['hot'] = True

    # 2. 第一目標:出掉一部分,剩下的把停損拉到成本價
    #    勝率不到五成的突破策略,先落袋一半、把這筆單變成不會賠,
    #    對整體期望值的改善比把目標訂更遠有用
    tp = tp1_price(pos, cfg)
    if tp and not pos['partials'] and bar['high'] >= tp:
        pos['partials'].append((TP1, tp, cfg.tp1_frac))
        pos['left'] = round(pos['left'] - cfg.tp1_frac, 10)
        if pos['stop'] < pos['entry']:
            pos['stop'] = pos['entry']
            pos['stop_kind'] = 'breakeven'
        if pos['left'] <= 0:                  # 一次全出就沒有剩下的了
            return TP1, tp

    # 3. 移動停利:出過第一批之後才啟動,跟著持倉最高價往上抬
    #    一進場就跟,價格還在原地晃就會被掃掉;等確認有一段獲利再跟
    pos['peak'] = max(pos['peak'], bar['high'])
    if pos['partials'] and cfg.trail_atr > 0 and pos.get('atr'):
        trail = pos['peak'] - cfg.trail_atr * pos['atr']
        if pos['stop'] is None or trail > pos['stop']:
            pos['stop'] = trail
            pos['stop_kind'] = 'trail'

    # 4. 訊號失效:收盤價判定
    if cfg.invalidate:
        close = bar['last']
        if pos.get('box_hi') and close < pos['box_hi']:
            return BOX, close
        ma, prev = bar.get('ma20'), bar.get('ma20_prev')
        if ma and prev and ma < prev and close < ma:
            return MA20, close
        if pos['hot'] and bar.get('rsi') is not None and bar['rsi'] < RSI_COLD:
            return RSI, close

    # 5. 時間出場
    if pos['bars'] >= cfg.max_bars:
        return TIME, bar['last']
    return None


def plan_text(pos, cfg=Cfg()):
    """進場推播用的出場計畫

    一行一件事,每行都以「動作 + 價格」開頭:掃描結果本來就是一堆數字,
    出場計畫若擠成一整段,真正要照著做的那兩個價格會被淹掉。順序照實際
    會發生的先後:先停損,再失效條件,最後停利。
    """
    if not pos.get('stop'):
        return '⚠️ <i>ATR 算不出來,給不了停損價,這檔建議略過</i>'
    r = risk_pct(pos)
    lines = [f"{PLAN_STOP} 停損 <b>{pos['stop']:.6g}</b>  <i>(-{r:.2f}%)</i>"]

    invalid = []
    if pos.get('box_hi'):
        invalid.append(f"收盤跌破箱頂 {pos['box_hi']:.6g}")
    invalid += ['MA20 轉弱', 'RSI 由熱轉弱']
    lines.append(f"　　<i>或 {' ／ '.join(invalid)} 即出場</i>")

    tp = tp1_price(pos, cfg)
    if tp:
        lines.append(
            f"{PLAN_TP} 目標 <b>{tp:.6g}</b>  "
            f"<i>(+{(tp / pos['entry'] - 1) * 100:.2f}%)</i> "
            f"出 {cfg.tp1_frac:.0%},停損移到成本 {pos['entry']:.6g}")
        lines.append(f"　　<i>之後用最高價 -{cfg.trail_atr:g}×ATR 跟著跑</i>")
    return '\n'.join(lines)
