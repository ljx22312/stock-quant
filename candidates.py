"""候选因子库 v2（与 factors.py 同接口：输入 df，输出按原行对齐的 pd.Series）。

候选设计论证（见讨论记录）：
  - mom_raw_20：裸动量对照——证明"A股短期裸动量不可用"（预期 IC 近 0 或为负）
  - trend_quality_20_60：趋势质量——动量 × 距高点惩罚 × 波动惩罚 × 量价配合，
    捕捉"健康的强势"而非"涨得多的票"（预期 IC 为正、多头侧）
  - defensive_shrink_20_60：防守型——波动压缩 × 缩量 × 回踩企稳（预期 IC 为正，低波动替代）

共线性规避：候选 A/B 与现有 f1（量价背离=1-corrP）在 corrP 上有共享项，
体检脚本会输出相关矩阵，相关系数 >0.7 的会标注；候选之间另用 rank 交互降低线性重叠。

防未来函数：与 factors.py 一致——收益一律 close.shift(N) 显式滞后；禁止 shift(-1)。
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from factors import (  # noqa: E402
    _back, _cs_rank, _date_col, _flat, _order, _ticker_col, _ts_corr,
    _winsorize_zscore,
)


# ---------- 候选 0：裸动量（对照，预期不可用） ----------

def factor_mom_raw_20_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：裸 20 日动量（对照组）

    金融逻辑：A 股 5-40 日窗口是反转主导区间，裸动量大概率失效。
    此因子作用是"对照参照物"，验证"不是所有多头侧写法都可行"。

    计算公式：ret20 = close/shift(20) - 1；raw = ret20；输出 winsorize+zscore
    创新点：无——刻意保持朴素以作对照。
    """
    tcol = _ticker_col(df)
    dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    ret20 = x["close"].astype(float) / g["close"].shift(20) - 1.0
    return _back(_winsorize_zscore(ret20), x).rename("factor_mom_raw_20_close")


# ---------- 候选 A：趋势质量（健康强势） ----------

def factor_trend_quality_20_60_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：趋势质量（动量 × 追高惩罚 × 波动惩罚 × 量价配合）

    金融逻辑：A 股单看"涨了多少"会追到山顶；把动量乘上"距 60 日高点的距离惩罚"
    （越接近高点越扣分）、"波动率惩罚"（越稳越加分）、"量价配合度"（上涨放量=健康，
    上涨缩量=虚），剩下来的才是可持有的强势——趋势健康、还在半山腰、有量确认。

    计算公式：
      r_t   = close_t / close.shift(1)_t - 1
      v_t   = volume_t / volume.shift(1)_t - 1
      ret20 = close / close.shift(20) - 1        # 动量骨架
      dist  = close / ts_max(high, 60) - 1       # 追高惩罚（距 60 日高点距离，<=0）
      volp  = 1 / (1 + ts_std(r, 20))            # 波动率惩罚
      corrP = ts_corr(r, v, 20)                  # 量价配合度
      raw   = rank(ret20) * (1 - dist) * volp * (0.5 + corrP)
      输出  = winsorize01(raw, .01, .99) |> zscore

    创新点：四个维度均为乘法交互（无线性加权）；rank 用于横截面相对强弱，
    ts_corr / ts_std / ts_max 构成时序交互；"距高点惩罚"是本因子与裸动量
    的本质区别——它表达的是"买爬坡中的票，不买登顶的票"。
    """
    tcol = _ticker_col(df)
    dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    close = x["close"].astype(float)
    volume = x["volume"].astype(float)

    ret1 = close / g["close"].shift(1) - 1.0
    vchg = volume / g["volume"].shift(1) - 1.0
    x = x.assign(__ret=ret1, __vchg=vchg)
    g = x.groupby(tcol, sort=False)

    ret20 = close / g["close"].shift(20) - 1.0
    dist = close / _flat(g["high"].rolling(60, min_periods=30).max()) - 1.0
    std20 = _flat(g["__ret"].rolling(20, min_periods=10).std())
    volp = 1.0 / (1.0 + std20)
    corr_pv = _ts_corr(x, tcol, "__ret", "__vchg", 20, 10)

    raw = _cs_rank(ret20, dcol, x) * (1.0 - dist) * volp * (0.5 + corr_pv)
    return _back(_winsorize_zscore(raw), x).rename(
        "factor_trend_quality_20_60_close")


# ---------- 候选 B：防守型（波动压缩 × 缩量 × 回踩企稳） ----------

def factor_defensive_shrink_20_60_close(df: pd.DataFrame) -> pd.Series:
    """
    因子名称：防守型企稳（波动压缩 × 缩量 × 回踩）

    金融逻辑：跟随我们已验证"波动压缩"思路的多头侧映射——低波动 + 缩量 +
    离 60 日低点有回升，是"洗筹后企稳待启动"的形态；与 f1/f2 的"背离/过热"
    完全不同侧，且在震荡市（f1/f2 失效的时候）可能独立有效。对风险厌恶
    资金是可选的低波替代。

    计算公式：
      r_t     = close / close.shift(1) - 1
      vol_ratio = ts_std(r,20) / ts_std(r,120)   # 波动压缩
      shrink  = volume / ts_mean(volume, 20)     # 缩量
      recov   = close / ts_min(low, 60) - 1      # 回踩企稳度
      raw     = rank(vol_ratio) * rank(shrink) * rank(recov)
      输出    = winsorize01(raw, .01, .99) |> zscore

    创新点：三个 rank 相乘（横截面 × 横截面 × 横截面），只保留排名交互、
    不混绝对量纲；timing 部分全是向量化 rolling，无逐行循环。
    """
    tcol = _ticker_col(df)
    dcol = _date_col(df)
    x = _order(df, tcol, dcol)
    g = x.groupby(tcol, sort=False)
    close = x["close"].astype(float)
    volume = x["volume"].astype(float)

    ret1 = close / g["close"].shift(1) - 1.0
    x = x.assign(__ret=ret1)
    g = x.groupby(tcol, sort=False)

    std20 = _flat(g["__ret"].rolling(20, min_periods=10).std())
    std120 = _flat(g["__ret"].rolling(120, min_periods=72).std())
    vol_ratio = std20 / std120
    shrink = volume / _flat(g["volume"].rolling(20, min_periods=12).mean())
    recov = close / _flat(g["low"].rolling(60, min_periods=30).min()) - 1.0

    raw = (_cs_rank(vol_ratio, dcol, x) * _cs_rank(shrink, dcol, x) *
           _cs_rank(recov, dcol, x))
    return _back(_winsorize_zscore(raw), x).rename(
        "factor_defensive_shrink_20_60_close")
