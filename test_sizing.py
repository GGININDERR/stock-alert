"""驗證下單參數的算法 — 這是唯一會直接決定你下多少錢的程式碼

錯一個位數就是真金白銀,所以每條規則都要有測試把它釘住。
"""
import sizing


def approx(a, b, tol=1e-6):
    return abs(a - b) < tol


def test_risk_governs():
    """停損遠 → 部位小,而且虧損正好等於設定的風險金額"""
    qty, notional, risk, capped = sizing.plan(100, 90, risk_usd=20,
                                              max_notional=10000)
    assert approx(qty, 2.0), qty            # 20 ÷ (100-90)
    assert approx(notional, 200)
    assert approx(risk, 20) and not capped


def test_tighter_stop_gives_bigger_position():
    """同樣的風險金額,停損越近部位越大 —— 這正是固定金額下單做錯的地方"""
    wide = sizing.plan(100, 90, 20, 1e9)[0]     # 停損 10%
    tight = sizing.plan(100, 99, 20, 1e9)[0]    # 停損 1%
    assert approx(tight / wide, 10), (tight, wide)


def test_cap_binds_and_risk_shrinks():
    """部位上限綁住時,實際風險必須跟著變小,不能還宣稱冒 20 USDT"""
    qty, notional, risk, capped = sizing.plan(100, 99, risk_usd=20,
                                              max_notional=500)
    assert capped and approx(notional, 500)
    assert approx(qty, 5.0)
    assert approx(risk, 5.0)                # 5 顆 × 1 元停損距離
    assert risk < 20


def test_bad_input():
    """停損在進場價之上(或相等)是無效的單,不能算出負數量"""
    assert sizing.plan(100, 100) is None
    assert sizing.plan(100, 110) is None
    assert sizing.plan(0, -1) is None


def test_no_ticket_without_valid_stop():
    assert sizing.ticket_text('X', 100, 100) == ''


def test_small_price_precision():
    """小幣一顆不到 0.001,數量不能被取整成 0"""
    qty = sizing.plan(0.00004, 0.000039, 20, 1e9)[0]
    assert qty > 1e6
    assert sizing.fmt_qty(0.00123) == '0.00123'


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print('✓', fn.__name__)
    print(f'\n{len(fns)} 項全過')
