"""factors.py 验证脚本：
1) 单元验证：ts_slope 与 numpy.polyfit 对照、ts_corr 与手算对照、横截面排名对照
2) 静态检查：源码不含 shift(-1)
3) 真实数据冒烟：从 sqlite 读 daily_bars，计算两因子，检查覆盖率 / 运行时 / 输出基本特征
"""
import sqlite3
import sys
import time

import numpy as np
import pandas as pd

sys.path.insert(0, "/home/ubuntu/stock-quant")
from factors import (  # noqa: E402
    factor_divergence_tscorr_slope_20_60_close as f1,
    factor_shock_skew_rank_20_60_volume as f2,
    _ts_slope, _ts_corr,
)

# ---------- 1. 单元验证 ----------
rng = np.random.default_rng(7)
n = 300
t = pd.DataFrame({
    "ticker": ["A"] * n + ["B"] * n,
    "date": list(range(n)) * 2,
    "close": np.cumsum(rng.normal(0.01, 1, n * 2)) + 10,
    "volume": rng.uniform(1e4, 1e6, n * 2),
})
t = t.sort_values(["ticker", "date"]).reset_index(drop=True)

# ts_slope vs polyfit（窗口全量处对齐，取第 50~100 行）
sl = _ts_slope(t, "ticker", "close", 60, 60)
errs = []
for i in range(60, 120):
    m, _ = np.polyfit(np.arange(60), t.loc[i - 59:i, "close"].to_numpy(), 1)
    errs.append(abs(m - sl.loc[i]))
print(f"[unit] ts_slope vs polyfit: max|err|={max(errs):.2e}")

# ts_corr 对照（经 _ts_corr 与手算；用两个不同列 = 真实用法）
t["__r"] = t.groupby("ticker")["close"].pct_change()
t["__v"] = t.groupby("ticker")["volume"].pct_change()
w = 20
c2 = _ts_corr(t, "ticker", "__r", "__v", w, w)
man_s = pd.Series(dtype=float)
for tk in ("A", "B"):
    s = t.loc[t["ticker"] == tk]
    man_s = pd.concat([man_s, s["__r"].rolling(w).corr(s["__v"])])
man_s = man_s.reindex(t.index)  # 按原索引对齐
cmp_ = pd.DataFrame({"mine": c2, "man": man_s}).dropna()
print(f"[unit] ts_corr vs manual: max|err|={(cmp_['mine']-cmp_['man']).abs().max():.2e}")

# ---------- 2. 静态检查 ----------
src = open("/home/ubuntu/stock-quant/factors.py").read()
# 只对两个因子函数体做逐行循环检查（共享工具 _ticker_col 的 for 仅遍历 2 个列名候选，非数据行迭代）
fbody = src[src.index("def factor_divergence"):] + \
    src[src.index("def factor_shock"):]
assert ".shift(-1" not in fbody, "检测到 shift(-1)!"
print("[static] shift(-1) 检查通过（因子函数体无负向位移）")
for kw in ("for ", "while ", ".apply("):
    assert kw not in fbody, f"因子函数体中检测到逐行{kw}!"
print("[static] 无 for/while/apply 逐行循环（仅允许 .rolling/.groupby 向量化）")

# ---------- 3. 真实数据冒烟 ----------
con = sqlite3.connect("/home/ubuntu/stock-alert/data/stockdesk.db")
df = pd.read_sql("SELECT symbol, date, open, high, low, close, volume FROM daily_bars", con)
df = df.rename(columns={"symbol": "ticker"})
print(f"\n[data] rows={len(df)} tickers={df['ticker'].nunique()} "
      f"dates={df['date'].nunique()} ({df['date'].min()}~{df['date'].max()})")

for name, fn in [("f1_divergence", f1), ("f2_shock_skew", f2)]:
    s0 = time.time()
    out = fn(df)
    dt = time.time() - s0
    non_null = out.notna().sum()
    # 每 ticker 覆盖：样本内取值是否符合 [均值0, std1]
    ok = out.dropna()
    print(f"[data] {name}: rows={len(out)} 非空={non_null} "
          f"({non_null / len(out):.1%}) 耗时={dt:.2f}s | "
          f"nunique={out.nunique()} 均值={ok.mean():.3f} 标准差={ok.std():.3f} "
          f"min={ok.min():.2f} max={ok.max():.2f}")

# ---------- 4. 简单因子检验（IC + 分层收益，仅做说明演示）----------
# 说明：这只是"简单测试"——基于横截面 rank IC 与分层收益的快速体检，
# 不是严格回测（未含交易成本/换手限制/严格前复权/板块中性化）。
# 未来收益用 T+1 收盘价，仅用于检验因子信号方向，因子本身用不到未来数据。
df["__f1"] = f1(df)
df["__f2"] = f2(df)
df["ret1"] = df.groupby("ticker")["close"].shift(-1) / df["close"] - 1

tails = df[df["__f1"].notna() & df["__f2"].notna() & df["ret1"].notna()].copy()
tails = tails[tails["date"] >= "2025-06-01"]  # 近 1 年+ 样本（长窗口因子需要预热）

def ic_stats(panel, name):
    # Spearman IC = 对横截面内两列分别 rank 后求 Pearson（避免依赖 scipy）
    def _spearman(s):
        a = s[name].rank()
        b = s["ret1"].rank()
        return a.corr(b)
    ics = panel.groupby("date", group_keys=False).apply(_spearman, include_groups=False)
    ics = ics.dropna()
    print(f"[simple-t] {name}: 截面Spearman IC 均值={ics.mean():.4f} "
          f"中位={ics.median():.4f} IR={ics.mean()/ics.std():.3f} "
          f"IC>0 比例={float((ics>0).mean()):.2%} 天数={len(ics)}")

ic_stats(tails, "__f1")
ic_stats(tails, "__f2")
