#!/usr/bin/env bash
# Oracle Cloud(Ubuntu)一鍵安裝
#
# 用法:在機器上執行
#   curl -fsSL https://raw.githubusercontent.com/GGININDERR/stock-alert/main/deploy/setup.sh | bash
#
# 做完之後還要編輯 .env 填金鑰,再啟動服務 —— 腳本最後會告訴你怎麼做。
set -euo pipefail

REPO=https://github.com/GGININDERR/stock-alert.git
DIR=$HOME/stock-alert

echo "▶ 安裝系統套件"
sudo apt-get update -qq
sudo apt-get install -y -qq python3-venv python3-pip git

echo "▶ 取得程式碼"
if [ -d "$DIR/.git" ]; then
  git -C "$DIR" pull --ff-only
else
  git clone --depth 1 "$REPO" "$DIR"
fi
cd "$DIR"

echo "▶ 建立虛擬環境並安裝相依套件"
python3 -m venv .venv
./.venv/bin/pip install -q --upgrade pip
./.venv/bin/pip install -q -r requirements.txt

echo "▶ 準備環境變數檔"
if [ ! -f .env ]; then
  cat > .env <<'ENVEOF'
# bot1:原本的訊號機器人(要讓這台也服務 bot1 才需要填)
TELEGRAM_TOKEN=
TELEGRAM_CHAT_ID=

# bot2:自動交易專用 ← 先填這兩個
TELEGRAM_TOKEN_2=
TELEGRAM_CHAT_ID_2=

# 派網 API(還沒要用,先留空。權限只開唯讀,絕不要開提現)
PIONEX_KEY=
PIONEX_SECRET=
ENVEOF
  chmod 600 .env      # 只有自己讀得到,金鑰不外流
  echo "  已建立 .env(權限 600)"
else
  echo "  .env 已存在,保留原內容"
fi

echo "▶ 安裝 systemd 服務"
sudo cp deploy/stark-bot.service /etc/systemd/system/stark-bot.service
sudo systemctl daemon-reload

cat <<'DONE'

────────────────────────────────────────
安裝完成。接下來兩步:

1) 填金鑰
     nano ~/stock-alert/.env
   把 TELEGRAM_TOKEN_2 和 TELEGRAM_CHAT_ID_2 填上
   (Ctrl+O 存檔、Enter、Ctrl+X 離開)

2) 啟動
     sudo systemctl enable --now stark-bot
     journalctl -u stark-bot -f      # 看即時日誌,Ctrl+C 離開

常用指令:
     sudo systemctl restart stark-bot   # 改完程式後重啟
     sudo systemctl stop stark-bot      # 停止
     sudo systemctl status stark-bot    # 看狀態
     cd ~/stock-alert && git pull && sudo systemctl restart stark-bot   # 更新
────────────────────────────────────────
DONE
