#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""数据加载层：把本机三类行情 CSV 统一为回测友好的 DataFrame。

数据资产（口径详见 StockDesk AGENTS.md）：
  A股日线  /home/ubuntu/data/eastmoney_data/{code}_{name}.csv   前复权，volume=手
  场内基金 /home/ubuntu/data/downloads/tdx_data/fund/{sym}_{name}.csv  不复权，volume=股
  跨境基金 /home/ubuntu/data/downloads/tdx_data/cross_border_etf/     不复权，volume=手

统一输出：DataFrame(index=date 升序, columns=open/high/low/close/volume/amount)，
volume 一律换算为「股/份」，amount 为「元」。行内 close<=0 或 NaN 剔除，日期去重。

注意（防坑）：
  - 前复权价格随最新除权整体重算：收益率/比例正确，但**不能**用于涨停价、
    绝对价格、真实市值判断；
  - 基金日线为不复权：期间有分红的 ETF（如 510300 年度分红）在除权日会有
    价格跳空，长期回测存在小幅低估，精度敏感的场景请改用前复权序列。
"""
from __future__ import annotations

import glob
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

import pandas as pd

EM_DIR = "/home/ubuntu/data/eastmoney_data"
FUND_DIR = "/home/ubuntu/data/downloads/tdx_data/fund"
XB_DIR = "/home/ubuntu/data/downloads/tdx_data/cross_border_etf"
CACHE_DIR = Path(__file__).resolve().parent / "cache"
QFQ_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn")

COLS = ["open", "high", "low", "close", "volume", "amount"]


def read_ohlcv(path: str | os.PathLike, volume_unit: str = "hand") -> pd.DataFrame:
    """读单个 CSV。volume_unit: hand=手(×100→股) / share=股 / raw=原样。"""
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip().lower() for c in df.columns]
    need = {"date", *COLS}
    missing = need - set(df.columns)
    if missing:
        raise ValueError(f"{path}: 缺列 {missing}")
    df["date"] = pd.to_datetime(df["date"]).dt.strftime("%Y-%m-%d")
    df = df.drop_duplicates("date").sort_values("date").set_index("date")
    for c in COLS:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    if volume_unit == "hand":
        df["volume"] = df["volume"] * 100.0
    elif volume_unit != "share":
        pass
    df = df[df["close"].notna() & (df["close"] > 0)]
    # 无成交额列/缺额时补 0（vwap 回退均价）
    df["amount"] = df["amount"].fillna(0.0)
    return df[COLS]


def load_stock(code: str, start=None, end=None) -> pd.DataFrame:
    """A股前复权日线（eastmoney_data）。code: 6位数字。"""
    code = re.sub(r"^(sh|sz|bj)", "", code.lower())
    hits = glob.glob(f"{EM_DIR}/{code}_*.csv")
    if not hits:
        raise FileNotFoundError(f"eastmoney_data 无 {code}（{EM_DIR}/{code}_*.csv）")
    df = read_ohlcv(hits[0], volume_unit="hand")
    return _window(df, start, end)


def load_fund(symbol: str, start=None, end=None, adjust: str = "raw") -> pd.DataFrame:
    """场内基金/ETF 日线。symbol: sh510300 / sz159915。

    adjust="raw"  本地 tdx 不复权（快；**含份额折算/拆分跳空**，如 512100
                  2022-09 份额折算 +175%、512480 2026-07 拆分 -51%，回测慎用）；
    adjust="qfq"  腾讯前复权全历史（分红/折算已平滑，**回测推荐**；带磁盘缓存，
                  首次联网分页拉取约 5 年，之后读缓存）。
    """
    symbol = symbol.lower()
    if adjust == "qfq":
        return _window(_load_fund_qfq(symbol), start, end)
    for d, unit in ((FUND_DIR, "share"), (XB_DIR, "hand")):
        hits = glob.glob(f"{d}/{symbol}_*.csv") or glob.glob(f"{d}/{symbol[2:]}_*.csv")
        if hits:
            df = read_ohlcv(hits[0], volume_unit=unit)
            _warn_fund_gaps(symbol, df)
            return _window(df, start, end)
    raise FileNotFoundError(f"tdx fund/cross_border 无 {symbol}")


def _warn_fund_gaps(symbol: str, df: pd.DataFrame, thr: float = 0.21) -> None:
    """不复权基金数据隔夜跳空 >21% 大概率是份额折算/拆分，提醒改用 qfq。"""
    r = df["close"].pct_change().dropna()
    bad = r[abs(r) > thr]
    for d, v in bad.items():
        print(f"[qbacktest 警告] {symbol} 不复权序列 {d} 隔夜 {v:+.1%}，"
              f"疑似份额折算/拆分，回测请用 adjust='qfq'", file=sys.stderr)


def _tencent_qfq_page(symbol: str, want: int, start: str = "", end: str = "") -> list[list]:
    """一页 fqkline：[date, open, close, high, low, volume(手), ...]。

    裸域 ifzq 只支持显式 start,end 区间（空 start + end 会返回空），
    取最新一页时 start/end 均留空。
    """
    import time as _t
    last = None
    param = f"{symbol},day,{start},{end},{want},qfq"
    for host in QFQ_HOSTS:
        url = f"{host}/appstock/app/fqkline/get?param={param}"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            raw = urllib.request.urlopen(req, timeout=10).read()
        except Exception as e:  # noqa: BLE001
            last = e
            _t.sleep(1)
            continue
        j = json.loads(raw.decode("utf-8", errors="replace"))
        d = (j.get("data") or {}).get(symbol) or {}
        return d.get("qfqday") or d.get("day") or []
    raise last or RuntimeError("fqkline 全部主机失败")


def _load_fund_qfq(symbol: str) -> pd.DataFrame:
    CACHE_DIR.mkdir(exist_ok=True)
    cp = CACHE_DIR / f"{symbol}_qfq.csv"
    if cp.exists():
        return pd.read_csv(cp, index_col="date")
    rows: dict[str, list] = {}

    def merge(page: list[list]) -> None:
        for k in page:
            try:
                d, o, c, h, l, v = (k[0], float(k[1]), float(k[2]),
                                    float(k[3]), float(k[4]), float(k[5]))
                amt = (o + h + l + c) / 4.0 * v * 100.0
                rows[d] = [d, o, h, l, c, v * 100.0, amt]
            except (ValueError, IndexError):
                continue

    page = _tencent_qfq_page(symbol, 800)          # 最新一页
    merge(page)
    end = page[0][0] if page else ""
    for _ in range(8):                              # 向前翻页最多 8 页
        if not end or len(page) < 800:
            break
        page = _tencent_qfq_page(symbol, 800, start="1990-01-01", end=end)
        if not page:
            break
        earliest = page[0][0]
        merge(page)
        if earliest >= end or len(page) < 800:      # 无新增或已到上市起点
            break
        end = earliest
    if not rows:
        raise RuntimeError(f"{symbol} 腾讯 qfq 拉取失败（检查网络）")
    df = pd.DataFrame([rows[k] for k in sorted(rows)],
                      columns=["date", *COLS]).set_index("date")
    df.index.name = "date"
    df.to_csv(cp)
    return df


def load(symbol: str, start=None, end=None) -> pd.DataFrame:
    """自动识别：带交易所前缀(sh/sz+bj)→基金，纯6位数字→A股。"""
    if re.match(r"^(sh|sz|bj)\d{6}$", symbol.lower()):
        return load_fund(symbol, start, end)
    return load_stock(symbol, start, end)


def _window(df: pd.DataFrame, start, end) -> pd.DataFrame:
    if start:
        df = df[df.index >= str(start)]
    if end:
        df = df[df.index <= str(end)]
    return df


class Panel:
    """多标的对齐面板：union 日历 + 停牌处理。

    close  ：ffill（估值按停牌前收盘价）
    open   ：不填充（NaN = 当日不可交易，引擎跳过调仓）
    """

    def __init__(self, frames: dict[str, pd.DataFrame]):
        assert frames, "frames 为空"
        self.symbols = list(frames)
        self.calendar = sorted(set().union(*[set(f.index) for f in frames.values()]))
        idx = pd.Index(self.calendar, name="date")
        self.frames = {s: f.reindex(idx) for s, f in frames.items()}
        self.close = pd.DataFrame({s: f["close"] for s, f in self.frames.items()}).ffill()
        self.close_raw = pd.DataFrame({s: f["close"] for s, f in self.frames.items()})
        self.open = pd.DataFrame({s: f["open"] for s, f in self.frames.items()})
        self.high = pd.DataFrame({s: f["high"] for s, f in self.frames.items()})
        self.low = pd.DataFrame({s: f["low"] for s, f in self.frames.items()})
        self.amount = pd.DataFrame({s: f["amount"] for s, f in self.frames.items()})
        self.volume = pd.DataFrame({s: f["volume"] for s, f in self.frames.items()})

    def exec_price(self, mode: str = "open") -> pd.DataFrame:
        """执行价矩阵：open/close/vwap；vwap=amount/volume，缺失回退 (h+l+c)/3，再回退 close。"""
        if mode == "open":
            px = self.open.copy()
        elif mode == "close":
            px = self.close.copy()
        elif mode == "vwap":
            with_vol = self.volume > 0
            px = (self.amount / self.volume).where(with_vol)
            px = px.fillna((self.high + self.low + self.close) / 3.0)
            px = px.fillna(self.close)
        else:
            raise ValueError(f"exec_price 未知模式: {mode}")
        # open 模式下当日缺开盘价但当日实际有收盘（数据缺损）→ 回退当日收盘；
        # 注意用未 ffill 的原始收盘：停牌日（整行缺失）仍保持不可交易
        if mode == "open":
            fallback = self.close_raw.where(self.close_raw.notna() & self.open.isna())
            px = px.fillna(fallback)
            px = px.where(px.notna() & (px > 0))
        return px

    def tradable(self, mode: str = "open") -> pd.DataFrame:
        px = self.exec_price(mode)
        return px.notna() & (px > 0)
