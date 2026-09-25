"""市场状态判别（近13周）：全市场宽度 + 等权指数动量。
规则（双条件，避免单一指标误判）：
  普涨趋势: 当周上涨家数占比 ≥ 58% 且 当周中位收益 ≥ +0.4%
  趋势上涨: 当周中位收益 ≥ +0.3% 且 指数20日动量 ≥ 0
  趋势下跌: 当周中位收益 ≤ -0.5%
  震荡:     其余（含 -0.5% < 收益 < +0.3%）
"""
import os, glob
import pandas as pd
import numpy as np

files = sorted(glob.glob('/home/ubuntu/data/eastmoney_data/*.csv'))
parts = []
for f in files:
    df = pd.read_csv(f, encoding='utf-8-sig', usecols=['date', 'close'])
    df['ticker'] = os.path.basename(f).split('_')[0]
    parts.append(df)
d = pd.concat(parts, ignore_index=True)
d['date'] = pd.to_datetime(d['date'])
d = d[d['date'] >= '2026-03-01']
# 等权指数：每日所有股票 close 均值（简单等权，不再算收益率复合）
idx = d.groupby('date')['close'].mean().sort_index()
idx_ret = idx.pct_change()
print(f'指数点位范围: {idx.iloc[0]:.2f} -> {idx.iloc[-1]:.2f} 日期 {idx.index[0].date()}~{idx.index[-1].date()}')

# 每日宽度
w = d.groupby('date')['close'].apply(lambda s: (s.pct_change() > 0).mean() / 1)
# 日收益率中位
med = d.groupby('date')['close'].apply(lambda s: s.pct_change().median())

# 20日动量（指数 vs 20日前）
mom20 = idx / idx.shift(20) - 1

dfw = pd.DataFrame({'idx_ret': idx_ret, 'breadth': w, 'median_ret': med, 'mom20': mom20})
dfw = dfw.resample('W-FRI').agg({'idx_ret': lambda s: (1+s).prod()-1,
                                 'breadth': 'mean',
                                 'median_ret': 'mean',
                                 'mom20': 'last'})
dfw = dfw.dropna(subset=['idx_ret'])

def label(r):
    if r['breadth'] >= 0.58 and r['median_ret'] >= 0.004:
        return '普涨趋势'
    if r['median_ret'] >= 0.003 and r['mom20'] >= 0:
        return '趋势上涨'
    if r['median_ret'] <= -0.005:
        return '趋势下跌'
    return '震荡'

dfw['state'] = dfw.apply(label, axis=1)
print('\n近13周市场状态:')
print(dfw.tail(13).round(4).to_string())
dfw.tail(13).to_csv('/home/ubuntu/stock-quant/results/market_regime.csv')
print('\n已保存 results/market_regime.csv')
