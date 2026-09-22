#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""audit_valuation.py — 估值/股息数据质量校验。

校验项:
1. 覆盖面: valuation/ 与 finance/ 文件数、行数、日期范围
2. 一致性: valuation 逐日 close vs 东财日线 close（抽样 20 只, |差|<0.5%）
3. 独立源交叉: 最新估值 vs 腾讯实时行情 PB/总市值（抽样, |差|<5%）
4. 股息率合理性: 已知高息股（长江电力/茅台/大行）最新 div_yield 抽查
5. 快照 vs 历史衔接: snap 最新价 vs valuation 最新 close
输出: results/valuation_audit.md + stdout
"""
import os
import random
import sys
from datetime import datetime

import pandas as pd

sys.path.insert(0, "/home/ubuntu")
from dlcommon import Requester, log, out_path

VAL_DIR = out_path("valuation")
FIN_DIR = out_path("finance")
SNAP = sorted(os.listdir(out_path("valuation_snapshot")))[-1] if os.listdir(out_path("valuation_snapshot")) else None
EM_DIR = "/home/ubuntu/data/eastmoney_data"
OUT_MD = "/home/ubuntu/stock-quant/results/valuation_audit.md"

lines = ["# 估值/股息数据质量校验", "",
         f"- 校验时间: {datetime.now():%Y-%m-%d %H:%M}", ""]


def section(t):
    lines.append(f"## {t}")
    lines.append("")


def ok_fail(cond):
    return "✅" if cond else "❌"


def main():
    # 1. 覆盖面
    section("1. 覆盖面")
    val_files = [f for f in os.listdir(VAL_DIR) if f.endswith(".csv")]
    fin_files = [f for f in os.listdir(FIN_DIR) if f.endswith(".csv")]
    em_files = [f for f in os.listdir(EM_DIR) if f[:6].isdigit()]
    lines.append(f"- 东财日线股票数: {len(em_files)}；估值历史文件: {len(val_files)}；财务指标文件: {len(fin_files)}")
    sample_val = random.Random(1).sample(val_files, min(30, len(val_files)))
    rows_n, dmin, dmax = [], None, None
    for fn in sample_val:
        df = pd.read_csv(os.path.join(VAL_DIR, fn), usecols=["date"], low_memory=False)
        rows_n.append(len(df))
        dmin = df["date"].min() if dmin is None else min(dmin, df["date"].min())
        dmax = df["date"].max() if dmax is None else max(dmax, df["date"].max())
    lines.append(f"- 估值历史抽样30只: 平均 {sum(rows_n)//len(rows_n)} 行/只, 日期范围 {dmin} ~ {dmax}")
    lines.append("")

    # 2. close 一致性: valuation(不复权) vs 腾讯原始日K(不复权)
    section("2. 不复权 close 与腾讯原始日K一致性（抽样15只）")
    req = Requester(min_interval=0.35)
    codes = sorted({f.split("_")[0] for f in em_files})
    random.Random(2).shuffle(codes)
    n_check = n_bad = 0
    bad_list = []
    for code in codes:
        if n_check >= 15:
            break
        vp = os.path.join(VAL_DIR, f"{code}.csv")
        if not os.path.exists(vp):
            continue
        try:
            suf = "sh" if code.startswith("6") else ("bj" if code[:2] in ("43", "83", "87", "88", "92") or code[0] == "4" else "sz")
            txt = req.get_text(
                f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={suf}{code},day,,,5,",
                encoding="utf-8")
            import json as _json
            j = _json.loads(txt)
            sym = suf + code
            dd = (j.get("data") or {}).get(sym) or {}
            kl = dd.get("day") or []
            if not kl:
                continue
            t_date, t_close = kl[-1][0], float(kl[-1][2])
            va = pd.read_csv(vp, usecols=["date", "close"], low_memory=False)
            row = va[va["date"] == t_date]
            if row.empty:
                continue
            diff = abs(float(row.iloc[0]["close"]) - t_close) / t_close
            n_check += 1
            if diff > 0.005:
                n_bad += 1
                bad_list.append(f"{code}@{t_date}: {diff:.3%}")
        except Exception as e:  # noqa: BLE001
            continue
    lines.append(f"- 抽查 {n_check} 只，不复权收盘价偏差>0.5% 的 {n_bad} 只"
                 + (f"：{bad_list}" if bad_list else ""))
    lines.append("")
    lines.append("> 注：估值历史的 close 为**不复权价**（官方口径），与 eastmoney_data 的前复权日线不可直接比价；")
    lines.append("> 计算 PE/PB 请直接用本目录的 pe_ttm/pb 列，或自行用复权因子换算。")
    lines.append("")

    # 3. 腾讯独立源交叉（PB / 总市值）
    section("3. 独立源交叉校验（腾讯实时 vs 最新估值，抽样15只）")
    req = Requester(min_interval=0.35)
    n_ok = n_bad = 0
    xbad = []
    checked = 0
    for code in codes:
        if checked >= 15:
            break
        vp = os.path.join(VAL_DIR, f"{code}.csv")
        if not os.path.exists(vp):
            continue
        try:
            suf = "sh" if code.startswith("6") else ("bj" if code[:2] in ("43", "83", "87", "88", "92") or code[0] == "4" else "sz")
            txt = req.get_text(f"https://qt.gtimg.cn/q={suf}{code}", encoding="gbk")
            f_ = txt.split('"')[1].split("~")
            t_pb, t_mv = float(f_[46]), float(f_[45]) * 1e8
            va = pd.read_csv(vp, low_memory=False).dropna(subset=["pb"])
            if va.empty or t_pb <= 0:
                continue
            row = va.iloc[-1]
            dpb = abs(row["pb"] - t_pb) / t_pb
            dmv = abs(row["total_mv"] - t_mv) / t_mv if row["total_mv"] else 0
            checked += 1
            if dpb > 0.05 or dmv > 0.05:
                n_bad += 1
                xbad.append(f"{code}: pb {row['pb']:.2f} vs {t_pb:.2f} ({dpb:.1%}), mv {dmv:.1%}")
            else:
                n_ok += 1
        except Exception as e:  # noqa: BLE001
            xbad.append(f"{code}: fetch fail {str(e)[:60]}")
    lines.append(f"- 交叉 {checked} 只: 一致 {n_ok}, 偏差>5% {n_bad}" + (f"：{xbad}" if xbad else ""))
    lines.append("")

    # 4. 股息率合理性
    section("4. 股息率抽查（高息认知股）")
    known = {"600900": "长江电力", "600519": "贵州茅台", "601398": "工商银行",
             "601288": "农业银行", "000001": "平安银行"}
    for code, name in known.items():
        vp = os.path.join(VAL_DIR, f"{code}.csv")
        if not os.path.exists(vp):
            continue
        va = pd.read_csv(vp, low_memory=False).dropna(subset=["div_yield"])
        if va.empty:
            lines.append(f"- {name}({code}): 无分红记录（可能正常）")
            continue
        row = va.iloc[-1]
        lines.append(f"- {name}({code}): 最新股息率TTM {row['div_yield']:.2f}% (div_ttm={row['div_ttm']}, close={row['close']}, {row['date']})")
    lines.append("")

    # 5. 快照衔接
    if SNAP:
        section("5. 当日快照衔接")
        snap = pd.read_csv(out_path("valuation_snapshot", SNAP), dtype={"code": str}, low_memory=False)
        okc = badc = 0
        for code in codes[:50]:
            vp = os.path.join(VAL_DIR, f"{code}.csv")
            s = snap[snap["code"] == code]
            if s.empty or not os.path.exists(vp):
                continue
            va = pd.read_csv(vp, usecols=["date", "close"], low_memory=False)
            sc, vc = s.iloc[0]["price"], va.iloc[-1]["close"]
            if pd.isna(sc) or pd.isna(vc) or vc == 0:
                continue
            if abs(sc - vc) / vc < 0.02:
                okc += 1
            else:
                badc += 1
        lines.append(f"- 快照价 vs 估值历史最新 close（50只）: 一致 {okc}, 偏差≥2% {badc}（当日涨跌会导致正常差异）")
        lines.append("")

    os.makedirs(os.path.dirname(OUT_MD), exist_ok=True)
    with open(OUT_MD, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print("\n".join(lines))
    log(f"audit → {OUT_MD}")


if __name__ == "__main__":
    main()
