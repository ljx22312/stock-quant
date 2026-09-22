#!/usr/bin/env python3
"""全市场单日 IC 复盘：因子(9/4截面) vs 实际收益(9/7)，附 9/2-9/7 逐日 IC 序列。

用法: python3 fullmarket_today_ic.py  （后台运行约 5 分钟）
输出: results/fullmarket_today_ic.json + 控制台
"""
import json
import os
import sys
import time
import warnings
from pathlib import Path

warnings.filterwarnings("ignore")
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factors import (  # noqa: E402
    factor_divergence_tscorr_slope_20_60_close as F1,
    factor_shock_skew_rank_20_60_volume as F2,
)

DATA = Path("/home/ubuntu/data/eastmoney_data")
OUT = Path(__file__).parent / "results"
START = "2022-01-01"
EVAL_DAYS = ["2026-08-24", "2026-08-25", "2026-08-26", "2026-08-27", "2026-08-28",
             "2026-08-31", "2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04",
             "2026-09-07"]


def main():
    t0 = time.time()
    files = sorted(DATA.glob("*.csv"))
    print(f"[load] {len(files)} files", flush=True)
    parts = []
    for i, f in enumerate(files):
        df = pd.read_csv(f, encoding="utf-8-sig")
        df["ticker"] = os.path.basename(f).split("_")[0]
        parts.append(df)
        if (i + 1) % 2000 == 0:
            print(f"[load] {i+1}/{len(files)} {time.time()-t0:.0f}s", flush=True)
    d = pd.concat(parts, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] >= START].sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] {len(d):,} rows {d['ticker'].nunique()} stocks "
          f"{d['date'].min().date()}..{d['date'].max().date()} {time.time()-t0:.0f}s", flush=True)

    t1 = time.time()
    d["f1"] = F1(d)
    d["f2"] = F2(d)
    print(f"[factors] {time.time()-t1:.0f}s", flush=True)

    g = d.groupby("ticker", sort=False)
    d["ret_next"] = g["close"].shift(-1) / d["close"] - 1.0
    d["ret_today"] = g["close"].shift(1)
    d["ret_today"] = d["close"] / d["ret_today"] - 1.0  # 当日收益(收对收)

    out = {"n_stocks": int(d["ticker"].nunique()),
           "data_range": [str(d["date"].min().date()), str(d["date"].max().date())]}

    # ---------- 逐日全市场 IC 序列 ----------
    ic_rows = []
    for day in EVAL_DAYS:
        dt = pd.Timestamp(day)
        sub = d[d["date"] == dt]
        if not len(sub):
            continue
        sub2 = sub.dropna(subset=["f1", "ret_next"])
        r = {"date": day, "n": int(len(sub2)),
             "up_ratio": float((sub["ret_today"] > 0).mean()),
             "median_ret": float(sub["ret_today"].median())}
        for f in ("f1", "f2"):
            s = sub2.dropna(subset=[f])
            ic = s[f].rank().corr(s["ret_next"].rank())
            r[f"ic_{f}"] = float(ic)
        ic_rows.append(r)
        print(f"[ic] {day}: f1={r.get('ic_f1', float('nan')):+.3f} "
              f"f2={r.get('ic_f2', float('nan')):+.3f} "
              f"宽度={r['up_ratio']:.0%} 中位={r['median_ret']:+.2%}", flush=True)
    out["daily_ic"] = ic_rows

    # ---------- 9/4 信号 -> 9/7 收益：全市场十分位 ----------
    sig = d[d["date"] == pd.Timestamp("2026-09-04")].dropna(subset=["f1", "f2"]).copy()
    sig = sig.dropna(subset=["ret_next"])
    for f in ("f1", "f2"):
        sig["pct"] = sig[f].rank(pct=True)
        sig["dec"] = (sig["pct"] * 10).clip(0, 9).astype(int)
        dec = sig.groupby("dec")["ret_next"].agg(["mean", "count"])
        out[f"{f}_decile_0904_to_0907"] = {
            int(k): {"mean_ret": float(v["mean"]), "n": int(v["count"])}
            for k, v in dec.iterrows()}
        top = sig.nlargest(50, f)["ret_next"]
        bot = sig.nsmallest(50, f)["ret_next"]
        out[f"{f}_top50_ret"] = float(top.mean())
        out[f"{f}_bottom50_ret"] = float(bot.mean())
        print(f"\n[{f}] 9/4信号->9/7收益 全市场十分位（负向因子，dec0应好于dec9）:")
        for k, v in dec.iterrows():
            print(f"   dec{k}: {v['mean']:+.2%} ({int(v['count'])}只)")
        print(f"   Top50(回避): {top.mean():+.2%}  Bottom50(低吸): {bot.mean():+.2%}  "
              f"价差 {bot.mean()-top.mean():+.2%}")

    # ---------- 9/7 当日市场宽度（全市场） ----------
    today = d[d["date"] == pd.Timestamp("2026-09-07")]
    out["mkt_0907"] = {"n": int(len(today)),
                       "up_ratio": float((today["ret_today"] > 0).mean()),
                       "median_ret": float(today["ret_today"].median()),
                       "mean_ret": float(today["ret_today"].mean())}
    print(f"\n[9/7 全市场] {len(today)}只 上涨占比 {out['mkt_0907']['up_ratio']:.0%} "
          f"中位 {out['mkt_0907']['median_ret']:+.2%}")

    out["runtime_sec"] = round(time.time() - t0, 1)
    with open(OUT / "fullmarket_today_ic.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"\n[done] {time.time()-t0:.0f}s", flush=True)


if __name__ == "__main__":
    main()
