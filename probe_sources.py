#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""probe_sources.py — 数据源探针：小样本逐请求计时 + 成功率 + 全量外推
用法: python3 probe_sources.py   (结果写 results/probe_sources.json + stdout 摘要)
"""
import json
import os
import random
import re
import statistics
import time
import traceback
from datetime import datetime

import requests

try:
    import akshare as ak
    AK_VERSION = ak.__version__
except Exception as e:  # pragma: no cover
    ak = None
    AK_VERSION = f"IMPORT_FAIL: {e}"

import pandas as pd

BASE = os.path.dirname(os.path.abspath(__file__))
EM_DIR = "/home/ubuntu/data/eastmoney_data"
RESULT_JSON = os.path.join(BASE, "results", "probe_sources.json")

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
HEADERS = {"User-Agent": UA, "Referer": "https://quote.eastmoney.com/"}
TIMEOUT = 25

RESULTS = {"started_at": datetime.now().isoformat(), "akshare_version": AK_VERSION, "sources": {}}


def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def suffix_of(code: str) -> str:
    if code.startswith(("92", "43", "83", "87", "88")) or (code.startswith("4") and len(code) == 6):
        return "BJ"
    if code.startswith("6"):
        return "SH"
    return "SZ"


def load_universe():
    """从东财日线目录拿全A代码清单（文件名 {code}_{name}.csv）"""
    codes = []
    for fn in sorted(os.listdir(EM_DIR)):
        m = re.match(r"^(\d{6})_", fn)
        if m:
            codes.append(m.group(1))
    return codes


def stratified_sample(codes, n=20, seed=42):
    """按板块分层抽样：60x/00x/30x/68x/北交 各尽量均分"""
    groups = {"60": [], "00": [], "30": [], "68": [], "BJ": []}
    for c in codes:
        if c.startswith("6"):
            g = "68" if c.startswith("688") or c.startswith("689") else "60"
        elif c.startswith(("43", "83", "87", "88", "92")) or (c.startswith("4") and len(c) == 6):
            g = "BJ"
        elif c.startswith("0"):
            g = "00"
        elif c.startswith("3"):
            g = "30"
        else:
            g = "00"
        groups[g].append(c)
    rnd = random.Random(seed)
    sample = []
    per = max(1, n // len(groups))
    for g, lst in groups.items():
        if lst:
            sample += rnd.sample(lst, min(per, len(lst)))
    return sample


def timed_call(fn, *args, **kwargs):
    t0 = time.time()
    try:
        df = fn(*args, **kwargs)
        dt = time.time() - t0
        rows = 0 if df is None else len(df)
        return dt, rows > 0, rows, None
    except Exception as e:
        return time.time() - t0, False, 0, f"{type(e).__name__}: {str(e)[:180]}"


def summarize(times, oks, errs, label, full_n, workers_note=1.0):
    ok_times = [t for t, o in zip(times, oks) if o]
    rec = {
        "label": label,
        "n_probe": len(times),
        "n_ok": sum(oks),
        "success_rate": round(sum(oks) / len(times), 3) if times else 0.0,
        "avg_sec": round(statistics.mean(ok_times), 3) if ok_times else None,
        "p50_sec": round(statistics.median(ok_times), 3) if ok_times else None,
        "max_sec": round(max(ok_times), 3) if ok_times else None,
        "errors_sample": errs[:5],
    }
    if rec["avg_sec"] and rec["success_rate"] >= 0.95:
        rec["full_serial_min"] = round(full_n * rec["avg_sec"] / 60, 1)
        rec["full_parallel_min_x2"] = round(full_n * rec["avg_sec"] / 60 / 1.8, 1)
    return rec


def probe_valuation_history(codes, sample):
    """主源: ak.stock_value_em 逐股全历史估值; 同时裸测 datacenter RPT_VALUEANALYSIS_DET"""
    out = {"akshare": [], "raw": []}
    errs, oks, times = [], [], []
    for code in sample:
        dt, ok, rows, err = timed_call(ak.stock_value_em, symbol=code)
        times.append(dt); oks.append(ok)
        if err:
            errs.append(f"{code}: {err}")
        out["akshare"].append({"code": code, "sec": round(dt, 2), "ok": ok, "rows": rows})
        log(f"  stock_value_em {code}: {dt:.2f}s ok={ok} rows={rows}")
    rec = summarize(times, oks, errs, "历史估值-akshare stock_value_em", full_n=len(codes))
    rec["probe_detail"] = out["akshare"]
    # 看看返回的列名，确认字段
    try:
        df = ak.stock_value_em(symbol=sample[0])
        rec["columns"] = list(df.columns)
        rec["date_min"] = str(df["数据日期"].min()) if "数据日期" in df.columns and len(df) else None
    except Exception as e:
        rec["columns"] = f"ERR {e}"
    RESULTS["sources"]["valuation_history_akshare"] = rec

    # 裸测 datacenter-web（作为 akshare 失效时的直连备选）
    times, oks, errs = [], [], []
    for code in sample[:6]:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
               f"?reportName=RPT_VALUEANALYSIS_DET&columns=ALL&pageSize=5&pageNumber=1"
               f'&filter=(SECUCODE%3D%22{code}.{suffix_of(code)}%22)'
               f"&sortColumns=TRADE_DATE&sortTypes=-1&source=HSF10&client=PC")
        t0 = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            j = r.json()
            ok = (j.get("code") == 0 or j.get("result") is not None) and r.status_code == 200
            rows = len((j.get("result") or {}).get("data") or [])
            if not ok:
                errs.append(f"{code}: http={r.status_code} body={r.text[:120]}")
        except Exception as e:
            ok, rows = False, 0
            errs.append(f"{code}: {type(e).__name__} {str(e)[:120]}")
        times.append(time.time() - t0); oks.append(ok)
    RESULTS["sources"]["valuation_history_raw_dc"] = summarize(times, oks, errs,
                                                               "历史估值-直连RPT_VALUEANALYSIS_DET", len(codes))


def probe_fhps():
    """分红送配: ak.stock_fhps_em 按报告期"""
    times, oks, errs = [], [], []
    for d in ["20231231", "20250630", "20260630"]:
        dt, ok, rows, err = timed_call(ak.stock_fhps_em, date=d)
        times.append(dt); oks.append(ok)
        if err:
            errs.append(f"{d}: {err}")
        log(f"  stock_fhps_em {d}: {dt:.2f}s ok={ok} rows={rows}")
    rec = summarize(times, oks, errs, "分红送配-akshare stock_fhps_em 按报告期", full_n=120)
    RESULTS["sources"]["fhps"] = rec


def probe_snapshot_push2delay():
    """估值快照: push2delay clist 验证估值字段"""
    fields = "f12,f14,f2,f3,f5,f6,f8,f9,f23,f114,f115,f116,f117,f20,f21,f62"
    times, oks, errs = [], [], []
    nonnull = {}
    for pn in [1, 2]:
        url = ("https://push2delay.eastmoney.com/api/qt/clist/get"
               f"?pn={pn}&pz=100&po=1&np=1&fltt=2&invt=2&fid=f3"
               "&fs=m:0+t:6,m:0+t:80,m:1+t:2,m:1+t:23,m:0+t:81+s:2048"
               f"&fields={fields}")
        t0 = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            j = r.json()
            data = (j.get("data") or {})
            diff = data.get("diff") or []
            ok = r.status_code == 200 and len(diff) > 0
            for k in ["f9", "f23", "f114", "f115", "f116", "f117", "f20", "f21"]:
                cnt = sum(1 for d in diff if d.get(k) not in (None, "-", ""))
                nonnull.setdefault(k, []).append(cnt)
            if not ok:
                errs.append(f"pn={pn}: http={r.status_code} body={r.text[:120]}")
        except Exception as e:
            ok = False
            errs.append(f"pn={pn}: {type(e).__name__} {str(e)[:120]}")
        times.append(time.time() - t0); oks.append(ok)
        log(f"  push2delay clist pn={pn}: {times[-1]:.2f}s ok={ok}")
    rec = summarize(times, oks, errs, "估值快照-push2delay clist", full_n=60)
    rec["nonnull_per_100"] = {k: min(v) for k, v in nonnull.items()}
    RESULTS["sources"]["snapshot_push2delay"] = rec


def probe_finance_main(sample):
    """财务主指标: datacenter RPT_F10_FINANCE_MAINFINADATA"""
    times, oks, errs = [], [], []
    cols = None
    for code in sample[:20]:
        url = ("https://datacenter-web.eastmoney.com/api/data/v1/get"
               "?reportName=RPT_F10_FINANCE_MAINFINADATA&columns=ALL&pageSize=10&pageNumber=1"
               f'&filter=(SECUCODE%3D%22{code}.{suffix_of(code)}%22)'
               "&sortColumns=REPORT_DATE&sortTypes=-1&source=HSF10&client=PC")
        t0 = time.time()
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
            j = r.json()
            rows = (j.get("result") or {}).get("data") or []
            ok = r.status_code == 200 and len(rows) > 0
            if rows and cols is None:
                cols = list(rows[0].keys())[:40]
            if not ok:
                errs.append(f"{code}: http={r.status_code} body={r.text[:120]}")
        except Exception as e:
            ok = False
            errs.append(f"{code}: {type(e).__name__} {str(e)[:120]}")
        times.append(time.time() - t0); oks.append(ok)
    rec = summarize(times, oks, errs, "财务主指标-直连RPT_F10_FINANCE_MAINFINADATA", full_n=len(sample) and 5552)
    rec["columns_sample"] = cols
    RESULTS["sources"]["finance_main"] = rec
    log(f"  finance_main: 成功率 {rec['success_rate']}, p50 {rec['p50_sec']}s")


def probe_kline_sources(codes):
    """日线增量: push2his 主源 + 新浪备选"""
    # push2his
    url = ("https://push2his.eastmoney.com/api/qt/stock/kline/get?secid=1.600000"
           "&klt=101&fqt=1&fields1=f1,f2,f3&fields2=f51,f52,f53,f54,f55,f56,f57"
           "&beg=20260820&end=20500101")
    t0 = time.time()
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        j = r.json()
        kl = ((j.get("data") or {}).get("klines")) or []
        ok = r.status_code == 200 and len(kl) > 0
        body = f"rows={len(kl)} last={kl[-1][:40] if kl else '-'}"
    except Exception as e:
        ok, body = False, f"{type(e).__name__} {str(e)[:120]}"
    RESULTS["sources"]["kline_push2his"] = {"label": "日线增量-push2his", "ok": ok,
                                            "sec": round(time.time() - t0, 2), "detail": body}
    # 新浪
    t0 = time.time()
    try:
        u2 = ("https://quotes.sina.cn/cn/api/jsonp_v2.php/var%20_x=/CN_MarketDataService.getKLineData"
              "?symbol=sh600000&scale=240&ma=no&datalen=5")
        r = requests.get(u2, headers={"User-Agent": UA, "Referer": "https://finance.sina.com.cn/"}, timeout=TIMEOUT)
        ok = r.status_code == 200 and ("close" in r.text)
        body = r.text[:120]
    except Exception as e:
        ok, body = False, f"{type(e).__name__} {str(e)[:120]}"
    RESULTS["sources"]["kline_sina"] = {"label": "日线增量-新浪", "ok": ok,
                                        "sec": round(time.time() - t0, 2), "detail": body}
    # 腾讯批量行情（快照备选）
    t0 = time.time()
    try:
        syms = ",".join(("sh" if suffix_of(c) == "SH" else ("bj" if suffix_of(c) == "BJ" else "sz")) + c
                        for c in codes[:60])
        r = requests.get(f"https://qt.gtimg.cn/q={syms}", headers={"User-Agent": UA}, timeout=TIMEOUT)
        r.encoding = "gbk"
        ok = r.status_code == 200 and r.text.count("~") > 500
        body = f"len={len(r.text)} sample={r.text[:80]}"
    except Exception as e:
        ok, body = False, f"{type(e).__name__} {str(e)[:120]}"
    RESULTS["sources"]["snapshot_tencent"] = {"label": "快照备选-腾讯批量", "ok": ok,
                                              "sec": round(time.time() - t0, 2), "detail": body}


def make_decisions():
    d = {}
    v = RESULTS["sources"].get("valuation_history_akshare", {})
    if v.get("full_serial_min") is not None:
        d["valuation_history"] = ("FULL_5552" if v["full_serial_min"] <= 90
                                  else f"ACTIVE_POOL_FIRST(全量需{v['full_serial_min']}min)")
    else:
        d["valuation_history"] = "PROBE_FAIL→用备选源/自算兜底"
    f = RESULTS["sources"].get("finance_main", {})
    if f.get("full_serial_min") is not None:
        d["finance_main"] = ("FULL_5552" if f["full_serial_min"] <= 90
                             else f"ACTIVE_POOL_FIRST(全量需{f['full_serial_min']}min)")
    else:
        d["finance_main"] = "PROBE_FAIL→暂缓"
    RESULTS["decisions"] = d


def main():
    os.makedirs(os.path.dirname(RESULT_JSON), exist_ok=True)
    codes = load_universe()
    log(f"universe: {len(codes)} codes, akshare={AK_VERSION}")
    sample = stratified_sample(codes, n=25)
    log(f"sample: {sample}")

    try:
        probe_valuation_history(codes, sample)
    except Exception:
        RESULTS["sources"]["valuation_history_akshare"] = {"error": traceback.format_exc()[-500:]}
    try:
        probe_fhps()
    except Exception:
        RESULTS["sources"]["fhps"] = {"error": traceback.format_exc()[-500:]}
    try:
        probe_snapshot_push2delay()
    except Exception:
        RESULTS["sources"]["snapshot_push2delay"] = {"error": traceback.format_exc()[-500:]}
    try:
        probe_finance_main(sample)
    except Exception:
        RESULTS["sources"]["finance_main"] = {"error": traceback.format_exc()[-500:]}
    try:
        probe_kline_sources(codes)
    except Exception:
        RESULTS["sources"]["kline_push2his"] = {"error": traceback.format_exc()[-500:]}

    make_decisions()
    RESULTS["finished_at"] = datetime.now().isoformat()
    with open(RESULT_JSON, "w", encoding="utf-8") as f:
        json.dump(RESULTS, f, ensure_ascii=False, indent=2, default=str)
    log("=" * 60)
    for k, rec in RESULTS["sources"].items():
        if "error" in rec:
            log(f"✗ {k}: probe crashed")
        else:
            log(f"✓ {k}: success={rec.get('success_rate')} avg={rec.get('avg_sec')}s "
                f"full_serial_min={rec.get('full_serial_min')}")
    log(f"decisions: {json.dumps(RESULTS['decisions'], ensure_ascii=False)}")
    log(f"json → {RESULT_JSON}")


if __name__ == "__main__":
    main()
