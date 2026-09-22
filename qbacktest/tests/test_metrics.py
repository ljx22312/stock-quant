#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""指标测试：手工验算常量对照。

序列：[100, 110, 99, 108.9]
  日收益 r = [+10%, -10%, +10%]
  total = 8.9%；最大回撤 = 99/110 - 1 = -10%（峰 idx1 → 谷 idx2，随后未创新高前回撤收窄）
  mean(r) = 1/30；std(r, ddof=1) = sqrt(0.013333..) = 0.1154701
  sharpe = (1/30)/0.1154701×√252 = 4.58258
  下行偏差 = sqrt(mean(min(r,0)²)) = sqrt(0.01/3) = 0.0577350
  sortino = (1/30)/0.0577350×√252 = 9.16515
"""
import unittest

import pandas as pd

from qbacktest.metrics import compute_metrics, format_metrics


class TestMetrics(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        eq = pd.Series([100.0, 110.0, 99.0, 108.9],
                       index=pd.Index(["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]))
        cls.m = compute_metrics(eq)

    def test_total_and_cagr(self):
        self.assertAlmostEqual(self.m["total_return"], 0.089, places=10)
        years = 4 / 252
        self.assertAlmostEqual(self.m["cagr"], (1.089) ** (1 / years) - 1, places=10)

    def test_max_drawdown(self):
        self.assertAlmostEqual(self.m["max_drawdown"], -0.10, places=10)
        self.assertEqual(self.m["dd_peak"], "2024-01-02")
        self.assertEqual(self.m["dd_trough"], "2024-01-03")

    def test_sharpe_sortino_hand_computed(self):
        self.assertAlmostEqual(self.m["sharpe"], 4.5825757, places=5)
        self.assertAlmostEqual(self.m["sortino"], 9.1651514, places=5)

    def test_vol_and_winrate(self):
        import numpy as np
        r = pd.Series([0.1, -0.1, 0.1])
        self.assertAlmostEqual(self.m["ann_vol"], r.std(ddof=1) * np.sqrt(252), places=10)
        self.assertAlmostEqual(self.m["win_rate_daily"], 2 / 3, places=10)

    def test_format_line_smoke(self):
        line = format_metrics(self.m)
        self.assertIn("总收益 8.90%", line)
        self.assertIn("回撤 -10.00%", line)


if __name__ == "__main__":
    unittest.main()
