#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""绩效指标套件（全部基于日频净值序列，年化 252 交易日）。

口径声明：
  ann_vol    日收益样本标准差(ddof=1)×√252
  sharpe     (日均收益 - 日化rf) / 日波动 × √252，rf 默认 0
  sortino    下行偏差 = √(mean(min(r,0)²)) × √252（对 0 目标，非 MAR）
  max_dd     净值相对历史峰值的最大回撤，附峰/谷/修复日期
  calmar     CAGR / |max_dd|
  turnover   年化单边换手 = 累计成交额 / 平均净值 / 年数
"""
from __future__ import annotations

import numpy as np
import pandas as pd

TRADING_DAYS = 252


def daily_returns(equity: pd.Series) -> pd.Series:
    return equity.pct_change().dropna()


def compute_metrics(equity: pd.Series, *, rf: float = 0.0,
                    trades: pd.DataFrame | None = None,
                    weights: pd.DataFrame | None = None) -> dict:
    eq = equity.astype(float)
    r = daily_returns(eq)
    n = len(r)
    out: dict = {"start": eq.index[0], "end": eq.index[-1], "n_days": n + 1}
    if n == 0 or eq.iloc[0] <= 0:
        return out

    total = eq.iloc[-1] / eq.iloc[0] - 1.0
    years = (n + 1) / TRADING_DAYS
    cagr = (1.0 + total) ** (1.0 / years) - 1.0 if years > 0 and (1 + total) > 0 else np.nan
    ann_vol = r.std(ddof=1) * np.sqrt(TRADING_DAYS) if n > 1 else np.nan
    rf_d = rf / TRADING_DAYS
    sharpe = ((r.mean() - rf_d) / r.std(ddof=1) * np.sqrt(TRADING_DAYS)
              if n > 1 and r.std(ddof=1) > 0 else np.nan)
    downside = np.sqrt(np.mean(np.minimum(r - rf_d, 0.0) ** 2))
    sortino = ((r.mean() - rf_d) / downside * np.sqrt(TRADING_DAYS)
               if downside > 0 else np.nan)

    cummax = eq.cummax()
    dd = eq / cummax - 1.0
    mdd = float(dd.min())
    trough = dd.idxmin()
    peak = eq.loc[:trough].idxmax()
    recovered = None
    after = dd.loc[trough:]
    rec_hits = after[after >= -1e-12]
    if len(rec_hits):
        recovered = rec_hits.index[0]

    out.update({
        "total_return": float(total),
        "cagr": float(cagr) if cagr == cagr else None,
        "ann_vol": float(ann_vol) if ann_vol == ann_vol else None,
        "sharpe": float(sharpe) if sharpe == sharpe else None,
        "sortino": float(sortino) if sortino == sortino else None,
        "max_drawdown": mdd,
        "dd_peak": peak, "dd_trough": trough, "dd_recovered": recovered,
        "calmar": float(cagr / abs(mdd)) if mdd < 0 and cagr == cagr else None,
        "win_rate_daily": float((r > 0).mean()),
        "best_day": float(r.max()), "worst_day": float(r.min()),
    })

    if trades is not None and len(trades):
        buy_val = trades.loc[trades["side"] == "buy", "value"].sum()
        sell_val = trades.loc[trades["side"] == "sell", "value"].sum()
        avg_eq = eq.mean()
        out["turnover_annual"] = float((buy_val + sell_val) / 2.0 / avg_eq / years)
        out["n_trades"] = int(len(trades))
        out["total_cost"] = float((trades["commission"] + trades["tax"]).sum())
        # 交易胜率：按「买入到下一次全部卖出」配对过于复杂，这里给日均权益口径即可
    if weights is not None:
        gross = weights.abs().sum(axis=1)
        out["exposure"] = float((gross > 1e-6).mean())
        out["avg_gross_weight"] = float(gross.mean())
    return out


def format_metrics(m: dict) -> str:
    """单行摘要（终端/日志用）。"""
    def pct(v):
        return "-" if v is None else f"{v * 100:.2f}%"
    def num(v, d=2):
        return "-" if v is None else f"{v:.{d}f}"
    return (f"总收益 {pct(m.get('total_return'))} | CAGR {pct(m.get('cagr'))} | "
            f"波动 {pct(m.get('ann_vol'))} | 夏普 {num(m.get('sharpe'))} | "
            f"索提诺 {num(m.get('sortino'))} | 回撤 {pct(m.get('max_drawdown'))} | "
            f"卡玛 {num(m.get('calmar'))} | 胜率 {pct(m.get('win_rate_daily'))} | "
            f"暴露 {pct(m.get('exposure'))}")


def markdown_report(m: dict, equity: pd.Series, benchmark: pd.Series | None = None,
                    title: str = "回测报告") -> str:
    lines = [f"# {title}", "",
             f"- 区间: {m.get('start')} ~ {m.get('end')}（{m.get('n_days')} 个净值点）", ""]
    rows = [
        ("总收益", m.get("total_return"), "pct"), ("年化收益 CAGR", m.get("cagr"), "pct"),
        ("年化波动", m.get("ann_vol"), "pct"), ("夏普比率", m.get("sharpe"), "num"),
        ("索提诺比率", m.get("sortino"), "num"), ("最大回撤", m.get("max_drawdown"), "pct"),
        ("卡玛比率", m.get("calmar"), "num"), ("日胜率", m.get("win_rate_daily"), "pct"),
        ("最好/最差单日", None, "range"), ("持仓暴露", m.get("exposure"), "pct"),
        ("年化换手(单边)", m.get("turnover_annual"), "num"),
        ("成交笔数", m.get("n_trades"), "int"), ("总交易成本", m.get("total_cost"), "num"),
    ]
    lines += ["| 指标 | 值 |", "|---|---|"]
    for name, v, kind in rows:
        if kind == "range":
            lines.append(f"| {name} | {m.get('best_day', 0)*100:.2f}% / {m.get('worst_day', 0)*100:.2f}% |")
        elif kind == "pct":
            lines.append(f"| {name} | {'-' if v is None else f'{v*100:.2f}%'} |")
        elif kind == "int":
            lines.append(f"| {name} | {'-' if v is None else int(v)} |")
        else:
            lines.append(f"| {name} | {'-' if v is None else f'{v:.3f}'} |")
    if m.get("dd_peak"):
        lines += ["", f"最大回撤区间: 峰 {m['dd_peak']} → 谷 {m['dd_trough']}"
                  + (f"，修复于 {m['dd_recovered']}" if m.get("dd_recovered") else "，未修复")]
    if benchmark is not None:
        b = benchmark.reindex(equity.index).ffill()
        b = b / b.iloc[0]
        x = equity / equity.iloc[0]
        lines += ["", f"基准区间收益: {b.iloc[-1]-1:.2%}，策略超额: {x.iloc[-1]-b.iloc[-1]:.2%}"]
    return "\n".join(lines)
