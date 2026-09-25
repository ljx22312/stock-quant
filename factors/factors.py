"""日频量价因子库（原创）。

数据约定：df 为日频面板，需含 close/open/high/low/volume 列；
         标的列自动识别（ticker 或 symbol，无则报错），日期列可选（date，用于横截面操作）。
         所有时序算子按标的分组；无日期列时横截面排名退化为时序排名（见 docstring 说明）。
防未来函数：价格收益一律以 close.shift(1)（昨收）为基准显式滞后；禁止任何引用未来交易日数据的负向位移。
"""
from __future__ import annotations

import numpy as np
import pandas as pd


# ---------- 共享工具 ----------

def _ticker_col(df: pd.DataFrame) -> str:
    for c in ("ticker", "symbol"):
        if c in df.columns:
            return c
    raise ValueError("df 需要包含 ticker 或 symbol 列")


def _date_col(df: pd.DataFrame) -> str | None:
    return "date" if "date" in df.columns else None


def _order(df: pd.DataFrame, tcol: str, dcol: str | None) -> pd.DataFrame:
    """按 (ticker, date) 稳定排序并重建 RangeIndex，同时保留原始行标签以便因子输出回映射。"""
    x = df.copy()
    x["__oi"] = df.index.to_numpy()
    keys = [tcol] + ([dcol] if dcol else [])
    x = x.sort_values(keys if len(keys) > 1 else keys[0], kind="mergesort")
    return x.reset_index(drop=True)


def _flat(s: pd.Series) -> pd.Series:
    """groupby-rolling 结果统一展平为按 x 行序对齐的 Series（去掉组键层）。"""
    if isinstance(s.index, pd.MultiIndex):
        s = s.reset_index(level=0, drop=True)
    return s


def _back(s: pd.Series, x: pd.DataFrame) -> pd.Series:
    """把按 x 行序的 Series 映射回 df 的原始行标签（直接按位置，无标签对齐歧义）。"""
    return pd.Series(s.to_numpy(), index=x["__oi"].to_numpy(), name=s.name)


def _group_rolling_sum(x: pd.DataFrame, tcol: str, col: str, w: int, mp: int) -> pd.Series:
    """按标的分组的滚动求和，结果按原索引对齐（与 x 行序一致）。"""
    s = x.groupby(tcol, sort=False)[col].rolling(w, min_periods=mp).sum()
    return s.reset_index(level=0, drop=True)


def _ts_corr(x: pd.DataFrame, tcol: str, a: str, b: str, w: int, mp: int) -> pd.Series:
    """按标的分组的滚动 Pearson 相关 corr(a, b)。"""
    pc = x.groupby(tcol, sort=False)[[a, b]].rolling(w, min_periods=mp).corr()
    out = pc.xs(a, level=-1)[b]
    return out.reset_index(level=0, drop=True)


def _ts_slope(x: pd.DataFrame, tcol: str, col: str, w: int, mp: int) -> pd.Series:
    """滚动 OLS 斜率 slope(y ~ 组内时序位置)，完全向量化、O(n)。

    窗口内位置 p 为等差序列，其中心 center 与分母 denom=width*(width^2-1)/12 可解析，
    slope = (S_py - center * S_y) / denom，其中 S_py / S_y 为滚动和。
    部分窗口（行数 < w）时 width/center/denom 按实际长度计算，数学上精确成立。
    """
    pos = x.groupby(tcol, sort=False).cumcount().astype(float)
    width = np.minimum(pos + 1.0, float(w))
    center = pos - (width - 1.0) / 2.0
    denom = width * (width ** 2 - 1.0) / 12.0
    yy = x[col].astype(float)
    S_y = _group_rolling_sum(x, tcol, col, w, mp)
    S_py = _group_rolling_sum(x.assign(py=pos * yy), tcol, "py", w, mp)
    slope = (S_py - center * S_y) / denom
    slope = slope.where(width >= mp)  # 不足 min_periods 置 NaN
    return slope


def _cs_rank(s: pd.Series, dcol: str | None, x: pd.DataFrame) -> pd.Series:
    """横截面排名（按日期分组）；无日期列时退化为全序列排名（单标的场景）。"""
    if dcol:
        return s.groupby(x[dcol]).rank(pct=True)
    return s.rank(pct=True)


def _winsorize_zscore(s: pd.Series, lo: float = 0.01, hi: float = 0.99) -> pd.Series:
    """缩尾（1%/99% 分位截断）+ Z-Score 标准化（均值 0 / 标准差 1），NaN 位置保留。"""
    v = s.astype(float)
    q = v.quantile([lo, hi])
    v = v.clip(q[lo], q[hi])
    sd = v.std(ddof=0)
    if not np.isfinite(sd) or sd == 0:
        return (v - v.mean()) * 0.0  # 常量序列输出全 0
    return (v - v.mean()) / sd


# ---------- 因子 1 ----------

def factor_divergence_tscorr_slope_20_60_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：量价背离 × 波动压缩 × 趋势斜率（上行耗竭预警）

    金融逻辑：趋势未被破坏（60 日斜率 > 0）是多数人继续看多的理由，但若同期出现
    量价背离（20 日收益-量能变化相关性转负：价升量缩 / 价跌量增，说明上涨靠记忆而非
    真金白银），且短端波动率相对长端收缩（行情进入"冷冻区"），则上行往往已近耗竭，
    随后出现反转/回调的概率显著上升。因子值越大，预期未来收益越差（看空信号方向）。

    计算公式：
      r_t   = close_t / close.shift(1)_t - 1
      v_t   = volume_t / volume.shift(1)_t - 1
      corrP = ts_corr(r, v, 20)
      div   = 1 - corrP                    # 量价背离度
      comp  = ts_std(r, 20) / ts_std(r, 100)   # 波动压缩比
      slope = ts_slope(close, 60) / ts_mean(close, 60)   # 归一化趋势斜率
      raw   = div * comp * slope           # 纯乘法交互（非线性组合）
      输出  = winsorize01(raw, 0.01, 0.99) |> zscore

    创新点：以 ts_corr(价格收益, 量能变化) 刻画量价背离这一"缺量"信号，交给
    ts_std 比（时序统计）与 ts_slope（高级时序）做三重乘法交互——三者缺一不可，
    不存在任何线性加权，且方向由斜率符号天然携带（下跌趋势中背离因子自然减弱）。
    """
    tcol = _ticker_col(df)
    dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    close = x["close"].astype(float)
    volume = x["volume"].astype(float)

    # 防未来函数：收益一律以昨收 shift(1) 为基准
    ret1 = close / g["close"].shift(1) - 1.0
    vchg = volume / g["volume"].shift(1) - 1.0
    x = x.assign(__ret=ret1, __vchg=vchg)
    g = x.groupby(tcol, sort=False)  # 重建分组，确保可访问 __ret/__vchg

    corr_pv = _ts_corr(x, tcol, "__ret", "__vchg", 20, 10)          # 20 日量价相关
    std20 = _flat(g["__ret"].rolling(20, min_periods=10).std())
    std100 = _flat(g["__ret"].rolling(100, min_periods=60).std())
    comp = std20 / std100                                           # 波动压缩比
    slope = _ts_slope(x, tcol, "close", 60, 42) / \
        _flat(g["close"].rolling(60, min_periods=42).mean())        # 归一化趋势斜率

    diverg = 1.0 - corr_pv                                          # 量价背离度
    raw = diverg * comp * slope
    return _back(_winsorize_zscore(raw), x).rename(
        "factor_divergence_tscorr_slope_20_60_close")


# ---------- 因子 2 ----------

def factor_shock_skew_rank_20_60_volume(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：流动性冲击 × 收益偏度 × 波动位置（拥挤交易过热）

    金融逻辑：当日量能相对 20 日均量的冲击强度在全市场（当日横截面）中的排名，
    衡量"相对放量强度"；若同时收益分布右偏（skew > 0，偶发大阳线、尾部被透支），
    且短端波动率相对长端抬升（ratio > 1），则典型地对应投机资金拥挤进场——
    横截面相对放量 + 右偏 + 波动抬升三者共振时，短期预期收益往往向均值回归（反转）。
    因子值越大，预期未来收益越差（看空信号方向）。

    计算公式：
      r_t   = close_t / close.shift(1)_t - 1
      shock = volume_t / ts_mean(volume, 20)     # 流动性冲击
      rankS = cross_rank(shock, by=date)         # 横截面排名（相对放量强度）
      skew  = ts_skewness(r, 60)                 # 高阶矩：收益偏度
      ratio = ts_std(r, 20) / ts_std(r, 120)     # 波动位置
      raw   = rankS * (1 + skew) * ratio         # 纯乘法交互（非线性组合）
      输出  = winsorize01(raw, 0.01, 0.99) |> zscore

    创新点：以 rank（横截面处理）捕捉相对强弱、以 ts_skewness（高级时序）刻画
    高阶矩尾部风险、以 ts_mean / ts_std 统计量做交互：放量强度只有叠加右偏与
    波动抬升才成立为"过热"信号；三个维度相乘而非线性叠加，避免与单一量价因子
    共线。
    """
    tcol = _ticker_col(df)
    dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    close = x["close"].astype(float)
    volume = x["volume"].astype(float)

    ret1 = close / g["close"].shift(1) - 1.0     # 防未来函数：昨收 shift(1) 为基准
    x = x.assign(__ret=ret1)
    g = x.groupby(tcol, sort=False)  # 重建分组，确保可访问 __ret

    shock = volume / _flat(g["volume"].rolling(20, min_periods=12).mean())  # 流动性冲击
    rank_shock = _cs_rank(shock, dcol, x)                             # 横截面排名
    skew = _flat(g["__ret"].rolling(60, min_periods=36).skew())       # 收益偏度
    std20 = _flat(g["__ret"].rolling(20, min_periods=10).std())
    std120 = _flat(g["__ret"].rolling(120, min_periods=72).std())
    ratio = std20 / std120                                            # 波动位置

    raw = rank_shock * (1.0 + skew) * ratio
    return _back(_winsorize_zscore(raw), x).rename(
        "factor_shock_skew_rank_20_60_volume")
