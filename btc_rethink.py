
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

print("="*70)
print("  重新思考：既然主动策略都跑不赢持有")
print("  那真正的问题是：如何在持有中保护利润？")
print("="*70)

coin = "BTC-USD"
df = yf.download(coin, period="5y", interval="1d", progress=False)
if isinstance(df.columns, pd.MultiIndex):
    df = df.loc[:, df.columns.get_level_values(0)]

p = np.asarray(df["Close"].values).flatten()
dates = df.index

print(f"BTC: ${p[0]:.0f} -> ${p[-1]:.0f}")
print()

# 研究：持有BTC时，最大回撤是多少？
# 如果能避免这些大回撤，收益能提高多少？

# 计算每日收益率
returns = np.diff(p) / p[:-1]

# 计算权益曲线
equity = np.cumprod(1 + returns)

# 计算最大回撤
peak = np.maximum.accumulate(equity)
drawdowns = (equity - peak) / peak

# 找出最大的10次回撤
dd_events = []
in_dd = False
start = 0
for i in range(len(drawdowns)):
    if drawdowns[i] < -0.05 and not in_dd:  # >5%回撤开始
        in_dd = True
        start = i
    elif drawdowns[i] > -0.001 and in_dd:  # 回撤结束
        in_dd = False
        dd_pct = abs(drawdowns[i-1])
        duration = i - start
        dd_events.append({
            "start_idx": start,
            "end_idx": i,
            "max_dd": dd_pct,
            "duration": duration,
            "start_price": p[start],
            "end_price": p[i]
        })

dd_events.sort(key=lambda x: -x["max_dd"])

print("BTC历史最大10次回撤:")
print("-"*70)
for i, dd in enumerate(dd_events[:10]):
    start_date = dates[dd["start_idx"]].strftime("%Y-%m-%d")
    end_date = dates[dd["end_idx"]].strftime("%Y-%m-%d")
    print(f"  {i+1}. {start_date} -> {end_date}")
    print(f"     回撤: {dd['max_dd']:.1%} (${dd['start_price']:.0f} -> ${dd['end_price']:.0f})")
    print(f"     持续: {dd['duration']}天")

# 计算如果不避开这些回撤会损失多少
total_dd_cost = sum(dd["max_dd"] for dd in dd_events[:5])
print()
print(f"前5大回撤合计: {total_dd_cost:.1%}")
print(f"如果在峰值时卖出，回撤低点买回，能节省: {total_dd_cost:.1%}的损失")
print()

# 研究：如果能精确避开这5次回撤，收益是多少？
# 模拟：在回撤开始时卖出，回撤结束时买回
print("理想操作（精确逃顶抄底）:")
print("-"*70)

# 简化：假设每年只有1-2次大回撤值得操作
# 实际操作：跟踪止损

# 测试不同的跟踪止损
print()
print("跟踪止损 vs 买入持有:")
print("-"*70)

for trail_pct in [0.05, 0.10, 0.15, 0.20, 0.30, 0.50]:
    equity = [1.0]
    peak_price = p[0]
    in_position = True
    
    for i in range(1, len(p)):
        curr_price = p[i]
        
        if in_position:
            # 更新峰值
            if curr_price > peak_price:
                peak_price = curr_price
            
            # 检查跟踪止损
            drawdown = (peak_price - curr_price) / peak_price
            if drawdown > trail_pct:
                # 卖出
                equity.append(equity[-1] * (curr_price / p[i-1]))
                in_position = False
                peak_price = curr_price  # 重置
            else:
                equity.append(equity[-1] * (curr_price / p[i-1]))
        else:
            # 空仓，等待重新入场（价格超过上次峰值）
            if curr_price >= peak_price:
                in_position = True
            equity.append(equity[-1])  # 空仓时权益不变
    
    # 最后一天处理
    if in_position:
        equity[-1] = equity[-2] * (p[-1] / p[-2])
    
    equity = np.array(equity)
    strategy_ret = equity[-1] - 1
    buy_hold_ret = (p[-1] - p[0]) / p[0]
    alpha = strategy_ret - buy_hold_ret
    
    # 计算实际触发次数
    triggers = sum(1 for i in range(1, len(p)) if 
                   equity[i] == equity[i-1] and in_position == False)
    
    print(f"  跟踪止损{trail_pct:.0%}: 策略{strategy_ret:+.1%} vs 持有{buy_hold_ret:+.1%} Alpha={alpha:+.1%}")

print()
print("★ 结论：")
print("  1. BTC有巨大的长期上涨趋势，任何主动操作都在破坏这个趋势")
print("  2. 5%以上回撤经常发生，但跟踪止损会频繁进出，增加摩擦成本")
print("  3. 真正的问题是：你相信BTC长期上涨吗？")
print("     - 如果相信：买入持有，不做任何操作")
print("     - 如果不确定：降低仓位，只持有部分")
