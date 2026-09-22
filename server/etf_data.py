#!/usr/bin/env python3
"""ETF 数据模块：精选目录、腾讯实时行情、日线合并（本地 tdx 基底 + 腾讯增量尾巴）。

数据链路（全部服务端完成，前端零感知）：
  实时行情  qt.gtimg.cn（腾讯，GBK）。字段序：1名称 3现价 4昨收 5今开 6成交量(手)
            30时间 31涨跌额 32涨跌幅 33最高 34最低 37成交额(万) 38换手率
  日线      本地 tdx CSV（不复权，volume=股）为基底，腾讯 fqkline 增量补尾（volume=手）。
            若两源在重叠日的收盘偏差 >0.2%（说明期间发生分红除权、前复权基准漂移），
            整段改用腾讯前复权序列，保证曲线连续。
            磁盘缓存 data/etf_cache/{sym}.json；收盘时段 30 分钟、其余 8 小时刷新一次，
            刷新失败降级返回旧数据（不阻塞行情台）。

口径：日线 volume 统一为「手」、amount 为「元」，与站点 daily_bars 约定一致。
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.request
from datetime import timedelta
from pathlib import Path

HERE = Path(__file__).resolve().parent
CACHE_DIR = Path(os.environ.get("ETF_CACHE_DIR", str(HERE.parent / "data" / "etf_cache")))
FUND_DIR = Path("/home/ubuntu/data/downloads/tdx_data/fund")          # volume=股
XB_DIR = Path("/home/ubuntu/data/downloads/tdx_data/cross_border_etf")  # volume=手
DRIFT_TOL = 0.002          # 重叠日收盘相对偏差阈值（>0.2% 视为除权漂移）
CLOSE_REFRESH_SEC = 1800   # 收盘时段（15:00~次日02:00）缓存刷新间隔
IDLE_REFRESH_SEC = 8 * 3600

# ---------- 精选目录（symbol, 展示名, 类别） ----------
CATALOG = [
    # 宽基
    ("sh510050", "上证50ETF", "broad"), ("sh510300", "沪深300ETF", "broad"),
    ("sh510500", "中证500ETF", "broad"), ("sh512100", "中证1000ETF", "broad"),
    ("sh563360", "A500ETF华泰柏瑞", "broad"), ("sz159338", "A500ETF国泰", "broad"),
    ("sz159915", "创业板ETF", "broad"), ("sh588000", "科创50ETF", "broad"),
    ("sh510880", "红利ETF", "broad"), ("sz159901", "深证100ETF", "broad"),
    # 跨境
    ("sh513100", "纳指ETF", "cross"), ("sh513500", "标普500ETF", "cross"),
    ("sh513520", "日经ETF", "cross"), ("sh513030", "德国ETF", "cross"),
    ("sh513080", "法国ETF", "cross"), ("sz159920", "恒生ETF", "cross"),
    ("sh513180", "恒生科技ETF", "cross"), ("sh513090", "香港证券ETF", "cross"),
    ("sh513310", "中韩半导体ETF", "cross"),
    # 行业主题
    ("sh512480", "半导体ETF", "sector"), ("sh512760", "芯片ETF", "sector"),
    ("sh512170", "医疗ETF", "sector"), ("sh512010", "医药ETF", "sector"),
    ("sz159992", "创新药ETF", "sector"), ("sh513120", "港股创新药ETF", "sector"),
    ("sh512690", "酒ETF", "sector"), ("sh512800", "银行ETF", "sector"),
    ("sh512000", "券商ETF", "sector"), ("sh512660", "军工ETF", "sector"),
    ("sh515790", "光伏ETF", "sector"), ("sh515030", "新能源车ETF", "sector"),
    ("sh515880", "通信ETF", "sector"), ("sz159852", "软件ETF", "sector"),
    ("sz159869", "游戏ETF", "sector"), ("sh512400", "有色ETF", "sector"),
    ("sh515220", "煤炭ETF", "sector"), ("sz159928", "消费ETF", "sector"),
    ("sh562500", "机器人ETF", "sector"), ("sz159611", "电力ETF", "sector"),
    # 商品
    ("sh518880", "黄金ETF", "cmdty"), ("sz161226", "白银LOF", "cmdty"),
    ("sz159985", "豆粕ETF", "cmdty"), ("sz161129", "原油LOF", "cmdty"),
    # 债券/货币
    ("sh511010", "国债ETF", "bond"), ("sh511260", "十年国债ETF", "bond"),
    ("sh511090", "30年国债ETF", "bond"), ("sh511380", "可转债ETF", "bond"),
    ("sh511990", "华宝添益", "bond"), ("sh511880", "银华日利", "bond"),
]
CAT_NAME = {"broad": "宽基", "cross": "跨境", "sector": "行业", "cmdty": "商品", "bond": "债券"}
CATALOG_BY_SYM = {s: {"symbol": s, "name": n, "cat": c} for s, n, c in CATALOG}

SYM_RE = re.compile(r"^(sh|sz)\d{6}$")
_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()
_quote_cache: tuple[float, dict] | None = None  # (fetched_ts, {sym: doc})


def _lock(sym: str) -> threading.Lock:
    with _locks_guard:
        if sym not in _locks:
            _locks[sym] = threading.Lock()
        return _locks[sym]


def _http(url: str, timeout: float = 6.0) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    return urllib.request.urlopen(req, timeout=timeout).read()


def _bj_now():
    from datetime import datetime, timezone
    return datetime.now(timezone(timedelta(hours=8)))


# ---------- 实时行情（腾讯） ----------
def tencent_quotes(symbols: list[str]) -> dict[str, dict]:
    """批量抓实时行情，返回 snapshot 同形文档（缺数的标的置空）。整体失败抛异常。"""
    out: dict[str, dict] = {}
    for i in range(0, len(symbols), 50):
        batch = symbols[i:i + 50]
        url = "https://qt.gtimg.cn/q=" + ",".join(batch)
        raw = _http(url).decode("gbk", errors="replace")
        for part in raw.strip().split(";"):
            part = part.strip()
            if '="' not in part:
                continue
            sym = part.split("=")[0].split("_")[-1]
            f = part.split('="', 1)[1].rstrip('";\r').split("~")
            if len(f) < 39 or not f[3]:
                continue
            try:
                out[sym] = {
                    "symbol": sym, "name": f[1], "price": float(f[3]),
                    "prev_close": float(f[4]), "open": float(f[5]),
                    "volume_hand": float(f[6]), "quote_time": f[30],
                    "pct_chg": round(float(f[32]), 3), "high": float(f[33]),
                    "low": float(f[34]), "amount_wan": float(f[37]),
                    "turnover_rate": float(f[38]) if f[38] else None,
                }
            except (ValueError, IndexError):
                continue
    return out


def etf_list(q: dict) -> list[dict]:
    """/api/etf：目录 + 实时行情合并。symbols 可选过滤。失败时回退 10 分钟内的旧缓存。"""
    global _quote_cache
    syms = [s.strip() for s in str(q.get("symbols") or "").split(",") if s.strip()]
    syms = [s for s in syms if s in CATALOG_BY_SYM] or [c[0] for c in CATALOG]
    try:
        quotes = tencent_quotes(syms)
        global_ts = time.time()
        _quote_cache = (global_ts, quotes)
    except Exception:
        if _quote_cache and time.time() - _quote_cache[0] < 600:
            quotes = _quote_cache[1]
        else:
            quotes = {}
    out = []
    for sym in syms:
        item = dict(CATALOG_BY_SYM[sym])
        item["cat_name"] = CAT_NAME[item["cat"]]
        qd = quotes.get(sym)
        if qd:
            item.update({k: d for k, d in qd.items() if k not in ("symbol", "name")})
            item["stale"] = False
        else:
            item["stale"] = True
        out.append(item)
    return out


# ---------- 日线（本地基底 + 腾讯增量） ----------
def _find_local(sym: str):
    """返回 (path, volume_is_share)；找不到返回 (None, False)。"""
    hits = sorted(FUND_DIR.glob(f"{sym}_*.csv"))
    if hits:
        return hits[0], True
    hits = sorted(XB_DIR.glob(f"{sym}_*.csv")) + sorted(XB_DIR.glob(f"{sym[2:]}_*.csv"))
    if hits:
        return hits[0], False
    return None, False


def _read_local(path: Path, vol_is_share: bool) -> list[list]:
    rows: dict[str, list] = {}
    with open(path, encoding="utf-8-sig") as f:
        for line in f:
            parts = line.strip().split(",")
            if len(parts) < 7 or not re.match(r"^\d{4}-\d{2}-\d{2}$", parts[0]):
                continue
            try:
                d, o, c, h, l, v, a = parts[:7]
                o, c, h, l = float(o), float(c), float(h), float(l)
                v, a = float(v), float(a) if a else 0.0
                if o <= 0 or c <= 0:
                    continue
                rows[d] = [d, o, h, l, c, v / 100.0 if vol_is_share else v, a]
            except ValueError:
                continue
    return [rows[k] for k in sorted(rows)]


KLINE_HOSTS = ("https://ifzq.gtimg.cn", "https://web.ifzq.gtimg.cn")  # web. 前缀偶发 501，裸域优先


def _tencent_daily(sym: str, want: int = 120) -> list[list]:
    """fqkline：[date, open, close, high, low, volume(手), ...] → 本模块行格式（amount 估算）。"""
    last_err = None
    for host in KLINE_HOSTS:
        url = (f"{host}/appstock/app/fqkline/get?param={sym},day,,,{want},qfq")
        try:
            raw = _http(url)
        except Exception as e:  # noqa: BLE001
            last_err = e
            continue
        j = json.loads(raw.decode("utf-8", errors="replace"))
        d = (j.get("data") or {}).get(sym) or {}
        kl = d.get("qfqday") or d.get("day") or []
        out = []
        for k in kl:
            try:
                date, o, c, h, l, v = k[0], float(k[1]), float(k[2]), float(k[3]), float(k[4]), float(k[5])
                amt = (o + h + l + c) / 4.0 * v * 100.0  # fqkline 无成交额，按均价估算（元）
                out.append([date, o, h, l, c, v, round(amt)])
            except (ValueError, IndexError):
                continue
        return out
    raise last_err or RuntimeError("fqkline all hosts failed")


def _drift(a: list[list], b: list[list]) -> bool:
    """两序列按日期比对最近 ≤5 个重叠日收盘，中位相对偏差 > 阈值视为除权漂移。"""
    bmap = {r[0]: r[4] for r in b}
    diffs = [abs(r[4] - bmap[r[0]]) / r[4] for r in a[-40:] if r[0] in bmap and r[4] > 0]
    if len(diffs) < 2:
        return False
    diffs.sort()
    med = diffs[len(diffs) // 2]
    return med > DRIFT_TOL


def _need_refresh(meta: dict) -> bool:
    age = time.time() - meta.get("fetched_at", 0)
    if age < 300:
        return False
    now = _bj_now()
    post_close = now.hour >= 15 or now.hour < 2
    return age > (CLOSE_REFRESH_SEC if post_close else IDLE_REFRESH_SEC)


def _series(sym: str) -> list[list]:
    """合并后的完整日线（可能网络失败时为本地基底）。行：[date,o,h,l,c,vol手,amount元]。"""
    path, vol_is_share = _find_local(sym)
    if path is None:
        raise ValueError(f"unknown fund symbol: {sym}")
    local = _read_local(path, vol_is_share)
    cache = CACHE_DIR / f"{sym}.json"
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        obj = json.loads(cache.read_text())
        rows, meta = obj["rows"], obj.get("meta", {})
    except Exception:
        rows, meta = local, {}
    # 本地文件比缓存新（tdx 推送恢复后），以本地为基底重算
    if local and (not rows or (local[-1][0] > rows[-1][0] and rows[-1][0] not in {r[0] for r in local[-3:]})):
        rows = local
    if _need_refresh(meta) or not rows:
        try:
            tail = _tencent_daily(sym, want=90)
            if tail:
                if _drift(rows or local, tail):
                    big = _tencent_daily(sym, want=800) or tail  # 除权 → 改用腾讯整段
                    rows = big
                else:
                    have = {r[0] for r in rows}
                    rows = rows + [r for r in tail if r[0] not in have]
        except Exception:
            pass  # 网络失败：保留旧数据
        else:
            meta = {"fetched_at": time.time(), "src": path.name}
        try:
            cache.write_text(json.dumps({"rows": rows, "meta": meta}, ensure_ascii=False))
        except Exception:
            pass
    return rows


def etf_daily(q: dict) -> list[dict]:
    """/api/etfdaily?symbol=sh510300&limit=250：与 /api/daily 同形的 K 线数组。"""
    sym = str(q.get("symbol") or "").strip()
    if not SYM_RE.match(sym):
        raise ValueError("symbol required (e.g. sh510300)")
    limit = min(max(int(q.get("limit") or 120), 1), 2000)
    with _lock(sym):
        rows = _series(sym)
    return [{"symbol": sym, "date": r[0], "open": r[1], "high": r[2],
             "low": r[3], "close": r[4], "volume": r[5], "amount": r[6]}
            for r in rows[-limit:]]


if __name__ == "__main__":  # 自检：python3 etf_data.py
    missing = [s for s, _, _ in CATALOG if _find_local(s)[0] is None]
    print(f"目录 {len(CATALOG)} 只，本地缺文件: {missing or '无'}")
    qs = tencent_quotes(["sh510300", "sz159915"])
    for s, d in qs.items():
        print(f"行情 {s}: {d['price']} {d['pct_chg']}% 额{d['amount_wan']/1e4:.1f}亿 @{d['quote_time']}")
    rows = _series("sh510300")
    print(f"510300 合并序列 {len(rows)} 根，{rows[0][0]} ~ {rows[-1][0]}，末日量(手) {rows[-1][5]:.0f}")
    # 单位自证：合并末日量应与腾讯实时 volume_hand 同量级（手）
    if "sh510300" in qs and rows[-1][0] in {r[0] for r in rows}:
        print(f"实时量(手) {qs['sh510300']['volume_hand']:.0f}（同日对照用）")
