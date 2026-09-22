#!/usr/bin/env python3
"""StockDesk 本机数据服务（零第三方依赖，Python 标准库）。

职责（接口与历史云端网关同形状，供前端/worker/115 机无缝切换）：
  POST /ingest                     数据推送入口（x-sync-token 鉴权；可选转发旧云端网关保过渡期双活）
  GET  /api/*                      只读行情接口（与 legacy/cloudbase/functions/api 同语义，{data,ts}）
  /collections/ai_requests|ai_replies/documents  AI 消息队列（与历史云端 DB 网关同语义）

配置（环境变量，可从同目录 .env 读取）：
  PORT/HOST          默认 8791 / 127.0.0.1
  DB_PATH            默认 <repo>/data/stockdesk.db
  INGEST_TOKEN       /ingest 鉴权令牌（= jobs 端 cloud_sync.json 里的 token）
  FORWARD_URL        转发目标（旧云端 ingest 地址，归档见 legacy/cloudbase/）；为空则只写本地
  FORWARD_TOKEN      转发令牌（默认同 INGEST_TOKEN）
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import sqlite3
import statistics
import threading
import time
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent

import etf_data  # noqa: E402  ETF/场内基金数据模块（精选目录+腾讯行情+日线合并）

# ---------- 配置 ----------
def load_dotenv(path: Path):
    try:
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())
    except FileNotFoundError:
        pass

load_dotenv(HERE / ".env")

PORT = int(os.environ.get("PORT", "8791"))
HOST = os.environ.get("HOST", "127.0.0.1")
DB_PATH = os.environ.get("DB_PATH", str(REPO / "data" / "stockdesk.db"))
INGEST_TOKEN = os.environ.get("INGEST_TOKEN", "")
FORWARD_URL = os.environ.get("FORWARD_URL", "")
FORWARD_TOKEN = os.environ.get("FORWARD_TOKEN", INGEST_TOKEN)
# 每日截面 CSV 目录（/api/market、/api/valsnap 直读，缺省本机数据资产布局）
MARKETDATA_DIR = Path(os.environ.get("MARKETDATA_DIR", "/home/ubuntu/marketdata"))
VALSNAP_DIR = Path(os.environ.get("VALSNAP_DIR", "/home/ubuntu/data/downloads/valuation_snapshot"))
VALUATION_DIR = Path(os.environ.get("VALUATION_DIR", "/home/ubuntu/data/downloads/valuation"))
FUNDFLOW_DIR = Path(os.environ.get("FUNDFLOW_DIR", "/home/ubuntu/fundflow/data"))

CST = timezone(timedelta(hours=8))


def bj_now():
    return datetime.now(CST)


# 允许访问 /collections 的 Host（浏览器同源 + 本机 worker 回环）
ALLOWED_COL_HOSTS = (
    "127.0.0.1", "localhost", "stock-16601896519.site", "www.stock-16601896519.site",
)

# ---------- sqlite schema ----------
SCHEMA = {
    "stocks": """CREATE TABLE IF NOT EXISTS stocks (
        symbol TEXT PRIMARY KEY, name TEXT, type TEXT)""",
    "snapshots": """CREATE TABLE IF NOT EXISTS snapshots (
        d TEXT NOT NULL, symbol TEXT NOT NULL, ts INTEGER,
        name TEXT, price REAL, pct_chg REAL, prev_close REAL, open REAL,
        high REAL, low REAL, volume_hand REAL, amount_wan REAL, turnover_rate REAL,
        pe_ttm REAL, circ_mv REAL, total_mv REAL, avg_price REAL, quote_time TEXT,
        PRIMARY KEY (d, symbol))""",
    "daily_bars": """CREATE TABLE IF NOT EXISTS daily_bars (
        symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
        close REAL, volume REAL, amount REAL,
        PRIMARY KEY (symbol, date))""",
    "hour_bars": """CREATE TABLE IF NOT EXISTS hour_bars (
        symbol TEXT NOT NULL, date TEXT NOT NULL, open REAL, high REAL, low REAL,
        close REAL, volume REAL, PRIMARY KEY (symbol, date))""",
    "ticks": """CREATE TABLE IF NOT EXISTS ticks (
        symbol TEXT NOT NULL, date TEXT NOT NULL, price REAL, pct_chg REAL,
        volume_hand REAL, amount_wan REAL, PRIMARY KEY (symbol, date))""",
    "vol_profile": """CREATE TABLE IF NOT EXISTS vol_profile (
        symbol TEXT NOT NULL, hm TEXT NOT NULL, avg_vol REAL,
        PRIMARY KEY (symbol, hm))""",
    "signals": """CREATE TABLE IF NOT EXISTS signals (
        symbol TEXT NOT NULL, rule TEXT NOT NULL, ts INTEGER NOT NULL,
        message TEXT, id INTEGER, doc_id TEXT, PRIMARY KEY (symbol, rule, ts))""",
    "macro_indicators": """CREATE TABLE IF NOT EXISTS macro_indicators (
        id TEXT PRIMARY KEY, name TEXT, unit TEXT, latest REAL, prev REAL, series TEXT)""",
    "ai_requests": """CREATE TABLE IF NOT EXISTS ai_requests (
        id TEXT PRIMARY KEY, mode TEXT, question TEXT, session_id TEXT, status TEXT,
        model TEXT, skill TEXT, target_id TEXT, created_at INTEGER, updated_at INTEGER)""",
    "ai_replies": """CREATE TABLE IF NOT EXISTS ai_replies (
        request_id TEXT PRIMARY KEY, text TEXT, thinking TEXT, done INTEGER,
        created_at INTEGER, updated_at INTEGER)""",
}

# 主键列（= ingest 接口的 _id 语义）；snapshots 额外带 d（推送日）维度
KEY = {
    "stocks": ("symbol",), "snapshots": ("symbol",), "daily_bars": ("symbol", "date"),
    "hour_bars": ("symbol", "date"), "ticks": ("symbol", "date"),
    "vol_profile": ("symbol", "hm"), "signals": ("symbol", "rule", "ts"),
    "macro_indicators": ("id",),
}
# ingest 可写列（业务字段，不含主键）
COLS = {
    "stocks": ("name", "type"),
    "snapshots": ("ts", "name", "price", "pct_chg", "prev_close", "open", "high",
                  "low", "volume_hand", "amount_wan", "turnover_rate", "pe_ttm",
                  "circ_mv", "total_mv", "avg_price", "quote_time"),
    "daily_bars": ("open", "high", "low", "close", "volume", "amount"),
    "hour_bars": ("open", "high", "low", "close", "volume"),
    "ticks": ("price", "pct_chg", "volume_hand", "amount_wan"),
    "vol_profile": ("avg_vol",),
    "signals": ("message", "id", "doc_id"),
    "macro_indicators": ("name", "unit", "latest", "prev", "series"),
}
NUMCOLS = {  # 数值列（float）
    "snapshots": ("price", "pct_chg", "prev_close", "open", "high", "low", "volume_hand",
                  "amount_wan", "turnover_rate", "pe_ttm", "circ_mv", "total_mv", "avg_price"),
    "daily_bars": ("open", "high", "low", "close", "volume", "amount"),
    "hour_bars": ("open", "high", "low", "close", "volume"),
    "ticks": ("price", "pct_chg", "volume_hand", "amount_wan"),
    "vol_profile": ("avg_vol",),
    "macro_indicators": ("latest", "prev"),
}
INTCOLS = {"snapshots": ("ts",), "signals": ("ts", "id")}
ALLOWED = set(SCHEMA.keys()) - {"ai_requests", "ai_replies"}

API_COLS = {  # /api 输出列（剔除内部列 d/doc_id）
    "stocks": ("symbol", "name", "type"),
    "snapshots": ("symbol", "ts", "name", "price", "pct_chg", "prev_close", "open",
                  "high", "low", "volume_hand", "amount_wan", "turnover_rate", "pe_ttm",
                  "circ_mv", "total_mv", "avg_price", "quote_time"),
    "daily_bars": ("symbol", "date", "open", "high", "low", "close", "volume", "amount"),
    "hour_bars": ("symbol", "date", "open", "high", "low", "close", "volume"),
    "ticks": ("symbol", "date", "price", "pct_chg", "volume_hand", "amount_wan"),
    "vol_profile": ("symbol", "hm", "avg_vol"),
    "signals": ("symbol", "rule", "ts", "message", "id"),
    "macro_indicators": ("id", "name", "unit", "latest", "prev", "series"),
}


def connect():
    Path(DB_PATH).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = connect()
    for ddl in SCHEMA.values():
        conn.execute(ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_sig_ts ON signals(ts)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_snap_d ON snapshots(d)")
    conn.commit()
    conn.close()


def now_ms():
    return int(time.time() * 1000)


def _num(v):
    if v is None:
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _int(v):
    if v is None:
        return None
    try:
        return int(v)
    except (ValueError, TypeError):
        return None


def bj_day():
    return bj_now().strftime("%Y%m%d")


# ---------- ingest 写库 ----------
def ingest_docs(collection: str, docs: list) -> tuple:
    table = collection
    conn = connect()
    up = 0
    failed = []
    try:
        for doc in docs:
            if not isinstance(doc, dict):
                failed.append({"doc": str(doc)[:50], "err": "not object"})
                continue
            key = KEY[table]
            if not all(doc.get(k) is not None for k in key):
                failed.append({"doc": doc.get("symbol") or doc.get("id"), "err": "missing key"})
                continue
            colset = {}
            if table == "snapshots":
                colset["d"] = bj_day()
            for k in key:
                v = doc.get(k)
                if k in INTCOLS.get(table, ()) or table == "signals" and k == "ts":
                    colset[k] = _int(v)
                else:
                    colset[k] = v
            if table == "signals":
                # doc_id 镜像 ingest 接口的 _id 语义（doc._id || symbol_rule_ts），供按 id 删除
                colset["doc_id"] = doc.get("_id") or doc.get("doc_id") or \
                    f"{doc.get('symbol')}_{doc.get('rule')}_{doc.get('ts')}"
            for c in COLS[table]:
                if c not in doc:
                    continue
                v = doc[c]
                if c in NUMCOLS.get(table, ()):
                    colset[c] = _num(v)
                elif c in INTCOLS.get(table, ()):
                    colset[c] = _int(v)
                elif c == "series":
                    colset[c] = v if isinstance(v, str) else json.dumps(v, ensure_ascii=False)
                else:
                    colset[c] = v
            cols = list(colset.keys())
            conn.execute(
                f"INSERT OR REPLACE INTO {table} ({','.join(cols)}) "
                f"VALUES ({','.join('?' for _ in cols)})",
                [colset[c] for c in cols])
            up += 1
        conn.commit()
    except Exception as e:  # noqa: BLE001
        conn.rollback()
        failed.append({"err": str(e)[:200]})
    finally:
        conn.close()
    return up, failed


def ingest_delete(collection: str, ids: list) -> int:
    """按 ingest _id 语义删除（幂等；供维护操作，当前无定时调用方）。"""
    table = collection
    conn = connect()
    deleted = 0
    try:
        for raw in ids:
            sid = str(raw)
            if table in ("daily_bars", "hour_bars", "ticks") and "_" in sid:
                sym, _, dt = sid.rpartition("_")
                deleted += conn.execute(
                    f"DELETE FROM {table} WHERE symbol=? AND date=?", (sym, dt)).rowcount
            elif table in ("stocks", "snapshots"):
                deleted += conn.execute(f"DELETE FROM {table} WHERE symbol=?", (sid,)).rowcount
            elif table == "vol_profile" and "_" in sid:
                sym, _, hm = sid.rpartition("_")
                deleted += conn.execute(
                    "DELETE FROM vol_profile WHERE symbol=? AND hm=?", (sym, hm)).rowcount
            elif table == "macro_indicators":
                deleted += conn.execute("DELETE FROM macro_indicators WHERE id=?", (sid,)).rowcount
            elif table == "signals":
                deleted += conn.execute("DELETE FROM signals WHERE doc_id=?", (sid,)).rowcount
        conn.commit()
    finally:
        conn.close()
    return deleted


# ---------- 转发旧云端网关（过渡期双活；失败重试 3 次并记日志） ----------
_forward_pool = ThreadPoolExecutor(max_workers=2)
_forward_log = REPO / "data" / "forward.log"


def _log_forward(msg: str):
    try:
        REPO.joinpath("data").mkdir(parents=True, exist_ok=True)
        with open(_forward_log, "a") as f:
            f.write(f"[{bj_now():%Y-%m-%d %H:%M:%S}] {msg}\n")
    except Exception:
        pass


def forward(payload: dict):
    if not FORWARD_URL:
        return
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    for attempt in range(3):
        try:
            req = urllib.request.Request(FORWARD_URL, data=body, method="POST",
                                         headers={"Content-Type": "application/json",
                                                  "x-sync-token": FORWARD_TOKEN})
            with urllib.request.urlopen(req, timeout=30) as r:
                resp = json.loads(r.read().decode("utf-8") or "{}")
            if resp.get("ok"):
                return
            _log_forward(f"转发被拒({attempt + 1}): {payload.get('collection')} {json.dumps(resp)[:200]}")
        except Exception as e:  # noqa: BLE001
            _log_forward(f"转发失败({attempt + 1}): {payload.get('collection')} {str(e)[:200]}")
        time.sleep(1 + attempt * 2)


# ---------- AI 消息队列（与历史云端 DB 网关同语义） ----------
QCOLS = {
    "ai_requests": ("mode", "question", "session_id", "status", "model", "skill",
                    "target_id", "created_at", "updated_at"),
    "ai_replies": ("text", "thinking", "done", "created_at", "updated_at"),
}


def unwrap(v):
    """历史网关的文档字段可能带 {$date:{$numberLong:ms}} 包装；普通值原样返回。"""
    if isinstance(v, dict) and "$date" in v:
        d = v["$date"]
        if isinstance(d, dict) and "$numberLong" in d:
            return _int(d["$numberLong"])
        return d
    return v


def q_upsert(tbl: str, doc: dict):
    """插入/更新一条队列文档，返回 (id, done)。"""
    if tbl == "ai_requests":
        rid = str(doc.get("id") or doc.get("_id") or os.urandom(16).hex())
        fields = {k: unwrap(doc.get(k)) for k in QCOLS[tbl] if k in doc}
        conn = connect()
        try:
            if conn.execute("SELECT 1 FROM ai_requests WHERE id=?", (rid,)).fetchone():
                sets = {k: v for k, v in fields.items() if k not in ("created_at",)}
                if sets:
                    conn.execute("UPDATE ai_requests SET " + ",".join(f"{k}=?" for k in sets) +
                                 ", updated_at=? WHERE id=?", [*sets.values(), now_ms(), rid])
            else:
                cols = ["id"] + [c for c in QCOLS[tbl]]
                vals = [rid] + [fields.get(c) for c in QCOLS[tbl]]
                conn.execute(f"INSERT INTO ai_requests ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                             vals)
            conn.commit()
        finally:
            conn.close()
        return rid, False
    # ai_replies：request_id 为主键（worker 按 request_id 查/更，等价网关单条回复）
    req_id = str(unwrap(doc.get("request_id")) or unwrap(doc.get("id")) or "")
    if not req_id:
        return "", False
    text = unwrap(doc.get("text"))
    thinking = unwrap(doc.get("thinking"))
    done = 1 if unwrap(doc.get("done")) else 0
    created = _int(unwrap(doc.get("created_at"))) or now_ms()
    conn = connect()
    try:
        if conn.execute("SELECT 1 FROM ai_replies WHERE request_id=?", (req_id,)).fetchone():
            sets = {}
            if text is not None:
                sets["text"] = text
            if thinking is not None:
                sets["thinking"] = thinking
            if "done" in doc:
                sets["done"] = done
            if sets:
                conn.execute("UPDATE ai_replies SET " + ",".join(f"{k}=?" for k in sets) +
                             ", updated_at=? WHERE request_id=?", [*sets.values(), now_ms(), req_id])
        else:
            conn.execute(
                "INSERT INTO ai_replies (request_id,text,thinking,done,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (req_id, text or "", thinking or "", done, created, now_ms()))
        conn.commit()
    finally:
        conn.close()
    return req_id, bool(done)


def q_list(tbl: str, query: dict, order: list, limit):
    cond, args = "", []
    for k, v in (query or {}).items():
        col = "id" if tbl == "ai_requests" and k == "_id" else \
              "request_id" if tbl == "ai_replies" and k == "_id" else k
        if col not in QCOLS[tbl] and col not in ("id", "request_id"):
            continue
        cond += (" AND " if cond else " WHERE ") + f"{col}=?"
        args.append(v)
    sql = f"SELECT * FROM {tbl}{cond}"
    if order:
        for o in order:
            f, dr = o.get("field"), "DESC" if str(o.get("direction", "")).lower() == "desc" else "ASC"
            if f in QCOLS[tbl] or f in ("id", "request_id"):
                sql += f" ORDER BY {f} {dr}"
                break
    elif tbl == "ai_requests":
        sql += " ORDER BY created_at ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"
    conn = connect()
    try:
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if tbl == "ai_requests":
            d = {k: r[k] for k in QCOLS[tbl]}
            d["_id"] = r["id"]
        else:
            d = {"text": r["text"], "thinking": r["thinking"], "done": bool(r["done"]),
                 "created_at": r["created_at"], "updated_at": r["updated_at"]}
            d["_id"] = r["request_id"]
        out.append(d)
    return out


# ---------- /api 只读（复刻 cloud/functions/api/index.js） ----------
def db_rows(table: str, sql: str, args=()):
    conn = connect()
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def doc_of(table: str, row) -> dict:
    out = {}
    for c in API_COLS[table]:
        if c == "series":
            v = row["series"]
            try:
                v = json.loads(v) if isinstance(v, str) else (v or [])
            except Exception:
                v = []
            out[c] = v
        elif c in row.keys():
            out[c] = row[c]
    return out


def api_quote(q):
    symbols = [s.strip() for s in str(q.get("symbols") or "").split(",") if s.strip()]
    sql = ("SELECT s.* FROM snapshots s JOIN "
           "(SELECT symbol, MAX(d) md FROM snapshots GROUP BY symbol) m "
           "ON s.symbol = m.symbol AND s.d = m.md")
    args = []
    if symbols:
        sql += " WHERE s.symbol IN (%s)" % ",".join("?" * len(symbols))
        args = symbols
    sql += " ORDER BY s.symbol LIMIT 200"
    return [doc_of("snapshots", r) for r in db_rows("snapshots", sql, args)]


def api_series(table: str, q, default_limit):
    symbol = str(q.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol required")
    limit = min(max(int(q.get("limit") or default_limit), 1), 2000)
    rows = db_rows(table, "SELECT * FROM %s WHERE symbol=? ORDER BY date DESC LIMIT ?" % table,
                   (symbol, limit))
    return [doc_of(table, r) for r in reversed(rows)]


def api_stocks(q):
    t = str(q.get("type") or "").strip()
    sql = "SELECT * FROM stocks"
    args = []
    if t in ("stock", "index", "industry"):
        sql += " WHERE type=?"
        args = [t]
    sql += " ORDER BY symbol LIMIT 500"
    return [doc_of("stocks", r) for r in db_rows("stocks", sql, args)]


def api_signals(q):
    limit = min(max(int(q.get("limit") or 50), 1), 500)
    symbol = str(q.get("symbol") or "").strip()
    sql = "SELECT * FROM signals"
    args = []
    if symbol:
        sql += " WHERE symbol=?"
        args = [symbol]
    sql += " ORDER BY ts DESC LIMIT ?"
    args.append(limit)
    return [doc_of("signals", r) for r in db_rows("signals", sql, args)]


def api_stats():
    out = {}
    for t in ("daily_bars", "hour_bars", "ticks", "stocks", "snapshots",
              "signals", "vol_profile", "macro_indicators"):
        try:
            out[t] = db_rows(t, f"SELECT COUNT(*) c FROM {t}")[0]["c"]
        except Exception:
            out[t] = -1
    return out


def api_tick(q):
    symbol = str(q.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol required")
    day = str(q.get("date") or "").strip() or bj_day()
    rows = db_rows("ticks",
                   "SELECT * FROM ticks WHERE symbol=? AND date>=? AND date<=? "
                   "ORDER BY date ASC LIMIT 400", (symbol, day + "0000", day + "2359"))
    used_day = day
    if not rows:
        latest = db_rows("ticks",
                         "SELECT * FROM ticks WHERE symbol=? ORDER BY date DESC LIMIT 400", (symbol,))
        if latest:
            mx = latest[0]["date"][:8]
            rows = [r for r in reversed(latest) if r["date"][:8] == mx]
            used_day = mx
    return [doc_of("ticks", r) for r in rows], used_day


def api_profile(q):
    symbol = str(q.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol required")
    rows = db_rows("vol_profile", "SELECT * FROM vol_profile WHERE symbol=? "
                   "ORDER BY hm ASC LIMIT 100", (symbol,))
    return [doc_of("vol_profile", r) for r in rows]


def api_macro(q):
    iid = str(q.get("id") or "").strip()
    rows = db_rows("macro_indicators", "SELECT * FROM macro_indicators ORDER BY id LIMIT 100")
    out = []
    for r in rows:
        d = doc_of("macro_indicators", r)
        if not iid:
            d["series"] = d["series"][-140:]
        out.append(d)
    if iid:
        out = [d for d in out if d.get("id") == iid]
    return out


def _latest_dated(directory: Path, prefix: str):
    """目录里 prefix_YYYYMMDD.csv 的最新一天；返回 (YYYYMMDD, Path) 或 (None, None)。"""
    best = ""
    try:
        for p in directory.iterdir():
            n = p.name
            if n.startswith(prefix + "_") and n.endswith(".csv"):
                d = n[len(prefix) + 1:-4]
                if len(d) == 8 and d.isdigit() and d > best:
                    best = d
    except FileNotFoundError:
        return None, None
    return (best, directory / f"{prefix}_{best}.csv") if best else (None, None)


def api_market(q):
    """大盘温度：全市场快照涨跌家数/中位涨幅 + 涨停/炸板/跌停池聚合。"""
    day, path = _latest_dated(MARKETDATA_DIR / "snapshot", "market")
    if not path:
        return {"date": None}
    up = down = flat = 0
    pcts = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                p = float(r.get("pct"))
            except (TypeError, ValueError):
                continue
            pcts.append(p)
            if p > 0:
                up += 1
            elif p < 0:
                down += 1
            else:
                flat += 1
    med = statistics.median(pcts) if pcts else None
    limit_up = max_lbc = zha_ban = 0
    zt = MARKETDATA_DIR / "ztpool" / f"ztpool_{day}.csv"
    if zt.exists():
        with open(zt, encoding="utf-8-sig") as f:
            for r in csv.DictReader(f):
                limit_up += 1
                try:
                    max_lbc = max(max_lbc, int(float(r.get("lbc") or 0)))
                except ValueError:
                    pass
    zb = MARKETDATA_DIR / "ztpool" / f"zbpool_{day}.csv"
    if zb.exists():
        with open(zb, encoding="utf-8-sig") as f:
            zha_ban = sum(1 for _ in csv.DictReader(f))
    dt = MARKETDATA_DIR / "ztpool" / f"dtpool_{day}.csv"
    limit_down = None
    if dt.exists():
        with open(dt, encoding="utf-8-sig") as f:
            limit_down = sum(1 for _ in csv.DictReader(f))
    return {
        "date": day, "up": up, "down": down, "flat": flat,
        "median_pct": round(med, 2) if med is not None else None,
        "limit_up": limit_up, "max_lbc": max_lbc,
        "limit_down": limit_down, "zha_ban": zha_ban,
        "zha_ban_rate": round(zha_ban / (zha_ban + limit_up) * 100, 1) if (zha_ban + limit_up) else None,
    }


VALSNAP_FIELDS = ("pb", "vol_ratio", "main_net", "main_ratio", "pe_dynamic", "pe_static", "pe_ttm")


def api_valsnap(q):
    """个股收盘估值/资金快照（valuation_snapshot CSV，字段含 pb/量比/主力净流入）。"""
    symbol = str(q.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol required")
    day, path = _latest_dated(VALSNAP_DIR, "snap")
    if not path:
        return {}
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            if (r.get("code") or "").strip() != symbol:
                continue
            out = {"date": day}
            for k in VALSNAP_FIELDS:
                try:
                    out[k] = float(r.get(k))
                except (TypeError, ValueError):
                    out[k] = None
            return out
    return {"date": day}


# /api/valuation 可选指标（对应 valuation CSV 列）
VAL_METRICS = ("pe_ttm", "pe_static", "pb", "ps_ttm", "pcf_ocf_ttm", "peg",
               "div_yield", "total_mv", "circ_mv")


def api_valuation(q):
    """个股估值历史（valuation CSV，2018 起）：全量算分位，抽稀 ≤600 点下发。"""
    symbol = str(q.get("symbol") or "").strip()
    metric = str(q.get("metric") or "pe_ttm").strip()
    if not symbol:
        raise ValueError("symbol required")
    if not re.fullmatch(r"\d{6}|(?:sh|sz|bj)\d{6}|801\d{3}", symbol):
        raise ValueError("bad symbol")
    if metric not in VAL_METRICS:
        raise ValueError(f"metric must be one of {VAL_METRICS}")
    path = VALUATION_DIR / f"{symbol}.csv"
    if not path.exists():
        return {}
    rows = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            try:
                rows.append((r["date"], float(r[metric])))
            except (KeyError, TypeError, ValueError):
                continue
    rows.sort(key=lambda x: x[0])
    if not rows:
        return {}
    vals = [v for _, v in rows]
    latest_v = vals[-1]
    stats = {
        "count": len(vals),
        "latest": latest_v,
        "pctile": round(sum(1 for v in vals if v < latest_v) / len(vals) * 100, 1),
        "min": round(min(vals), 4),
        "max": round(max(vals), 4),
        "median": round(statistics.median(vals), 4),
    }
    pts = rows
    if len(pts) > 600:  # 均匀抽稀且保留最新点
        stride = math.ceil(len(pts) / 600)
        pts = pts[::stride]
        if pts[-1] != rows[-1]:
            pts.append(rows[-1])
    return {"symbol": symbol, "metric": metric, "start": rows[0][0],
            "updated": rows[-1][0], "points": pts, "stats": stats}


def api_fundflow(q):
    """个股主力资金流历史（同花顺口径，约 10 个月逐日；金额单位元）。"""
    symbol = str(q.get("symbol") or "").strip()
    if not symbol:
        raise ValueError("symbol required")
    if not re.fullmatch(r"\d{6}|(?:sh|sz|bj)\d{6}|801\d{3}", symbol):
        raise ValueError("bad symbol")
    path = FUNDFLOW_DIR / f"{symbol}.csv"
    if not path.exists():
        return {}
    out = []
    with open(path, encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            row = {"date": r.get("date")}
            for k in ("main_net", "main_ratio", "super_net", "large_net",
                      "medium_net", "small_net", "close"):
                try:
                    row[k] = float(r.get(k))
                except (TypeError, ValueError):
                    row[k] = None
            out.append(row)
    return {"symbol": symbol, "count": len(out), "rows": out} if out else {}


def api_etf(q):
    """/api/etf：ETF 精选目录 + 腾讯实时行情（合并返回）。"""
    return etf_data.etf_list(q)


def api_etfdaily(q):
    """/api/etfdaily?symbol=sh510300&limit=250：ETF 日线（本地 tdx + 腾讯增量）。"""
    return etf_data.etf_daily(q)


# ---------- HTTP 服务 ----------
CACHE_TTL = {"quote": 30, "tick": 30, "signals": 15, "market": 600, "valsnap": 600,
             "valuation": 600, "fundflow": 600, "etf": 60, "etfdaily": 300}
CACHE_DEFAULT = 300
_cache = {}
_cache_lock = threading.Lock()


def cached(key: str, ttl: int, fn):
    now = time.time()
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < ttl:
            return hit[1]
    out = fn()
    with _cache_lock:
        if len(_cache) > 200:
            for k in [k for k, (t, _) in _cache.items() if now - t > 600]:
                _cache.pop(k, None)
        _cache[key] = (now, out)
    return out


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "StockDeskDataService/1.0"

    def log_message(self, fmt, *args):
        print(f"[{bj_now():%H:%M:%S}] {self.address_string()} {fmt % args}")

    # ---- 基础 ----
    def _send(self, code, obj, ctype="application/json; charset=utf-8"):
        if isinstance(obj, str):
            body = obj.encode("utf-8")
        else:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET,POST,PATCH,DELETE,OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type,x-sync-token,Authorization")
        self.end_headers()
        try:
            self.wfile.write(body)
        except BrokenPipeError:
            pass

    def _read_body(self):
        ln = int(self.headers.get("Content-Length") or 0)
        if ln <= 0:
            return {}
        raw = self.rfile.read(ln).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    def _host_ok(self):
        h = (self.headers.get("Host") or "").split(":")[0].lower()
        return h in ALLOWED_COL_HOSTS or h.startswith("127.0.0.1")

    # ---- 分发 ----
    def do_OPTIONS(self):
        self._send(204, "")

    def do_GET(self):
        self._handle()

    def do_POST(self):
        self._handle()

    def do_PATCH(self):
        self._handle()

    def do_DELETE(self):
        self._handle()

    def _handle(self):
        try:
            return self._dispatch()
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            try:
                self._send(500, {"error": f"internal: {str(e)[:200]}"})
            except Exception:
                pass

    def _dispatch(self):
        method = self.command
        path = self.path.split("?", 1)[0]
        qs = self.path.split("?", 1)[1] if "?" in self.path else ""
        q = ({k: v[0] for k, v in urllib.parse.parse_qs(qs, keep_blank_values=True).items()}
             if qs else {})

        # ---- ingest（115 推送入口） ----
        if path.rstrip("/") == "/ingest":
            if method == "GET":
                return self._send(200, {"ok": True, "service": "ingest", "ts": now_ms()})
            if method != "POST":
                return self._send(405, {"error": "method not allowed"})
            token = self.headers.get("x-sync-token") or q.get("token") or ""
            if not INGEST_TOKEN or token != INGEST_TOKEN:
                return self._send(401, {"error": "unauthorized"})
            body = self._read_body()
            collection = str(body.get("collection") or "")
            cname = collection[9:] if collection.startswith("signals_") else collection
            if cname not in ALLOWED:
                return self._send(400, {"error": f"collection must be one of {sorted(ALLOWED)}"
                                        "（signals_<rule> 映射到 signals）"})
            if body.get("action") == "delete":
                ids = body.get("ids") or []
                if not ids:
                    return self._send(400, {"error": "ids required"})
                deleted = ingest_delete(cname, ids)
                if FORWARD_URL:
                    _forward_pool.submit(forward, {"collection": collection, "action": "delete", "ids": ids})
                return self._send(200, {"ok": True, "deleted": deleted})
            docs = body.get("docs")
            if not isinstance(docs, list) or not docs:
                return self._send(200, {"ok": True, "upserted": 0})
            up, failed = ingest_docs(cname, docs)
            if FORWARD_URL:
                _forward_pool.submit(forward, {"collection": collection, "docs": docs})
            return self._send(200, {"ok": True, "upserted": up, "failed": failed[:20]})

        # ---- AI 消息队列 ----
        m = re.match(r"^/collections/(ai_requests|ai_replies)/documents(?:/([^/]+))?$", path)
        if m:
            if not self._host_ok():
                return self._send(403, {"error": "forbidden host"})
            tbl, oid = m.group(1), m.group(2)
            if method == "GET" and not oid:
                try:
                    query = json.loads(q.get("query") or "{}")
                    order = json.loads(q.get("order") or "[]")
                except Exception:
                    query, order = {}, []
                limit = q.get("limit")
                return self._send(200, {"list": q_list(tbl, query, order, limit)})
            if method == "POST" and not oid:
                body = self._read_body()
                docs = body.get("data") or body.get("docs") or []
                ids = [rid for d in docs for rid, _ in [q_upsert(tbl, d)] if rid]
                return self._send(200, {"insertedIds": ids})
            if method == "PATCH" and oid:
                body = self._read_body()
                setv = (body.get("data") or {}).get("$set") or body.get("$set") or {}
                self._q_patch(tbl, oid, setv)
                return self._send(200, {"ok": True, "updated": 1})
            if method == "DELETE" and oid:
                conn = connect()
                try:
                    key = "id" if tbl == "ai_requests" else "request_id"
                    conn.execute(f"DELETE FROM {tbl} WHERE {key}=?", (oid,))
                    conn.commit()
                finally:
                    conn.close()
                return self._send(200, {"ok": True, "deleted": 1})
            return self._send(405, {"error": "method not allowed"})

        # ---- api 只读 ----
        if path == "/api" or path.startswith("/api/"):
            return self._api(method, path, q)
        return self._send(404, {"error": f"not found (path={path})"})

    def _q_patch(self, tbl: str, oid: str, setv: dict):
        conn = connect()
        try:
            sets = {k: unwrap(v) for k, v in (setv or {}).items() if k in QCOLS[tbl]}
            if not sets:
                return
            if tbl == "ai_requests":
                conn.execute("UPDATE ai_requests SET " + ",".join(f"{k}=?" for k in sets) +
                             ", updated_at=? WHERE id=?", [*sets.values(), now_ms(), oid])
            else:
                if "done" in sets:
                    sets["done"] = 1 if sets["done"] else 0
                conn.execute("UPDATE ai_replies SET " + ",".join(f"{k}=?" for k in sets) +
                             ", updated_at=? WHERE request_id=?", [*sets.values(), now_ms(), oid])
            conn.commit()
        finally:
            conn.close()

    def _api(self, method: str, path: str, q: dict):
        if method != "GET":
            return self._send(405, {"error": "method not allowed"})
        route = path[len("/api"):].rstrip("/") or "/health"
        routes = ["/health", "/quote", "/daily", "/stocks", "/signals", "/stats",
                  "/hour", "/tick", "/profile", "/macro", "/market", "/valsnap",
                  "/valuation", "/fundflow", "/etf", "/etfdaily"]
        hit = next((r for r in routes if route.endswith(r)), None)
        if not hit:
            return self._send(404, {"error": f"not found (path={path})"})
        ttl = CACHE_TTL.get(hit.lstrip("/"), CACHE_DEFAULT)
        key = path + (("?" + urllib.parse.urlencode(q, doseq=True)) if q else "")
        try:
            data = cached(key, ttl, lambda: self._route_fn(hit, q))
        except ValueError as e:
            return self._send(400, {"error": str(e)})
        return self._send(200, data)

    def _route_fn(self, hit: str, q: dict):
        ts = now_ms()
        if hit == "/health":
            return {"ok": True, "ts": ts}
        if hit == "/quote":
            return {"data": api_quote(q), "ts": ts}
        if hit == "/daily":
            return {"data": api_series("daily_bars", q, 120), "ts": ts}
        if hit == "/hour":
            return {"data": api_series("hour_bars", q, 120), "ts": ts}
        if hit == "/stocks":
            return {"data": api_stocks(q), "ts": ts}
        if hit == "/signals":
            return {"data": api_signals(q), "ts": ts}
        if hit == "/stats":
            return {"data": api_stats(), "ts": ts}
        if hit == "/tick":
            rows, day = api_tick(q)
            return {"data": rows, "day": day, "ts": ts}
        if hit == "/profile":
            return {"data": api_profile(q), "ts": ts}
        if hit == "/macro":
            return {"data": api_macro(q), "ts": ts}
        if hit == "/market":
            return {"data": api_market(q), "ts": ts}
        if hit == "/valsnap":
            return {"data": api_valsnap(q), "ts": ts}
        if hit == "/valuation":
            return {"data": api_valuation(q), "ts": ts}
        if hit == "/fundflow":
            return {"data": api_fundflow(q), "ts": ts}
        if hit == "/etf":
            return {"data": api_etf(q), "ts": ts}
        if hit == "/etfdaily":
            return {"data": api_etfdaily(q), "ts": ts}
        raise ValueError("unknown route")


if __name__ == "__main__":
    init_db()
    print(f"StockDesk 本地数据服务启动 port={PORT} db={DB_PATH} "
          f"ingest_token={'set' if INGEST_TOKEN else 'MISSING'} "
          f"forward={'on -> ' + FORWARD_URL if FORWARD_URL else 'off'}")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
