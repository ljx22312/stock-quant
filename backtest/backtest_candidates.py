#!/usr/bin/env python3
"""候选因子体检：5 因子（现有2 + 候选3）统一对比。

输出：
  results/candidates_ic.json       总 IC / 周 IC / 相关矩阵
  results/candidates_summary.json  全部指标汇总
  results/candidates_ALL.csv       一行一因子（直接对比）

运行（远程机）：
  python3 backtest_candidates.py --data-dir data/eastmoney_data \\
      --hold 5 --cost 0.0015 --start 2022-01-01 --end 2026-09-01

重点看三件事：
  1. 候选 vs 裸动量：mom_raw_20 的 IC 是否接近 0/为负（验证"A股裸动量不可用"的假设）
  2. 候选 A/B 的总 IC 是否为正（多头侧）+ 上周（8/31 前后）IC 是否为正（补充反转失效窗口）
  3. 相关矩阵：候选与 f1/f2 相关系数 < 0.7（去共线性达标）
"""
import argparse
import csv
import glob
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factors import (  # noqa: E402
    factor_divergence_tscorr_slope_20_60_close as F1,
    factor_shock_skew_rank_20_60_volume as F2,
)
from candidates import (  # noqa: E402
    factor_defensive_shrink_20_60_close as FDEF,
    factor_mom_raw_20_close as FMOM,
    factor_trend_quality_20_60_close as FTQ,
)

TRADING = 252
FACTORS = [
    ("f1_divergence", F1),
    ("f2_shock_skew", F2),
    ("cand_mom_raw_20", FMOM),
    ("cand_trend_quality", FTQ),
    ("cand_defensive", FDEF),
]


def load_data(data_dir: Path, start: str, end: str) -> pd.DataFrame:
    files = sorted(glob.glob(str(data_dir / "*.csv")))
    print(f"[load] {len(files)} files", flush=True)
    parts = []
    for i, f in enumerate(files):
        df = pd.read_csv(f, encoding="utf-8-sig")
        df = df[["date", "open", "close", "high", "low", "volume"]]
        df["ticker"] = os.path.basename(f).split("_")[0]
        parts.append(df)
        if (i + 1) % 2000 == 0:
            print(f"[load] {i+1}/{len(files)}", flush=True)
    d = pd.concat(parts, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d[(d["date"] >= start) & (d["date"] <= end)]
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] rows={len(d)} tickers={d['ticker'].nunique()} "
          f"dates={d['date'].nunique()}", flush=True)
    return d


def ic_series(sub: pd.DataFrame, col: str) -> pd.Series:
    g = sub.groupby("date", sort=True)
    return g.apply(lambda s: s[col].rank().corr(s["ret_next"].rank()),
                   include_groups=False).dropna()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/eastmoney_data")
    ap.add_argument("--hold", type=int, default=5)
    ap.add_argument("--cost", type=float, default=0.0015)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    t0 = time.time()
    dd = load_data(Path(args.data_dir), args.start, args.end)
    g = dd.groupby("ticker", sort=False)
    dd["open_next"] = g["open"].shift(-1)
    dd["open_H"] = g["open"].shift(-args.hold)
    dd["ret_hold"] = dd["open_H"] / dd["open_next"] - 1.0
    dd["ret_next"] = g["close"].shift(-1) / dd["close"] - 1.0

    for name, fn in FACTORS:
        dd[name] = fn(dd)
        print(f"[factor] {name} done {time.time()-t0:.0f}s", flush=True)

    # ---------- 总 IC / 周 IC ----------
    ic_all = {}
    weekly_rows = []
    for name, _ in FACTORS:
        sub = dd.dropna(subset=[name, "ret_next"])
        ic = ic_series(sub, name)
        ic_all[name] = {
            "n_days": int(len(ic)),
            "ic_mean": float(ic.mean()),
            "ic_std": float(ic.std()),
            "ic_ir": float(ic.mean() / ic.std()) if ic.std() > 0 else 0.0,
            "ic_pos_ratio": float((ic > 0).mean()),
            "ic_t": float(ic.mean() / ic.std() * np.sqrt(len(ic)))
            if ic.std() > 0 else 0.0,
        }
        # 周 IC
        wic = dd.dropna(subset=[name, "ret_next"]).copy()
        wic["week"] = wic["date"].dt.to_period("W")
        w_ic = wic.groupby("week").apply(
            lambda s: s[name].rank().corr(s["ret_next"].rank()),
            include_groups=False).dropna()
        weekly_rows.append({"factor": name,
                            "weekly": {str(k): (float(v) if not pd.isna(v) else None)
                                       for k, v in w_ic.tail(14).items()}})
        print(f"[IC] {name}: mean={ic_all[name]['ic_mean']:+.4f} "
              f"IR={ic_all[name]['ic_ir']:+.3f} t={ic_all[name]['ic_t']:+.1f} "
              f"pos={ic_all[name]['ic_pos_ratio']:.1%}", flush=True)

    # ---------- 相关矩阵（逐日截面相关后取均值）----------
    names = [n for n, _ in FACTORS]
    corr_mat = pd.DataFrame(index=names, columns=names, dtype=float)
    for i, na in enumerate(names):
        for j, nb in enumerate(names):
            if j < i:
                continue
            sub = dd.dropna(subset=[na, nb])
            # 逐日截面 Pearson，取均值
            c = sub.groupby("date").apply(
                lambda s: s[na].corr(s[nb]), include_groups=False).dropna()
            v = float(c.mean()) if len(c) else float("nan")
            corr_mat.loc[na, nb] = v
            corr_mat.loc[nb, na] = v
    print("\n[corr] 因子间平均截面相关：\n", corr_mat.round(3), flush=True)

    # ---------- 分位 / 多空 ----------
    af = TRADING / args.hold
    combos = {}
    for name, _ in FACTORS:
        sub = dd.dropna(subset=[name]).copy()
        sub["zsign"] = sub.groupby("date")[name].transform(
            lambda s: s.rank(pct=True))
        sub["decile"] = (sub["zsign"] * 10).clip(0, 9).astype(int)
        dates = np.sort(sub["date"].unique())
        sig = pd.to_datetime([x for i, x in enumerate(dates)
                              if i % args.hold == 0])
        sd = sub[sub["date"].isin(sig)]
        dr = sd.groupby(["date", "decile"])["ret_hold"].mean().unstack(1)
        mkt = sd.groupby("date")["ret_hold"].mean()
        d0, d9 = dr[0], dr[9]
        common = sorted(set(d0.index) & set(d9.index))
        ls = d0.loc[common] - d9.loc[common]
        ls_net = ls - args.cost * 2

        def st(s):
            s = s.dropna()
            if len(s) < 10:
                return {}
            return {"n_periods": int(len(s)),
                    "ann_ret": float(s.mean() * af),
                    "ann_vol": float(s.std() * np.sqrt(af)),
                    "sharpe": float(s.mean() / s.std() * np.sqrt(af))
                    if s.std() > 0 else 0.0,
                    "max_dd": float((s.cumsum() - s.cumsum().cummax()).min())}
        combos[name] = {
            "decile_ann": {int(k): round(float(v) * af, 4)
                           for k, v in dr.mean(axis=0).items()},
            "ls_gross": st(ls),
            "ls_net": st(ls_net),
            "mkt": st(mkt),
        }
        print(f"[combo] {name}: decile1/10 = "
              f"{combos[name]['decile_ann'].get(0):.4f}/{combos[name]['decile_ann'].get(9):.4f} "
              f"| net={combos[name]['ls_net'].get('ann_ret')}", flush=True)

    # ---------- 保存 ----------
    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)
    summary = {"config": {"start": args.start, "end": args.end,
                          "hold": args.hold, "cost": args.cost,
                          "n_symbols": int(dd["ticker"].nunique())},
               "ic": ic_all, "weekly": weekly_rows,
               "corr_matrix": corr_mat.round(4).to_dict(),
               "combos": combos,
               "runtime_sec": round(time.time() - t0, 1)}
    with open(out / "candidates_summary.json", "w") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=2, default=str)

    rows = []
    for name, _ in FACTORS:
        rows.append({
            "factor": name,
            "ic_mean": ic_all[name]["ic_mean"],
            "ic_ir": ic_all[name]["ic_ir"],
            "ic_t": ic_all[name]["ic_t"],
            "corr_f1": corr_mat.loc[name, "f1_divergence"],
            "corr_f2": corr_mat.loc[name, "f2_shock_skew"],
            "decile1_ann": combos[name]["decile_ann"].get(0),
            "decile10_ann": combos[name]["decile_ann"].get(9),
            "ls_net_ann": combos[name]["ls_net"].get("ann_ret"),
            "ls_net_sharpe": combos[name]["ls_net"].get("sharpe"),
        })
    with open(out / "candidates_ALL.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(rows[0].keys()))
        w.writeheader()
        for r in rows:
            w.writerow(r)
    print(f"[done] {time.time()-t0:.0f}s -> {out/'candidates_ALL.csv'}", flush=True)


if __name__ == "__main__":
    main()
