"""龙虎榜事件研究：上榜后 D1/D2/D5/D10/D20/D30 收益，按上榜原因分组。

数据：/home/ubuntu/marketdata/lhb/lhb_2026-06-01_to_2026-09-02.csv
方法：事件研究（event study）：
  - 每记录 = 一个"上榜事件"，D1..D30 涨幅为数据自带（东财口径：上榜日次日收盘等）
  - 分组：a) 按上榜原因大类（涨幅偏离/换手/振幅/ST/风险警示）
         b) 按龙虎榜净买入额方向（net>0 买入席位 vs net<0 卖出席位）
         c) 按换手率/流通市值规模
  输出：分组平均涨幅 + 样本数 + 是否统计显著（t 检验近似）
  意义：验证"龙虎榜炒作是否可跟随" —— 买上榜后 N 日涨幅（正向持续性 或 反转）
"""
import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path

SRC = Path("/home/ubuntu/marketdata/lhb/lhb_2026-06-01_to_2026-09-02.csv")
OUT = Path("/home/ubuntu/stock-quant/results/lhb_event_study.json")

rows = list(csv.DictReader(open(SRC, encoding="utf-8-sig")))
print(f"记录数: {len(rows)}")


def fnum(v):
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def cat_group(explanation):
    ex = str(explanation)
    if "涨幅" in ex or "收盘价格涨幅" in ex:
        return "涨幅偏离"
    if "换手" in ex:
        return "高换手"
    if "振幅" in ex:
        return "振幅"
    if "跌幅" in ex or "收盘价格跌幅" in ex:
        return "跌幅偏离"
    if "ST" in ex or "风险警示" in ex or "未完成股改" in ex:
        return "ST/风险警示"
    if "上市" in ex:
        return "新股"
    return "其他"


def tval(vals):
    n = len(vals)
    if n < 2:
        return (0.0, 0.0)
    m = statistics.mean(vals)
    sd = statistics.stdev(vals) if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else 0.0
    return (m, m / se if se else 0.0)


d_horizons = ["D1_CLOSE_ADJCHRATE", "D5_CLOSE_ADJCHRATE", "D10_CLOSE_ADJCHRATE",
              "D20_CLOSE_ADJCHRATE", "D30_CLOSE_ADJCHRATE"]
result = {"by_category": {}, "by_net_direction": {}, "total": {}}

# 全部样本
for h in d_horizons:
    vals = [fnum(r[h]) for r in rows if fnum(r[h]) is not None]
    m, t = tval(vals)
    result["total"][h] = {"mean": round(m, 4), "t": round(t, 2), "n": len(vals)}

# 按上榜原因大类
by_cat = defaultdict(list)
for r in rows:
    by_cat[cat_group(r["EXPLANATION"])].append(r)
for cat, recs in by_cat.items():
    entry = {}
    for h in d_horizons:
        vals = [fnum(r[h]) for r in recs if fnum(r[h]) is not None]
        m, t = tval(vals)
        entry[h] = {"mean": round(m, 4), "t": round(t, 2), "n": len(vals)}
    result["by_category"][cat] = entry

# 按净买入方向（net>0 = 买盘主导）
by_net = {"net_positive": [], "net_negative": []}
for r in rows:
    net = fnum(r["BILLBOARD_NET_AMT"])
    if net is None:
        continue
    by_net["net_positive" if net > 0 else "net_negative"].append(r)
for grp, recs in by_net.items():
    entry = {}
    for h in d_horizons:
        vals = [fnum(r[h]) for r in recs if fnum(r[h]) is not None]
        m, t = tval(vals)
        entry[h] = {"mean": round(m, 4), "t": round(t, 2), "n": len(vals)}
    result["by_net_direction"][grp] = entry

OUT.parent.mkdir(exist_ok=True, parents=True)
with open(OUT, "w") as f:
    json.dump(result, f, ensure_ascii=False, indent=2, default=str)

print("\n=== 总样本（上榜后各日平均涨幅 %）===")
for h, v in result["total"].items():
    print(f"  {h:28} 均值={v['mean']:+.3f}%  t={v['t']:6.2f}  n={v['n']}")

print("\n=== 按上榜原因大类 D1/D5/D10/D30 ===")
for cat, e in result["by_category"].items():
    line = "  ".join(f"{h.replace('_CLOSE_ADJCHRATE','')[1:]}d={e[h]['mean']:+.3f}(t{e[h]['t']:.1f})"
                     for h in ["D1_CLOSE_ADJCHRATE", "D5_CLOSE_ADJCHRATE",
                               "D10_CLOSE_ADJCHRATE", "D30_CLOSE_ADJCHRATE"])
    print(f"  {cat:10} {line}")

print("\n=== 按净买卖方向 ===")
for grp, e in result["by_net_direction"].items():
    line = "  ".join(f"{h.replace('_CLOSE_ADJCHRATE','')[1:]}d={e[h]['mean']:+.3f}(t{e[h]['t']:.1f})"
                     for h in ["D1_CLOSE_ADJCHRATE", "D5_CLOSE_ADJCHRATE",
                               "D10_CLOSE_ADJCHRATE", "D30_CLOSE_ADJCHRATE"])
    print(f"  {grp:14} {line}")
print(f"\n已保存 -> {OUT}")
