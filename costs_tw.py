"""台股零股交易成本 — 決定策略可不可行的那一層

小資金做零股,勝負不在選股,在成本。台股來回要付:
  買進手續費 0.1425% × 券商折扣,且有「單筆低消」
  賣出手續費 0.1425% × 券商折扣,同樣有低消
  證交稅     0.3%(只在賣出時收)

低消才是關鍵。本金 1000 元、低消 20 元的話,來回光手續費就是 40 元
= 本金的 4%,再加證交稅 0.3%,等於每筆單要先漲 4.3% 才回本 —— 任何
突破策略都撐不住這個門檻。低消 1 元的券商則是 0.5%,才有得玩。

所以這支不是週邊工具,它是篩選條件的一部分:
  1. breakeven_pct() 算出「這筆單要漲幾 % 才不賠」
  2. scan_tw 用它擋掉成本佔比過高的標的(--max-cost)
  3. 也用它當停損距離的下限(--cost-mult):停損若比成本門檻還窄,
     期望值必為負,跟 exits.DEF_MIN_RISK 的道理相同,只是台股的
     數字不一樣(加密貨幣來回 0.2%,台股零股至少 0.5%)。

費率是法定的,折扣與低消各券商不同,所以後兩者做成參數。
"""

FEE_RATE = 0.001425     # 法定手續費率(單邊)
TAX_RATE = 0.003        # 證交稅,只在賣出時收(一般股票;ETF 為 0.1%)
ETF_TAX_RATE = 0.001

DEF_DISCOUNT = 0.6      # 券商手續費折扣,電子下單常見 6 折
DEF_FEE_MIN = 1.0       # 單筆低消(元)。零股低消 1 元的券商才適合小資金
DEF_CAPITAL = 1000.0    # 起手資金(元)


def shares_for(price, capital=DEF_CAPITAL):
    """這筆資金買得起幾股(零股以 1 股為單位)。買不起 1 股回 0"""
    if price <= 0:
        return 0
    return int(capital // price)


def fee(amount, discount=DEF_DISCOUNT, fee_min=DEF_FEE_MIN):
    """單邊手續費:金額 × 費率 × 折扣,但不低於低消"""
    return max(amount * FEE_RATE * discount, fee_min)


def round_trip(price, shares, discount=DEF_DISCOUNT, fee_min=DEF_FEE_MIN,
               etf=False):
    """來回總成本(元):買手續費 + 賣手續費 + 證交稅

    賣出價未知,保守以買進價估算 —— 賺錢時實際稅費會更高,所以這個
    估算是樂觀方向的下限,拿來當門檻剛好偏嚴。
    """
    amount = price * shares
    if amount <= 0:
        return 0.0
    tax = amount * (ETF_TAX_RATE if etf else TAX_RATE)
    return fee(amount, discount, fee_min) * 2 + tax


def breakeven_pct(price, capital=DEF_CAPITAL, discount=DEF_DISCOUNT,
                  fee_min=DEF_FEE_MIN, etf=False):
    """這筆單要漲幾 % 才打平。買不起 1 股回 None

    回傳的是「佔投入金額」的百分比,不是佔本金 —— 零股買不滿本金時
    (例如 1000 元買 300 元的股票只能買 3 股 = 900 元),用投入金額算
    才是這筆單真正要跨過的門檻。
    """
    n = shares_for(price, capital)
    if n < 1:
        return None
    amount = price * n
    return round_trip(price, n, discount, fee_min, etf) / amount * 100


def cost_text(price, capital=DEF_CAPITAL, discount=DEF_DISCOUNT,
              fee_min=DEF_FEE_MIN, etf=False):
    """推播用的一行成本說明"""
    n = shares_for(price, capital)
    if n < 1:
        return f'買不起 1 股(股價 {price:.2f} > 本金 {capital:.0f})'
    be = breakeven_pct(price, capital, discount, fee_min, etf)
    return (f"{capital:.0f} 元可買 <b>{n}</b> 股(投入 {price * n:.0f} 元),"
            f"來回成本 {round_trip(price, n, discount, fee_min, etf):.1f} 元 "
            f"= <b>{be:.2f}%</b>")
