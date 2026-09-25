#!/usr/bin/env python3
"""行业 × 市场状态 矩阵分析（方案 B：规则筛选行业池）。

方法：
- 31 个申万一级行业（akshare index_component_sw，已缓存 sw_industry_map.csv）
- 规则筛选行业池：行业内 60 日波动分位 ≤ 40% 且上市 ≥ 3 年（每日动态筛选，无未来函数）
- 市场状态：market_regime 双条件规则（普涨趋势/趋势上涨/趋势下跌/震荡）
- 每单元格：f1/f2 IC、t 值、月度调仓（hold=20）五分组多空净收益
- 基线行：全市场、你的 25 只原始池

输出：
  results/industry_regime_matrix.csv  长表（industry × state × factor）
  results/industry_regime_summary.md  可读摘要
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

ROOT = Path(__file__).parent
DATA = Path("/home/ubuntu/data/eastmoney_data")
SW = ROOT / "sw_industry_map.csv"
OUT = ROOT / "results"
OUT.mkdir(exist_ok=True)

START = "2021-11-01"
HOLD = 20
COST = 0.0015
MIN_DAYS_IC = 40      # 每单元格最少交易日数（IC 才有意义；趋势下跌 58 天要保留）
MIN_DAYS_LS = 5       # 每单元格最少调仓期数
MIN_STOCKS = 15       # 行业筛后最少股票数

USER_POOL = {"601169":"北京银行","600900":"长江电力","600801":"华新建材","002001":"新和成",
             "000552":"甘肃能化","000958":"电投产融","600916":"中国黄金","601096":"宏盛华源",
             "601857":"中国石油","002155":"湖南黄金","000902":"新洋丰","601318":"中国平安",
             "300750":"宁德时代","000001":"平安银行","300059":"东方财富","600519":"贵州茅台",
             "002594":"比亚迪","601899":"紫金矿业","600036":"招商银行","000858":"五粮液",
             "600798":"宁波海运","002782":"可立克","000039":"中集集团","600585":"海螺水泥",
             "600863":"华能蒙电"}


def groll(g, col, w, mp, fn):
    s = getattr(g[col].rolling(w, min_periods=mp), fn)()
    return s.reset_index(level=0, drop=True)


def load_all():
    t0 = time.time()
    sw = pd.read_csv(SW, dtype={"ticker": str})
    # 只加载有行业归属的股票（5211/5552），减少 IO
    valid = set(sw["ticker"])
    files = [f for f in sorted(DATA.glob("*.csv"))
             if os.path.basename(f).split("_")[0] in valid]
    print(f"[load] {len(files)}/{len(list(DATA.glob('*.csv')))} files (industry-mapped)", flush=True)
    parts = []
    for i, f in enumerate(files):
        df = pd.read_csv(f, encoding="utf-8-sig")
        df["ticker"] = os.path.basename(f).split("_")[0]
        parts.append(df)
        if (i + 1) % 1000 == 0:
            print(f"[load] {i+1}/{len(files)} {time.time()-t0:.0f}s", flush=True)
    d = pd.concat(parts, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] >= START].sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] {len(d):,} rows {d['ticker'].nunique()} stocks {time.time()-t0:.0f}s", flush=True)
    return d, sw


def market_state(d):
    """双条件市场状态（周度），返回日 -> 状态 映射。"""
    idx = d.groupby("date")["close"].mean().sort_index()
    idx_ret = idx.pct_change()
    w = d.groupby("date")["close"].apply(lambda s: (s.pct_change() > 0).mean())
    med = d.groupby("date")["close"].apply(lambda s: s.pct_change().median())
    mom20 = idx / idx.shift(20) - 1
    dfw = pd.DataFrame({"ret": idx_ret, "breadth": w, "med": med, "mom20": mom20})
    dfw = dfw.resample("W-FRI").agg({"ret": lambda s: (1 + s).prod() - 1,
                                     "breadth": "mean", "med": "mean",
                                     "mom20": "last"}).dropna(subset=["ret"])

    def label(r):
        if r["breadth"] >= 0.58 and r["med"] >= 0.004:
            return "普涨趋势"
        if r["med"] >= 0.003 and r["mom20"] >= 0:
            return "趋势上涨"
        if r["med"] <= -0.005:
            return "趋势下跌"
        return "震荡"
    dfw["state"] = dfw.apply(label, axis=1)
    wk = dfw.reset_index()
    # resample('W-FRI') 的周标签是周五，to_period 也要按周五对齐
    wk["week"] = (pd.to_datetime(wk["date"]) - pd.Timedelta(days=2)).dt.to_period("W-WED")
    return dict(zip(wk["week"], wk["state"])), dfw


def industry_pools(d, sw):
    """规则筛选行业池（每日动态，无未来函数）。"""
    g = d.groupby("ticker", sort=False)
    d["ret"] = g["close"].pct_change()
    d["vol60"] = groll(g, "ret", 60, 40, "std")
    d["vol_pct"] = d.groupby("date")["vol60"].rank(pct=True)
    d["n_days"] = g.cumcount() + 1
    d = d.merge(sw[["ticker", "industry_name"]], on="ticker", how="left")
    # 每日符合条件的股票进入行业池
    d["in_pool"] = (d["vol_pct"] <= 0.4) & (d["n_days"] >= 756)
    return d


def cell_ic(sub, col):
    ic = sub.groupby("date").apply(
        lambda s: s[col].rank().corr(s["ret_next"].rank()),
        include_groups=False).dropna()
    if len(ic) < MIN_DAYS_IC:
        return np.nan, np.nan, len(ic)
    t = ic.mean() / ic.std() * np.sqrt(len(ic)) if ic.std() > 0 else 0
    return ic.mean(), t, len(ic)


def cell_ls(sub, col):
    """月度调仓（hold=20）五分组多空，分状态切片。"""
    sub = sub.dropna(subset=[col]).copy()
    if len(sub) < 1000:
        return np.nan, np.nan, 0
    sub["pct"] = sub.groupby("date")[col].transform(lambda s: s.rank(pct=True))
    sub["q"] = (sub["pct"] * 5).clip(0, 4).astype(int)
    dates = np.sort(sub["date"].unique())
    sig = pd.to_datetime([x for i, x in enumerate(dates) if i % HOLD == 0])
    sd = sub[sub["date"].isin(sig)]
    if sd["date"].nunique() < MIN_DAYS_LS:
        return np.nan, np.nan, 0
    dr = sd.groupby(["date", "q"])["ret_hold"].mean().unstack(1)
    if 0 not in dr.columns or 4 not in dr.columns:
        return np.nan, np.nan, 0
    common = sorted(set(dr[0].index) & set(dr[4].index))
    if len(common) < MIN_DAYS_LS:
        return np.nan, np.nan, 0
    ls = (dr[0].loc[common] - dr[4].loc[common]).dropna()
    net = ls - COST * 2
    ann = net.mean() * (252 / HOLD)
    sharpe = net.mean() / net.std() * np.sqrt(252 / HOLD) if net.std() > 0 else 0
    return ann, sharpe, len(net)


def main():
    t0 = time.time()
    d, sw = load_all()

    # 因子（全市场算一次，行业切片时直接取）
    t1 = time.time()
    d["f1"] = F1(d)
    d["f2"] = F2(d)
    print(f"[factors] {time.time()-t1:.0f}s")

    g = d.groupby("ticker", sort=False)
    d["open_next"] = g["open"].shift(-1)
    d["open_H"] = g["open"].shift(-HOLD)
    d["ret_hold"] = d["open_H"] / d["open_next"] - 1.0
    d["ret_next"] = g["close"].shift(-1) / d["close"] - 1.0

    # 市场状态
    state_map, dfw = market_state(d)
    # 日线数据的周标签也要按 W-WED（对应周五结束的周）
    d["week"] = (d["date"] - pd.Timedelta(days=2)).dt.to_period("W-WED")
    d["state"] = d["week"].map(state_map)
    dfw.to_csv(OUT / "market_regime_full.csv")
    print(f"[regime] {dfw['state'].value_counts().to_dict()}", flush=True)
    # 状态覆盖检查
    cov = d.groupby("state")["date"].nunique()
    print(f"[regime] 日线状态覆盖: {cov.to_dict()}", flush=True)

    # 行业池
    d = industry_pools(d, sw)

    # ---------- 矩阵 ----------
    industries = sorted(sw["industry_name"].unique())
    states = ["趋势下跌", "震荡", "趋势上涨", "普涨趋势"]
    rows = []
    for i, ind in enumerate(industries, 1):
        ind_d = d[(d["industry_name"] == ind) & d["in_pool"]]
        n_stocks = ind_d["ticker"].nunique()
        if n_stocks < MIN_STOCKS:
            print(f"[{i:2d}/31] {ind:6s} SKIP (筛后仅 {n_stocks} 只)")
            continue
        for st in states:
            sub = ind_d[ind_d["state"] == st]
            ic1, t1, n1 = cell_ic(sub, "f1")
            ic2, t2, n2 = cell_ic(sub, "f2")
            ls1, sh1, np1 = cell_ls(sub, "f1")
            ls2, sh2, np2 = cell_ls(sub, "f2")
            rows.append({"industry": ind, "state": st, "n_stocks": n_stocks,
                         "f1_ic": ic1, "f1_t": t1, "f1_ls_net": ls1, "f1_ls_sharpe": sh1,
                         "f2_ic": ic2, "f2_t": t2, "f2_ls_net": ls2, "f2_ls_sharpe": sh2,
                         "n_days_ic": n1, "n_periods_ls": np1})
        print(f"[{i:2d}/31] {ind:6s} {n_stocks:3d} 只 | "
              f"下跌f1={rows[-4]['f1_ic']:+.3f} 震荡f1={rows[-3]['f1_ic']:+.3f} "
              f"上涨f1={rows[-2]['f1_ic']:+.3f}")

    # ---------- 基线：全市场 ----------
    print("\n[baseline] 全市场")
    for st in states:
        sub = d[d["state"] == st]
        ic1, t1, _ = cell_ic(sub, "f1")
        ic2, t2, _ = cell_ic(sub, "f2")
        ls1, sh1, _ = cell_ls(sub, "f1")
        ls2, sh2, _ = cell_ls(sub, "f2")
        rows.append({"industry": "全市场(基线)", "state": st, "n_stocks": d["ticker"].nunique(),
                     "f1_ic": ic1, "f1_t": t1, "f1_ls_net": ls1, "f1_ls_sharpe": sh1,
                     "f2_ic": ic2, "f2_t": t2, "f2_ls_net": ls2, "f2_ls_sharpe": sh2,
                     "n_days_ic": _, "n_periods_ls": _})
        print(f"  {st}: f1={ic1:+.3f}(t{t1:+.1f}) f2={ic2:+.3f}(t{t2:+.1f})")

    # ---------- 基线：用户 25 只 ----------
    print("[baseline] 用户25只")
    user_d = d[d["ticker"].isin(USER_POOL)]
    for st in states:
        sub = user_d[user_d["state"] == st]
        ic1, t1, _ = cell_ic(sub, "f1")
        ic2, t2, _ = cell_ic(sub, "f2")
        ls1, sh1, _ = cell_ls(sub, "f1")
        ls2, sh2, _ = cell_ls(sub, "f2")
        rows.append({"industry": "用户25只(基线)", "state": st, "n_stocks": 25,
                     "f1_ic": ic1, "f1_t": t1, "f1_ls_net": ls1, "f1_ls_sharpe": sh1,
                     "f2_ic": ic2, "f2_t": t2, "f2_ls_net": ls2, "f2_ls_sharpe": sh2,
                     "n_days_ic": _, "n_periods_ls": _})
        print(f"  {st}: f1={ic1:+.3f}(t{t1:+.1f}) f2={ic2:+.3f}(t{t2:+.1f})")

    out = pd.DataFrame(rows)
    out.to_csv(OUT / "industry_regime_matrix.csv", index=False)
    print(f"\n[done] {time.time()-t0:.0f}s -> results/industry_regime_matrix.csv")

    # ---------- 可读摘要 ----------
    _write_summary(out, dfw)


def _write_summary(out, dfw):
    states = ["趋势下跌", "震荡", "趋势上涨", "普涨趋势"]
    lines = ["# 行业 × 市场状态 矩阵（f1 因子，方案 B 规则筛选池）\n"]
    lines.append(f"- 筛选规则：行业内 60 日波动分位 ≤ 40% 且上市 ≥ 3 年（每日动态）")
    lines.append(f"- 状态分布：{dfw['state'].value_counts().to_dict()}")
    lines.append(f"- 样本：{START} 至今，hold={HOLD}，cost={COST}\n")

    lines.append("\n## f1 IC 矩阵（越负越好；NaN=样本不足）\n")
    p1 = out.pivot(index="industry", columns="state", values="f1_ic")
    p1 = p1.reindex(columns=states)
    lines.append(p1.round(3).to_markdown())

    lines.append("\n\n## f1 T 值矩阵（|t|>2 才可信）\n")
    p2 = out.pivot(index="industry", columns="state", values="f1_t")
    p2 = p2.reindex(columns=states)
    lines.append(p2.round(1).to_markdown())

    lines.append("\n\n## f1 月度调仓多空净收益矩阵（hold=20, cost=0.15%）\n")
    p3 = out.pivot(index="industry", columns="state", values="f1_ls_net")
    p3 = p3.reindex(columns=states)
    lines.append(p3.round(3).to_markdown())

    lines.append("\n\n## f2 IC 矩阵（对照）\n")
    p4 = out.pivot(index="industry", columns="state", values="f2_ic")
    p4 = p4.reindex(columns=states)
    lines.append(p4.round(3).to_markdown())

    with open(OUT / "industry_regime_summary.md", "w") as f:
        f.write("\n".join(lines))
    print(f"[summary] -> results/industry_regime_summary.md")


if __name__ == "__main__":
    main()
