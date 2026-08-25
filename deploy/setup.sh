#!/usr/bin/env bash
# 常駐主機一鍵安裝 — Ubuntu 與 Oracle Linux 都支援
#
# 用法:在機器上執行
#   curl -fsSL https://raw.githubusercontent.com/GGININDERR/stock-alert/main/deploy/setup.sh | bash
#
# 兩種發行版的差別只有套件管理員與預設使用者(ubuntu / opc),其餘完全相同。
# 自動偵測而不是要你選,是因為選錯的代價是裝到一半失敗,而這一步的使用者
# 通常正是最不想處理這種問題的人。
set -euo pipefail

REPO=https://github.com/GGININDERR/stock-alert.git
DIR=$HOME/stock-alert
USER_NAME=$(id -un)

echo "▶ 偵測系統"
if command -v apt-get >/dev/null 2>&1; then
  PKG=apt
  echo "  Ubuntu/Debian(使用者 $USER_NAME)"
elif command -v dnf >/dev/null 2>&1; then
  PKG=dnf
  echo "  Oracle Linux/RHEL(使用者 $USER_NAME)"
else
  echo "✗ 找不到 apt 或 dnf,不支援這個系統"; exit 1
fi

# 1GB 的機器跑 dnf 會被 OOM 殺掉(實測 Oracle Linux + E2.1.Micro 就是這樣)。
# 先補一個 swap 檔,裝完之後留著也無妨 —— 常駐服務本身只吃 30MB,
# 有 swap 只是讓偶爾的尖峰不會直接把行程殺掉。
ensure_swap() {
  local mem_mb swap_mb
  mem_mb=$(free -m | awk '/^Mem:/{print $2}')
  swap_mb=$(free -m | awk '/^Swap:/{print $2}')
  if [ "$mem_mb" -ge 1800 ] || [ "$swap_mb" -ge 1000 ]; then
    echo "  記憶體 ${mem_mb}MB、swap ${swap_mb}MB,不需要額外配置"
    return
  fi
  # 已經掛上就什麼都不用做
  if swapon --show 2>/dev/null | grep -q '/swapfile'; then
    echo "  /swapfile 已啟用"
    return
  fi
  if [ ! -f /swapfile ]; then
    echo "  記憶體只有 ${mem_mb}MB,建立 2GB swap(約 10~30 秒)"
    sudo fallocate -l 2G /swapfile 2>/dev/null || \
      sudo dd if=/dev/zero of=/swapfile bs=1M count=2048 status=none
  else
    echo "  /swapfile 已存在但未啟用,重新格式化"
  fi
  # 檔案可能是上一次跑到一半留下的,沒有 swap 標頭,所以一律重做一次。
  # mkswap 的參數在各發行版不一致(Oracle Linux 的版本沒有 -q),只用最基本的形式。
  sudo chmod 600 /swapfile
  sudo mkswap /swapfile >/dev/null
  sudo swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || \
    echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
  echo "  swap 已啟用:$(free -m | awk '/^Swap:/{print $2}')MB"
}

# 只裝缺的。Oracle Linux 的映像檔通常已經有 python3 與 git,重裝一次
# 只是白白吃記憶體,而記憶體正是這台最缺的東西。
missing_pkgs() {
  local out=()
  command -v python3 >/dev/null || out+=("$1")
  command -v git >/dev/null || out+=(git)
  python3 -c 'import ensurepip' >/dev/null 2>&1 || out+=("$2")
  echo "${out[@]}"
}

echo "▶ 檢查記憶體"
ensure_swap

echo "▶ 安裝系統套件"
if [ "$PKG" = apt ]; then
  NEED=$(missing_pkgs python3 python3-venv)
  if [ -n "$NEED" ]; then
    sudo apt-get update -qq
    sudo apt-get install -y -qq $NEED python3-pip
  else
    echo "  已具備,略過"
  fi
else
  NEED=$(missing_pkgs python3 python3-pip)
  if [ -n "$NEED" ]; then
    sudo dnf install -y -q $NEED
  else
    echo "  已具備,略過"
  fi
fi

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
# 服務檔裡的使用者與路徑依實際環境產生,不寫死 —— 寫死 ubuntu 的話,
# 在 Oracle Linux(使用者 opc)上會靜靜地啟動失敗。
sed -e "s#__USER__#$USER_NAME#g" -e "s#__DIR__#$DIR#g" \
    deploy/stark-bot.service | sudo tee /etc/systemd/system/stark-bot.service >/dev/null
sudo systemctl daemon-reload

cat <<DONE

────────────────────────────────────────
安裝完成。接下來兩步:

1) 填金鑰
     nano $DIR/.env
   把 TELEGRAM_TOKEN_2 和 TELEGRAM_CHAT_ID_2 填上
   (Ctrl+O 存檔、Enter、Ctrl+X 離開)

2) 啟動
     sudo systemctl enable --now stark-bot
     journalctl -u stark-bot -f      # 看即時日誌,Ctrl+C 離開

常用指令:
     sudo systemctl restart stark-bot   # 改完程式後重啟
     sudo systemctl stop stark-bot      # 停止
     sudo systemctl status stark-bot    # 看狀態
     cd $DIR && git pull && sudo systemctl restart stark-bot   # 更新
────────────────────────────────────────
DONE
