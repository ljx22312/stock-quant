"""盘中监控引擎：拉快照 -> 评估盘中规则 -> 冷却去重 -> 推送。

用法：
  python3 monitor/run_monitor.py            # 常驻循环（交易时段内工作，其余时间休眠）
  python3 monitor/run_monitor.py --once     # 强制执行一个周期（dry-run 不推送）
  python3 monitor/run_monitor.py --push-test  # 发一条微信测试推送

config.json 关键项：
  push_enabled   false 时只记录信号不推送（dry-run 试运行用）
  index_code     指数市场代码（如 sh000001），供指数级规则使用
  params.<规则>.cooldown_minutes  可覆盖全局冷却，如每天一次设 1200
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from monitor.notify import Notifier
from monitor.quotes import fetch_quotes, is_trading_now
from monitor.store import Store
from monitor.strategies import INDEX_RULES, INTRADAY_RULES, RuleContext

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(ROOT / "logs" / "monitor.log", encoding="utf-8"),
    ],
)
log = logging.getLogger("monitor")

CONFIG_PATH = ROOT / "config.json"


def load_config() -> dict:
    with open(CONFIG_PATH, encoding="utf-8") as f:
        return json.load(f)


class Engine:
    def __init__(self, config: dict, dry_run: bool = False):
        self.cfg = config
        self.dry_run = dry_run or not config.get("push_enabled", True)
        self.store = Store(config["db_path"])
        self.notifier = None
        if not self.dry_run:
            self.notifier = Notifier(config["wxpusher_config"], config["recipient"])
        self.names = {w["symbol"]: w["name"] for w in config["watchlist"]}
        self.symbols = list(self.names)
        self.index_code = config.get("index_code")  # 如 'sh000001'
        # 盘中价格轨迹: {symbol: [(ts, price, cum_volume_hand), ...]}
        self.intraday: dict[str, list[tuple]] = {s: [] for s in self.symbols}
        self.index_intraday: list[tuple] = []
        # 轨迹落盘：每 5 分钟增量追加到 data/intraday/<YYYYMMDD>/<代码>.csv
        self.flush_dir = ROOT / "data" / "intraday"
        self.flushed_ts: dict[str, float] = {}
        self.last_flush = 0.0
        self.today = datetime.now().strftime("%Y%m%d")
        self._stale_log_day = ""  # 休市防护的"当日已告警"标记，避免每个周期刷日志
        self.rules = [(n, INTRADAY_RULES[n]) for n in config["intraday_rules_enabled"]
                      if n in INTRADAY_RULES]
        self.index_rules = [(n, INDEX_RULES[n]) for n in config.get("index_rules_enabled", [])
                            if n in INDEX_RULES]
        # 规则用的静态数据：日线（gap 需要）与同时段量基线（vol_ratio 需要）
        self.daily = {}
        self.vol_profiles = {}
        self._load_static()
        self.fail_streak = 0

    def _load_static(self):
        for s in self.symbols:
            try:
                self.daily[s] = self.store.get_daily(s, limit=30)
                self.vol_profiles[s] = self.store.get_vol_profile(s)
            except Exception as e:
                log.warning("%s 静态数据加载失败: %s", s, e)
                self.daily[s] = []
                self.vol_profiles[s] = {}

    def _flush_intraday(self):
        """把内存中未落盘的轨迹增量追加写入 CSV，失败不影响主流程。"""
        try:
            day_dir = self.flush_dir / self.today
            targets = list(self.intraday.items())
            if self.index_code:
                targets.append((self.index_code, self.index_intraday))
            for sym, hist in targets:
                last = self.flushed_ts.get(sym, 0)
                rows = [r for r in hist if r[0] > last]
                if not rows:
                    continue
                day_dir.mkdir(parents=True, exist_ok=True)
                path = day_dir / f"{sym}.csv"
                new = not path.exists()
                with open(path, "a", encoding="utf-8") as f:
                    if new:
                        f.write("ts,price,cum_volume_hand\n")
                    for r in rows:
                        vol = r[2] if len(r) > 2 else ""
                        f.write(f"{r[0]},{r[1]},{vol}\n")
                self.flushed_ts[sym] = rows[-1][0]
        except Exception as e:
            log.warning("轨迹落盘失败: %s", e)

    def _roll_day(self):
        """跨天时先把前一日轨迹落盘，再清空盘中轨迹并重载静态数据。"""
        today = datetime.now().strftime("%Y%m%d")
        if today != self.today:
            self._flush_intraday()
            self.today = today
            self.intraday = {s: [] for s in self.symbols}
            self.index_intraday = []
            self.flushed_ts = {}
            self._load_static()
            log.info("新的一天，清空盘中轨迹并重载静态数据")

    def _fire(self, symbol: str, name: str, rule_name: str, msg: str,
              cooldown_min: int):
        if self.store.in_cooldown(symbol, rule_name, cooldown_min):
            return
        self.store.log_signal(symbol, rule_name, msg)
        log.info("信号: %s", msg)
        if self.dry_run:
            print(f"[DRY-RUN 推送] {msg}")
        elif self.notifier and self.notifier.send(msg):
            log.info("推送成功")
        else:
            log.error("推送失败: %s", msg)

    def run_cycle(self):
        extra = (self.index_code,) if self.index_code else ()
        quotes = fetch_quotes(self.symbols, extra_codes=extra)
        if not quotes:
            raise RuntimeError("行情拉取为空")

        # 休市防护：节假日快照冻结在前一交易日（quote_time 日期非今天），跳过评估与
        # 轨迹记录。否则 fast_drop 在"开盘回退今开"分支会把前一交易日开盘->收盘的
        # 全天涨跌当成 30 分钟变动误报。仅在明确识别到过期快照时拦截，缺字段则放行。
        today = datetime.now().strftime("%Y%m%d")
        sample = quotes.get(self.symbols[0]) or next(iter(quotes.values()))
        qt = str(sample.get("quote_time", ""))
        if qt and qt[:8] != today:
            if self._stale_log_day != today:
                log.info("行情快照日期为 %s（非今日，休市），跳过评估", qt[:8])
                self._stale_log_day = today
            return

        now = int(time.time())

        for symbol, q in quotes.items():
            if symbol == self.index_code:
                self.index_intraday.append((now, q["price"]))
                continue
            hist = self.intraday.setdefault(symbol, [])
            hist.append((now, q["price"], q.get("volume_hand", 0.0)))
            for rule_name, rule in self.rules:
                params = self.cfg["params"].get(rule_name, {})
                ctx = RuleContext(
                    symbol=symbol,
                    name=self.names.get(symbol, q["name"]),
                    quote=q,
                    intraday=hist,
                    daily=self.daily.get(symbol, []),
                    params=params,
                    vol_profile=self.vol_profiles.get(symbol, {}),
                    index_intraday=self.index_intraday,
                    index_quote=quotes.get(self.index_code, {}),
                )
                try:
                    msg = rule(ctx)
                except Exception as e:
                    log.warning("规则 %s 在 %s 上执行异常: %s", rule_name, symbol, e)
                    continue
                if msg:
                    self._fire(symbol, ctx.name, rule_name, msg,
                               params.get("cooldown_minutes", self.cfg["cooldown_minutes"]))

        # 指数级规则（每周期一次）
        if self.index_code and self.index_code in quotes and self.index_rules:
            iq = quotes[self.index_code]
            for rule_name, rule in self.index_rules:
                params = self.cfg["params"].get(rule_name, {})
                ctx = RuleContext(
                    symbol=self.index_code, name=iq.get("name", "指数"),
                    quote=iq, intraday=self.index_intraday, params=params,
                )
                try:
                    msg = rule(ctx)
                except Exception as e:
                    log.warning("指数规则 %s 执行异常: %s", rule_name, e)
                    continue
                if msg:
                    self._fire(self.index_code, ctx.name, rule_name, msg,
                               params.get("cooldown_minutes", self.cfg["cooldown_minutes"]))

    def run(self):
        interval = self.cfg["poll_interval_sec"]
        log.info("监控启动: %d 只股票, 间隔 %ds, 盘中规则 %s, 指数规则 %s, 推送 %s",
                 len(self.symbols), interval, self.cfg["intraday_rules_enabled"],
                 self.cfg.get("index_rules_enabled", []),
                 "关闭(dry-run)" if self.dry_run else "开启")
        # SIGTERM 转成 SystemExit，保证退出前执行 finally 里的落盘
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            while True:
                self._roll_day()
                if not is_trading_now():
                    time.sleep(60)
                    continue
                try:
                    self.run_cycle()
                    self.fail_streak = 0
                    if time.time() - self.last_flush >= 300:
                        self._flush_intraday()
                        self.last_flush = time.time()
                    time.sleep(interval)
                except Exception as e:
                    self.fail_streak += 1
                    backoff = 10 if self.fail_streak < 3 else 60
                    log.warning("第 %d 次连续失败: %s，%ds 后重试", self.fail_streak, e, backoff)
                    time.sleep(backoff)
                    if self.fail_streak == 5 and self.notifier:
                        self.notifier.send("【告警】行情监控连续失败 5 次，请检查网络或数据源")
        finally:
            self._flush_intraday()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="强制执行一个周期（dry-run 不推送）")
    parser.add_argument("--push-test", action="store_true", help="发送微信测试推送")
    args = parser.parse_args()

    cfg = load_config()

    if args.push_test:
        n = Notifier(cfg["wxpusher_config"], cfg["recipient"])
        ok = n.send(f"【测试】stock-alert 推送通道正常 {datetime.now():%H:%M:%S}")
        print("推送成功" if ok else "推送失败")
        return

    engine = Engine(cfg, dry_run=args.once)
    if args.once:
        engine.run_cycle()
        for s, hist in engine.intraday.items():
            if hist:
                print(f"{engine.names[s]}({s}): 现价 {hist[-1][1]:.2f}")
        if engine.index_intraday:
            print(f"指数({engine.index_code}): {engine.index_intraday[-1][1]:.2f}")
        return

    engine.run()


if __name__ == "__main__":
    main()
