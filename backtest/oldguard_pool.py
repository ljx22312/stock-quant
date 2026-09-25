#!/usr/bin/env python3
"""老登股池专项分析：25 只防守型池内验证 f1/f2 + 当前信号。

数据：eastmoney_data（主，长历史）+ stockdesk.db daily_bars 补 9/2-9/4。
输出：results/oldguard_pool.json + 控制台摘要。
"""
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

POOL = {
    "601169": "北京银行", "600900": "长江电力", "600801": "华新建材",
    "002001": "新和成", "000552": "甘肃能化", "000958": "电投产融",
    "600916": "中国黄金", "601096": "宏盛华源", "601857": "中国石油",
    "002155": "湖南黄金", "000902": "新洋丰", "601318": "中国平安",
    "300750": "宁德时代", "000001": "平安银行", "300059": "东方财富",
    "600519": "贵州茅台", "002594": "比亚迪", "601899": "紫金矿业",
    "600036": "招商银行", "000858": "五粮液", "600798": "宁波海运",
    "002782": "可立克", "000039": "中集集团", "600585": "海螺水泥",
    "600863": "华能蒙电",
}
# 严格老登版：剔除池内高波动/科技属性最重的 4 只
PSEUDO = {"300750": "宁德时代", "002594": "比亚迪", "300059": "东方财富", "002782": "可立克"}
HOLD = 5
COST = 0.0015
START = "2022-01-01"


def load_panel(data_dir: Path, db_path: Path) -> pd.DataFrame:
    parts = []
    for t in POOL:
        f = data_dir / f"{t}_{POOL[t]}.csv"
        if not f.exists():
            # 文件名可能有空格，按前缀匹配
            cand = list(data_dir.glob(f"{t}_*.csv"))
            if not cand:
                raise FileNotFoundError(f"missing {t}")
            f = cand[0]
        d = pd.read_csv(f, encoding="utf-8-sig")
        d = d[["date", "open", "close", "high", "low", "volume"]]
        d["ticker"] = t
        parts.append(d)
    em = pd.concat(parts, ignore_index=True)
    em["date"] = pd.to_datetime(em["date"])

    import sqlite3
    con = sqlite3.connect(db_path)
    q = f"SELECT symbol, date, open, high, low, close, volume FROM daily_bars \
         WHERE symbol IN ({','.join('?' * len(POOL))}) AND date > '2026-09-01'"
    site = pd.read_sql_query(q, con, params=list(POOL.keys()))
    con.close()
    if len(site):
        site = site.rename(columns={"symbol": "ticker"})
        site["date"] = pd.to_datetime(site["date"])
        print(f"[load] site extension {len(site)} rows: "
              f"{site['date'].min().date()}..{site['date'].max().date()}")
    d = pd.concat([em, site], ignore_index=True)
    d = d.drop_duplicates(["ticker", "date"], keep="first")
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] pool rows={len(d)} tickers={d['ticker'].nunique()} "
          f"dates={d['date'].nunique()} {d['date'].min().date()}..{d['date'].max().date()}")
    return d


def spearman_daily(sub: pd.DataFrame, col: str) -> pd.Series:
    return sub.groupby("date").apply(
        lambda s: s[col].rank().corr(s["ret_next"].rank()),
        include_groups=False).dropna()


def main():
    t0 = time.time()
    root = Path(__file__).parent
    dd = load_panel(root.parent / "data" / "eastmoney_data",
                    Path("/home/ubuntu/stock-alert/data/stockdesk.db"))

    for name, fn in (("f1", F1), ("f2", F2)):
        dd[name] = fn(dd)
    g = dd.groupby("ticker", sort=False)
    dd["open_next"] = g["open"].shift(-1)
    dd["open_H"] = g["open"].shift(-HOLD)
    dd["ret_hold"] = dd["open_H"] / dd["open_next"] - 1.0
    dd["ret_next"] = g["close"].shift(-1) / dd["close"] - 1.0
    print(f"[factor] done {time.time()-t0:.0f}s")

    out = {}

    # ---------- 1. 池内 IC ----------
    for name in ("f1", "f2"):
        sub = dd[dd["date"] >= START].dropna(subset=[name, "ret_next"])
        ic = spearman_daily(sub, name)
        out[f"{name}_ic"] = {
            "n_days": int(len(ic)),
            "mean": float(ic.mean()),
            "std": float(ic.std()),
            "ir": float(ic.mean() / ic.std()) if ic.std() > 0 else 0.0,
            "t": float(ic.mean() / ic.std() * np.sqrt(len(ic))) if ic.std() > 0 else 0.0,
            "pos_ratio": float((ic > 0).mean()),
        }
        # 年度 IC
        yr = {}
        for y in sorted(dd["date"].dt.year.unique()):
            if y < 2022:
                continue
            ys = sub[sub["date"].dt.year == y]
            if len(ys) < 60:
                continue
            yic = spearman_daily(ys, name)
            yr[int(y)] = {"days": int(len(yic)), "ic": float(yic.mean()),
                          "t": float(yic.mean() / yic.std() * np.sqrt(len(yic)))
                          if yic.std() > 0 else 0.0}
        out[f"{name}_ic"]["yearly"] = yr
        # 周 IC（最近 12 周）
        w = sub.assign(week=sub["date"].dt.to_period("W"))
        wic = w.groupby("week").apply(
            lambda s: s[name].rank().corr(s["ret_next"].rank()),
            include_groups=False).dropna()
        wk = [{"week": str(k), "ic": round(float(v), 4)}
              for k, v in wic.tail(12).items()]
        out[f"{name}_ic"]["weekly_tail12"] = wk
        # 8/31-9/4
        cur = sub[(sub["date"] >= "2026-08-31")]
        cic = spearman_daily(cur, name)
        out[f"{name}_ic"]["current_week"] = {
            "days": int(len(cic)), "mean": float(cic.mean()) if len(cic) else None}
        print(f"[IC] {name}: mean={out[f'{name}_ic']['mean']:+.4f} "
              f"t={out[f'{name}_ic']['t']:+.1f} pos={out[f'{name}_ic']['pos_ratio']:.0%} "
              f"| 8/31-9/4 {out[f'{name}_ic']['current_week']['mean']:+.4f}")

    # ---------- 2. 组合：五分组多空（低因子多头 / 高因子空头）----------
    af = 252 / HOLD
    for name in ("f1", "f2"):
        sub = dd[dd["date"] >= START].dropna(subset=[name]).copy()
        sub["pct"] = sub.groupby("date")[name].transform(
            lambda s: s.rank(pct=True))
        sub["q"] = (sub["pct"] * 5).clip(0, 4).astype(int)
        dates = np.sort(sub["date"].unique())
        sig = pd.to_datetime([x for i, x in enumerate(dates) if i % HOLD == 0])
        sd = sub[sub["date"].isin(sig)]
        dr = sd.groupby(["date", "q"])["ret_hold"].mean().unstack(1)
        mkt = sd.groupby("date")["ret_hold"].mean()
        common = sorted(set(dr[0].index) & set(dr[4].index))
        ls = dr[0].loc[common] - dr[4].loc[common]
        ls_net = ls - COST * 2

        def st(s):
            s = s.dropna()
            return {"n": int(len(s)),
                    "ann": float(s.mean() * af),
                    "ann_vol": float(s.std() * np.sqrt(af)),
                    "sharpe": float(s.mean() / s.std() * np.sqrt(af)) if s.std() > 0 else 0.0,
                    "maxdd": float((s.cumsum() - s.cumsum().cummax()).min())}
        out[f"{name}_ls"] = {"gross": st(ls), "net": st(ls_net),
                             "mkt": st(mkt),
                             "q_ann": {int(k): round(float(v) * af, 4)
                                       for k, v in dr.mean(axis=0).items()}}
        print(f"[LS] {name}: q0={out[f'{name}_ls']['q_ann'].get(0)} "
              f"q4={out[f'{name}_ls']['q_ann'].get(4)} "
              f"net={out[f'{name}_ls']['net']['ann']:+.4f} "
              f"sharpe={out[f'{name}_ls']['net']['sharpe']:+.2f}")

    # ---------- 3. 池特征：波动 / 收益分布 ----------
    last = dd["date"].max()
    ff = dd[dd["date"] >= "2025-01-01"].copy()
    ff["ret"] = ff.groupby("ticker")["close"].pct_change()
    stats = {}
    for t in POOL:
        s = ff[ff["ticker"] == t]["ret"].dropna()
        stats[t] = {
            "name": POOL[t],
            "ann_vol": float(s.std() * np.sqrt(252)),
            "avg_abs": float(s.abs().mean()),
            "n_days": int(len(s)),
        }
    out["pool_stats"] = stats
    worst = sorted(stats.items(), key=lambda kv: -kv[1]["ann_vol"])[:6]
    print("[vol] 池内年化波动 Top6:", ", ".join(
        f"{v['name']} {v['ann_vol']:.0%}" for _, v in worst))

    # ---------- 4. 最新信号（最后交易日截面）----------
    cur = dd[dd["date"] == last].dropna(subset=["f1", "f2"]).copy()
    cur["f1_pct"] = cur["f1"].rank(pct=True)
    cur["f2_pct"] = cur["f2"].rank(pct=True)
    cur["score"] = cur["f1_pct"] + cur["f2_pct"]  # 越接近2越危险(负向因子)
    cur = cur.sort_values("score")
    sig_rows = []
    for _, r in cur.iterrows():
        sig_rows.append({
            "ticker": r["ticker"], "name": POOL[r["ticker"]],
            "close": round(float(r["close"]), 2),
            "f1": round(float(r["f1"]), 3), "f1_pct": round(float(r["f1_pct"]), 3),
            "f2": round(float(r["f2"]), 3), "f2_pct": round(float(r["f2_pct"]), 3),
            "risk_score": round(float(r["score"]), 3),
            "label": _label(float(r["score"]), len(cur)),
        })
    out["signals"] = {"as_of": str(last.date()), "rows": sig_rows}

    # 严格老登版（剔除伪老登 4 只）汇总
    for name in ("f1", "f2"):
        sub = dd[(dd["date"] >= START) & (~dd["ticker"].isin(PSEUDO))] \
            .dropna(subset=[name, "ret_next"])
        ic = spearman_daily(sub, name)
        out[f"{name}_ic_strict21"] = {
            "n_days": int(len(ic)), "mean": float(ic.mean()),
            "t": float(ic.mean() / ic.std() * np.sqrt(len(ic))) if ic.std() > 0 else 0.0}
        cur21 = ic[(ic.index >= "2026-08-31")]
        out[f"{name}_ic_strict21"]["current_week"] = \
            float(cur21.mean()) if len(cur21) else None
        print(f"[IC-strict21] {name}: full={out[f'{name}_ic_strict21']['mean']:+.4f} "
              f"t={out[f'{name}_ic_strict21']['t']:+.1f} "
              f"w={out[f'{name}_ic_strict21']['current_week']}")

    out["runtime_sec"] = round(time.time() - t0, 1)
    out["config"] = {"pool": POOL, "start": START, "hold": HOLD, "cost": COST,
                     "pseudo_excluded": PSEUDO}
    res = Path("/home/ubuntu/stock-quant/results")
    res.mkdir(parents=True, exist_ok=True)
    with open(res / "oldguard_pool.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2, default=str)

    print("\n========== 最新信号（最后交易日 2026-09-04，风险分 = f1+f2 池内分位，越大越危险）==========")
    for r in sig_rows:
        print(f"{r['name']:<6} {r['ticker']} close={r['close']:<8} "
              f"f1={r['f1']:+.3f}(p{r['f1_pct']:.2f}) f2={r['f2']:+.3f}(p{r['f2_pct']:.2f}) "
              f"score={r['risk_score']:.3f} -> {r['label']}")


def _label(score: float, n: int) -> str:
    # score 为两因子分位和，范围 0~2；前1/4危险，后1/4安全
    hi = 2.0 * 0.75
    lo = 2.0 * 0.25
    if score >= hi:
        return "【回避】双因子高位"
    if score >= 2.0 * 0.65:
        return "减仓观察"
    if score <= lo:
        return "【低吸关注】双因子低位"
    if score <= 2.0 * 0.35:
        return "持有候选"
    return "中性"


if __name__ == "__main__":
    main()
