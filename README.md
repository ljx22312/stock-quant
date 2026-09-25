# StockDesk — A股自托管行情台

数据自动采集 → 本机数据服务 → 网页行情台 + AI 助手，全链路跑在自己的机器上。
附带一个盘中微信提醒器（历史起点，现为附属功能）。

## 总体架构

```
数据源（腾讯行情 / 东财 / akshare / FRED / 天天基金）
      │  jobs/  定时采集与同步（cron，交易日驱动）
      ▼
data/*.db  SQLite 本地主库（日线/小时线/快照/信号/宏观）
      │  HTTP（/ingest 写入，/api 只读）
      ▼
server/data_service.py :8791 —— 本机数据服务（零第三方依赖）
      │  同源反代
      ├────────────► web/          网页行情台（自选/K线/分时/信号/宏观）
      └────────────► ai/worker.js  AI 助手（fast 直调模型 / agent 跑 pi coding agent）

monitor/  盘中微信提醒（独立旁路：3 秒轮询 → 规则引擎 → WxPusher 推送）
```

## 目录结构

```
stock-alert/
├── config.json          # 监控股池 + 规则参数（monitor 与 jobs 共用，你要维护的文件之一）
├── monitor/             # 盘中微信提醒子系统
│   ├── run_monitor.py   #   盘中监控引擎入口（常驻）
│   ├── run_daily.py     #   收盘后日线更新 + 日线信号（15:35 cron）
│   ├── ctl.sh           #   进程管理 start/stop/restart/status
│   └── quotes/daily/strategies/store/notify.py
│                        #   行情源 / 日线源 / 信号规则（你要维护的文件之一）/ SQLite / WxPusher
├── server/
│   ├── data_service.py  # 本机数据服务：/api 只读 + /ingest 写入 + AI 消息队列
│   └── .env.example     # 数据服务配置模板
├── web/                 # 网页行情台前端（纯静态 + ECharts，nginx 托管）
├── ai/
│   ├── worker.js        # AI 出站轮询 worker：fast 直调模型 / agent spawn pi
│   ├── server.js        # 可选：本地 HTTP shell（Bearer 鉴权 + SSE 透传）
│   ├── extensions/      # pi 模型 provider 注册（kimi / oczen）+ 专家身份注入
│   ├── skill/           # 17 个 skill：15 个专家领域 + stock-data（取数工具）+ web-search
│   └── .env.example     # AI 配置模板（复制到仓库根 .env）
├── jobs/                # 定时任务：数据采集与同步（全部可独立运行）
│   ├── sync_data.py     #   数据推送端：stocks/daily/hours/snapshot/signals/profile/all
│   ├── sync_industry.py #   申万一级行业指数（读 dl_sw_industry 的 CSV）
│   ├── sync_macro.py    #   宏观/海外/情绪指标（读 dl_macro/dl_vix/dl_fred 的 CSV）
│   ├── archive_daily.py #   每日数据归档 zip
│   └── dl_*.py          #   原始数据下载：东财全A日线 / 季度财报 / 宏观 / 行业 / 基金 / FRED / QVIX
├── tools/               # 回测与验证：backtest_week / validate_drop_rules / plot_* / bench_fetch
├── deploy/crontab.example  # 定时任务快照（当前线上在用的那套）
├── data/                # 本地数据（不入库）：SQLite 库、下载 CSV、盘中轨迹、每日归档
└── logs/                # 运行日志（不入库）
```

## 快速开始

依赖：Python 3（仅需 `requests`，系统自带）；Node.js（仅 AI worker 需要）；可选 `akshare`（采集脚本用）。

### 1. 数据服务

```bash
cd server && cp .env.example .env   # 按需修改，默认即可
python3 data_service.py             # 监听 127.0.0.1:8791
```

主要接口：

| 接口 | 说明 |
|---|---|
| `GET /api/quote?symbols=601169,sh000300` | 最新快照 |
| `GET /api/daily?symbol=601169&limit=250` | 历史日线（升序） |
| `GET /api/hour?symbol=601169&limit=320` | 60 分钟线 |
| `GET /api/tick?symbol=601169` | 当日分时帧 |
| `GET /api/profile?symbol=601169` | 日内量能分布（48 个 5 分钟时段均量） |
| `GET /api/stocks?type=stock\|index` | 股池/指数目录 |
| `GET /api/signals?limit=50` | 最近信号 |
| `GET /api/macro` | 宏观指标 |
| `POST /ingest` | 数据写入入口（`x-sync-token` 鉴权，jobs/ 推送用） |

### 2. 网页

`web/` 是纯静态页，任意静态服务器托管后把 API 反代到数据服务即可。nginx 示例
（完整可用版见 `deploy/nginx-stockdesk.conf`）：

```nginx
server {
    listen 80;
    server_name stock-16601896519.site www.stock-16601896519.site;
    root /home/ubuntu/stock-alert/web;
    index index.html;

    # 同源 API / AI 队列：必须透传 Host，数据服务按 Host 白名单放行 /collections
    location /api/          { proxy_pass http://127.0.0.1:8791/api/;         proxy_set_header Host $host; }
    location /ingest        { proxy_pass http://127.0.0.1:8791/ingest;       proxy_set_header Host $host; }
    location /collections/  { proxy_pass http://127.0.0.1:8791/collections/; proxy_set_header Host $host; proxy_buffering off; }

    location / { try_files $uri $uri/ =404; }
}
```

### 3. 定时任务

参考 `deploy/crontab.example`：交易日收盘后全量同步、盘中每 5 分钟快照、每小时信号增量、
行业/宏观数据当日更新、每日归档。

### 4. AI 助手

```bash
cp ai/.env.example .env    # 填 KIMI_API_KEY / OPENCODE_API_KEY
node ai/worker.js          # 出站轮询 AI 请求队列，无需公网入站
```

- **⚡快速**：直调模型 API（kimi/k3、oczen/deepseek-v4-flash…），附带 stock-data 工具查真实行情。
- **🤖Agent**：spawn [pi coding agent](https://github.com/badlogic/pi-mono)（RPC 模式），按用户选择的专家
  skill 载入 `ai/skill/<name>/SKILL.md`，以该领域专家身份工作，能自己写 Python 跑计算/回测。
  会话落 `~/.pi/agent/sessions/<session_id>/`，终端 `pi --resume` 可续聊。

## 盘中微信提醒（monitor/）

盘中 3 秒轮询股池快照（腾讯 `qt.gtimg.cn` 批量，1Hz 压测零失败、延迟 ~40ms），触发规则经
`(股票, 规则)` 冷却去重后经 WxPusher 推送微信。股池与规则参数都在 `config.json`。

| 规则 | 级别 | 说明 |
|---|---|---|
| `zscore_dev` | 盘中 | 价格偏离盘中均值 z 分数超阈值 |
| `vol_ratio` | 盘中 | 最近 5 分钟量为同时段基线的倍数超阈值 |
| `gap` | 盘中 | 现价相对昨收跳空超阈值 |
| `limit_event` | 盘中 | 涨停/跌停事件 |
| `fast_drop` / `slow_drop` | 盘中 | 快速/缓慢下挫 |
| `index_move` | 指数 | 指数短时涨跌幅超阈值 |
| `ma_cross` | 日线 | 收盘价上穿/下穿 MA20（收盘后评估） |

```bash
python3 monitor/run_monitor.py --push-test   # 测试推送通道
python3 monitor/run_monitor.py --once        # 单周期冒烟（不推送）
bash monitor/ctl.sh start|status|stop        # 常驻管理
python3 monitor/run_daily.py                 # 手动跑一次日线任务
```

## 数据链路要点（已实测）

- **实时**：腾讯批量快照一个请求带全部股池（≤60 只），快照本身约 3 秒刷新。
- **日线**：腾讯前复权日线为主（每次取最近 150 根 upsert），东财接口限流激进只作兜底。
- **同步水位**：jobs/ 推送按 `data/sync_state/` 里的水位增量推送，幂等 upsert，可随时重跑。
- **运行时依赖**：监控与数据服务仅 `requests`；akshare 只在 jobs/ 的采集脚本里用。

## 已知边界

- 交易时段判断只看"周一至五 + 时间"，法定节假日会空跑（拉到的行情日期不是今天，
  规则不会误触发，但进程不退出）。需要精确交易日历可后续加。
- 推送依赖 WxPusher 免费额度，规则触发频繁时请加大 `cooldown_minutes`。
- 下载类脚本（dl_eastmoney / dl_fund_hold）为断点续传/外机运行设计，见各文件头部说明。

## backtest/ —— 量化回测工作区

本仓库自 2026-09-25 起合并量化回测子系统（backtest/ 子目录，独立提交历史经 subtree 并入）：

- backtest/qbacktest/ —— 零依赖回测框架（引擎/记账/指标/策略/测试，详见其 README）
- backtest/factors/ —— 独立因子库（F2/F4/F6、影线类 + factor_lib 算子库）
- backtest/*.py —— 全市场因子检验（IC/分层/多空）、市场状态分析、数据审计脚本
- backtest/BACKTEST_GUIDE.md —— 回测口径与数据说明（必读）

数据不入库（.gitignore 排除语料/结果/缓存）。
