"""驗證雙機器人的路由 — 錯了就是訊息發到錯的頻道,而且很難查"""
import os
import notify
import tg_poll


def with_env(**kw):
    old = {k: os.environ.get(k) for k in kw}
    os.environ.update({k: v for k, v in kw.items() if v is not None})
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
    return old


def test_default_is_bot1():
    """沒設 TG_BOT 時必須維持原行為 —— 現有腳本一個字都沒改"""
    with_env(TG_BOT=None)
    assert notify.default_bot() == 1


def test_tg_bot_switches():
    with_env(TG_BOT='2')
    assert notify.default_bot() == 2


def test_bad_tg_bot_falls_back():
    """打錯字不能導致靜音,一律退回 bot1"""
    for bad in ('abc', '', '9', '-1'):
        with_env(TG_BOT=bad)
        assert notify.default_bot() == 1, bad


def test_creds_are_separate():
    """兩隻機器人的金鑰不能互相污染"""
    with_env(TELEGRAM_TOKEN='t1', TELEGRAM_CHAT_ID='c1',
             TELEGRAM_TOKEN_2='t2', TELEGRAM_CHAT_ID_2='c2', TG_BOT=None)
    assert notify.creds(1) == ('t1', 'c1')
    assert notify.creds(2) == ('t2', 'c2')


def test_configured_detects_missing():
    with_env(TELEGRAM_TOKEN_2=None, TELEGRAM_CHAT_ID_2=None)
    assert not notify.configured(2)


def test_send_without_config_returns_false():
    """沒設定時回 False,不能丟例外中斷主流程"""
    with_env(TELEGRAM_TOKEN_2=None, TELEGRAM_CHAT_ID_2=None)
    assert notify.send('x', bot=2) is False


def test_tg_poll_switches_bot():
    with_env(TELEGRAM_TOKEN='t1', TELEGRAM_CHAT_ID='c1',
             TELEGRAM_TOKEN_2='t2', TELEGRAM_CHAT_ID_2='c2')
    assert tg_poll.use_bot(2) and tg_poll.TOKEN == 't2' and tg_poll.BOT == 2
    assert 'bott2/' in tg_poll.API or tg_poll.API.endswith('bott2/')
    assert tg_poll.use_bot(1) and tg_poll.TOKEN == 't1' and tg_poll.BOT == 1


def test_scan_bull_still_works():
    """既有模組的 send_telegram 介面不能變"""
    import scan_bull
    assert callable(scan_bull.send_telegram)


if __name__ == '__main__':
    fns = [v for k, v in sorted(globals().items()) if k.startswith('test_')]
    for fn in fns:
        fn()
        print('✓', fn.__name__)
    print(f'\n{len(fns)} 項全過')
