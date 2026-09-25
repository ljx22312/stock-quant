#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_lower_shadow_volskew_30d 下影长度 × 量能偏度 × 价格偏离（探索因子，方向待全量验证）

一句话逻辑：当日下影线长度相对个股自身 30 日历史是否异常（时序 z），乘 30 日量能
偏度的负值（sk<0 一侧 = 量能平稳/萎缩，无放量脉冲），再乘价格相对 30 日均线的偏离，
三者交互捕捉"长下影 + 量能形态 + 价格位置"的组合——数值两端分别对应承接/出货语境。

⚠️ 状态：**未做全量验证，方向未定**（公式交互含负号项，方向需全量 IC 裁定）。
验证通过前勿入快照复合分、勿直接用于交易决策。实现为用户给定公式原样整理，
工具函数命名对齐 factor_lib 约定。

计算公式（index=(date,ticker)；时序按 ticker 分组，截面按 date 分组）：
  sr_lower = min(open, close) - low                    # 下影线长度
  z = (sr_lower - ts_mean(sr_lower,30)) / (ts_std(sr_lower,30) + eps)   # 下影时序 z
  vol1 = volume.pct_change().shift(1)                  # 当日量变化再滞后 1 日（更保守防未来）
  sk = ts_skew(vol1, 30)                               # 30 日量变化偏度（min_periods=15）
  pd_ = close / ts_mean(close,30) - 1                  # 价格偏离 30 日均线
  raw = z * (-sk) * pd_ → 逐日横截面 rank(pct) → 缩尾 1%/99% → Z-Score

防未来函数：全部输入 T 日收盘后可得；量变化额外 shift(1) 只用到 T-1 信息；无 shift(-1)。

用法（在本机数据目录上）：
  python3 factor_lower_shadow_volskew_30d.py --date 2026-09-07   # 当日全市场截面
仅依赖 pandas/numpy。数据源：/home/ubuntu/data/eastmoney_data/（代码_名称.csv，前复权日线）
"""
import argparse, os
import numpy as np
import pandas as pd

EPS = 1e-9


def load_panel(data_dir, tail=320):
    """读全 A 日线 CSV 尾部 tail 行，拼成 (date,ticker) 面板（OHLCV，float32）"""
    names, frames = {}, []
    for fn in sorted(os.listdir(data_dir)):
        if not fn.endswith(".csv"):
            continue
        code = fn[:6]
        if not code.isdigit() or len(fn) < 8 or fn[6] != "_":
            continue
        d = pd.read_csv(os.path.join(data_dir, fn), encoding="utf-8-sig", engine="c")
        if len(d) < 10:
            continue
        d = d.iloc[-tail:]
        d["date"] = pd.to_datetime(d["date"])
        for c in ["open", "high", "low", "close", "volume"]:
            d[c] = d[c].astype("float32")
        d["ticker"] = code
        names[code] = fn[7:-4]
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    return df.set_index(["date", "ticker"]).sort_index(), names


def _ts_mean(s, w):
    """按 ticker 滚动均值（min_periods = max(3, w//2)），防跨股票串味"""
    return s.groupby(level="ticker").transform(
        lambda x: x.rolling(w, min_periods=max(3, w // 2)).mean())


def _ts_std(s, w):
    """按 ticker 滚动标准差（与 _ts_mean 同窗口口径）"""
    return s.groupby(level="ticker").transform(
        lambda x: x.rolling(w, min_periods=max(3, w // 2)).std())


def _winsorize_z(r, lo=0.01, hi=0.99):
    """缩尾(lo/hi) + 横截面 Z-Score。输入 r 已完成逐日 rank(pct)，不再重复排名。"""
    d = r.index.get_level_values("date")
    q1 = r.groupby(d).quantile(lo)
    q2 = r.groupby(d).quantile(hi)
    r = r.clip(d.map(q1.to_dict()), d.map(q2.to_dict()))
    mu = r.groupby(d).mean()
    sd = r.groupby(d).std(ddof=0).replace(0, np.nan)
    return (r - d.map(mu.to_dict())) / d.map(sd.to_dict())


def factor_lower_shadow_volskew_30d(df):
    """输入 (date,ticker) 面板（列 open/high/low/close/volume），返回同索引因子分。
    公式为用户给定实现：下影时序 z × (-30日量变化偏度) × 30日价格偏离。
    方向未全量验证，用前先跑 IC 定正负。"""
    sr_lower = np.minimum(df["open"], df["close"]) - df["low"]           # 下影线长度
    z = (sr_lower - _ts_mean(sr_lower, 30)) / (_ts_std(sr_lower, 30) + EPS)  # 时序 z
    vol1 = df["volume"].groupby(level="ticker", sort=False).pct_change().shift(1)  # 量变化(shift1 防未来)
    sk = vol1.groupby(level="ticker", sort=False).transform(
        lambda x: x.rolling(30, min_periods=15).skew())                  # 30 日量能偏度
    pd_ = df["close"] / _ts_mean(df["close"], 30) - 1                    # 价格偏离 30 日均线
    raw = (z * (-sk) * pd_).groupby(level="date", sort=False).rank(pct=True)  # 逐日横截面排名
    return _winsorize_z(raw)                                             # 1%/99% 缩尾 + zscore


def main():
    ap = argparse.ArgumentParser(description="下影×量能偏度×价格偏离因子：当日全市场截面（方向待验证）")
    ap.add_argument("--data", default="/home/ubuntu/data/eastmoney_data")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认数据末日")
    ap.add_argument("--tail", type=int, default=320, help="每只股票读尾部行数（覆盖滚动窗口）")
    ap.add_argument("--topn", type=int, default=20, help="两端各打印只数")
    ap.add_argument("--save", default=None, help="保存当日全截面 CSV 的路径")
    ap.add_argument("--keep-limit-up", action="store_true",
                    help="默认剔除当日涨停(ret>=9.4%)股（次日买不进）；加此参数则保留")
    a = ap.parse_args()

    df, names = load_panel(a.data, a.tail)
    if a.date is None:
        last = df.index.get_level_values("date").max()
    else:
        last = pd.Timestamp(a.date)
    if last not in df.index.get_level_values("date"):
        raise SystemExit(f"数据中没有 {a.date}（数据末日 {df.index.get_level_values('date').max().date()}）")

    f = factor_lower_shadow_volskew_30d(df)
    ret1 = df["close"].groupby(level="ticker").pct_change()
    if not a.keep_limit_up:
        f[ret1 >= 0.094] = np.nan

    sub = f.loc[last].dropna()
    out = pd.DataFrame({
        "close": df["close"].loc[last].reindex(sub.index).round(3),
        "ret_T": (ret1.loc[last].reindex(sub.index) * 100).round(2),
        "fac": sub.round(3),
        "fac_pct": sub.rank(pct=True).round(3),
    })
    out["name"] = out.index.map(names).fillna("")
    out = out.sort_values("fac")

    print(f"{last.date()} 全市场有效样本 {len(out)} 只（方向未验证，两端都打印）")
    print(f"\n=== 最高 {a.topn} 只 ===")
    print(out.tail(a.topn).to_string())
    print(f"\n=== 最低 {a.topn} 只 ===")
    print(out.head(a.topn).to_string())
    if a.save:
        out.to_csv(a.save, encoding="utf-8-sig")
        print(f"\n全截面已存: {a.save}")


if __name__ == "__main__":
    main()
