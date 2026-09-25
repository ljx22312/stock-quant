"""全市场日线因子回测：IC + 分层组合 + 多空组合（含交易成本）。

数据：/home/ubuntu/data/eastmoney_data/*.csv（5552 只前复权日线）
因子：factors.py 的两个因子（方向：值越大 -> 预期收益越差，即看空）
回测口径：
  - 信号时点：T 日收盘后计算因子（不含任何未来信息）
  - 执行：T+1 日开盘价成交（真实可达）
  - 持有：持有 H 个交易日（T+1 开盘买 -> T+1+H 开盘卖）
  - 收益：用收盘价复权序列，成交量前复权（adj）
  - 成本：双边 0.15%（佣金+印花税+滑点），分情况输出（0 成本 / 0.15%）
  - 组合：按因子值 10 分位分组；多空 = 第1分位（看多) - 第10分位（看空）等权
  - 基准：全部股票等权收益
输出：
  results/backtest_summary.json      汇总指标
  results/factor_ic.json             每因子 IC 序列统计
  results/decile_returns.csv         每分位年化收益
  results/dates.csv                  回测日期序列（诊断用）
"""
import csv
import glob
import json
import os
import sys
import time
from collections import defaultdict
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/ubuntu/stock-quant")
from factors import (  # noqa: E402
    factor_divergence_tscorr_slope_20_60_close as F_DIVERGENCE,
    factor_shock_skew_rank_20_60_volume as F_SHOCK,
)

DATA_DIR = Path("/home/ubuntu/data/eastmoney_data")
OUT = Path("/home/ubuntu/stock-quant/results")
OUT.mkdir(parents=True, exist_ok=True)

TRADING_DAYS = 252
START = "2022-01-01"      # 回测起点（留 1 年预热给 120 日窗口）
END = "2026-09-01"        # 数据截止
HOLD = 5                  # 持有天数
COST = 0.0015             # 单边成本 0.15%（保守：佣金+印花+滑点）
N_DECILE = 10

t0 = time.time()


def load_all() -> pd.DataFrame:
    """并行读取全部 CSV -> 单表。"""
    files = sorted(glob.glob(str(DATA_DIR / "*.csv")))
    print(f"[load] {len(files)} files", flush=True)
    parts = []
    for i, f in enumerate(files):
        df = pd.read_csv(f, encoding="utf-8-sig")
        df = df[["date", "open", "close", "high", "low", "volume"]]
        df["ticker"] = os.path.basename(f).split("_")[0]
        parts.append(df)
        if (i + 1) % 500 == 0:
            print(f"[load] {i+1}/{len(files)} {time.time()-t0:.0f}s", flush=True)
    d = pd.concat(parts, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] >= START]
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] rows={len(d)} tickers={d['ticker'].nunique()} "
          f"dates={d['date'].nunique()} ({time.time()-t0:.0f}s)", flush=True)
    return d


def add_forward(d: pd.DataFrame) -> pd.DataFrame:
    """加 T+1 开盘价、T+H 开盘价、持有期收益。

    ret_hold：T+1 开盘买入 -> T+H 开盘卖出（持有 H 天，跨 H 个交易日价差）。
    信号日 T 收盘计算因子，收益归到 T（实际持仓从 T+1 开始，信号与持仓错位 1 天）。
    """
    g = d.groupby("ticker", sort=False)
    d["open_next"] = g["open"].shift(-1)                     # T+1 开盘（买入价）
    d["open_H"] = g["open"].shift(-HOLD)                     # T+H 开盘（卖出价）
    d["ret_hold"] = d["open_H"] / d["open_next"] - 1.0
    d["ret_next"] = g["close"].shift(-1) / d["close"] - 1.0
    return d


def setup_portfolio(d: pd.DataFrame, factor: pd.Series, name: str):
    """按因子分位分组 -> 多空组合日收益序列（含成本）。

    权重：组合内所有股票等权，每日换仓到最新信号（实际周频信号，持有 H 天意味着
    信号重叠；这里简化：信号日 {每 H 天} 换仓，中间不动）。
    """
    d = d.copy()
    d["sig"] = factor
    d["zsign"] = d.groupby("date")["sig"].transform(
        lambda s: s.rank(pct=True))
    d["decile"] = (d["zsign"] * N_DECILE).clip(0, N_DECILE - 1).astype(int)

    recs = []
    for sdate, gd in d.groupby("date", sort=True):
        gd = gd.dropna(subset=["sig", "open_next", "open_H", "ret_hold"])
        if len(gd) < 20:
            recs.append({"date": sdate, "port_ret": np.nan, "mkt_ret": np.nan,
                         "long_ret": np.nan, "short_ret": np.nan, "n_stock": len(gd)})
            continue
        mkt = gd["ret_hold"].mean()
        for dcl in (0, N_DECILE - 1):
            sub = gd[gd["decile"] == dcl]
            if len(sub) < 5:
                dcl_ret = np.nan
            else:
                dcl_ret = sub["ret_hold"].mean()
            recs.append({"date": sdate,
                         "port_ret": np.nan, "mkt_ret": mkt,
                         "long_ret": dcl_ret if dcl == 0 else np.nan,
                         "short_ret": dcl_ret if dcl == N_DECILE - 1 else np.nan,
                         "n_stock": len(gd)})
    return pd.DataFrame(recs)


def main():
    d = load_all()
    print(f"[factor] computing... {time.time()-t0:.0f}s", flush=True)
    f1 = F_DIVERGENCE(d)
    f2 = F_SHOCK(d)
    d["f1"] = f1
    d["f2"] = f2
    d = add_forward(d)
    print(f"[factor] done {time.time()-t0:.0f}s", flush=True)

    # ---------- IC ----------
    ic_rows = []
    for name, col in [("f1_divergence", "f1"), ("f2_shock_skew", "f2")]:
        sub = d.dropna(subset=[col, "ret_next"])
        g = sub.groupby("date", sort=True)
        rows = g.apply(
            lambda s: s[col].rank().corr(s["ret_next"].rank()),
            include_groups=False)
        rows = rows.dropna()
        ic_rows.append({
            "factor": name,
            "n_days": int(len(rows)),
            "ic_mean": float(rows.mean()),
            "ic_std": float(rows.std()),
            "ic_ir": float(rows.mean() / rows.std()) if rows.std() > 0 else 0.0,
            "ic_pos_ratio": float((rows > 0).mean()),
            "ic_t": float(rows.mean() / rows.std() * np.sqrt(len(rows))),
        })
        print(f"[IC] {name}: mean={rows.mean():.4f} IR={ic_rows[-1]['ic_ir']:.3f} "
              f"pos={ic_rows[-1]['ic_pos_ratio']:.1%} t={ic_rows[-1]['ic_t']:.1f}",
              flush=True)

    # ---------- 分层/多空组合（非重叠持有期收益）----------
    # 信号日 T 收盘（每 H 天一次），T+1 开盘买入、T+H 开盘卖出；收益序列非重叠，
    # 年化用 252/H。
    combos = []
    for name, col in [("f1_divergence", "f1"), ("f2_shock_skew", "f2")]:
        sub = d.dropna(subset=[col]).copy()
        sub["sig"] = sub[col]
        sub["zsign"] = sub.groupby("date")["sig"].transform(
            lambda s: s.rank(pct=True))
        sub["decile"] = (sub["zsign"] * N_DECILE).clip(0, N_DECILE - 1).astype(int)

        dates = np.sort(sub["date"].unique())
        sig_dates = pd.to_datetime([d for i, d in enumerate(dates) if i % HOLD == 0])
        sd = sub[sub["date"].isin(sig_dates)]

        group = sd.groupby(["date", "decile"], sort=True)
        decile_ret = group["ret_hold"].mean().unstack(level=1)  # 信号日 x 10
        mkt = sd.groupby("date")["ret_hold"].mean()
        combos.append({
            "factor": name,
            "decile_ret": decile_ret,
            "mkt": mkt,
        })
        ann_factor = TRADING_DAYS / HOLD
        decile_ann = decile_ret.mean(axis=0) * ann_factor
        print(f"[decile] {name} 年化:{['%.4f' % x for x in decile_ann.to_list()]}",
              flush=True)
    # 多空：分位0 - 分位9
    for i, cb in enumerate(combos):
        ls = cb["decile_ret"][0] - cb["decile_ret"][N_DECILE - 1]
        ls_net = ls - COST * 2   # 每次换仓双边成本（信号日每 H 天一次）
        mkt = cb["mkt"]

        def stats(s, ann_factor=TRADING_DAYS / HOLD):
            s = s.dropna()
            if len(s) < 10:
                return {}
            return {
                "n_periods": int(len(s)),
                "ann_ret": float(s.mean() * ann_factor),
                "ann_vol": float(s.std() * np.sqrt(ann_factor)),
                "sharpe": float(s.mean() / s.std() * np.sqrt(ann_factor)) if s.std() > 0 else 0.0,
                "max_dd": float((s.cumsum() - s.cumsum().cummax()).min()),
            }
        c = combos[i]
        c.update({
            "ls_gross": stats(ls),
            "ls_net": stats(ls_net),
            "mkt_stats": stats(mkt),
        })
        print(f"[LS] {name} gross:{c['ls_gross']}", flush=True)
        print(f"[LS] {name} net  :{c['ls_net']}", flush=True)

    # ---------- 保存 ----------
    out = {
        "config": {"start": START, "end": END, "hold": HOLD, "cost_single": COST,
                   "n_decile": N_DECILE, "n_symbols": int(d["ticker"].nunique())},
        "ic": ic_rows,
        "combos": [{"factor": c["factor"],
                    "ls_gross": c["ls_gross"], "ls_net": c["ls_net"],
                    "mkt": c["mkt_stats"]} for c in combos],
        "runtime_sec": round(time.time() - t0, 1),
    }
    with open(OUT / "backtest_summary.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    # 分位收益 CSV
    with open(OUT / "decile_returns.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["factor", "decile", "ann_ret_gross"])
        ann_factor = TRADING_DAYS / HOLD
        for c in combos:
            for i, v in enumerate(c["decile_ret"].mean(axis=0) * ann_factor):
                w.writerow([c["factor"], i, round(float(v), 6)])
    print(f"[done] total {time.time()-t0:.0f}s -> {OUT/'backtest_summary.json'}",
          flush=True)


if __name__ == "__main__":
    main()
