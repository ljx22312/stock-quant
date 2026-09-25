#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""ai_shadow_asymmetry_price_rank_20d 影线不对称 × 价格偏离（探索因子，方向待全量验证）

一句话逻辑：当日 K 线的上/下影线不对称程度，相对个股自身 20 日历史是否异常
（时序 z），再乘当前价格相对 20 日均线的偏离——因子值一端是"高位 + 上影异常长"
的见顶嫌疑形态，另一端是"低位 + 下影异常长"的探底承接形态。

⚠️ 状态：**未做全量验证，方向未定**。若全量 IC 为负（高位长上影组次日-5日跑输，
与 G3 同族预期），按 factor_lib 的"做多打分"约定应取 -z 使用；验证通过前勿入
快照复合分、勿直接用于交易决策。

计算公式（index=(date,ticker)，全部按 ticker 分组算时序、按 date 分组做截面）：
  upper = high - max(open, close)                     # 上影线
  lower = min(open, close) - low                      # 下影线
  shadow_ratio = (upper - lower) / (high - low + eps) # >0 上影偏长，<0 下影偏长
  z_shadow = (shadow_ratio - ts_mean(shadow_ratio,20)) / (ts_std(shadow_ratio,20) + eps)
  price_dev = close / ts_mean(close,20) - 1           # >0 价格偏高
  raw = z_shadow * price_dev
  out = 横截面 rank(raw) → 缩尾 1%/99% → Z-Score      # 高 = 高位长上影（见顶嫌疑端）

防未来函数：全部输入 T 日收盘后可得（rolling 含当日），无任何 shift(-1)。

用法（在本机数据目录上）：
  python3 ai_shadow_asymmetry_price_rank_20d.py --date 2026-09-07   # 当日全市场截面
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


def _post_cs(raw, lo=0.01, hi=0.99):
    """统一横截面后处理：按日 rank(pct) → 缩尾(lo/hi) → Z-Score（按本因子规格 1%/99%）"""
    d = raw.index.get_level_values("date")
    r = raw.groupby(d).rank(pct=True)
    q1 = r.groupby(d).quantile(lo)
    q2 = r.groupby(d).quantile(hi)
    r = r.clip(d.map(q1.to_dict()), d.map(q2.to_dict()))
    mu = r.groupby(d).mean()
    sd = r.groupby(d).std(ddof=0).replace(0, np.nan)
    return (r - d.map(mu.to_dict())) / d.map(sd.to_dict())


def factor_ai_shadow_asymmetry_price_rank_20d(df):
    """输入 (date,ticker) 面板（列 open/high/low/close/volume），返回同索引因子分。
    高分 = 高位 + 上影异常长（见顶嫌疑端）；方向未全量验证，用前先跑 IC 定正负。"""
    high, low, close, opn = df["high"], df["low"], df["close"], df["open"]
    rng = (high - low).clip(lower=EPS)
    upper = high - np.maximum(opn, close)
    lower = np.minimum(opn, close) - low
    shadow_ratio = (upper - lower) / rng                # >0 上影偏长
    z_shadow = (shadow_ratio - _ts_mean(shadow_ratio, 20)) / (_ts_std(shadow_ratio, 20) + EPS)
    price_dev = close / _ts_mean(close, 20) - 1
    return _post_cs(z_shadow * price_dev)


def main():
    ap = argparse.ArgumentParser(description="影线不对称×价格偏离因子：当日全市场截面（方向待验证）")
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

    f = factor_ai_shadow_asymmetry_price_rank_20d(df)
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

    print(f"{last.date()} 全市场有效样本 {len(out)} 只（高分=高位长上影见顶嫌疑端，"
          f"低分=低位长下影承接端；方向未验证，两端都打印）")
    print(f"\n=== 最高 {a.topn} 只（见顶嫌疑端，若 IC 为负应回避）===")
    print(out.tail(a.topn).to_string())
    print(f"\n=== 最低 {a.topn} 只（探底承接端）===")
    print(out.head(a.topn).to_string())
    if a.save:
        out.to_csv(a.save, encoding="utf-8-sig")
        print(f"\n全截面已存: {a.save}")


if __name__ == "__main__":
    main()
