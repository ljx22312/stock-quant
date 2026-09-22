#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""qbacktest —— StockDesk 本机严谨回测框架（零第三方依赖：pandas + numpy）。

快速上手::

    from qbacktest import Config, run, load_fund, Panel
    from qbacktest.strategies.ma_cross import MaCross

    frames = {"sh510300": load_fund("sh510300", start="2022-01-01")}
    res = run(MaCross(fast=20, slow=60), Panel(frames), Config())
    print(res.summary_line())

严谨性契约（详见 README）：
  目标权重矩阵驱动；T 日信号 T+1 日成交；佣金/印花税/滑点；
  不做空、不加杠杆、整手交易、现金不透支；停牌不调仓按前收盘估值。
"""
from .data import EM_DIR, FUND_DIR, Panel, load, load_fund, load_stock, read_ohlcv
from .engine import Config, Result, Strategy, run
from .metrics import compute_metrics, format_metrics

__all__ = [
    "Config", "Result", "Strategy", "run",
    "Panel", "load", "load_fund", "load_stock", "read_ohlcv",
    "compute_metrics", "format_metrics", "EM_DIR", "FUND_DIR",
]
__version__ = "0.1.0"
