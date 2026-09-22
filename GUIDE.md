# StockDesk 量化因子工作流指南（GUIDE）

> 本机：82.156.15.12（Ubuntu 轻量云，1GB 内存）
> 远程机：CPU/内存充足（已完成一次全市场回测）
> 更新日期：2026-09-05
>
> 一句话定位：从"本机全市场日线数据"到"日频因子方向性与可交易性结论"的完整工具链。

---

## 0. 目录与文件清单

```
/home/ubuntu/stock-quant/
├── factors.py                # 因子库（2 个原创因子 + 共享算子工具）
├── verify_factors.py         # 因子单元验证 + 静态合规检查 + 冒烟
├── audit_eastmoney.py        # 数据审计（完整性/异常/复权交叉验证）
├── backtest_fullmarket.py    # 标准全市场回测（单参数：H=5, 成本=0.15%）
├── backtest_params.py        # 参数化回测（多 H/成本/中性化/避雷组合，⭐主力）
├── lhb_event_study.py        # 龙虎榜事件研究（上榜后 D1~D30 涨幅）
├── weekly_analysis.py        # 近期因子效果检查（本周 + 近 12 周 IC）
├── BACKTEST_GUIDE.md         # 回测口径细节（给远程机第一版）
├── PARALLEL_FINDINGS.md      # 并行调研结论（龙虎榜/概念映射/数据关系）
├── GUIDE.md                  # 本文档
└── results/                  # 所有输出
    ├── backtest_summary.json     # 标准回测结果（全市场 5552 只）
    ├── lhb_event_study.json      # 龙虎榜事件研究结果
    ├── weekly_analysis.json      # 本周因子效果
    ├── backtest_full.log         # 标准回测日志
    ├── weekly.log                # 周分析日志
    └── *.log                     # 各脚本运行日志
```

---

## 1. 数据（唯一主力：eastmoney_data）

| 数据 | 位置 | 说明 |
|---|---|---|
| **全A日线（主力）** | `/home/ubuntu/data/eastmoney_data/*.csv` | 5,552 只，2006-03-21 ~ 2026-09-01，字段 `date,open,close,high,low,volume,amount`，东财前复权 |
| 概念映射 | `/home/ubuntu/marketdata/concepts/concepts_map.csv` | 71,547 行，覆盖全A 100%（股票→概念，中性化用） |
| 龙虎榜 | `/home/ubuntu/marketdata/lhb/lhb_2026-06-01_to_2026-09-02.csv` | 6,327 条，含上榜后 D1~D30 涨幅（避雷因子用） |
| 涨停池/板块/FRED/基金 | `/home/ubuntu/marketdata/`、`/home/ubuntu/data/fred/` 等 | 扩展方向，暂未接入回测 |

**数据关系**：
- eastmoney_data 与网站主库 `stockdesk.db` 的 `daily_bars` 同源（交叉验证 0% 偏离），前者是后者 72 只子集的全市场扩集
- 网站主库覆盖到 2026-09-04（实时增量）；eastmoney 截至 2026-09-01
- **回测必须用 eastmoney_data**（全市场截面），网站库只作近期验证

---

## 2. 因子（factors.py）

两个原创日频因子，方向均为：**因子值越大 → 预期未来收益越差（看空方向）**。

| 因子 | 逻辑 | 核心算子 |
|---|---|---|
| `factor_divergence_tscorr_slope_20_60_close` | 量价背离 × 波动压缩 × 趋势斜率（上行耗竭预警） | ts_corr(20)、ts_std 比(20/100)、ts_slope(60)、乘法交互 |
| `factor_shock_skew_rank_20_60_volume` | 流动性冲击 × 收益偏度 × 波动位置（拥挤交易过热） | rank(横截面)、ts_mean(20)、ts_skewness(60)、ts_std 比(20/120)、乘法交互 |

**硬性合规**（已验证）：
- 无未来函数：源码无 `shift(-1)`，收益一律 `close.shift(1)` 滞后
- 全向量化：无 for/while/apply 逐行循环，仅 `.rolling()`/`.groupby()`
- min_periods：窗口 50%~70%
- 输出：1%/99% 缩尾 + Z-Score 标准化
- 数值核验：`ts_slope` vs `np.polyfit` 误差 2.4e-16；`ts_corr` 与手算 0.0

**调用约定**：df 需含 `ticker` 或 `symbol` 列 + `date` 列 + `close/volume`（等 6 列）。输出 Series 与输入行一一对应（已映射回原始索引）。

---

## 3. 快速开始（本机或远程机）

### 3.1 环境

```bash
python3 -c "import pandas, numpy; print(pandas.__version__, numpy.__version__)"
# 需 pandas>=2.0, numpy>=2.0；无其他依赖（Spearman 用 rank 后 Pearson，不依赖 scipy）
```

### 3.2 验证因子正确性

```bash
python3 verify_factors.py
# 输出：单元验证（ts_slope/ts_corr 对照）+ 静态合规 + 200 只冒烟
```

### 3.3 全市场标准回测（单参数）

```bash
python3 backtest_fullmarket.py
# 输出：results/backtest_summary.json + decile_returns.csv
# 参考结果（2022-01-01~2026-09-01, 5552 只, H=5, 成本 0.15% 单边）：
#   f1: IC -0.030 / t -6.0 / 多空 net 年化 +1.0% / 夏普 0.05 / 回撤 -49%
#   f2: IC -0.049 / t -16.6 / 多空 net 年化 -6.0% / 夏普 -0.46 / 回撤 -36%
#   —— 方向成立，但成本吃光收益，不可直接实盘（详见 §6）
```

### 3.4 参数化回测（⭐ 主力，多组合一次跑）

```bash
# 核心：持有周期 × 成本敏感度（推荐参数）
python3 backtest_params.py \
  --data-dir data/eastmoney_data \
  --hold-list 1 3 5 10 20 \
  --cost-list 0.0005 0.001 0.0015 0.003 \
  --start 2022-01-01 --end 2026-09-01

# 加概念中性化（需概念映射文件）
python3 backtest_params.py \
  --data-dir data/eastmoney_data \
  --hold-list 5 10 \
  --cost-list 0.001 0.0015 0.003 \
  --concept-map /path/to/concepts_map.csv \
  --start 2022-01-01

# 加龙虎榜避雷（子区间，需龙虎榜 CSV）
python3 backtest_params.py \
  --data-dir data/eastmoney_data \
  --hold-list 5 10 \
  --cost-list 0.001 0.0015 \
  --lhb-csv /path/to/lhb_2026-06-01_to_2026-09-02.csv \
  --lhb-start 2026-06-01 \
  --start 2022-01-01

# 极端分位试点（10% 分位改为 top5%/bottom5%）
python3 backtest_params.py --data-dir data/eastmoney_data \
  --decile 20 --quantile-pct 0.05 --hold-list 5 10 --cost-list 0.001 0.0015
```

**输出**：
- `ALL_RESULTS.csv`：一行一组合汇总（hold/cost/neutralize/lhb_avoid/factor/n_periods/ls_net_ann/ls_net_sharpe/…）—— **先看这张表**
- `params_<id>.json`：每个组合明细

**推荐跑批**（远程机约 2-5 分钟）：

```bash
python3 backtest_params.py \
  --data-dir data/eastmoney_data \
  --hold-list 1 3 5 10 20 \
  --cost-list 0.0005 0.001 0.0015 0.003 \
  --concept-map <概念映射路径> \
  --lhb-csv <龙虎榜路径> --lhb-start 2026-06-01 \
  --start 2022-01-01 --end 2026-09-01
```

### 3.5 龙虎榜事件研究

```bash
python3 lhb_event_study.py
# 输出：results/lhb_event_study.json
# 结论：上榜后 D30 平均 -15.4%（t=-37）；高换手上榜 D30 -21.3% 最差；
#      涨幅偏离上榜 D1 +1.03%（t=7.6）后转跌；净买入>0 D1 +0.83%（t=6.4）
```

### 3.6 本周/近期因子效果检查

```bash
python3 weekly_analysis.py
# 输出：results/weekly_analysis.json + weekly.log
# 内容：东财全市场 8/31、9/1 分位收益；网站库 72 只本周逐日相关；近 12 周周均 IC
# 注意：本周（8/31-9/4）因子方向与历史背离（8/31 周 f1 IC +0.356），
#      属普涨行情下的暂时现象（因子为低频反转特性，趋势市失效正常）
```

---

## 4. 结果解读参考

| 指标 | 参考 |
|---|---|
| IC 均值 | 绝对值 >0.03 值得关注；>0.05 优 |
| ICIR | >0.5 较好；>1 优 |
| t 值 | >3 才统计显著 |
| 分位单调 | 看空因子理想形态：分位 0 年化 > 分位 9 年化（单调递减） |
| 多空净收益 | >0 且跑赢市场基准才可交易；<0 则不可 |
| 最大回撤 | 多空 <0.3 可接受 |
| n_periods | 信号日数，<20 的组合结论不可信（勿读） |

**已有结论（全市场版）**：
- 方向性：两因子 IC 显著为负（t=-6.0/-16.6），方向成立
- 可交易性：0.15% 单边成本下 f1 net +1.0%、f2 net -6.0%，皆跑输等权基准（25%/17.6%）
- 死因：5 日换仓频率对 0.3% 双边成本太敏感 → **找长持有期/低成本组合是主要出路**

---

## 5. 已知局限（必须写进结论）

1. **幸存者偏差**：5,552 只是当前上市股票，不含退市股，历史截面偏乐观
2. **前复权**：收益率比例正确，但**不能**做涨停/跌停过滤、绝对价格、市值加权
3. **未做完整组合回测**：无停牌过滤、无融券成本（多空近似"头部做多+尾部规避"）
4. **A 股做空需融券**：多空组合收益是理论值，实际做空成本更高
5. **amount=0 问题**：网站主库近一年 57% 的 amount 为 0（采集缺失），**回测不用网站库做收益计算**；eastmoney amount 完整可用

---

## 6. 常见问题（FAQ）

**Q: 本机 1GB 内存，全量回测会不会 OOM？**
A: 会。本机曾全量 OOM（582 万行全加载+因子中间量）。**全量回测请放远程机/高性能机跑**；本机只跑小样本（≤500 只）或分析性小任务。

**Q: 远程机和本机的数据路径不同怎么办？**
A: 该脚本唯一需要改的是 `--data-dir`。远程机若数据在 `D:\stock_quant\data\eastmoney_data`（Windows），`--data-dir D:/stock_quant/data/eastmoney_data` 即可（脚本用 `pathlib` 跨平台）。Windows 下 CSV 编码兼容 UTF-8 BOM。

**Q: 概念映射/龙虎榜文件远程机没有？**
A: 从本机传过去：
```bash
scp /home/ubuntu/marketdata/concepts/concepts_map.csv <远程机>:/path/to/
scp /home/ubuntu/marketdata/lhb/lhb_2026-06-01_to_2026-09-02.csv <远程机>:/path/to/
```

**Q: 只想验证因子能跑通，不跑全量？**
A: 用 `backtest_params.py --data-dir <小样本目录>` 或 `verify_factors.py`。脚本已支持 `--hold-list` 传单值。

**Q: 报告里的 `ls_gross` vs `ls_net` 区别？**
A: `gross` = 不扣成本的组合收益；`net` = 扣每次换仓双边成本（`cost*2`/信号日）。**判断可交易性看 `ls_net`。**

**Q: 为什么 `backtest_full.log` 里两行 LS 都写着 f2 而不是 f1？**
A: 已知的打印标签 bug（循环变量残留），summary JSON 内数据是正确对应的，不影响结论。

---

## 7. 功能规划（待办 / 可跑）

- [x] 全市场标准回测（2022-01-01 起，H=5，0.15%）
- [x] 参数化回测（H/成本/中性化/避雷/极端分位）
- [x] 龙虎榜事件研究
- [x] 本周因子效果分析
- [ ] 行业中性化精确版（当前为概念中性化）
- [ ] 成本敏感度全矩阵（0.05%~0.3%）
- [ ] 持有周期敏感度（H=1~20）
- [ ] 组合层面优化（换手率限制/权重约束）
- [ ] 全市场 30 日 RSI/动量等其他因子扩展
