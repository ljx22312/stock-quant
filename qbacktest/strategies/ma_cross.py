#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例策略 1：双均线择时（多头/空仓，单资产或每资产独立择时）。

信号（T 收盘计算，引擎 T+1 执行）：
  MA_fast(close) > MA_slow(close) → 目标权重 1，否则 0。
多资产时每只独立判断、权重归一（等权拆分 1/n，避免隐式加杠杆）。
"""
from __future__ import annotations

import pandas as pd

from ..data import Panel
from ..engine import Strategy


class MaCross(Strategy):
    name = "ma_cross"

    def __init__(self, fast: int = 20, slow: int = 60):
        assert fast < slow, "fast 必须小于 slow"
        self.fast, self.slow = fast, slow

    def target_weights(self) -> pd.DataFrame:
        close = self.panel.close
        ma_f = close.rolling(self.fast).mean()
        ma_s = close.rolling(self.slow).mean()
        long_sig = (ma_f > ma_s).fillna(False)
        n = len(close.columns)
        return long_sig.astype(float) / n  # 独立信号等权归一（Σw ≤ 1）
