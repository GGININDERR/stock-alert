# -*- coding: utf-8 -*-
"""機加酒自動化篩選主程式。

流程:讀 config → 逐條件抓 Trip.com → 篩選 → 過濾已通知過的 → LINE 推播。

用法:
  python -m trip_alert.main            # 正式跑
  TRIP_MOCK=1 python -m trip_alert.main  # 用假資料測整條流程
"""
import json
import os

from . import notify
from .filters import filter_packages
from .scraper import fetch_packages

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "config.json")
SEEN_PATH = os.path.join(HERE, "seen.json")


def _load_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _save_json(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def run():
    config = _load_json(CONFIG_PATH, {"搜尋條件": []})
    seen = set(_load_json(SEEN_PATH, []))
    new_seen = set(seen)
    total_notified = 0

    for cond in config.get("搜尋條件", []):
        print(f"== 搜尋:{cond['名稱']} ==")
        try:
            packages = fetch_packages(cond)
        except Exception as e:  # noqa: BLE001
            print(f"  抓取失敗:{e}")
            continue

        hits = filter_packages(packages, cond)
        print(f"  抓到 {len(packages)} 筆,符合 {len(hits)} 筆")

        for pkg in hits:
            if pkg["id"] in seen:
                continue  # 之前通知過,略過
            notify.send(notify.format_package(cond, pkg))
            new_seen.add(pkg["id"])
            total_notified += 1

    _save_json(SEEN_PATH, sorted(new_seen))
    print(f"本次新通知 {total_notified} 筆。")


if __name__ == "__main__":
    run()
