
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

# 正确获取BTC真实历史数据
t = yf.Ticker("BTC-USD")

# 获取更长的数据
df = t.history(period="max")
print(f"BTC历史数据: {len(df)}行")
print(f"开始: {df.index[0]}")
print(f"结束: {df.index[-1]}")
print(f"起始价格: ${df['Close'].iloc[0]:.2f}")
print(f"结束价格: ${df['Close'].iloc[-1]:.2f}")
print()

# 检查是否有2020-2023的数据(包含COVID崩盘和后续恢复)
p = np.asarray(df["Close"].values).flatten()
print(f"数据点: {len(p)}")
print(f"2019年底: ${p[max(0,len(p)-1500)]:.0f}")
print(f"2020年初: ${p[max(0,len(p)-1400)]:.0f}")
print(f"2022年底: ${p[max(0,len(p)-800)]:.0f}")
print(f"2024年: ${p[max(0,len(p)-200)]:.0f}")
print(f"当前: ${p[-1]:.0f}")

# 最大的几次回撤
returns = np.diff(p) / p[:-1]
equity = np.cumprod(1 + returns)
peak = np.maximum.accumulate(equity)
dd = (equity - peak) / peak

# 找到最大回撤点
max_dd_idx = np.argmin(dd)
max_dd_val = abs(dd[max_dd_idx])

print()
print(f"最大回撤: {max_dd_val:.1%}")
print(f"回撤日期: {df.index[max_dd_idx]}")

# 计算2020年以来的收益
# 找2020年初的索引
for i in range(len(df)):
    if df.index[i].year == 2020 and df.index[i].month == 1:
        print(f"2020-01价格: ${df['Close'].iloc[i]:.0f}")
        print(f"2020-01至今收益: {(p[-1] - df['Close'].iloc[i]) / df['Close'].iloc[i]:.1%}")
        break
