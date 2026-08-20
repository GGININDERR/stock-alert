"""把訊號換算成可以直接照著下的單子 — 半自動下單用

掃描器給的是「買什麼、停損在哪」,但沒回答「買多少」。而這題才是最容易
做錯的:憑感覺每筆都下一樣的金額,等於停損越近的單風險越小、越遠的單風
險越大,完全反過來。

正確的作法是固定「每筆願意虧多少錢」,再由停損距離反推數量:

    數量 = 每筆風險金額 ÷ (進場價 − 停損價)

這樣不管停損是 1% 還是 4.5%,一筆單被掃掉就是虧同一個數字,績效才會由
勝率與賺賠比決定,而不是由「剛好下多大」決定。

但這個公式有個陷阱:停損越近、算出來的部位越大。early 的停損常常只有
1%,20 USDT 的風險會換算成 2000 USDT 的部位——小幣的掛單簿吃不下,滑價
會把優勢吃光。所以另外設一個部位上限,兩者取小的,並且明講是哪一個綁住
了你,免得以為自己還在冒 20 USDT 的風險。
"""
import os

DEF_RISK = 20.0          # 每筆願意虧的金額(USDT)
DEF_MAX_NOTIONAL = 500.0  # 單筆部位上限(USDT):停損太近時避免部位爆掉


def env_float(name, fallback):
    """讓風險參數能用 repo variable 設定,不必改程式"""
    try:
        return float(os.environ[name])
    except (KeyError, ValueError):
        return fallback


def plan(entry, stop, risk_usd=DEF_RISK, max_notional=DEF_MAX_NOTIONAL):
    """回傳 (數量, 部位金額, 實際風險金額, 是否被上限綁住);算不出來回 None"""
    dist = entry - stop
    if dist <= 0 or entry <= 0:
        return None
    qty = risk_usd / dist
    notional = qty * entry
    capped = notional > max_notional
    if capped:                      # 部位上限優先,實際風險因此變小
        qty = max_notional / entry
        notional = max_notional
    return qty, notional, qty * dist, capped


def fmt_qty(q):
    """數量的顯示精度:交易所的最小下單單位各幣不同,這裡只求好讀

    刻意不四捨五入到整數 —— 小幣一顆不到 0.001 USDT,取整會讓部位差很多。
    """
    if q >= 1000:
        return f'{q:,.0f}'
    if q >= 1:
        return f'{q:,.2f}'
    return f'{q:.4g}'


def ticket_text(sym, entry, stop, target=None,
                risk_usd=DEF_RISK, max_notional=DEF_MAX_NOTIONAL):
    """下單參數區塊(Telegram HTML);算不出來回空字串"""
    got = plan(entry, stop, risk_usd, max_notional)
    if not got:
        return ''
    qty, notional, risk_actual, capped = got
    head = (f"部位上限 {max_notional:g} USDT 綁住,實際風險 "
            f"{risk_actual:.1f} USDT" if capped
            else f"風險 {risk_usd:g} USDT")
    out = (f"\n📋 <b>下單參數</b> <i>({head})</i>\n"
           f"<code>{sym}</code>  買 <b>{fmt_qty(qty)}</b> 顆"
           f"  ≈ {notional:.0f} USDT\n"
           f"進場 {entry:.6g} ｜ 停損 <b>{stop:.6g}</b>")
    if target:
        out += f" ｜ 目標 {target:.6g}"
    return out + '\n'
