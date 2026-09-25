#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例策略 2：ETF 动量轮动。

信号（T 收盘计算，引擎 T+1 执行）：
  每 `rebalance` 个交易日调仓一次（其余日期权重沿用，由引擎 ffill 承担）；
  lookback 日收益率排名，取 top_k 等权持有；
  absolute_momentum=True 时，仅持有 lookback 收益 > 0 的标的（其余仓位留现金）。

只在本机已验证数据的 ETF 池上使用；动量窗口需 ≥ 20 日避免噪音。
"""
from __future__ import annotations

import pandas as pd

from ..data import Panel
from ..engine import Strategy


class EtfMomentum(Strategy):
    name = "etf_momentum"

    def __init__(self, lookback: int = 60, top_k: int = 2,
                 rebalance: int = 20, absolute_momentum: bool = True):
        assert lookback >= 5 and 1 <= top_k and rebalance >= 1
        self.lookback, self.top_k = lookback, top_k
        self.rebalance, self.absolute = rebalance, absolute_momentum

    def target_weights(self) -> pd.DataFrame:
        close = self.panel.close
        mom = close / close.shift(self.lookback) - 1.0
        dates = close.index
        rows = []
        held = pd.Series(0.0, index=close.columns)
        for i, d in enumerate(dates):
            if i >= self.lookback and i % self.rebalance == 0:
                m = mom.loc[d].dropna()
                m = m[m > 0] if self.absolute else m.sort_values(ascending=False)
                picks = m.sort_values(ascending=False).head(self.top_k).index.tolist()
                held = pd.Series(0.0, index=close.columns)
                if picks:
                    held[picks] = 1.0 / len(picks)
            rows.append(held.copy())
        return pd.DataFrame(rows, index=dates, columns=close.columns)
