#!/usr/bin/env python3
"""参数化全市场回测：一次算因子，多组合出报告。

用途（远程机/高性能机运行）：
  python3 backtest_params.py \
      --data-dir data/eastmoney_data \
      --hold-list 1 5 10 20 \
      --cost-list 0.0005 0.001 0.0015 0.003 \
      --end 2026-09-01

可选模块（需要额外数据，缺省自动跳过）：
  --concept-map path/concepts_map.csv   概念中性化（已备：/home/ubuntu/marketdata/concepts/concepts_map.csv）
  --lhb-csv     path/lhb.csv            龙虎榜避雷子区间验证（已备：/home/ubuntu/marketdata/lhb/lhb_2026-06-01_to_2026-09-02.csv）
  --lhb-start   2026-06-01              龙虎榜验证区间起点（默认取数据最早日期）

其他参数：
  --decile 10            分位数（10=常规十分位；20 则 top5%/bottom5% 更加极端）
  --quantile-pct 0.10    多空取分位1与分位N（默认首末十分位）
  --start 2022-01-01     回测起点
  --out-dir results      输出目录

输出：
  <out-dir>/params_<id>.json   每组合一份（id = hold<H>_cost<C>_neut<0|1>_lhb<0|1>）
  <out-dir>/ALL_RESULTS.csv    汇总表（一行一个组合，便于直接对比/贴回）
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

TRADING = 252
NEUTRALIZE_COLS = ["bk", "bk_name", "stock_code"]


def load_data(data_dir: Path, start: str) -> pd.DataFrame:
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
    d = d[d["date"] >= start].sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] rows={len(d)} tickers={d['ticker'].nunique()} "
          f"dates={d['date'].nunique()}", flush=True)
    return d


def attach_bars(dd: pd.DataFrame, hold: int) -> pd.DataFrame:
    """按 hold 加持有期收益列（信号 T、买 T+1 开盘、卖 T+hold 开盘）。"""
    dd = dd.copy()
    gd = dd.groupby("ticker", sort=False)
    dd["open_next"] = gd["open"].shift(-1)
    dd["open_H"] = gd["open"].shift(-hold)
    dd["ret_hold"] = dd["open_H"] / dd["open_next"] - 1.0
    dd["ret_next"] = gd["close"].shift(-1) / dd["close"] - 1.0
    return dd


def load_concept_map(path: str | None) -> dict | None:
    if not path or not Path(path).exists():
        return None
    m = {}
    with open(path, encoding="utf-8-sig") as fh:
        for row in csv.DictReader(fh):
            m.setdefault(row["stock_code"], row.get("bk_name") or row.get("bk"))
    print(f"[concept] 映射 {len(m)} 只股票", flush=True)
    return m


def load_lhb(path: str | None, start: str, end: str) -> pd.DataFrame | None:
    if not path or not Path(path).exists():
        return None
    l = pd.read_csv(path, encoding="utf-8-sig")
    l["date"] = pd.to_datetime(l["TRADE_DATE"])
    l["ticker"] = l["SECURITY_CODE"].astype(str).str.zfill(6)
    l = l[(l["date"] >= start) & (l["date"] <= end)]
    # 避雷标签：上榜原因含"换手" 或 净卖（NET_BS_AMT<0）
    l["is_high_turnover"] = l["EXPLANATION"].str.contains("换手", na=False)
    l["is_net_sell"] = l.get("NET_BS_AMT", pd.Series(index=l.index)).fillna(0) < 0
    print(f"[lhb] {len(l)} 条避雷事件 ({start}~{end})", flush=True)
    return l


def neutralize_group(dd: pd.DataFrame, col: str, cmap: dict | None) -> pd.Series:
    """概念中性化：每个日期各概念组内 demean（减去概念均值）。"""
    if not cmap:
        return dd[col]
    dd = dd.copy()
    dd["_concept"] = dd["ticker"].map(cmap).fillna("NO_CONCEPT")
    return dd[col] - dd.groupby(["date", "_concept"])[col].transform("mean")


def add_avoid_flag(dd: pd.DataFrame, lhb: pd.DataFrame | None,
                   days_back: int = 3) -> pd.Series:
    """龙虎榜避雷：近 N 日内上榜且（高换手 or 净卖）标记 True。"""
    if lhb is None or lhb.empty:
        return pd.Series(False, index=dd.index)
    dd = dd.copy()
    dd["_flag"] = False
    events = lhb[lhb["is_high_turnover"] | lhb["is_net_sell"]][
        ["ticker", "date"]].drop_duplicates()
    for days in range(0, days_back + 1):
        ev = events.copy()
        ev["date"] = ev["date"] + pd.Timedelta(days=days)  # 未来 N 天内曾上榜
        dd = dd.merge(ev, on=["ticker", "date"], how="left", indicator=True)
        dd["_flag"] = dd["_flag"] | (dd["_merge"] == "both")
        dd = dd.drop(columns=["_merge"])
    return dd["_flag"]


def stats(s, af, min_periods: int = 10):
    s = s.dropna()
    if len(s) < min_periods:
        return {}
    return {"n_periods": int(len(s)),
            "ann_ret": float(s.mean() * af),
            "ann_vol": float(s.std() * np.sqrt(af)),
            "sharpe": float(s.mean() / s.std() * np.sqrt(af)) if s.std() > 0 else 0.0,
            "max_dd": float((s.cumsum() - s.cumsum().cummax()).min())}


def run_combination(dd: pd.DataFrame, hold: int, cost: float,
                    decile: int, quantile_pct: float,
                    cmap: dict | None, lhb: pd.DataFrame | None,
                    lhb_start: str | None) -> dict:
    af = TRADING / hold
    # 注意：跨组合重算 ret_hold 依赖 hold，须在列已存在时也逐组合重加
    dd = attach_bars(dd, hold)
    ic_rows = []
    combos = []
    for name, col in (("f1_divergence", "f1"), ("f2_shock_skew", "f2")):
        sub = dd.dropna(subset=[col, "ret_next"]).copy()
        if len(sub) < 100:
            continue
        g = sub.groupby("date", sort=True)
        ics = g.apply(lambda s: s[col].rank().corr(s["ret_next"].rank()),
                      include_groups=False).dropna()
        ic_rows.append({"factor": name, "n_days": int(len(ics)),
                        "ic_mean": float(ics.mean()),
                        "ic_ir": float(ics.mean() / ics.std()) if ics.std() > 0 else 0.0,
                        "ic_t": float(ics.mean() / ics.std() * np.sqrt(len(ics)))
                        if ics.std() > 0 else 0.0})

        sub2 = dd.dropna(subset=[col]).copy()
        # 中性化（可选）
        if cmap:
            sub2[col] = neutralize_group(sub2, col, cmap)
        # 龙虎榜避雷（可选，子区间）
        lhb_avoid = None
        sub_lhb = sub2
        if lhb is not None:
            start = lhb_start or str(lhb["date"].min().date())
            sub_lhb = sub2[sub2["date"] >= start]
            avoid = add_avoid_flag(sub_lhb, lhb)
            sub_lhb = sub_lhb.assign(_avoid=avoid.to_numpy())
        # 信号日（每 hold 天一次）
        base = sub_lhb if lhb is not None else sub2
        base = base.copy()
        base["zsign"] = base.groupby("date")[col].transform(
            lambda s: s.rank(pct=True))
        base["decile"] = (base["zsign"] * decile).clip(0, decile - 1).astype(int)
        dates = np.sort(base["date"].unique())
        sig_dates = pd.to_datetime([x for i, x in enumerate(dates) if i % hold == 0])
        sd = base[base["date"].isin(sig_dates)]
        dr = sd.groupby(["date", "decile"])["ret_hold"].mean().unstack(1)
        mkt = sd.groupby("date")["ret_hold"].mean()

        # 多空：底分位与顶分位（quantile_pct 控制取哪两档）
        low_dcl = int(np.floor(quantile_pct * decile))          # 做多档
        high_dcl = int(np.ceil((1 - quantile_pct) * decile) - 1)  # 做空档
        # LHB 子区间信号日数少，放宽 min_periods（默认 10，子区间 5）
        mp = 10 if lhb is None else 5
        d0, d9 = dr[low_dcl], dr[high_dcl]
        common = sorted(set(d0.index) & set(d9.index))
        ls = d0.loc[common] - d9.loc[common]
        ls_net = ls - cost * 2
        if lhb is not None:
            sd_c = sd[~sd["_avoid"]]
            dr_c = sd_c.groupby(["date", "decile"])["ret_hold"].mean().unstack(1)
            if low_dcl in dr_c.columns and high_dcl in dr_c.columns:
                d0c, d9c = dr_c[low_dcl], dr_c[high_dcl]
                cm = sorted(set(d0c.index) & set(d9c.index))
                ls_avoid = d0c.loc[cm] - d9c.loc[cm]
                ls_avoid_net = ls_avoid - cost * 2
            else:
                ls_avoid = ls
                ls_avoid_net = ls_net
            combos.append({"factor": name, "ls_gross": stats(ls, af, mp),
                           "ls_net": stats(ls_net, af, mp),
                           "ls_avoid_gross": stats(ls_avoid, af, mp),
                           "ls_avoid_net": stats(ls_avoid_net, af, mp),
                           "mkt": stats(mkt, af, mp),
                           "n_avoid": int(len(sub_lhb[sub_lhb["_avoid"]]))})
        else:
            combos.append({"factor": name, "ls_gross": stats(ls, af, mp),
                           "ls_net": stats(ls_net, af, mp), "mkt": stats(mkt, af, mp)})
    return {"ic": ic_rows, "combos": combos}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--data-dir", default="data/eastmoney_data")
    ap.add_argument("--concept-map", default=None)
    ap.add_argument("--lhb-csv", default=None)
    ap.add_argument("--lhb-start", default=None)
    ap.add_argument("--hold-list", nargs="+", type=int, default=[5])
    ap.add_argument("--cost-list", nargs="+", type=float, default=[0.0015])
    ap.add_argument("--decile", type=int, default=10)
    ap.add_argument("--quantile-pct", type=float, default=0.10)
    ap.add_argument("--start", default="2022-01-01")
    ap.add_argument("--end", default="2026-09-01")
    ap.add_argument("--out-dir", default="results")
    args = ap.parse_args()

    t0 = time.time()
    data_dir = Path(args.data_dir)
    dd = load_data(data_dir, args.start)
    dd["date"] = pd.to_datetime(dd["date"])
    dd = dd[dd["date"] <= pd.to_datetime(args.end)]
    print(f"[factor] computing... {time.time()-t0:.0f}s", flush=True)
    dd["f1"] = F1(dd)
    dd["f2"] = F2(dd)
    print(f"[factor] done {time.time()-t0:.0f}s", flush=True)

    cmap = load_concept_map(args.concept_map)
    lhb = load_lhb(args.lhb_csv, args.lhb_start or args.start, args.end)

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    all_rows = []
    for hold in args.hold_list:
        for cost in args.cost_list:
            for neut in ([False, True] if cmap else [False]):
                for lhb_on in ([False, True] if lhb is not None else [False]):
                    res = run_combination(
                        dd, hold, cost, args.decile, args.quantile_pct,
                        cmap if neut else None,
                        lhb if lhb_on else None,
                        args.lhb_start)
                    rid = f"h{hold}_c{int(cost*10000)}_n{int(neut)}_l{int(lhb_on)}"
                    with open(out_dir / f"params_{rid}.json", "w") as fh:
                        json.dump({"params": {"hold": hold, "cost": cost,
                                              "decile": args.decile,
                                              "quantile_pct": args.quantile_pct,
                                              "neutralize": bool(neut),
                                              "lhb_avoid": bool(lhb_on)},
                                   **res}, fh, ensure_ascii=False, indent=2)
                    for c in res["combos"]:
                        all_rows.append({
                            "hold": hold, "cost": cost,
                            "neutralize": int(neut), "lhb_avoid": int(lhb_on),
                            "factor": c["factor"],
                            "n_periods": c["ls_net"].get("n_periods"),
                            "ls_net_ann": c["ls_net"].get("ann_ret"),
                            "ls_net_sharpe": c["ls_net"].get("sharpe"),
                            "ls_net_maxdd": c["ls_net"].get("max_dd"),
                            "ls_gross_ann": c["ls_gross"].get("ann_ret"),
                            "mkt_ann": c["mkt"].get("ann_ret"),
                            "avoid_net_ann": c.get("ls_avoid_net", {}).get("ann_ret"),
                        })
                    print(f"[{rid}] done {time.time()-t0:.0f}s", flush=True)

    # 汇总表
    with open(out_dir / "ALL_RESULTS.csv", "w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=list(all_rows[0].keys()))
        w.writeheader()
        for r in all_rows:
            w.writerow(r)
    print(f"[done] {time.time()-t0:.0f}s -> {out_dir/'ALL_RESULTS.csv'} "
          f"({len(all_rows)} 组合)", flush=True)


if __name__ == "__main__":
    main()
