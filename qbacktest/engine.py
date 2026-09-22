#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""回测引擎：目标权重契约 + T+1 执行 + 严格现金会计。

设计参考（GitHub 成品惯例）：
  backtrader —— 策略只表达意图（order_target_percent），引擎负责成交与账务；
  vectorbt  —— 「信号序列 → 组合」的向量化语义，权重矩阵驱动。

本引擎契约（严谨性落点）：
  1. 策略输出 target_weights(date × symbol) 矩阵，只能用 ≤ 当日收盘的信息；
  2. 引擎统一把信号滞后 exec_lag(默认1) 个日历日执行 —— T 日收盘信号，
     T+1 日按 exec_price(默认次日开盘) 成交，从根本上杜绝未来函数；
  3. 成本：佣金(双边) + 印花税(卖出，A股股票 0.1%，ETF 0) + 滑点(不利方向)；
  4. A股现货约束：不允许做空（权重<0 报错）、不加杠杆（权重和>1 报错），
     最小交易单位 min_lot 手（100 股/份），现金账户不允许透支；
  5. 停牌：当日无行情的标的不调仓（维持原有持仓），估值按停牌前收盘 ffill。

会计模型（逐日循环，资产维向量化）：
  sizing 基数 = 前一收盘总权益（首日为 init_cash），按滑点后买价计算目标股数并取整手；
  先卖后买，卖出释放现金；买入受现金约束，不足一手放弃；逐笔记成交流水。
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

from .data import Panel
from .metrics import compute_metrics, format_metrics, markdown_report

EPS = 1e-9


@dataclass
class Config:
    init_cash: float = 1_000_000.0
    exec_lag: int = 1                 # 信号滞后执行天数（1 = T收盘信号 T+1 成交）
    exec_price: str = "open"          # open | close | vwap
    commission: float = 2.5e-4        # 佣金（单边，按成交额）
    stamp_tax: float = 0.0            # 印花税（卖出；A股股票 1e-3，场内基金 0）
    slippage: float = 5e-5            # 滑点（不利方向，价格比例）
    min_lot: int = 100                # 最小交易单位（股/份）；0 = 允许零股
    annualization: int = 252
    allow_fractional: bool = False    # True 时按权重精确到小数股（研究用）

    def __post_init__(self):
        if self.exec_lag < 1:
            raise ValueError("exec_lag 必须 ≥1（T+1 及以上），=0 会引入未来函数")
        if self.exec_price not in ("open", "close", "vwap"):
            raise ValueError("exec_price ∈ {open, close, vwap}")


@dataclass
class Result:
    equity: pd.Series                 # 逐日总权益（收盘估值）
    weights: pd.DataFrame             # 每日收盘后实际持仓权重
    cash: pd.Series
    trades: pd.DataFrame              # 成交流水
    config: Config
    metrics: dict = field(default_factory=dict)

    def summary_line(self) -> str:
        return format_metrics(self.metrics)

    def report(self, benchmark: pd.Series | None = None, title: str = "回测报告") -> str:
        return markdown_report(self.metrics, self.equity, benchmark, title)

    def save(self, out_dir: str) -> None:
        import os
        os.makedirs(out_dir, exist_ok=True)
        self.equity.rename("equity").to_csv(f"{out_dir}/equity.csv",
                                            index_label="date")
        self.trades.to_csv(f"{out_dir}/trades.csv", index=False)
        self.weights.to_csv(f"{out_dir}/weights.csv", index_label="date")


class Strategy:
    """策略基类：fit 阶段拿全量数据（须自行防前视），产出目标权重矩阵。

    权重语义：w(t, i) = 打算让资产 i 占组合的比例（现金 = 1 - Σw）。
    引擎在 t+exec_lag 日将其变为实际持仓。w 在信号日之间自动向前填充。
    """
    name = "strategy"

    def fit(self, panel: Panel) -> None:  # noqa: B027
        self.panel = panel

    def target_weights(self) -> pd.DataFrame:  # 子类必须实现
        raise NotImplementedError


def _validate_weights(w: pd.DataFrame) -> pd.DataFrame:
    if (w < -EPS).any().any():
        raise ValueError("target_weights 出现负值：A股现货账户不支持做空")
    gross = w.sum(axis=1)
    if (gross > 1 + 1e-4).any():
        bad = gross[gross > 1 + 1e-4]
        raise ValueError(f"权重和超过 1（不加杠杆）：{bad.head().to_dict()}")
    return w.clip(lower=0.0)


def run(strategy: Strategy, panel: Panel, config: Config | None = None) -> Result:
    cfg = config or Config()
    strategy.fit(panel)
    w_sig = _validate_weights(strategy.target_weights())
    cal = panel.calendar
    w_sig = w_sig.reindex(cal).ffill().fillna(0.0)
    # T 信号 → T+lag 日执行（对齐到统一日历后整体位移）
    w_exec = w_sig.shift(cfg.exec_lag).fillna(0.0)
    syms = list(panel.symbols)

    px = panel.exec_price(cfg.exec_price)          # 执行价（含缺口）
    tradable = panel.tradable(cfg.exec_price)
    close = panel.close                            # ffill 估值
    close_arr = close[syms].to_numpy(float)
    px_arr = px[syms].to_numpy(float)
    td_arr = tradable[syms].to_numpy(float)
    w_arr = w_exec[syms].to_numpy(float)

    n_days, n_assets = len(cal), len(syms)
    cash = cfg.init_cash
    shares = np.zeros(n_assets)
    last_close = np.full(n_assets, np.nan)
    equity_hist = np.empty(n_days)
    cash_hist = np.empty(n_days)
    w_hist = np.empty((n_days, n_assets))
    trades: list[dict] = []

    for t in range(n_days):
        # ---- 1) sizing 基数 = 执行时点权益：现金 + 持仓×执行价（停牌用最后已知收盘）。
        # 执行价在成交时刻即可观察（开盘价），不构成未来函数；
        # 用执行时点权益可避免“前收盘基数 × 今日价”在趋势行情中的幻影换仓。
        row_px, row_td, row_w = px_arr[t], td_arr[t], w_arr[t]
        mtm_exec = np.where((row_td > 0) & np.isfinite(row_px),
                            shares * row_px,
                            np.where(np.isfinite(last_close), shares * last_close, 0.0))
        base_equity = cash + float(mtm_exec.sum())

        buy_px = row_px * (1.0 + cfg.slippage)     # 买入价 = 执行价×(1+滑点)
        desired = shares.copy()                    # 默认维持不动
        if cfg.allow_fractional:
            lot = 1.0
        else:
            lot = cfg.min_lot or 1
        for i in range(n_assets):
            if not row_td[i] or not np.isfinite(row_px[i]) or row_px[i] <= 0:
                continue                           # 当日停牌/无行情：跳过调仓
            tgt_val = base_equity * row_w[i]
            n_sh = int(tgt_val / buy_px[i] / lot) * lot if lot > 1 else tgt_val / buy_px[i]
            desired[i] = n_sh

        # ---- 2) 先卖后买，逐笔记账 ----
        deltas = desired - shares
        order = np.argsort(-deltas)                # 卖出(delta<0)优先
        for i in order:
            d = deltas[i]
            if abs(d) < EPS or not row_td[i]:
                continue
            if d < 0:                             # 卖出
                px_sell = row_px[i] * (1.0 - cfg.slippage)
                qty = -d
                value = qty * px_sell
                comm = value * cfg.commission
                tax = value * cfg.stamp_tax
                cash += value - comm - tax
                shares[i] += d
                trades.append(dict(date=cal[t], symbol=syms[i], side="sell",
                                   price=round(px_sell, 4), shares=float(qty),
                                   value=value, commission=comm, tax=tax))
            else:                                  # 买入（受现金约束）
                px_buy = buy_px[i]
                for _ in range(2):                 # 扣费后可能差一手，重试一次
                    qty = d
                    value = qty * px_buy
                    comm = value * cfg.commission
                    if value + comm > cash:
                        affordable = cash / (px_buy * (1.0 + cfg.commission))
                        d = int(affordable / lot) * lot if lot > 1 else affordable
                        if d <= 0:
                            d = 0
                        continue
                    break
                if d <= 0:
                    continue
                qty = d
                value = qty * px_buy
                comm = value * cfg.commission
                cash -= value + comm
                shares[i] += qty
                trades.append(dict(date=cal[t], symbol=syms[i], side="buy",
                                   price=round(px_buy, 4), shares=float(qty),
                                   value=value, commission=comm, tax=0.0))

        # ---- 3) 收盘估值 ----
        c = close_arr[t]
        has = np.isfinite(c)
        last_close[has] = c[has]  # 无行情的标的维持最后已知收盘价
        mtm = np.where(np.isfinite(last_close), shares * last_close, 0.0)
        eq = cash + float(mtm.sum())
        equity_hist[t] = eq
        cash_hist[t] = cash
        mv = shares * np.where(np.isfinite(last_close), last_close, 0.0)
        w_hist[t] = mv / eq if eq > 0 else 0.0

    idx = pd.Index(cal, name="date")
    equity = pd.Series(equity_hist, index=idx)
    if (equity <= 0).any():
        raise RuntimeError("权益出现非正值：检查数据（价格缺口）或成本设置")
    weights_df = pd.DataFrame(w_hist, index=idx, columns=syms)
    trades_df = pd.DataFrame(trades, columns=["date", "symbol", "side", "price",
                                              "shares", "value", "commission", "tax"])
    m = compute_metrics(equity, trades=trades_df, weights=weights_df)
    return Result(equity=equity, weights=weights_df, cash=pd.Series(cash_hist, index=idx),
                  trades=trades_df, config=cfg, metrics=m)
