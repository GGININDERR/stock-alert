# -*- coding: utf-8 -*-
"""LINE 通知(Messaging API 的 push message)。

⚠️ LINE Notify 已於 2025/3/31 停止服務,本模組改用 LINE Messaging API。
需要的環境變數:
  LINE_CHANNEL_TOKEN  — Messaging API channel 的 access token
  LINE_USER_ID        — 你的 userId(把自己加為 bot 好友後可取得)
"""
import os
import requests

PUSH_URL = "https://api.line.me/v2/bot/message/push"


def send(text):
    token = os.getenv("LINE_CHANNEL_TOKEN")
    user_id = os.getenv("LINE_USER_ID")
    if not token or not user_id:
        print("[notify] 未設定 LINE_CHANNEL_TOKEN / LINE_USER_ID,改印出訊息:\n" + text)
        return False

    resp = requests.post(
        PUSH_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "to": user_id,
            # LINE 單則文字上限 5000 字
            "messages": [{"type": "text", "text": text[:4900]}],
        },
        timeout=20,
    )
    if resp.status_code != 200:
        print(f"[notify] LINE 推播失敗 {resp.status_code}: {resp.text}")
        return False
    return True


def format_package(cond, pkg):
    return (
        f"✈️🏨 {cond['名稱']} 找到方案!\n"
        f"────────────\n"
        f"{pkg['標題']}(★{pkg['飯店星級']})\n"
        f"💰 總價 NT${pkg['總價']:,}\n"
        f"🛫 去程 {pkg['去程出發']} / 🛬 回程 {pkg['回程出發']}\n"
        f"航空:{pkg['航空公司']}\n"
        f"{pkg['連結']}"
    )
