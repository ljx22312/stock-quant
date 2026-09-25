#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F2 放量滞涨背离因子（做多打分，低分 = 回避）

一句话逻辑：成交量显著放大但价格几乎不动（涨幅趋零）＝多空在高位剧烈换手、
出货概率大，事件后 1~10 日统计上持续跑输。

计算公式：raw = (volume / ts_mean(volume,5)) / (1 + |当日涨跌幅|)
          F2   = -横截面标准化(按日 rank → 缩尾 → Z-Score)
分数越低 → 越像「天量但价格不动」→ 应回避。

全量验证（2015-01 ~ 2026-09，约 2800 个交易日）：
  日 IC +0.030，ICIR 0.34，12/12 个年份全部为正。单调性 0.76。
  注意：F2 与 F4 的 Spearman 相关高达 0.92（两个因子都以"当日量比"为主成分），
  是同一个信号的两个近似度量——组合使用时一般只取 F4（F4 全量指标略优），
  F2 可作佐证；不要 F2+F4 同时加权重（等于量比计两遍）。

用法：
  python3 F2_vol_stagnation.py --date 2026-09-07
  python3 F2_vol_stagnation.py --date 2026-09-07 --save f2_20260907.csv
仅依赖 pandas/numpy。数据源：/home/ubuntu/data/eastmoney_data/
"""
import argparse, os
import numpy as np
import pandas as pd


def load_panel(data_dir, tail=320):
    """读全 A 日线 CSV 尾部 tail 行，拼成 (date,ticker) 面板（仅 close/volume，float32）"""
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
        for c in ["close", "volume"]:
            d[c] = d[c].astype("float32")
        d["ticker"] = code
        names[code] = fn[7:-4]
        frames.append(d)
    df = pd.concat(frames, ignore_index=True)
    return df.set_index(["date", "ticker"]).sort_index(), names


def _post_cs(raw):
    """统一横截面后处理：按日 rank(pct) → 缩尾(0.005/0.995) → Z-Score"""
    d = raw.index.get_level_values("date")
    r = raw.groupby(d).rank(pct=True)
    lo = r.groupby(d).quantile(0.005)
    hi = r.groupby(d).quantile(0.995)
    r = r.clip(d.map(lo.to_dict()), d.map(hi.to_dict()))
    mu = r.groupby(d).mean()
    sd = r.groupby(d).std(ddof=0).replace(0, np.nan)
    return (r - d.map(mu.to_dict())) / d.map(sd.to_dict())


def factor_F2(df):
    """输入 (date,ticker) 面板（列 close/volume），返回同索引 F2 因子分（低分=回避）"""
    close, vol = df["close"], df["volume"]
    ma5v = vol.groupby(level="ticker").transform(
        lambda x: x.rolling(5, min_periods=3).mean())
    ret1 = close.groupby(level="ticker").pct_change()
    raw = (vol / ma5v) / (1 + ret1.abs())        # 放量 + 近乎平盘 → raw 最大
    return -_post_cs(raw)


def main():
    ap = argparse.ArgumentParser(description="F2 放量滞涨背离因子：当日全市场截面")
    ap.add_argument("--data", default="/home/ubuntu/data/eastmoney_data")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认数据末日")
    ap.add_argument("--tail", type=int, default=320, help="每只股票读尾部行数")
    ap.add_argument("--topn", type=int, default=20, help="打印最差（回避侧）只数")
    ap.add_argument("--save", default=None, help="保存当日全截面 CSV 的路径")
    ap.add_argument("--keep-limit-up", action="store_true",
                    help="默认剔除当日涨停(ret>=9.4%)股；加此参数则保留")
    a = ap.parse_args()

    df, names = load_panel(a.data, a.tail)
    if a.date is None:
        last = df.index.get_level_values("date").max()
    else:
        last = pd.Timestamp(a.date)
    if last not in df.index.get_level_values("date"):
        raise SystemExit(f"数据中没有 {a.date}（数据末日 {df.index.get_level_values('date').max().date()}）")

    f = factor_F2(df)
    ret1 = df["close"].groupby(level="ticker").pct_change()
    if not a.keep_limit_up:
        f[ret1 >= 0.094] = np.nan

    sub = f.loc[last].dropna()
    close_d = df["close"].loc[last].reindex(sub.index)
    out = pd.DataFrame({
        "close": close_d.round(3),
        "ret_T": (ret1.loc[last].reindex(sub.index) * 100).round(2),
        "F2": sub.round(3),
        "F2_pct": sub.rank(pct=True).round(3),
    })
    out["name"] = out.index.map(names).fillna("")
    out = out.sort_values("F2")

    print(f"{last.date()} 全市场有效样本 {len(out)} 只（低分=放量滞涨形态，应回避）")
    print(f"\n=== 最差 {a.topn} 只（回避侧）===")
    print(out.head(a.topn).to_string())
    print("\n=== 最好 5 只 ===")
    print(out.tail(5).to_string())
    if a.save:
        out.to_csv(a.save, encoding="utf-8-sig")
        print(f"\n全截面已存: {a.save}")


if __name__ == "__main__":
    main()
