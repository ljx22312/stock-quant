"""数据审计：/home/ubuntu/data/eastmoney_data 全A日线。

审计项：
1. 文件数 / 唯一代码 / 字段一致性
2. 日期完整性：每只股票逐年交易日数（发现停牌/缺日）、最早/最晚日期、是否有重复日期
3. 数值异常：open/high/low/close 是否为 0 或负、high<max(open,close) 或 low>min(open,close)（复权错误信号）、volume/amount 为 0 的比例
4. 与网站主库交叉验证：相同 symbol 在重叠日期上的 close/volume 是否一致（判断复权口径是否一致）
5. 复权口径：用 EPS/分红无法直接验证，但与网站库比对 + 检查历史价格是否"整体偏移"来推断
6. 输出：统计摘要 JSON + 异常清单 CSV（便于后续决定剔除哪些股票/日期）
"""
import csv
import glob
import json
import os
import sqlite3
from collections import Counter
from pathlib import Path

DATA_DIR = Path("/home/ubuntu/data/eastmoney_data")
OUT_DIR = Path("/home/ubuntu/stock-quant/audit")
OUT_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = "/home/ubuntu/stock-alert/data/stockdesk.db"

files = sorted(DATA_DIR.glob("*.csv"))
print(f"文件数: {len(files)}", flush=True)

issues = []          # 异常记录
summary = {}
summary["n_files"] = len(files)
summary["fields_sets"] = Counter()
summary["n_rows"] = 0
summary["dup_date_rows"] = 0
summary["zero_volume_rows"] = 0
summary["zero_amount_rows"] = 0
summary["bad_price_rows"] = 0
summary["bad_ohlc_rows"] = 0          # 高低价关系违反
summary["nan_rows"] = 0
summary["min_date"] = None
summary["max_date"] = None
first_years = Counter()
per_symbol = []        # (code, nrows, first, last)
codes = set()

for f in files:
    code = f.stem.split("_")[0]
    codes.add(code)
    with open(f, encoding="utf-8-sig", newline="") as fh:
        r = csv.reader(fh)
        header = next(r, None)
        summary["fields_sets"][tuple(header)] += 1
        rows = []
        seen_dates = set()
        nrows = 0
        dup = 0
        zvol = zamt = badp = badoh = nan_ = 0
        first = last = None
        for row in r:
            if not row or len(row) < 7:
                continue
            nrows += 1
            d, o, c, h, l, v, a = row[:7]
            if first is None:
                first = d
            last = d
            if d in seen_dates:
                dup += 1
            seen_dates.add(d)
            try:
                o, c, h, l = float(o), float(c), float(h), float(l)
                v = float(v); a = float(a)
            except ValueError:
                nan_ += 1
                continue
            if v <= 0:
                zvol += 1
            if a <= 0:
                zamt += 1
            if min(o, c, h, l) <= 0 or any(x != x for x in (o, c, h, l)):
                badp += 1
            if h < max(o, c) or l > min(o, c):
                badoh += 1
        summary["n_rows"] += nrows
        summary["dup_date_rows"] += dup
        summary["zero_volume_rows"] += zvol
        summary["zero_amount_rows"] += zamt
        summary["bad_price_rows"] += badp
        summary["bad_ohlc_rows"] += badoh
        summary["nan_rows"] += nan_
        if summary["min_date"] is None or (first and first < summary["min_date"]):
            summary["min_date"] = first
        if summary["max_date"] is None or (last and last > summary["max_date"]):
            summary["max_date"] = last
        if first:
            first_years[first[:4]] += 1
        per_symbol.append((code, nrows, first or "", last or ""))
        if dup or badp or badoh > 3:
            issues.append({"file": f.stem, "rows": nrows, "dup": dup,
                           "bad_ohlc": badoh, "bad_price": badp,
                           "zero_volume": zvol, "first": first, "last": last})

summary["n_symbols"] = len(codes)
summary["first_year_dist"] = dict(sorted(first_years.items()))

# 每年平均交易日统计（用全市场合并的日期频率）
print("=== 数据基本盘 ===", flush=True)
print(json.dumps({k: v for k, v in summary.items() if k not in ("first_year_dist", "fields_sets")},
                 ensure_ascii=False, indent=2, default=str), flush=True)
print("字段集合:", {str(k): v for k, v in summary["fields_sets"].items()}, flush=True)

# 异常写入文件
with open(OUT_DIR / "issues.csv", "w", newline="") as fh:
    w = csv.DictWriter(fh, fieldnames=["file", "rows", "dup", "bad_ohlc", "bad_price",
                                       "zero_volume", "first", "last"])
    w.writeheader()
    for it in issues:
        w.writerow(it)
print(f"异常文件数（写 issues.csv）: {len(issues)}", flush=True)

# ---------- 与网站主库交叉验证 ----------
print("=== 与网站主库 daily_bars 交叉验证（复权口径）===", flush=True)
con = sqlite3.connect(DB_PATH)
con.row_factory = sqlite3.Row
rows = con.execute(
    "SELECT symbol, date, close FROM daily_bars WHERE date >= '2025-01-01'").fetchall()
site = {}
for r in rows:
    site[(r["symbol"], r["date"])] = r["close"]

# 取网站池中存在于 eastmoney 的股票，比对最近 5 个重叠日期
sample_codes = ["000001", "601169", "600036", "000858", "300750"]
cmp = []
for code in sample_codes:
    fpath = DATA_DIR / f"{code}_*.csv"
    matches = list(DATA_DIR.glob(f"{code}_*.csv"))
    if not matches:
        continue
    with open(matches[0], encoding="utf-8-sig", newline="") as fh:
        r = csv.reader(fh)
        next(r, None)
        em = {}
        for row in r:
            if row:
                em[row[0]] = float(row[2])  # close
    common = [d for d in list(em)[:80] if (code, d) in site]
    for d in sorted(common)[:5]:
        s1, s2 = em[d], site[(code, d)]
        cmp.append({"code": code, "date": d, "eastmoney_close": s1, "site_close": s2,
                    "diff_pct": round((s1 - s2) / s2 * 100, 4) if s2 else None})
for c in cmp:
    print(c, flush=True)

with open(OUT_DIR / "crosscheck.json", "w") as fh:
    json.dump({"summary": {k: str(v) for k, v in summary.items()
                           if k not in ("first_year_dist", "fields_sets")},
               "first_year_dist": summary["first_year_dist"],
               "crosscheck": cmp},
              fh, ensure_ascii=False, indent=2, default=str)
print("审计完成 ->", OUT_DIR, flush=True)
