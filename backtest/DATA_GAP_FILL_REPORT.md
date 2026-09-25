# 数据补齐工程报告（DATA GAP FILL）

> 日期：2026-09-05 ｜ 执行：ZCode ｜ 触发：115 机数据迁移完成后，针对「估值/股息空白」的补数

## 0. 摘要（TL;DR）

- **探针先行**：`stock-quant/probe_sources.py` 实测 5 类数据源，全量外推估值历史 32min / 财务 16min / 分红 2.3min → 全部触发"≤90min 拉全量"规则，对 5,552 只全量拉取
- **新增四大类数据**：官方逐日估值历史（2018 起）、分红送配全史（1997 起 55,892 条）、财务主指标逐股历史（1997 起）、全A估值快照（当日基线）
- **多源容灾**：每类数据均配主源+备源，429/403 自动切换；腾讯源限流后熔断直切新浪已实测生效
- **日常更新已挂 crontab**：工作日 16:10 日更链、周五分红周更、每月财务月更
- **MOVE.csv 已删除**：根因是 FRED 无此序列（404 实锤），非临时故障

## 1. 网络源探测结论（本机实测）

| 源 | 状态 | 用途 |
|---|---|---|
| push2delay.eastmoney.com | ✅ 200（15分钟延时通道） | 估值快照唯一可达的东财行情通道 |
| push2 / push2his.eastmoney.com | ❌ 连接被掐断（exit 52） | 实时行情/历史K线不可用 |
| datacenter-web.eastmoney.com | ✅ 100% 成功 | 估值历史/财务主指标/分红（核心源） |
| qt.gtimg.cn 腾讯 | ✅ 可用，持续高频会限流 | 快照/日线备源（已加熔断） |
| 新浪 vip.stock/quotes.sina | ✅ 可用 | 资金流主源、日线备源 |
| baostock (TCP 10030) | ❌ 端口不通 | 不可用 |
| legulegu.com | ❌ 403 反爬 | 不可用 |
| fred.stlouisfed.org | ✅ 8/9 序列正常 | MOVE 不存在（404），已删除坏文件 |
| stooq.com | ❌ JS 质询盾 | MOVE 替代尝试失败 |

## 2. 新增数据资产

| 目录 | 内容 | 规模 | 源 |
|---|---|---|---|
| `data/downloads/valuation/` | 逐日 PE(TTM)/PE静/PB/PS/市现率/PEG/总市值/流通市值/股本 + `div_ttm`/`div_yield`（TTM股息率，计算列） | 5,552 CSV × ~2,106 行，官方数据自 **2018-01-02** 起 | datacenter `RPT_VALUEANALYSIS_DET` |
| `data/downloads/finance/` | 财务主指标 25 字段（EPS/BPS/ROE/毛利率/流动比率/周转率等，含 notice_date 可防未来函数） | 5,552 CSV，报告期自 **1997 年**起 | datacenter `RPT_F10_FINANCE_MAINFINADATA` |
| `data/downloads/dividends/` | 分红送配明细（每10股派现/送转/除权除息日/股息率）+ 合并 `dividend_all.csv` | 118 个报告期，合并 55,892 行，实质数据自 **1997Q2** 起（1997Q1/Q3 等早期报告期为空占位） | akshare `stock_fhps_em` |
| `data/downloads/valuation_snapshot/` | 每日全A估值快照（PE动/静/TTM、PB、市值、主力净流入） | 当日基线 5,909 行（21 秒拉完） | push2delay clist |
| `data/downloads/macro/cpi_em.csv` `ppi_em.csv` | CPI/PPI 东财官方口径（全国/城市/农村 同比环比），**至 2026-07** | 223/247 行，2008-01 / 2006-01 起 | datacenter `RPT_ECONOMY_CPI/PPI` |

## 3. 采集脚本（均在 /home/ubuntu/）

| 脚本 | 说明 |
|---|---|
| `stock-quant/probe_sources.py` | 数据源探针：小样本计时+成功率+全量外推，换机器/换网络先跑这个 |
| `dl_valuation_snapshot.py` | 估值快照（主 push2delay / 备腾讯）；`--date` 指定日 |
| `dl_valuation_history.py` | 估值历史全量（断点续传）；`--update` 增量补最近15个交易日 |
| `build_dividend_yield.py` | 用分红事件×估值close 计算逐日 TTM 股息率，回写 valuation/*.csv |
| `dl_dividends.py` | 分红按报告期（`--recent N` 增量）；备源直连 RPT_SHAREBONUS_DET |
| `dl_macro_em.py` | 宏观 CPI/PPI 直连东财刷新，带新鲜度门槛（不达标不落盘），月更 |
| `dl_finance_main.py` | 财务主指标全量；`--update` 增量补最近8个报告期 |
| `dl_eastmoney_increment.py` | 日线增量（主腾讯 / 备新浪，熔断切换），就地追加 eastmoney_data |
| `dlcommon.py` | 公共库：限速/重试/断点续传/manifest |
| `daily_update.sh` | cron 总控（daily/weekly/monthly 三模式） |

## 4. 定时任务（crontab，用户选定"全套每日更新"）

```
10 16 * * 1-5  daily_update.sh daily    # 估值快照→日线增量→估值增量→全市场快照→板块资金流→资金流增量
0   18 * * 5   daily_update.sh weekly   # 分红近4期 + 股息率TTM重算
0   6  1 * *   daily_update.sh monthly  # 财务主指标增量 + 估值兜底补漏
```
日志：`~/data/logs/<模式>_<时间戳>.log`。周末自动跳过；法定节假日会空跑（各脚本幂等无害）。

## 5. 口径与已知限制

1. **官方估值历史只到 2018-01-02**：东财 `RPT_VALUEANALYSIS_DET` 的历史深度如此。更早的 PE/PB 可用 `data/downloads/financial/`（yjbb 2019Q4 起）自算，或接受 2018 起点
2. **股息率 TTM 是计算列**：官方接口不含逐日股息率，`div_yield` = 近365天已实施分红(每股,税前) / 当日收盘价。事件源为"除权除息日"，预案/未实施不计入
3. **日线增量行**：9/2 之后的追加行来自腾讯前复权（9/1 及之前为东财源），`amount` 列为空（腾讯/新浪 K 线无成交额字段），且前复权基准与东财存在细微口径差；建议每月与桌面机全量对账一次
4. **push2delay 是延时行情**：收盘后拉取不影响 EOD 用途，但不可用于盘中实时
5. **MOVE 指数缺失**：FRED 无此序列、stooq 被 JS 盾挡。如需美债波动率，可日后挂代理从 Yahoo Finance 拉 `^MOVE`
6. **金十源宏观停更**：akshare `macro_china_cpi_monthly`（金十 datacenter-api）数据止于 2025-09，`macro_china_industrial_production_yoy` 同样陈旧且在 pandas 3.x 下有兼容 bug——工业增加值宏观数维持 115 机版本（止于 2025-09），CPI/PPI 已改用东财官方口径（`dl_macro_em.py`）

## 6. 运维备忘

- 断点续传：所有脚本"文件>500B 即跳过"，中断后直接重跑；估值/财务全量重跑只补缺口
- `manifest.jsonl` 记录每个文件的来源/行数/耗时，排查数据出处用它
- 腾讯限流特征：连接被重置或空回复，熔断后本轮全走新浪；新浪也限流时任务会记 failures，重跑即可
- 质量校验：`python3 stock-quant/audit_valuation.py`（覆盖面/不复权价一致性/腾讯独立源交叉/高息股股息率抽查/快照衔接）

## 7. 质量校验结果（2026-09-05 实测，详见 results/valuation_audit.md）

| 校验项 | 结果 |
|---|---|
| 覆盖面 | 估值 5,552 / 财务 5,552 / 日线 5,552，估值历史 2018-01-02~2026-09-04 ✅ |
| 不复权 close vs 腾讯原始日K（15只） | 偏差>0.5% 为 0 只 ✅ |
| 最新 PB/总市值 vs 腾讯实时（15只独立源） | 全部一致 ✅ |
| 股息率抽查 | 长电 3.52% / 茅台 3.91% / 工行 3.82% / 农行 3.58% / 平安 5.01%，均符合认知 ✅ |
| 当日快照衔接（50只） | 全部一致 ✅ |
| 分红事件覆盖 | 5,386 / 5,552 只有实施分红记录（曾发现深市代码前导零丢失 bug，已修复重算） |

## 8. 执行耗时实录（供下次扩容参考）

- 探针 25 只样本：17 秒
- 估值快照全量 5,909 只：21 秒
- 分红 118 个报告期：80 秒
- 估值历史 5,552 只（2 workers）：约 40 分钟（含双进程事故重跑）
- 财务主指标 5,552 只（串行）：约 40 分钟
- 日线增量 5,552 只（腾讯限流→新浪熔断）：25 分钟
- 股息率 TTM 重算 5,552 只：3.7 分钟
- 事故记录：①`~/data/logs` 未建导致重定向失败；②`out_path` 未建叶子目录；③清理 `*.tmp` 误删正在写的文件导致主循环崩溃（排空机制保证了数据完整，教训：运行中勿清 tmp）
