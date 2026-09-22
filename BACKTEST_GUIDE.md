# 全市场日线因子回测指导（StockDesk 数据管道）

> 本文档写给远程执行机器（CPU/内存充裕，按全量模式设计）。
> 数据已在远程机就位，**执行者只需要知道一个数据目录：`<部署根>/data/eastmoney_data/`**。
> 所有代码、口径均已在本机（82.156.15.12, 轻量级 1GB 内存）验证：
> - 因子实现通过数值对照（ts_slope vs np.polyfit 误差 2.4e-16，ts_corr vs 手算 0.0）
> - 无未来函数（源码无 shift(-1)，收益一律以 shift(1) 滞后）
> - 回测流程逻辑在 200 只子样本上跑通（IC/分层/多空含成本全链路输出正常）

---

## 0. 任务一句话

用全 A 股 5,552 只前复权日线，检验两个原创日频因子的方向性与可交易性：
IC 体检 + 10 分位分层 + 多空组合（含 0.15% 单边成本），输出可评判的汇总指标。

---

## 1. 数据（唯一数据源：`data/eastmoney_data/` 目录）

**数据位置：`<部署根>/data/eastmoney_data/`**（目录名固定；部署根按远程机实际路径）

- **5,552 个 CSV 文件**，每只股票一个文件，命名 `<6位代码>_<股票名>.csv`
  - 例：`000001_平安银行.csv`、`300750_宁德时代.csv`
  - **北交所也含**（920 开头），无需剔除（也可按需剔除，见 §8）
- **字段（统一，UTF-8 带 BOM）：** `date, open, close, high, low, volume, amount`
- **复权口径：东方财富前复权（fqt=1）** —— 与网站主库 `daily_bars`（本机已实测交叉验证）：
  - `000001 / 300750 / 601169` 在 `2026-08-31`、`2026-09-01` 的 close **完全一致（偏离 0.0000%）**
  - 结论：两库同源，回测数据 = 网站实际行情数据的全市场扩集
- **覆盖（已审计）：** 12,188,970 行 / 5,552 只 / 1,130 个交易日
  - 日期范围：2006-03-21 ～ **2026-09-01**
  - **建议回测窗口：2022-01-01 ～ 2026-09-01**（留 1 年预热给 120 日窗口）
  - 无重复日期、无 NaN、无 0 成交量；少量坏价格（0.35%，多为 2015 前早期数据）

> ⚠️ 前复权注意：历史价格以"今天"为基准调整，**收益率比例正确**（本因子/回测只用比例，安全）；
> 但**不能**用于：涨停/跌停板判断（10%/20% 阈值）、绝对价格、真实市值。如要做涨跌停过滤需另取不复权数据。

---

## 2. 环境（远程机）

- Python ≥ 3.10（实测 3.12）
- pandas ≥ 2.0（实测 3.0.5）、numpy ≥ 2.0（实测 2.5.2）
- 无其他依赖（不依赖 scipy —— Spearman 用 rank 后 Pearson 实现）
- **内存：全量模式峰值约 2.5–4 GB**（5,552 只、2022 起约 582 万行 + 因子中间量）。远程机充足，直接跑 §6 即可。

---

## 3. 因子定义（两个，方向均为：**因子值越大 → 预期未来收益越差（看空）**）

### 因子 1：量价背离 × 波动压缩 × 趋势斜率（上行耗竭预警）

```
r_t   = close_t / close.shift(1)_t - 1
v_t   = volume_t / volume.shift(1)_t - 1
corrP = ts_corr(r, v, 20)                    # 20日量价相关
div   = 1 - corrP                            # 量价背离度
comp  = ts_std(r, 20) / ts_std(r, 100)       # 波动压缩比
slope = ts_slope(close, 60) / ts_mean(close, 60)   # 归一化趋势斜率
raw   = div * comp * slope                   # 纯乘法交互
输出  = winsorize0.01-0.99 |> z-score
```

### 因子 2：流动性冲击 × 收益偏度 × 波动位置（拥挤交易过热）

```
r_t   = close_t / close.shift(1)_t - 1
shock = volume_t / ts_mean(volume, 20)       # 流动性冲击
rankS = cross_rank(shock, by=date)           # 横截面排名（相对放量强度）
skew  = ts_skewness(r, 60)                   # 高阶矩：收益偏度
ratio = ts_std(r, 20) / ts_std(r, 120)       # 波动位置
raw   = rankS * (1 + skew) * ratio           # 纯乘法交互
输出  = winsorize0.01-0.99 |> z-score
```

算子池覆盖（>3 类组合、含 rank/ts_corr、无线性加权）：算术(mul/div)、横截面(rank)、时序统计(ts_corr/ts_std/ts_mean)、高级时序(ts_slope/ts_skewness)。

---

## 4. 回测口径（防未来函数，硬性）

| 项 | 值 |
|---|---|
| 信号时点 | **T 日收盘后**（因子只用到 T 及以前数据，`close.shift(1)` 显式滞后） |
| 执行 | **T+1 日开盘价**买入（真实可达） |
| 持有 | **H = 5 个交易日**：T+1 开盘买 → T+H 开盘卖 |
| 持有期收益 | `ret_hold = open.shift(-H) / open.shift(-1) - 1`，按信号日 T 对齐 |
| IC | T 日因子 vs T→T+1 收益（`close.shift(-1)/close - 1`）的截面 Spearman |
| 分层 | 每交易日按因子值 10 分位分组，组内等权 |
| 多空 | 分位 0（看多） − 分位 9（看空）等权 |
| 成本 | 单边 **0.15%**（佣金+印花+滑点保守值），持有 5 天换仓，双边 0.30%/次 |
| 年化 | 按非重叠持有期：`ann_factor = 252 / H` |
| 过滤 | 剔除因子/收益 NaN 行；组内 <5 只的日不参与组合 |

---

## 5. 因子代码（完整，保存为 `factors.py` 与回测脚本同目录）

```python
"""日频量价因子库（原创）。"""
from __future__ import annotations
import numpy as np
import pandas as pd

def _ticker_col(df):
    for c in ("ticker", "symbol"):
        if c in df.columns:
            return c
    raise ValueError("df 需要包含 ticker 或 symbol 列")

def _date_col(df):
    return "date" if "date" in df.columns else None

def _order(df, tcol, dcol):
    x = df.copy()
    x["__oi"] = df.index.to_numpy()
    keys = [tcol] + ([dcol] if dcol else [])
    x = x.sort_values(keys if len(keys) > 1 else keys[0], kind="mergesort")
    return x.reset_index(drop=True)

def _flat(s):
    if isinstance(s.index, pd.MultiIndex):
        s = s.reset_index(level=0, drop=True)
    return s

def _back(s, x):
    return pd.Series(s.to_numpy(), index=x["__oi"].to_numpy(), name=s.name)

def _group_rolling_sum(x, tcol, col, w, mp):
    s = x.groupby(tcol, sort=False)[col].rolling(w, min_periods=mp).sum()
    return s.reset_index(level=0, drop=True)

def _ts_corr(x, tcol, a, b, w, mp):
    """按标的分组的滚动 Pearson 相关 corr(a, b)。"""
    pc = x.groupby(tcol, sort=False)[[a, b]].rolling(w, min_periods=mp).corr()
    out = pc.xs(a, level=-1)[b]
    return out.reset_index(level=0, drop=True)

def _ts_slope(x, tcol, col, w, mp):
    """滚动 OLS 斜率 slope(y ~ 组内时序位置)，O(n) 精确解析式。
    slope = (S_py - center * S_y) / denom，denom = width*(width^2-1)/12。"""
    pos = x.groupby(tcol, sort=False).cumcount().astype(float)
    width = np.minimum(pos + 1.0, float(w))
    center = pos - (width - 1.0) / 2.0
    denom = width * (width ** 2 - 1.0) / 12.0
    yy = x[col].astype(float)
    S_y = _group_rolling_sum(x, tcol, col, w, mp)
    S_py = _group_rolling_sum(x.assign(py=pos * yy), tcol, "py", w, mp)
    slope = (S_py - center * S_y) / denom
    return slope.where(width >= mp)

def _cs_rank(s, dcol, x):
    if dcol:
        return s.groupby(x[dcol]).rank(pct=True)
    return s.rank(pct=True)

def _winsorize_zscore(s, lo=0.01, hi=0.99):
    v = s.astype(float)
    q = v.quantile([lo, hi])
    v = v.clip(q[lo], q[hi])
    sd = v.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return (v - v.mean()) * 0.0
    return (v - v.mean()) / sd

def factor_divergence_tscorr_slope_20_60_close(df: pd.DataFrame) -> pd.Series:
    """因子名称：量价背离 × 波动压缩 × 趋势斜率（上行耗竭预警）
    金融逻辑：趋势完好但量价背离+波动压缩，上行近耗竭，预示反转。
    看空方向：值越大预期收益越差。"""
    tcol = _ticker_col(df); dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    close = x["close"].astype(float); volume = x["volume"].astype(float)
    ret1 = close / g["close"].shift(1) - 1.0
    vchg = volume / g["volume"].shift(1) - 1.0
    x = x.assign(__ret=ret1, __vchg=vchg)
    g = x.groupby(tcol, sort=False)
    corr_pv = _ts_corr(x, tcol, "__ret", "__vchg", 20, 10)
    std20 = _flat(g["__ret"].rolling(20, min_periods=10).std())
    std100 = _flat(g["__ret"].rolling(100, min_periods=60).std())
    comp = std20 / std100
    slope = _ts_slope(x, tcol, "close", 60, 42) / \
        _flat(g["close"].rolling(60, min_periods=42).mean())
    raw = (1.0 - corr_pv) * comp * slope
    return _back(_winsorize_zscore(raw), x).rename(
        "factor_divergence_tscorr_slope_20_60_close")

def factor_shock_skew_rank_20_60_volume(df: pd.DataFrame) -> pd.Series:
    """因子名称：流动性冲击 × 收益偏度 × 波动位置（拥挤交易过热）
    金融逻辑：相对放量+右偏+波动抬升共振时，短期预期收益向均值回归。
    看空方向：值越大预期收益越差。"""
    tcol = _ticker_col(df); dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    close = x["close"].astype(float); volume = x["volume"].astype(float)
    ret1 = close / g["close"].shift(1) - 1.0
    x = x.assign(__ret=ret1)
    g = x.groupby(tcol, sort=False)
    shock = volume / _flat(g["volume"].rolling(20, min_periods=12).mean())
    rank_shock = _cs_rank(shock, dcol, x)
    skew = _flat(g["__ret"].rolling(60, min_periods=36).skew())
    std20 = _flat(g["__ret"].rolling(20, min_periods=10).std())
    std120 = _flat(g["__ret"].rolling(120, min_periods=72).std())
    ratio = std20 / std120
    raw = rank_shock * (1.0 + skew) * ratio
    return _back(_winsorize_zscore(raw), x).rename(
        "factor_shock_skew_rank_20_60_volume")
```

---

## 6. 回测脚本（全量模式，保存为 `backtest_fullmarket.py`）

```python
#!/usr/bin/env python3
"""全市场日线因子回测：IC + 10分位分层 + 多空组合（含单边0.15%成本）。

数据：data/eastmoney_data/*.csv（5552只前复权日线，唯一数据源）
因子：factors.py 两个因子（方向：值越大 -> 预期收益越差，即看空）
口径：信号 T 日收盘、T+1 开盘买入、T+H 开盘卖出、持有 5 天、非重叠持有期年化 252/5。
运行：python3 backtest_fullmarket.py   （全量约 5-10 分钟，CPU 充足）
"""
import csv, json, os, sys, time
from pathlib import Path
import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from factors import (  # noqa: E402
    factor_divergence_tscorr_slope_20_60_close as F1,
    factor_shock_skew_rank_20_60_volume as F2,
)

DATA_DIR = Path("data/eastmoney_data")   # 唯一需要改的路径
OUT = Path("results"); OUT.mkdir(exist_ok=True, parents=True)
START, END = "2022-01-01", "2026-09-01"
HOLD, COST, N_DECILE, TRADING = 5, 0.0015, 10, 252

t0 = time.time()


def load_all() -> pd.DataFrame:
    files = sorted(glob.glob(str(DATA_DIR / "*.csv")))
    print(f"[load] {len(files)} files", flush=True)
    parts = []
    for i, f in enumerate(files):
        df = pd.read_csv(f, encoding="utf-8-sig")
        df = df[["date", "open", "close", "high", "low", "volume"]]
        df["ticker"] = os.path.basename(f).split("_")[0]
        parts.append(df)
        if (i + 1) % 1000 == 0:
            print(f"[load] {i+1}/{len(files)} {time.time()-t0:.0f}s", flush=True)
    d = pd.concat(parts, ignore_index=True)
    d["date"] = pd.to_datetime(d["date"])
    d = d[d["date"] >= START]
    d = d.sort_values(["ticker", "date"]).reset_index(drop=True)
    print(f"[load] rows={len(d)} tickers={d['ticker'].nunique()} "
          f"dates={d['date'].nunique()} ({time.time()-t0:.0f}s)", flush=True)
    return d


def main():
    d = load_all()
    g = d.groupby("ticker", sort=False)
    d["open_next"] = g["open"].shift(-1)          # T+1 开盘（买入价）
    d["open_H"] = g["open"].shift(-HOLD)          # T+H 开盘（卖出价）
    d["ret_hold"] = d["open_H"] / d["open_next"] - 1.0
    d["ret_next"] = g["close"].shift(-1) / d["close"] - 1.0
    print(f"[factor] computing... {time.time()-t0:.0f}s", flush=True)
    d["f1"] = F1(d)
    d["f2"] = F2(d)
    print(f"[factor] done {time.time()-t0:.0f}s", flush=True)

    ic_rows, combos = [], []
    for name, col in (("f1_divergence", "f1"), ("f2_shock_skew", "f2")):
        sub = d.dropna(subset=[col, "ret_next"]).copy()
        gs = sub.groupby("date", sort=True)
        ics = gs.apply(lambda s: s[col].rank().corr(s["ret_next"].rank()),
                       include_groups=False).dropna()
        ic_rows.append({
            "factor": name, "n_days": int(len(ics)),
            "ic_mean": float(ics.mean()),
            "ic_std": float(ics.std()),
            "ic_ir": float(ics.mean() / ics.std()) if ics.std() > 0 else 0.0,
            "ic_pos_ratio": float((ics > 0).mean()),
            "ic_t": float(ics.mean() / ics.std() * np.sqrt(len(ics))) if ics.std() > 0 else 0.0,
        })
        print(f"[IC] {name}: mean={ics.mean():.4f} IR={ic_rows[-1]['ic_ir']:.3f} "
              f"pos={ic_rows[-1]['ic_pos_ratio']:.1%} t={ic_rows[-1]['ic_t']:.1f}", flush=True)

        sub = d.dropna(subset=[col]).copy()
        sub["zsign"] = sub.groupby("date")[col].transform(lambda s: s.rank(pct=True))
        sub["decile"] = (sub["zsign"] * N_DECILE).clip(0, N_DECILE - 1).astype(int)
        dates = np.sort(sub["date"].unique())
        sig_dates = pd.to_datetime([x for i, x in enumerate(dates) if i % HOLD == 0])
        sd = sub[sub["date"].isin(sig_dates)]
        decile_ret = sd.groupby(["date", "decile"], sort=True)["ret_hold"].mean().unstack(1)
        mkt = sd.groupby("date")["ret_hold"].mean()
        # 多空（分位0 - 分位9）
        d0, d9 = decile_ret[0], decile_ret[N_DECILE - 1]
        common = sorted(set(d0.index) & set(d9.index))
        ls = d0.loc[common] - d9.loc[common]
        ls_net = ls - COST * 2
        def stats(s, af=TRADING / HOLD):
            s = s.dropna()
            if len(s) < 10:
                return {}
            return {
                "n_periods": int(len(s)),
                "ann_ret": float(s.mean() * af),
                "ann_vol": float(s.std() * np.sqrt(af)),
                "sharpe": float(s.mean() / s.std() * np.sqrt(af)) if s.std() > 0 else 0.0,
                "max_dd": float((s.cumsum() - s.cumsum().cummax()).min()),
            }
        combos.append({
            "factor": name,
            "decile_ann_ret": {int(k): round(float(v) * TRADING / HOLD, 6)
                               for k, v in decile_ret.mean(axis=0).items()},
            "ls_gross": stats(ls), "ls_net": stats(ls_net), "mkt": stats(mkt),
        })
        print(f"[decile] {name} 年化:{['%.4f' % v for v in decile_ret.mean(axis=0)*TRADING/HOLD]}\n"
              f"[LS] {name} gross:{combos[-1]['ls_gross']}\n"
              f"[LS] {name} net  :{combos[-1]['ls_net']}", flush=True)

    out = {
        "config": {"data": str(DATA_DIR), "start": START, "end": END,
                   "hold": HOLD, "cost_single": COST, "n_decile": N_DECILE},
        "ic": ic_rows,
        "combos": combos,
        "runtime_sec": round(time.time() - t0, 1),
    }
    with open(OUT / "backtest_summary.json", "w") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    with open(OUT / "decile_returns.csv", "w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["factor", "decile", "ann_ret_gross"])
        for c in combos:
            for k, v in c["decile_ann_ret"].items():
                w.writerow([c["factor"], k, v])
    print(f"[done] {time.time()-t0:.0f}s -> {OUT/'backtest_summary.json'}", flush=True)


if __name__ == "__main__":
    import glob
    main()
```

---

## 7. 输出与评判标准

`results/backtest_summary.json` 含：每因子 IC 均值/IR/IC>0 比例/t 值、10 分位年化、多空 gross/net、市场基准、运行时长。

| 指标 | 好坏参考（日频 5 日持有） |
|---|---|
| IC 均值 | 绝对值 > 0.03 值得关注；> 0.05 优 |
| ICIR | > 0.5 较好；> 1 优 |
| t 值 | > 3 才认为统计显著（回测约 1130 个交易日） |
| 分位单调 | 看空因子的理想形态：分位 0 年化最高、分位 9 年化最低（单调递减） |
| 多空净收益（0.15% 单边） | > 0 且跑赢市场基准即算通过；< 0 则因子不可交易 |
| 最大回撤 | 多空组合 < 0.3 可接受 |

**本机 200 只子样本参考（仅为流程验证，不能代表全市场结论）：**
- f2：IC -0.051 / IR -0.383 / t -10.9 / 多空 gross 年化 25% / net 10% / 夏普 0.54
- f1：IC -0.028 / IR -0.151 / t -4.3 / 多空 gross 年化 22% / net 7%

---

## 8. 必须写在结论里的局限（防误导）

1. **幸存者偏差**：5,552 只是当前 A 股全市场（含北交所），不含已退市股票 → 历史截面偏乐观。
2. **前复权**：比例正确但无法做涨停/跌停过滤、绝对价格、市值加权。
3. **0.15% 单边成本是保守值**：A 股佣金约 0.025% + 卖出印花 0.05% + 滑点，5 日持有实际略高；建议另测 0.1%/0.3% 敏感度。
4. **未做中性化**：行业/市值/波动率中性化是下一步（本机有概念映射可做行业中性）。
5. **测试是 IC + 分层体检，不是完整组合回测**：无停牌过滤、无融券成本（A 股做空需融券，多空组合近似"头部做多 + 尾部规避"）。

---

## 9. 可选扩展（本机已发现的数据，远程机可按需启用）

| 数据 | 路径（本机） | 用途 |
|---|---|---|
| 龙虎榜（含上榜后 D1~D30 涨幅） | `/home/ubuntu/marketdata/lhb/lhb_2026-06-01_to_2026-09-02.csv` | 龙虎榜事件因子/炒作持续性 |
| 涨停/炸板/跌停池 | `/home/ubuntu/marketdata/ztpool/` | 市场情绪、连板高度因子 |
| 概念映射（71,548 行） | `/home/ubuntu/marketdata/concepts/concepts_map.csv` | 行业/概念中性化、主题聚类 |
| 板块资金流 | `/home/ubuntu/marketdata/board_flow/industry_20260902.csv` | 板块主力净流入聚合 |
| 网站主库实时增量 | `/home/ubuntu/stock-alert/data/stockdesk.db`（daily_bars 至 2026-09-04） | 回测窗口外的最近 3 天验证（eastmoney 数据截止 9/1，网站库到 9/4） |
