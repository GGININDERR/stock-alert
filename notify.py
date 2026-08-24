"""Telegram 推播 — 支援兩隻機器人

bot1 是原本那隻(掃描訊號),bot2 給自動交易用。分開的理由是隔離:
自動交易還在驗證階段,它的錯誤訊息、下單回報、緊急停止,都不該混進你
每天在看的訊號頻道;真的出事也不會汙染原本能用的東西。

目標由環境變數 TG_BOT 決定(1 或 2),預設 1 —— 所以現有腳本一個字
都不用改,行為完全不變。要讓某支腳本改發給 bot2,只要在執行它的時候
把 TG_BOT=2 放進環境即可,不必為每支腳本都加參數。

環境變數:
  TELEGRAM_TOKEN    / TELEGRAM_CHAT_ID     bot1
  TELEGRAM_TOKEN_2  / TELEGRAM_CHAT_ID_2   bot2
  TG_BOT            預設要發給哪一隻(1/2),未設為 1
"""
import os

import requests

API = 'https://api.telegram.org/bot{token}/sendMessage'

# 每隻機器人的環境變數名稱。bot2 沒設定時整個是 None,呼叫端才好判斷
# 「還沒接上」與「發送失敗」的差別。
BOTS = {
    1: ('TELEGRAM_TOKEN', 'TELEGRAM_CHAT_ID'),
    2: ('TELEGRAM_TOKEN_2', 'TELEGRAM_CHAT_ID_2'),
}


def default_bot():
    """目前預設要發給哪一隻;吃不懂的值一律當 1,不要因為打錯字就靜音"""
    try:
        n = int(os.environ.get('TG_BOT', '1'))
    except ValueError:
        return 1
    return n if n in BOTS else 1


def creds(bot=None):
    """回傳 (token, chat_id);沒設定回 (None, None)"""
    tok_env, chat_env = BOTS.get(bot or default_bot(), BOTS[1])
    return os.environ.get(tok_env), os.environ.get(chat_env)


def configured(bot=None):
    """這隻機器人有沒有設定好 —— 部署到一半時用來判斷該不該發"""
    return all(creds(bot))


def send(message, bot=None):
    """送出訊息;沒設定或失敗回 False(不丟例外,推播失敗不該中斷主流程)"""
    token, chat_id = creds(bot)
    if not token or not chat_id:
        return False
    try:
        r = requests.post(API.format(token=token), timeout=30, json={
            'chat_id': chat_id,
            'text': message,
            'parse_mode': 'HTML',
            'disable_web_page_preview': True,
        })
        return r.ok
    except Exception:
        return False
