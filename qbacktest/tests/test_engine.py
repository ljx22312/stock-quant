#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""引擎正确性测试：全部用可手工验算的合成数据。

覆盖四类严谨性断言：
  1) 零成本买入持有 → 净值 ≡ 价格比（含 T+1 滞后语义）；
  2) 佣金/印花税/滑点的逐笔记账精确；
  3) 末根K线信号永不成交（无未来函数的执行侧保证）；
  4) 整手取整、现金不透支、停牌不调仓按前收盘估值。
"""
import unittest

import numpy as np
import pandas as pd

from qbacktest import Config, Panel, Strategy, run


def frame(closes: dict[str, float], opens: dict[str, float] | None = None):
    dates = sorted(closes)
    o = opens or {d: closes[d] for d in dates}
    return pd.DataFrame({
        "open": [o[d] for d in dates], "high": [closes[d] for d in dates],
        "low": [closes[d] for d in dates], "close": [closes[d] for d in dates],
        "volume": [1e6] * len(dates), "amount": [1e7] * len(dates),
    }, index=pd.Index(dates, name="date"))


class ConstWeights(Strategy):
    """给定的权重矩阵原样输出（测试用）。"""

    def __init__(self, w: pd.DataFrame):
        self._w = w

    def target_weights(self) -> pd.DataFrame:
        return self._w


def one_asset(closes, **cfg_kw):
    dates = [f"2024-01-{i:02d}" for i in range(1, len(closes) + 1)]
    f = frame(dict(zip(dates, closes)))
    panel = Panel({"A": f})
    w = pd.DataFrame({"A": [1.0] * len(dates)}, index=pd.Index(dates))
    return run(ConstWeights(w), panel, Config(**cfg_kw)), dates


class TestEngine(unittest.TestCase):

    def test_buy_and_hold_no_cost(self):
        """零成本 fractional：第1日开盘买入，净值 ≡ init×close[t]/close[1]。"""
        closes = [10, 11, 12, 13, 14]
        res, dates = one_asset(closes, commission=0.0, slippage=0.0,
                               allow_fractional=True, min_lot=1)
        eq = res.equity
        self.assertAlmostEqual(eq.iloc[0], 1_000_000.0)        # 信号日未成交
        for t in range(1, len(dates)):
            self.assertAlmostEqual(eq.iloc[t], 1_000_000.0 * closes[t] / closes[1], places=6)
        self.assertEqual(len(res.trades), 1)                   # 只有一笔买入
        self.assertEqual(res.trades.iloc[0]["date"], dates[1])  # T+1 执行

    def test_cost_accounting_exact(self):
        """一买一卖，佣金/印花税/滑点逐笔核对。"""
        closes = [10, 10, 10]
        dates = [f"2024-02-{i:02d}" for i in range(1, 4)]
        f = frame(dict(zip(dates, closes)))
        w = pd.DataFrame({"A": [1.0, 0.0, 0.0]}, index=pd.Index(dates))
        cfg = Config(commission=0.001, stamp_tax=0.002, slippage=0.01,
                     allow_fractional=True, min_lot=1)
        res = run(ConstWeights(w), Panel({"A": f}), cfg)
        init = cfg.init_cash
        buy_px, sell_px = 10 * 1.01, 10 * 0.99
        # 现金约束下可买股数 = cash/(买价×(1+佣金))，买入后现金恰好为 0
        shares = init / (buy_px * (1 + 0.001))
        buy_val = shares * buy_px
        sell_val = shares * sell_px
        cash_final = sell_val * (1 - 0.001 - 0.002)
        self.assertAlmostEqual(res.cash.iloc[-1], cash_final, places=6)
        self.assertAlmostEqual(res.equity.iloc[-1], cash_final, places=6)  # 全现金
        self.assertEqual(len(res.trades), 2)
        self.assertAlmostEqual(res.trades.iloc[0]["price"], buy_px, places=6)
        self.assertAlmostEqual(res.trades.iloc[1]["price"], sell_px, places=6)
        self.assertAlmostEqual(res.metrics["total_cost"],
                               buy_val * 0.001 + sell_val * (0.001 + 0.002), places=6)

    def test_no_lookahead_last_bar_signal(self):
        """信号只出现在最后一根K线 → shift(1) 后永不成交，权益恒等于初始资金。"""
        closes = [10, 11, 12, 13]
        dates = [f"2024-03-{i:02d}" for i in range(1, 5)]
        f = frame(dict(zip(dates, closes)))
        w = pd.DataFrame({"A": [0.0, 0.0, 0.0, 1.0]}, index=pd.Index(dates))
        res = run(ConstWeights(w), Panel({"A": f}), Config())
        self.assertEqual(len(res.trades), 0)
        self.assertTrue((res.equity == 1_000_000.0).all())

    def test_lot_rounding(self):
        """min_lot=100：100000/10.1 → 990 股 → 900 股（整手）。"""
        closes = [10.0, 10.0, 10.0]
        dates = [f"2024-04-{i:02d}" for i in range(1, 4)]
        f = frame(dict(zip(dates, closes)))
        w = pd.DataFrame({"A": [1.0] * 3}, index=pd.Index(dates))
        res = run(ConstWeights(w), Panel({"A": f}),
                  Config(init_cash=100_000, commission=0.0, slippage=0.01, min_lot=100))
        self.assertEqual(res.trades.iloc[0]["shares"], 9900.0)  # 100000/10.1=9900.99 → 99手

    def test_cash_never_negative(self):
        """每天满仓追涨：现金恒 ≥ 0。"""
        closes = [10, 12, 14, 17, 20, 24]
        res, _ = one_asset(closes)
        self.assertTrue((res.cash >= -1e-9).all())

    def test_halted_asset_kept_and_ffill_mtm(self):
        """停牌日不调仓、持仓保留、估值用停牌前收盘。"""
        d = [f"2024-05-{i:02d}" for i in range(1, 5)]
        a = frame({d[0]: 10, d[1]: 10, d[2]: 11, d[3]: 12})
        b = pd.DataFrame({                   # B 第3日(d[2]) 停牌
            "open": [20, 20, np.nan, 20], "high": [20, 20, np.nan, 21],
            "low": [20, 20, np.nan, 20], "close": [20, 20, np.nan, 21],
            "volume": [1e6, 1e6, 0, 1e6], "amount": [2e7, 2e7, 0, 2.1e7],
        }, index=pd.Index(d))
        b = b.drop(index=d[2])               # 日历缺口 = 停牌
        w = pd.DataFrame({"A": 0.5, "B": 0.5}, index=pd.Index(d))
        res = run(ConstWeights(w), Panel({"A": a, "B": b}),
                  Config(allow_fractional=True, min_lot=1, slippage=0.0, commission=0.0))
        b_trades_on_halt = res.trades[(res.trades["symbol"] == "B")
                                      & (res.trades["date"] == d[2])]
        self.assertEqual(len(b_trades_on_halt), 0)             # 停牌日 B 无成交
        # 停牌期间 B 按停牌前收盘 20 估值：市值恒为 25000×20（而非 NaN/0）
        for day in (d[1], d[2]):
            b_mv = res.equity.loc[day] * res.weights.loc[day, "B"]
            self.assertAlmostEqual(b_mv, 500_000.0, places=2)

    def test_short_and_leverage_rejected(self):
        """负权重 / 权重和>1 直接报错。"""
        d = ["2024-06-01", "2024-06-02"]
        f = frame({d[0]: 10, d[1]: 10})
        panel = Panel({"A": f, "B": f.copy()})
        w_short = pd.DataFrame({"A": -0.5, "B": 0.5}, index=pd.Index(d))
        with self.assertRaises(ValueError):
            run(ConstWeights(w_short), panel, Config())
        w_lev = pd.DataFrame({"A": 0.8, "B": 0.8}, index=pd.Index(d))
        with self.assertRaises(ValueError):
            run(ConstWeights(w_lev), panel, Config())


if __name__ == "__main__":
    unittest.main()
