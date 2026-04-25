
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

def calc_rsi(p, n=14):
    d = np.diff(p, prepend=p[0])
    g = np.where(d>0, d, 0); l = np.where(d<0, -d, 0)
    ag = pd.Series(g).rolling(n).mean(); al = pd.Series(l).rolling(n).mean()
    return 100 - (100/(1 + ag/(al+1e-10)))

def calc_ma(p, n): return pd.Series(np.asarray(p).flatten()).rolling(n).mean()

def calc_bollinger(p, n=20, k=2):
    m = pd.Series(p).rolling(n).mean(); s = pd.Series(p).rolling(n).std()
    return m, m+k*s, m-k*s

print("="*70)
print("  真正的研究：理解市场为什么涨跌")
print("="*70)

# ETH全周期分析
coin = "ETH-USD"
df = yf.download(coin, period="5y", interval="1d", progress=False)
if isinstance(df.columns, pd.MultiIndex):
    df = df.loc[:, df.columns.get_level_values(0)]

p = np.asarray(df["Close"].values).flatten()
dates = df.index

print(f"ETH历史: ${p[0]:.0f} -> ${p[-1]:.0f}")
print(f"时间段: {dates[0].strftime('%Y-%m')} to {dates[-1].strftime('%Y-%m')}")
print()

# 分析每个年份的表现
years = []
for i in range(0, len(p)-365, 365):
    year_p = p[i:i+365]
    if len(year_p) < 300:
        continue
    ret = (year_p[-1] - year_p[0]) / year_p[0]
    years.append({
        "year": dates[i].year,
        "start": year_p[0],
        "end": year_p[-1],
        "return": ret
    })
    print(f"  {dates[i].year}: ${year_p[0]:.0f} -> ${year_p[-1]:.0f} = {ret:+.1%}")

print()
print("★ 市场特征分析：")

# 对每个年份计算指标
for y in years:
    start_idx = dates.get_indexer([pd.Timestamp(f"{y['year']}-01-01")], method='pad')[0]
    end_idx = start_idx + 365
    if end_idx > len(p):
        continue
    
    year_p = p[start_idx:min(end_idx, len(p))]
    
    if len(year_p) < 200:
        continue
    
    rsi = calc_rsi(year_p)
    rsi_mean = rsi.mean()
    rsi_std = rsi.std()
    
    ma20 = calc_ma(year_p, 20)
    ma50 = calc_ma(year_p, 50)
    above_ma20 = sum(year_p > ma20) / len(year_p)
    
    # 计算波动率
    returns = np.diff(np.log(year_p))
    vol = np.std(returns) * np.sqrt(365)
    
    # 计算趋势强度
    if year_p[-1] > year_p[0] * 1.2:
        regime = "STRONG_UP"
    elif year_p[-1] > year_p[0] * 1.05:
        regime = "UP"
    elif year_p[-1] < year_p[0] * 0.8:
        regime = "DOWN"
    else:
        regime = "RANGE"
    
    print(f"  {y['year']}: {regime:12} | RSI均值={rsi_mean:.0f} std={rsi_std:.0f} | >MA20={above_ma20:.0%} | 波动率={vol:.0%}")

print()
print("★ 真正的问题：")
print("  1. 趋势市场(STRONG_UP): RSI均值>55 -> 趋势跟随有效")
print("  2. 震荡市场(RANGE): RSI均值45-55 -> 均值回归有效")
print("  3. 下跌市场(DOWN): RSI均值<45 -> 空仓或做空")
