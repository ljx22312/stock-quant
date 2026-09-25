#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""博主规律 → 日线截面因子库（全量验证通过的保留集，2026-09-07 定稿）

来源：/home/ubuntu/stock-quant/persp_probe/（财经博主观点语料 2211 条 → 19 条规律事件研究
→ 11 因子全量验证）。本文件只含通过验收的 5 个因子 + 工具函数，自包含、无外部依赖。

验收口径：全 A 5398 只 × 2015-01 ~ 2026-09-07（2839 交易日），fwd1 与 fwd5 双窗口，
日截面 Spearman IC / ICIR / 分年一致性 / 2022 后是否衰减。结论与数字详见 README.md。

用法：
    df = ...  # MultiIndex (date, ticker)，列 open/high/low/close/volume，sort_index 后
    fac = factor_lib.compute_all(df)          # 全部保留因子，(date,ticker) 索引
    # 单因子：fac["F4_breakout_trap"] 等

工程规范（quant-factor-mining skill）：
- 时序算子包 df.groupby(level="ticker")，横截面算子包 df.groupby(level="date")；
- 防未来函数：全部输入用 close.shift(k)/pct_change()（天然滞后），因子在 T 日收盘后
  可得，预测 T+1 起收益；无任何 shift(-1)；
- 输出统一：横截面 rank → 缩尾(0.005/0.995) → 横截面 Z-Score（做多打分，
  负分区=建议回避；分高=越值得做多）。
"""
import numpy as np
import pandas as pd


# ---------- 横截面工具 ----------
def _dates(s: pd.Series) -> pd.Series:
    """返回与 s 同索引的 date level 值（供 groupby 用）"""
    return s.index.get_level_values("date")


def winsorize_cs(s: pd.Series, lo: float = 0.01, hi: float = 0.99) -> pd.Series:
    """按交易日横截面缩尾（0.01/0.99 分位）"""
    d = _dates(s)
    q1 = s.groupby(d).quantile(lo)
    q2 = s.groupby(d).quantile(hi)
    m1 = d.map(q1.to_dict())
    m2 = d.map(q2.to_dict())
    return s.clip(m1, m2)


def zscore_cs(s: pd.Series) -> pd.Series:
    """按交易日横截面 Z-Score（均值 0、标准差 1）"""
    d = _dates(s)
    mu = s.groupby(d).mean()
    sd = s.groupby(d).std(ddof=0).replace(0, np.nan)
    return (s - d.map(mu.to_dict())) / d.map(sd.to_dict())


def _post(raw: pd.Series) -> pd.Series:
    """统一后处理：先横截面 rank（稳健），再缩尾 + Z-Score"""
    d = _dates(raw)
    r = raw.groupby(d).rank(pct=True)
    return zscore_cs(winsorize_cs(r, 0.005, 0.995))


def _tmean(s: pd.Series, w: int) -> pd.Series:
    """按 ticker 的滚动均值（min_periods = max(3, w//2)）"""
    return s.groupby(level="ticker").transform(
        lambda x: x.rolling(w, min_periods=max(3, w // 2)).mean())


# ---------- F4 放量突破追高陷阱（R14 反向；fwd1 IC +0.039/ICIR 0.33，分年 12/12 正） ----------
def factor_breakout_trap_60d(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：放量突破陷阱 F4
    金融逻辑：日线箱体"放量突破"后均值回归（假突破）显著多于趋势延续——事件研究里
      放量创 60 日新高后 10 日跑输全 A 等权 1.19%（19 条规律中最亏的追入动作），
      "突破买入"是散户亏损动作的统计镜像。连续化评分 =
      距 60 日高点接近度(close/ts_max(close,60)) × 量比(volume/MA5)，越接近/越过
      前高且越放量 → 分越高（输出取负 → 低分=该回避的追高形态）。
    创新点：ts_max 高级时序 × 流动性冲击 × rank 交互。
    """
    close = df["close"]; vol = df["volume"]
    ma5v = _tmean(vol, 5)
    hi60 = close.groupby(level="ticker").transform(
        lambda x: x.rolling(60, min_periods=48).max())
    prox = close / hi60
    score = prox * (vol / ma5v)
    return -_post(score)


# ---------- F6 量价相关（探索定方向；fwd5 IC +0.058/ICIR 0.665 全场冠军，12/12 年正） ----------
def factor_vol_price_corr_10d(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：量价相关 F6
    金融逻辑：10 日滚动量价相关性最高的股票（放量上涨/缩量下跌的"健康量价"）次日-5 日
      反而跑输，量价背离（放量阴跌/缩量上涨）倾向修复——散户最爱的"量价齐升追涨"
      形态次日回吐，与博主"放量上涨=健康量价"的直觉相反（数据裁定方向）。
      与 F4 近正交（F4 打"位置+量"，F6 打"量价同步性"），是全库唯一独立维度。
    创新点：ts_corr 时序交互 × rank（满足 skill 硬约束）。
    """
    close = df["close"]; vol = df["volume"]
    g = close.groupby(level="ticker")
    corr = g.apply(lambda s: s.rolling(10, min_periods=6).corr(vol.loc[s.index]))
    corr.index = corr.index.droplevel(0)
    return -_post(corr.reindex(df.index))


# ---------- G3 冲高回落上影（R7；fwd1 IC +0.046/fwd5 +0.043，ICIR 0.42~0.44，12/12 年正） ----------
def factor_upper_shadow_fade_1d(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：冲高回落上影回吐 G3
    金融逻辑：盘中冲高≥2% 后收盘回吐越深（上影越长）→ 次日-5 日越跑输，上方抛压/
      出货的当日形态；"冲高回落是卖点"（博主高频话术）有统计基础，2022 后不衰减。
      零堆积条件因子：非事件日（未冲高≥2%）置 0，横截面平局多 → 十分位 spread/
      mono 失真，判读以 IC/ICIR/t/分年为准。
    创新点：K 线形态 × 基础算术（回吐幅度/前收）；与 F3 下影对称补上影缺口。
    """
    close = df["close"]; high = df["high"]
    prev = close.groupby(level="ticker").shift(1)
    surge = high / prev - 1
    fade = (high - close) / prev
    ev = surge >= 0.02
    raw = pd.Series(0.0, index=df.index, dtype=float)
    raw[ev] = fade[ev]
    return -_post(raw)


# ---------- G4 滞涨补涨 20 日反转（语料补涨/高低切族；fwd5 IC +0.058/ICIR 0.39，12/12 年正） ----------
def factor_laggard_rev_20d(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：滞涨补涨反转 G4
    金融逻辑：近 20 日涨幅在全市场排名越靠后（滞涨）→ 未来 1-5 日越倾向跑赢，
      A 股短中期截面反转；"找后排补涨/高低切/资金轮动"是语料中唯一未被形式化的
      高频做多话术。与 F2/F4/F6 的"回避追高"互补（卖热买冷），是全库唯一纯做多入口。
      注意与超跌因子的本质区别：它给低分的是"涨太多的"，不是"跌太多的"（后者被
      弱势股次日反抽抵消，F5/G2 因此证伪）。
    创新点：横截面 rank 反转 × 时序动量反向。
    """
    close = df["close"]
    ret20 = close.groupby(level="ticker").pct_change(20)
    return _post(-ret20)


# ---------- G1 放量下跌条件版（R2b 修正；ICIR fwd1 0.51 全场最高，IC 幅度小，定位辅助复核） ----------
def factor_vol_down_cond_5d(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：放量下跌条件版 G1
    金融逻辑：满足"量比≥1.5 且当日跌≥2%"的事件日（资金离场信号，R2b 事件研究
      10/12 年支持），量价越极端（跌越狠、放量越大）次日越弱；非事件日置 0。
      修正了一代 F1：全样本连续版给"微跌日"也打分，被次日超跌反抽污染而反向。
      定位：IC 幅度小（0.02）+ 事件稀疏，作"放量下跌次日回避"的人工复核项，
      不进每日复合分。
    创新点：事件门控 × 流动性冲击 × 基础算术交互。
    """
    close = df["close"]; vol = df["volume"]
    ma5v = _tmean(vol, 5)
    ret1 = close.groupby(level="ticker").pct_change()
    vr = vol / ma5v
    ev = (vr >= 1.5) & (ret1 <= -0.02)
    score = (-ret1 * vr).clip(lower=0)
    raw = pd.Series(0.0, index=df.index, dtype=float)
    raw[ev] = score[ev]
    return -_post(raw)


# ---------- 保留集（2026-09-07 全量验证通过） ----------
FACTORS = {
    "F4_breakout_trap": factor_breakout_trap_60d,      # 回避侧·追高陷阱
    "F6_vol_price_corr": factor_vol_price_corr_10d,    # 回避侧·量价齐升（独立维度）
    "G3_upper_shadow_fade": factor_upper_shadow_fade_1d,  # 回避侧·冲高回落
    "G4_laggard_rev20": factor_laggard_rev_20d,        # 做多侧·滞涨补涨
    "G1_vol_down_cond": factor_vol_down_cond_5d,       # 回避侧·条件放量下跌（辅助）
}

# 回避复合分（snapshot 用）：F4+F6+G3 标准化均值，越低越该回避
AVOID_COLS = ["F4_breakout_trap", "F6_vol_price_corr", "G3_upper_shadow_fade"]
# 低吸池：G4 最高分位（做多侧）
BUY_COL = "G4_laggard_rev20"


def compute_all(df: pd.DataFrame, drop_limit_up: bool = True) -> pd.DataFrame:
    """算全部保留因子，返回 (date,ticker) 索引的因子 DataFrame。
    drop_limit_up=True：T 日涨停(ret1>=9.4%)的成员置 NaN——次日无法买入，
    且一字板样本会扭曲 IC（persp_probe 修复过的坑）。"""
    out = pd.DataFrame(index=df.index)
    for name, fn in FACTORS.items():
        out[name] = fn(df)
    if drop_limit_up:
        ret1 = df["close"].groupby(level="ticker").pct_change()
        out[ret1 >= 0.094] = np.nan
    return out


if __name__ == "__main__":
    # 自检：构造迷你面板（3 只 × 400 日），确认全部因子可算、无异常值
    rng = np.random.default_rng(0)
    n = 400
    frames = []
    for i, code in enumerate(["600000", "000001", "300750"]):
        px = 10 * np.exp(np.cumsum(rng.normal(0.001, 0.02, n)))
        vol = rng.lognormal(15, 0.5, n)
        op = px * (1 + rng.normal(0, 0.005, n))
        hi = np.maximum(op, px) * (1 + rng.uniform(0, 0.01, n))
        lo = np.minimum(op, px) * (1 - rng.uniform(0, 0.01, n))
        dates = pd.bdate_range("2025-01-01", periods=n)
        frames.append(pd.DataFrame({
            "date": np.repeat(dates, 1), "ticker": code,
            "open": op, "high": hi, "low": lo, "close": px, "volume": vol}))
    d = pd.concat(frames).set_index(["date", "ticker"]).sort_index()
    f = compute_all(d)
    print("自检 OK：", f.shape, "NaN 占比", round(float(f.isna().mean().mean()), 3))
