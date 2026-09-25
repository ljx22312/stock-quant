#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F6 量价齐涨回避因子（做多打分，低分 = 回避）

一句话逻辑：最近 10 天"量价正相关"最强（放量上涨/缩量下跌的"健康量价"形态）的股票，
次日统计上反而跑输——放量上涨正是散户最爱追的形态，次日倾向回吐。
量价背离（放量下跌/缩量上涨）侧反而有修复倾向。与博主"放量上涨=健康"的叙事相反，
但这是全 A 截面 12/12 年验证的结果。

计算公式：raw = ts_corr(close, volume, 10)      # 时序相关系数，满足截面/时序分组规范
          F6   = -横截面标准化(按日 rank → 缩尾 → Z-Score)
分数越低 → 量价齐升越强 → 应回避。

全量验证（2015-01 ~ 2026-09，约 2800 个交易日）：
  日 IC +0.039，ICIR 0.42（三个因子中最强），12/12 年为正，2024/2025 不衰减反而增强。
  与 F4 相关仅 0.02（近正交）——F4 看"位置+当日量"，F6 看"10 日量价关系"，是两个独立维度，
  组合时 F4+F6 各计一次（取两分均值，最低 2% 为回避池）。

用法：
  python3 F6_vol_price_corr.py --date 2026-09-07
  python3 F6_vol_price_corr.py --date 2026-09-07 --save f6_20260907.csv
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


def factor_F6(df):
    """输入 (date,ticker) 面板（列 close/volume），返回同索引 F6 因子分（低分=回避）"""
    close, vol = df["close"], df["volume"]
    g = close.groupby(level="ticker")
    corr = g.apply(lambda s: s.rolling(10, min_periods=6).corr(vol.loc[s.index]))
    corr.index = corr.index.droplevel(0)
    corr = corr.reindex(df.index)
    return -_post_cs(corr)       # 量价正相关（齐涨）→ 取负 → 低分


def main():
    ap = argparse.ArgumentParser(description="F6 量价齐涨回避因子：当日全市场截面")
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

    f = factor_F6(df)
    ret1 = df["close"].groupby(level="ticker").pct_change()
    if not a.keep_limit_up:
        f[ret1 >= 0.094] = np.nan

    sub = f.loc[last].dropna()
    close_d = df["close"].loc[last].reindex(sub.index)
    out = pd.DataFrame({
        "close": close_d.round(3),
        "ret_T": (ret1.loc[last].reindex(sub.index) * 100).round(2),
        "F6": sub.round(3),
        "F6_pct": sub.rank(pct=True).round(3),
    })
    out["name"] = out.index.map(names).fillna("")
    out = out.sort_values("F6")

    print(f"{last.date()} 全市场有效样本 {len(out)} 只（低分=10日量价齐升，应回避）")
    print(f"\n=== 最差 {a.topn} 只（回避侧）===")
    print(out.head(a.topn).to_string())
    print("\n=== 最好 5 只 ===")
    print(out.tail(5).to_string())
    if a.save:
        out.to_csv(a.save, encoding="utf-8-sig")
        print(f"\n全截面已存: {a.save}")


if __name__ == "__main__":
    main()
