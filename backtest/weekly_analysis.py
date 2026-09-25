"""本周（2026-08-31 ~ 2026-09-04）因子效果分析。

数据源：
  - 东财全市场 5552 只，截至 2026-09-01（本周覆盖 8/31、9/1）
  - 网站库 daily_bars 72 只，截至 2026-09-04（本周覆盖 8/31-9/4）

分析：
  1. 全市场截面：因子在 8/31、9/1 的本周分布（top/bottom decile 的次日收益）
  2. 网站库 72 只：本周 5 天因子值变化与当日收益（实际发生了什么）
  3. 关键：东财截至今年的滚动 IC（近期月分解看因子是否"近期变弱/变强"）
"""
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/ubuntu/stock-quant")
from factors import (
    factor_divergence_tscorr_slope_20_60_close as F1,
    factor_shock_skew_rank_20_60_volume as F2,
)

WEEK = ["2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
OUT = Path("/home/ubuntu/stock-quant/results/weekly_analysis.json")

print("=== 1. 东财全市场本周因子截面 ===", flush=True)
import glob, os
files = sorted(glob.glob("/home/ubuntu/data/eastmoney_data/*.csv"))
parts = []
for f in files:
    df = pd.read_csv(f, encoding="utf-8-sig")
    df = df[["date", "open", "close", "high", "low", "volume"]]
    df["ticker"] = os.path.basename(f).split("_")[0]
    parts.append(df)
d = pd.concat(parts, ignore_index=True)
d["date"] = pd.to_datetime(d["date"])
d = d[d["date"] >= "2021-01-01"].sort_values(["ticker", "date"]).reset_index(drop=True)
print(f"[em] rows={len(d)} tickers={d['ticker'].nunique()} dates={d['date'].nunique()}",
      flush=True)
g = d.groupby("ticker", sort=False)
d["open_next"] = g["open"].shift(-1)
d["ret_next"] = g["close"].shift(-1) / d["close"] - 1.0
d["f1"] = F1(d)
d["f2"] = F2(d)

week = d[d["date"].isin(pd.to_datetime(WEEK[:2]))].copy()   # 东财只有 8/31、9/1
print(f"[em] 本周覆盖日期: {sorted(week['date'].dt.strftime('%Y-%m-%d').unique())}",
      flush=True)
res = {}
for dstr in [x for x in WEEK[:2] if x in set(week['date'].dt.strftime('%Y-%m-%d'))]:
    sub = week[week["date"].dt.strftime("%Y-%m-%d") == dstr].dropna(
        subset=["f1", "f2", "ret_next"])
    if sub.empty:
        continue
    for col, nm in (("f1", "f1"), ("f2", "f2")):
        # 分位组收益（组1=因子最小=看多，组10=因子最大=看空）
        qs = sub[col].rank(pct=True)
        grp = (qs * 10).clip(0, 9).astype(int)
        rets = sub.groupby(grp)["ret_next"].mean()
        print(f"[em] {dstr} {nm}: 组1(T+1)={rets.get(0, np.nan):.4f} "
              f"组10(T+1)={rets.get(9, np.nan):.4f} "
              f"多空={rets.get(0, np.nan)-rets.get(9, np.nan):.4f} "
              f"个股数={len(sub)}", flush=True)
        res.setdefault(dstr, {})[nm] = {
            "quantile1": float(rets.get(0, np.nan)),
            "quantile10": float(rets.get(9, np.nan)),
            "ls": float(rets.get(0, np.nan) - rets.get(9, np.nan)),
            "n": int(len(sub)),
        }

print("\n=== 2. 网站库 72 只本周实际表现（因子方向 vs 当日收益）===", flush=True)
con = sqlite3.connect("/home/ubuntu/stock-alert/data/stockdesk.db")
sd = pd.read_sql("SELECT symbol, date, close, open, volume FROM daily_bars "
                 "WHERE date >= '2026-01-01'", con)
sd = sd.rename(columns={"symbol": "ticker"})
sd["date"] = pd.to_datetime(sd["date"])
sd = sd.sort_values(["ticker", "date"]).reset_index(drop=True)
sg = sd.groupby("ticker", sort=False)
sd["ret"] = sg["close"].pct_change()
sd["f1"] = F1(sd)
sd["f2"] = F2(sd)
thisweek = sd[sd["date"].dt.strftime("%Y-%m-%d").isin(WEEK)].copy()
site = {}
for _, row in thisweek.iterrows():
    dstr = row["date"].strftime("%Y-%m-%d")
    site.setdefault(dstr, {})
for dstr in WEEK:
    sub = thisweek[thisweek["date"].dt.strftime("%Y-%m-%d") == dstr].dropna(
        subset=["f1", "f2"])
    if sub.empty:
        print(f"[site] {dstr}: 无数据", flush=True)
        continue
    # 因子横截面rank -> 本周"当天收益"与因子方向的关系
    for col, nm in (("f1", "f1"), ("f2", "f2")):
        rk = sub[col].rank(pct=True)
        corr1 = rk.corr(sub["ret"]) if sub["ret"].notna().sum() > 2 else np.nan
        print(f"[site] {dstr} {nm}: 因子与当日收益相关={corr1:.3f} "
              f"n={len(sub)}", flush=True)
        site.setdefault(dstr, {})[nm] = {
            "corr_with_day_ret": round(float(corr1), 4) if not pd.isna(corr1) else None,
            "n": int(len(sub)),
        }

# 3) 东财近 12 周周均 IC（因子近端有效性）
print("\n=== 3. 全市场近 12 周周均 IC（因子近端是否衰减）===", flush=True)
d_w = d.copy()
d_w["week"] = d_w["date"].dt.to_period("W")
ic_recs = []
for wk, sub in d_w.groupby("week"):
    sub = sub.dropna(subset=["f1", "f2", "ret_next"])
    if len(sub) < 200:
        continue
    ic1 = sub["f1"].rank().corr(sub["ret_next"].rank())
    ic2 = sub["f2"].rank().corr(sub["ret_next"].rank())
    ic_recs.append((str(wk), ic1, ic2))
ic_df = pd.DataFrame(ic_recs, columns=["week", "f1", "f2"]).tail(12)
for _, r in ic_df.iterrows():
    print(f"[ic-week] {r['week']}: f1_IC={r['f1']:+.4f} f2_IC={r['f2']:+.4f}", flush=True)

# 保存
with open(OUT, "w") as fh:
    json.dump({"eastmoney_week": res, "site_week": site,
               "weekly_ic_tail12": ic_df.to_dict("records")},
              fh, ensure_ascii=False, indent=2, default=str)
print(f"\n已保存 -> {OUT}", flush=True)
