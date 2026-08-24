"""常駐主迴圈 — 把排程從 GitHub Actions 搬到自己的機器上

為什麼要搬:GitHub 的 cron 會延遲三十分鐘以上,甚至整班丟掉(early 掃描
就這樣默默壞過好幾天)。紙上交易漏一輪只是數字難看,真錢部位漏一輪就是
停損沒移、減碼沒做。這支自己管時間,不看別人臉色。

順帶解決 /pos 的延遲:改用 long-polling 等 Telegram 推訊息,指令從
「等下一次排程」的 5~30 分鐘變成 1 秒內。

設計上刻意不自己實作任何業務邏輯 —— 掃描、出場、統計全部用 subprocess
呼叫既有腳本,跟 tg_poll 的做法一致。好處是這支掛掉不會影響腳本、腳本
出錯也不會拖垮這支,而且業務邏輯永遠只有一份。

CLI 用法:
  python bot_loop.py --bot 2          # 服務 bot2(自動交易頻道)
  python bot_loop.py --bot 1          # 服務 bot1
  python bot_loop.py --bot 2 --no-schedule   # 只收指令,不跑排程
  python bot_loop.py --bot 2 --once   # 跑一輪到期的排程就結束(測試用)
"""
import argparse
import json
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

import notify
import tg_poll

TPE = timezone(timedelta(hours=8))

POLL_TIMEOUT = 20        # long-polling 等待秒數:有訊息就立刻回,沒有就等
LOOP_GAP = 2             # 出錯後的重試間隔,避免壞掉時瘋狂重試洗版
SIGNALS = 'signals.jsonl'

# 排程表。key 是「這一輪的識別字串」—— 同一個 key 只會跑一次,所以
# 迴圈每幾秒醒來檢查都不會重複執行。用時鐘算 key 而不是記「上次跑完的
# 時間」,程式重啟後也不會把同一輪重跑一遍。
def jobs(opt):
    return [
        ('breakout 掃描', lambda now: now.strftime('%Y%m%d%H'),
         ['scan_bull.py', '--mode', 'breakout', '--record', SIGNALS]),
        ('early 掃描', lambda now: now.strftime('%Y%m%d%H'),
         ['scan_bull.py', '--mode', 'early', '--record', SIGNALS]),
        ('出場監控', lambda now: now.strftime('%Y%m%d%H'),
         ['positions.py']),
        # 每天台北 09:15 一次:key 只到日,所以一天只會觸發一次
        ('每日統計', lambda now: now.strftime('%Y%m%d') if now.hour == 9 else None,
         ['track.py', '--telegram']),
    ]


def run(cmd, bot):
    """執行腳本;TG_BOT 傳下去,結果才會發到對的頻道"""
    import os
    print(f'[{stamp()}] ▶ 執行 {" ".join(cmd)}', flush=True)
    env = dict(os.environ, TG_BOT=str(bot))
    try:
        p = subprocess.run([sys.executable] + cmd, capture_output=True,
                           text=True, env=env, timeout=600)
        tail = (p.stdout or '').strip().splitlines()
        for line in tail[-6:]:
            print(f'    {line}', flush=True)
        if p.returncode != 0:
            print(f'    ✗ exit {p.returncode}: {(p.stderr or "")[-400:]}',
                  flush=True)
        return p.returncode
    except subprocess.TimeoutExpired:
        print('    ✗ 執行超過 10 分鐘,放棄這一輪', flush=True)
        return -1


def stamp():
    return datetime.now(TPE).strftime('%m-%d %H:%M:%S')


def poll(offset):
    """long-polling 取訊息;回傳 (訊息清單, 新的 offset)

    連線本來就會被中斷(逾時、網路抖動),所以任何例外都只是回空清單,
    由外層迴圈重試 —— 收信失敗不該讓整支程式結束。
    """
    params = {'timeout': POLL_TIMEOUT, 'allowed_updates': json.dumps(['message'])}
    if offset:
        params['offset'] = offset
    url = tg_poll.API + 'getUpdates?' + urllib.parse.urlencode(params)
    try:
        with urllib.request.urlopen(url, timeout=POLL_TIMEOUT + 15) as r:
            data = json.load(r)
    except Exception as e:
        print(f'[{stamp()}] 收信失敗({type(e).__name__}),稍後重試', flush=True)
        time.sleep(LOOP_GAP)
        return [], offset
    if not data.get('ok'):
        # webhook 還掛著會回 409,解除後才收得到
        if data.get('error_code') == 409:
            print('偵測到 webhook 佔用,解除中', flush=True)
            tg_poll.api('deleteWebhook')
        time.sleep(LOOP_GAP)
        return [], offset
    ups = data.get('result', [])
    if ups:
        offset = ups[-1]['update_id'] + 1
    return ups, offset


def handle_updates(ups, chat_id):
    for u in ups:
        msg = u.get('message') or {}
        if str(msg.get('chat', {}).get('id')) != str(chat_id):
            continue                       # 只認自己的對話
        text = msg.get('text', '')
        try:
            action = tg_poll.handle(text)
        except Exception:
            traceback.print_exc()
            continue
        if action:
            print(f'[{stamp()}] 指令 {text[:30]} → {action}', flush=True)


def due_jobs(table, state, now):
    """回傳這一刻該跑的工作,並就地更新已跑過的記錄"""
    out = []
    for name, keyfn, cmd in table:
        key = keyfn(now)
        if key is None or state.get(name) == key:
            continue
        state[name] = key
        out.append((name, cmd))
    return out


def main(argv=None):
    p = argparse.ArgumentParser(description='常駐主迴圈')
    p.add_argument('--bot', type=int, default=2, choices=(1, 2),
                   help='服務哪一隻機器人,預設 2(自動交易頻道)')
    p.add_argument('--no-schedule', action='store_true',
                   help='只收指令,不跑排程(排程還留在 Actions 時用)')
    p.add_argument('--once', action='store_true', help='跑完到期的排程就結束')
    opt = p.parse_args(argv if argv is not None else sys.argv[1:])

    if not tg_poll.use_bot(opt.bot):
        tok, chat = notify.BOTS[opt.bot]
        print(f'{tok} / {chat} 沒設定,無法啟動')
        return 1

    table = jobs(opt)
    # 啟動時先把當下這一輪標記成跑過,否則重開機就會立刻補跑一次掃描,
    # 對已經過去的 K 棒發出遲到的訊號。
    state = {name: keyfn(datetime.now(TPE)) for name, keyfn, _ in table}
    print(f'[{stamp()}] 啟動:服務 bot{opt.bot}、'
          f'{"不跑排程" if opt.no_schedule else f"{len(table)} 項排程"}',
          flush=True)
    tg_poll.send(f'🟢 <b>常駐服務已啟動</b>\n<i>{stamp()} 台北</i>')

    offset = None
    while True:
        try:
            ups, offset = poll(offset)
            handle_updates(ups, tg_poll.CHAT_ID)

            if not opt.no_schedule:
                for name, cmd in due_jobs(table, state, datetime.now(TPE)):
                    print(f'[{stamp()}] ⏰ {name}', flush=True)
                    run(cmd, opt.bot)
            if opt.once:
                return 0
        except KeyboardInterrupt:
            print('收到中斷,結束')
            return 0
        except Exception:
            # 主迴圈絕對不能死。任何沒預期到的錯誤都印出來然後繼續,
            # 因為停掉的代價是持倉沒人看管。
            traceback.print_exc()
            time.sleep(LOOP_GAP)


if __name__ == '__main__':
    sys.exit(main())
