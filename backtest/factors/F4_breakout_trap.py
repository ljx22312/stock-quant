#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""F4 放量突破追高陷阱因子（做多打分，低分 = 回避）

一句话逻辑：日线放量逼近/创 60 日新高后，A 股个股统计上倾向回落（假突破多于真突破），
「突破买入」是散户亏损动作的统计镜像。

计算公式：raw = (close / ts_max(close,60)) * (volume / ts_mean(volume,5))
          F4   = -横截面标准化(按日 rank → 缩尾 0.5%/99.5% → Z-Score)
分数越低（越负）→ 越接近「放量冲前高」形态 → 次日/数日统计上跑输，应回避。

全量验证（2015-01 ~ 2026-09，5552 只，约 2800 个交易日，见 factor_report.csv）：
  日 IC +0.039，ICIR 0.33，12/12 个年份全部为正（含 2022 后），十分位单调性 0.99。
  即：这种股票"平均倾向"跑输，不是必跌，只能当参考维度。

用法（在本机数据目录上）：
  python3 F4_breakout_trap.py --date 2026-09-07          # 看某日全市场截面，默认打印最差 20 只
  python3 F4_breakout_trap.py --date 2026-09-07 --save f4_20260907.csv   # 保存全截面
仅依赖 pandas/numpy。数据源：/home/ubuntu/data/eastmoney_data/（代码_名称.csv，前复权日线）
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
    """统一横截面后处理：按日 rank(pct) → 缩尾(0.005/0.995) → Z-Score（与全量验证口径一致）"""
    d = raw.index.get_level_values("date")
    r = raw.groupby(d).rank(pct=True)
    lo = r.groupby(d).quantile(0.005)
    hi = r.groupby(d).quantile(0.995)
    r = r.clip(d.map(lo.to_dict()), d.map(hi.to_dict()))
    mu = r.groupby(d).mean()
    sd = r.groupby(d).std(ddof=0).replace(0, np.nan)
    return (r - d.map(mu.to_dict())) / d.map(sd.to_dict())


def factor_F4(df):
    """输入 (date,ticker) 面板（列 close/volume），返回同索引 F4 因子分（低分=回避）"""
    close, vol = df["close"], df["volume"]
    ma5v = vol.groupby(level="ticker").transform(
        lambda x: x.rolling(5, min_periods=3).mean())
    hi60 = close.groupby(level="ticker").transform(
        lambda x: x.rolling(60, min_periods=48).max())
    raw = (close / hi60) * (vol / ma5v)          # 越近前高且越放量 → raw 越大
    return -_post_cs(raw)


def main():
    ap = argparse.ArgumentParser(description="F4 放量突破追高陷阱因子：当日全市场截面")
    ap.add_argument("--data", default="/home/ubuntu/data/eastmoney_data")
    ap.add_argument("--date", default=None, help="YYYY-MM-DD，默认数据末日")
    ap.add_argument("--tail", type=int, default=320, help="每只股票读尾部行数（覆盖滚动窗口）")
    ap.add_argument("--topn", type=int, default=20, help="打印最差（回避侧）只数")
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

    f = factor_F4(df)
    ret1 = df["close"].groupby(level="ticker").pct_change()
    if not a.keep_limit_up:
        f[ret1 >= 0.094] = np.nan  # 涨停日置空，与全量验证口径一致

    sub = f.loc[last].dropna()
    close_d = df["close"].loc[last].reindex(sub.index)
    out = pd.DataFrame({
        "close": close_d.round(3),
        "ret_T": (ret1.loc[last].reindex(sub.index) * 100).round(2),
        "F4": sub.round(3),
        "F4_pct": sub.rank(pct=True).round(3),   # 0~1，越小越接近追高陷阱
    })
    out["name"] = out.index.map(names).fillna("")
    out = out.sort_values("F4")

    print(f"{last.date()} 全市场有效样本 {len(out)} 只（低分=放量冲前高形态，应回避）")
    print(f"\n=== 最差 {a.topn} 只（回避侧）===")
    print(out.head(a.topn).to_string())
    print("\n=== 最好 5 只（远离前高/缩量，非本因子关注侧）===")
    print(out.tail(5).to_string())
    if a.save:
        out.to_csv(a.save, encoding="utf-8-sig")
        print(f"\n全截面已存: {a.save}")


if __name__ == "__main__":
    main()
