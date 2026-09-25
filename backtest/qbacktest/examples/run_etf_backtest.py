#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""示例：本机 ETF 数据上的两类策略回测。

  A) 沪深300ETF 双均线择时（20/60），基准 = 满仓持有；
  B) 8 只 ETF 动量轮动（60 日动量，持有 Top2，20 日调仓，绝对动量过滤）。

运行：cd /home/ubuntu/stock-quant && python3 qbacktest/examples/run_etf_backtest.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from qbacktest import Config, Panel, load_fund, run
from qbacktest.strategies.etf_momentum import EtfMomentum
from qbacktest.strategies.ma_cross import MaCross

START = "2022-01-01"
OUT = Path(__file__).resolve().parents[2] / "results" / "qbacktest"
POOL = ["sh510300", "sh512100", "sz159915", "sh588000",
        "sh513180", "sh518880", "sh512480", "sh511260"]


def main():
    # adjust="qfq"：腾讯前复权，规避 tdx 不复权数据里的份额折算/拆分跳空
    # （512100 2022-09 折算 +175%、512480 2026-07 拆分 -51% 皆为假信号）
    frames = {s: load_fund(s, start=START, adjust="qfq") for s in POOL}
    for s, f in frames.items():
        print(f"  {s}: {len(f)} 根，{f.index[0]} ~ {f.index[-1]}")
    bench = frames["sh510300"]["close"]

    print("\n[A] 沪深300ETF 20/60 双均线（T+1 开盘执行，ETF 免印花税）")
    res_a = run(MaCross(20, 60), Panel({"sh510300": frames["sh510300"]}),
                Config(exec_price="open", stamp_tax=0.0))
    print("   ", res_a.summary_line())
    print(f"    基准(满仓持有) 同期收益: {bench.iloc[-1] / bench.iloc[0] - 1:.2%}")

    print("\n[B] 8 只 ETF 动量轮动：60日动量 Top2，20日调仓，绝对动量过滤")
    res_b = run(EtfMomentum(lookback=60, top_k=2, rebalance=20, absolute_momentum=True),
                Panel(frames), Config(exec_price="open", stamp_tax=0.0))
    print("   ", res_b.summary_line())

    OUT.mkdir(parents=True, exist_ok=True)
    res_a.save(str(OUT / "ma_cross_510300"))
    res_b.save(str(OUT / "etf_momentum"))
    (OUT / "etf_momentum/report.md").write_text(
        res_b.report(benchmark=bench, title=f"ETF 动量轮动回测（{START} ~ 数据截止）"))
    print(f"\n明细已保存: {OUT}/")


if __name__ == "__main__":
    main()
