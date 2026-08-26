#!/usr/bin/env python3
"""出貨明細表單價稽核。

用法：
    python3 check_prices.py <出貨明細表.xlsx> [更多檔案或資料夾 ...]

會針對每一筆出貨明細跑五項檢查，把有問題的列出來；
全部通過時 exit code 0，有任何異常時 exit code 1。
"""
import csv
import glob
import os
import sys
from collections import defaultdict

try:
    import openpyxl
except ImportError:
    sys.exit("請先安裝 openpyxl： pip install openpyxl")

MASTER = os.path.join(os.path.dirname(os.path.abspath(__file__)), "price_master.csv")
HEADER_KEY = "客戶代號"


def load_master(path=MASTER):
    """讀標準單價表，回傳 {品號: (品名, 單位, 標準單價)}。"""
    master = {}
    with open(path, encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            master[row["品號"].strip()] = (
                row["品名"].strip(),
                row["單位"].strip(),
                float(row["標準單價"]),
            )
    return master


def num(value):
    """把儲存格轉成 float，轉不動就回 None（空白、文字、None 都會走這裡）。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value).replace(",", "").strip())
    except ValueError:
        return None


def read_rows(path):
    """讀出一份出貨明細表的所有明細列。每個工作表各有一組表頭。"""
    rows = []
    workbook = openpyxl.load_workbook(path, data_only=True)
    for sheet in workbook:
        header = None
        for raw in sheet.iter_rows(values_only=True):
            if raw and raw[0] == HEADER_KEY:
                header = raw
                continue
            if not header or raw[0] is None:
                continue
            row = dict(zip(header, raw))
            row["_檔案"] = os.path.basename(path)
            row["_工作表"] = sheet.title
            rows.append(row)
    return rows


def dedupe(rows):
    """同一張出貨單會重複出現在多份累計檔裡，只留一份。"""
    seen = {}
    for row in rows:
        key = (
            row.get("出退貨單號"),
            row.get("品號"),
            row.get("交貨量"),
            row.get("單價"),
            row.get("金額"),
            row.get("出退貨日"),
        )
        seen.setdefault(key, row)
    return list(seen.values())


def where(row):
    return f"{row['_檔案']}[{row['_工作表']}] {row.get('出退貨日')} {row.get('客戶簡稱')} {row.get('出退貨單號')}"


def check(rows, master):
    """跑完所有檢查，回傳問題字串清單。"""
    problems = []

    # 1. 單價要等於標準價；順便抓不在價目表裡的品號
    for row in rows:
        code = str(row.get("品號") or "").strip()
        unit_price = num(row.get("單價"))
        if code not in master:
            problems.append(f"[未知品號] {where(row)} 品號{code} {row.get('品名')}")
            continue
        name, unit, standard = master[code]
        if unit_price is None or abs(unit_price - standard) > 0.005:
            qty = num(row.get("交貨量")) or 0
            # 用帳上實際金額跟「標準價應開的金額」相減，數字才對得上折讓金額
            booked = num(row.get("金額"))
            should_be = round(standard * qty)
            gap = (booked - should_be) if booked is not None else 0
            problems.append(
                f"[單價不符] {where(row)} {name} 單價{unit_price} 應為{standard} "
                f"(量{qty:g}，金額{booked:,.0f} 應為{should_be:,.0f}，差 {gap:+,.0f})"
            )
        if row.get("單位") and row["單位"] != unit:
            problems.append(
                f"[單位不符] {where(row)} {name} 單位{row['單位']} 應為{unit}"
            )

    # 2. 單價 x 交貨量 要等於金額（容許四捨五入的 1 元）
    for row in rows:
        qty, unit_price, amount = (
            num(row.get("交貨量")),
            num(row.get("單價")),
            num(row.get("金額")),
        )
        if None in (qty, unit_price, amount):
            continue
        if abs(qty * unit_price - amount) > 1:
            problems.append(
                f"[金額不符] {where(row)} {row.get('品名')} "
                f"{qty:g}x{unit_price}={qty * unit_price:,.2f} 但金額為{amount:,.0f}"
            )

    # 3. 單據層級：明細合計、稅額 5%、含稅金額三者要對得起來
    orders = defaultdict(list)
    for row in rows:
        orders[row.get("出退貨單號")].append(row)
    for order_no, lines in sorted(orders.items(), key=lambda kv: str(kv[0])):
        head = lines[0]
        detail = sum(num(l.get("金額")) or 0 for l in lines)
        discount = sum(num(l.get("折扣金額")) or 0 for l in lines)
        untaxed, tax, total = (
            num(head.get("未稅金額")),
            num(head.get("稅額")),
            num(head.get("銷貨金額(含稅)")),
        )
        if untaxed is not None and abs(detail - discount - untaxed) > 1:
            problems.append(
                f"[表頭不符] {where(head)} 明細合計{detail - discount:,.0f} "
                f"但未稅金額為{untaxed:,.0f}"
            )
        if None not in (untaxed, tax) and abs(round(untaxed * 0.05) - tax) > 1:
            problems.append(
                f"[稅額不符] {where(head)} 稅額{tax:,.0f} 應約{round(untaxed * 0.05):,.0f}"
            )
        if None not in (untaxed, tax, total) and abs(untaxed + tax - total) > 1:
            problems.append(
                f"[含稅不符] {where(head)} 含稅{total:,.0f} != {untaxed:,.0f}+{tax:,.0f}"
            )

    # 4. 同一客戶同一品項如果出現兩種單價，就算兩個都在價目表上也要示警
    by_customer = defaultdict(set)
    for row in rows:
        by_customer[(row.get("客戶代號"), row.get("客戶簡稱"), row.get("品名"))].add(
            num(row.get("單價"))
        )
    for (code, short_name, name), prices in sorted(
        by_customer.items(), key=lambda kv: str(kv[0])
    ):
        if len(prices) > 1:
            problems.append(
                f"[客戶多價] {short_name}({code}) {name} 出現 {sorted(prices)}"
            )

    # 5. 零負值
    for row in rows:
        qty, unit_price = num(row.get("交貨量")), num(row.get("單價"))
        if (unit_price or 0) <= 0 or (qty or 0) <= 0:
            problems.append(
                f"[零負值] {where(row)} {row.get('品名')} 量{qty} 單價{unit_price}"
            )

    return problems


def expand(args):
    """把資料夾展開成裡面的 xlsx。"""
    paths = []
    for arg in args:
        if os.path.isdir(arg):
            paths.extend(sorted(glob.glob(os.path.join(arg, "*.xlsx"))))
        else:
            paths.append(arg)
    return paths


def main(argv):
    paths = expand(argv[1:])
    if not paths:
        sys.exit(__doc__)

    master = load_master()
    rows = []
    for path in paths:
        rows.extend(read_rows(path))
    rows = dedupe(rows)

    dates = sorted(d for d in {r.get("出退貨日") for r in rows} if d)
    print(f"檔案 {len(paths)} 份，明細 {len(rows)} 筆，客戶 {len({r.get('客戶代號') for r in rows})} 家")
    if dates:
        print(f"出退貨日 {dates[0]} ~ {dates[-1]}")
    print()

    problems = check(rows, master)
    if not problems:
        print("✅ 全部通過，沒有發現單價異常。")
        return 0

    print(f"❌ 發現 {len(problems)} 項異常：")
    for problem in problems:
        print(" ", problem)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
