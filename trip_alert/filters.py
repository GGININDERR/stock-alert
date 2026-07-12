# -*- coding: utf-8 -*-
"""依 config 條件篩選機加酒方案。純函式,好測試。"""


def _hhmm(t):
    """'HH:MM' -> 分鐘數,方便比較時間先後。"""
    try:
        h, m = t.split(":")
        return int(h) * 60 + int(m)
    except (ValueError, AttributeError):
        return -1


def match(pkg, cond):
    """單一方案是否符合單一搜尋條件。回傳 (bool, 原因)。"""
    # 價格上限
    limit = cond.get("最高總價_台幣")
    if limit and pkg["總價"] > limit:
        return False, f"超過預算 ({pkg['總價']} > {limit})"

    # 飯店星級
    min_star = cond.get("最少飯店星級")
    if min_star and pkg["飯店星級"] < min_star:
        return False, f"星級不足 ({pkg['飯店星級']} < {min_star})"

    # 去程最晚出發
    dep_limit = cond.get("去程最晚出發時間")
    if dep_limit and _hhmm(pkg["去程出發"]) > _hhmm(dep_limit):
        return False, f"去程太晚 ({pkg['去程出發']} > {dep_limit})"

    # 回程最早出發(避免玩不到最後一天)
    ret_min = cond.get("回程最早出發時間")
    if ret_min and _hhmm(pkg["回程出發"]) < _hhmm(ret_min):
        return False, f"回程太早 ({pkg['回程出發']} < {ret_min})"

    title = pkg.get("標題", "")

    # 排除關鍵字
    for kw in cond.get("排除關鍵字", []):
        if kw in title:
            return False, f"含排除字「{kw}」"

    # 飯店關鍵字(若有設定,至少要命中一個)
    keys = cond.get("飯店關鍵字", [])
    if keys and not any(k in title for k in keys):
        return False, "未命中指定地區關鍵字"

    return True, "符合"


def filter_packages(packages, cond):
    """回傳符合條件的方案清單(依總價由低到高排序)。"""
    hits = [p for p in packages if match(p, cond)[0]]
    return sorted(hits, key=lambda p: p["總價"])
