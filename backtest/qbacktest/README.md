# qbacktest —— 本机严谨回测框架

零第三方依赖（仅 pandas + numpy），为 StockDesk 本机数据资产定制。
设计参考 GitHub 成品：借 **backtrader** 的"策略表达意图、引擎管账务"分层与
**vectorbt** 的"信号矩阵 → 组合"语义，但不引入其依赖链（本机 pandas 3.0 / numpy 2.5
环境下 backtrader 停维护、vectorbt 依赖 numba，均有兼容风险）。

## 快速上手

```bash
cd /home/ubuntu/stock-quant
python3 -m unittest qbacktest.tests.test_engine qbacktest.tests.test_metrics   # 正确性测试
python3 qbacktest/examples/run_etf_backtest.py                                  # 真实数据示例
```

```python
from qbacktest import Config, Panel, load_fund, run
from qbacktest.strategies.ma_cross import MaCross

frames = {"sh510300": load_fund("sh510300", start="2022-01-01")}
res = run(MaCross(20, 60), Panel(frames), Config(exec_price="open", stamp_tax=0.0))
print(res.summary_line())          # 一行指标摘要
print(res.report(benchmark=frames["sh510300"]["close"]))  # markdown 报告
res.save("results/qbacktest/demo") # equity.csv / trades.csv / weights.csv
```

## 严谨性契约（框架侧硬保证）

| 保证 | 实现方式 |
|---|---|
| **无未来函数** | 策略输出目标权重矩阵（只用 ≤T 收盘信息）；引擎统一 `shift(exec_lag=1)` 后才执行，末根K线信号永不成交 |
| **T+1 执行** | `exec_lag=1`（可调更保守），默认次日**开盘价**成交，可选 close / vwap |
| **真实成本** | 佣金（双边，默认万2.5）+ 印花税（卖出，股票千1 / ETF 0）+ 滑点（默认万5，不利方向） |
| **A股约束** | 不允许做空（负权重直接报错）、不加杠杆（权重和>1 报错）、整手交易（min_lot=100）、现金永不透支 |
| **停牌处理** | 当日无行情的标的不调仓（持仓保留），估值按停牌前收盘价 ffill |
| **可审计** | 全部成交流水（价格/数量/费用）落盘，逐日权重/现金/权益可复查 |

**sizing 口径**：目标股数 = 执行时点权益 × 目标权重 ÷ 滑点后买价，执行时点权益
= 现金 + 持仓×执行价（执行价在成交时刻即可观察，不构成前视）。这样满仓策略在趋势
行情中不会因"前收盘基数×今日价"产生幻影换仓（有专项测试锁定该行为）。

## 指标口径（年化 252 交易日）

总收益 / CAGR / 年化波动（std ddof=1×√252）/ 夏普（rf 可配，默认0）/
索提诺（下行偏差=√mean(min(r,0)²)）/ 最大回撤（附峰谷日期）/ 卡玛 /
日胜率 / 最好最差单日 / 持仓暴露 / 年化单边换手 / 总成本。

## 数据层与口径警示

| 数据 | 路径 | 复权 | 注意 |
|---|---|---|---|
| A股日线 | `data/eastmoney_data/` | **前复权** | 收益率正确；**不可**用于涨跌停判断/绝对价格/市值 |
| 场内基金 raw | `tdx_data/fund/`（volume 股） | **不复权** | ⚠️ 含份额折算/拆分跳空（512100 2022-09 折算 +175%、512480 2026-07 拆分 -51% 皆为假信号），`load_fund` 会在检测到 >21% 隔夜跳空时打警告 |
| 场内基金 qfq | 腾讯 fqkline（磁盘缓存 `qbacktest/cache/`） | **前复权** | **回测推荐**（`load_fund(sym, adjust="qfq")`），分红/折算已平滑，多数品种 2016 起全历史，首拉后走缓存 |
| 跨境基金 | `tdx_data/cross_border_etf/`（volume 手） | 不复权 | 同 raw 注意事项 |

- 统一输出 volume=股/份、amount=元；
- `Panel` 用 union 日历对齐多标的；close ffill 估值、open 不填充（NaN=停牌不可交易）；
- 本框架**不含**实时增量（那是行情台 `stock-alert/server/etf_data.py` 的职责），
  回测数据截至本地文件最后一日，报告须注明。

## 已知边界（不在 v0.1 范围）

- 涨跌停无法成交、最小报价单位、T+0 品种（跨境/债券/货币 ETF 实为 T+0）未建模
  ——当前按 T+1 保守处理；
- 盘口深度/冲击成本未建模（滑点为固定比例）；
- 分红/送转按价格序列口径隐含处理（前复权=分红再投资；不复权序列会低估含息收益）；
- 股票日内（小时线/分时）不在数据层，仅日线。

## 目录

```
qbacktest/
  data.py          # 加载层：read_ohlcv / load_stock / load_fund / Panel
  engine.py        # Config / Strategy(目标权重契约) / run / Result
  metrics.py       # compute_metrics / format_metrics / markdown_report
  strategies/      # ma_cross.py（双均线）、etf_momentum.py（动量轮动）
  tests/           # 合成数据正确性测试（12 项，全部手工验算）
  examples/        # run_etf_backtest.py（真实数据示例）
```

## 测试覆盖（`python3 -m unittest discover qbacktest`）

1. 零成本买入持有 ≡ 价格比（锁定 T+1 与 sizing 语义，恰好 1 笔成交）
2. 佣金/印花税/滑点逐笔记账精确到分
3. 末根K线信号永不成交（无前视的执行侧保证）
4. 整手取整 / 现金不透支 / 停牌不调仓按前收盘估值
5. 做空与杠杆直接报错
6. 夏普/索提诺/回撤/CAGR 与手工验算常数一致
