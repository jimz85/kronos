import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

def calc_rsi(prices, period=14):
    prices = np.asarray(prices).flatten()
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(period).mean()
    avg_loss = pd.Series(losses).rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_ma(prices, period):
    return pd.Series(np.asarray(prices).flatten()).rolling(period).mean()

def calc_atr(high, low, close, period=14):
    high = np.asarray(high).flatten()
    low = np.asarray(low).flatten()
    close = np.asarray(close).flatten()
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

def bilateral_rsi_15m(p, rsi_buy, rsi_sell, stop_pct, hold_bars):
    rsi = calc_rsi(p)
    trades = []
    pos = None
    entry_price = 0
    entry_i = 0
    
    for i in range(20, len(p) - 1):
        rsi_val = float(rsi.iloc[i])
        curr_price = p[i]
        
        if pos is None:
            if rsi_val < rsi_buy:
                pos = "long"
                entry_price = curr_price
                entry_i = i
            elif rsi_val > rsi_sell:
                pos = "short"
                entry_price = curr_price
                entry_i = i
        else:
            if pos == "long":
                if curr_price <= entry_price * (1 - stop_pct):
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
                elif rsi_val > rsi_sell or i - entry_i >= hold_bars:
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
            else:
                if curr_price >= entry_price * (1 + stop_pct):
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
                elif rsi_val < rsi_buy or i - entry_i >= hold_bars:
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
    
    return trades

def ma_trend_rsi_15m(p, h, l, ma_fast, ma_slow, rsi_buy, rsi_sell, stop_pct, hold_bars):
    ma_f = calc_ma(p, ma_fast)
    ma_s = calc_ma(p, ma_slow)
    rsi = calc_rsi(p)
    
    trades = []
    pos = None
    entry_price = 0
    entry_i = 0
    
    for i in range(max(ma_fast, ma_slow, 20), len(p) - 1):
        ma_f_val = float(ma_f.iloc[i])
        ma_s_val = float(ma_s.iloc[i])
        rsi_val = float(rsi.iloc[i])
        curr_price = p[i]
        
        is_up = ma_f_val > ma_s_val
        is_down = ma_f_val < ma_s_val
        
        if pos is None:
            if is_up and rsi_val < rsi_buy:
                pos = "long"
                entry_price = curr_price
                entry_i = i
            elif is_down and rsi_val > rsi_sell:
                pos = "short"
                entry_price = curr_price
                entry_i = i
        else:
            if pos == "long":
                if curr_price <= entry_price * (1 - stop_pct):
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
                elif not is_up or rsi_val > rsi_sell:
                    trades.append((curr_price - entry_price) / entry_price)
                    pos = None
            else:
                if curr_price >= entry_price * (1 + stop_pct):
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
                elif not is_down or rsi_val < rsi_buy:
                    trades.append((entry_price - curr_price) / entry_price)
                    pos = None
    
    return trades

print("="*70)
print("  BTC 15分钟K线策略测试")
print("="*70)

t = yf.Ticker("BTC-USD")
df = t.history(period="60d", interval="15m")

if df.empty or len(df) < 1000:
    print("数据不足")
    exit()

p = np.asarray(df["Close"].values).flatten()
h = np.asarray(df["High"].values).flatten()
l = np.asarray(df["Low"].values).flatten()

print(f"数据: {len(df)}根K线, 覆盖{(df.index[-1]-df.index[0]).days}天")
days = (df.index[-1]-df.index[0]).days
print(f"日均K线: {len(p)/days:.0f}根")
print()

results = []

# 策略1: 纯双边RSI
for rsi_buy in [35, 40, 45]:
    for rsi_sell in [55, 60, 65]:
        for stop_pct in [0.005, 0.01, 0.015, 0.02]:
            for hold in [12, 24, 48]:
                trades = bilateral_rsi_15m(p, rsi_buy, rsi_sell, stop_pct, hold)
                if len(trades) >= 30:
                    wins = [t for t in trades if t > 0]
                    losses = [t for t in trades if t < 0]
                    wr = len(wins) / len(trades)
                    pf = abs(sum(wins)/sum(losses)) if losses else 999
                    equity = [1.0]
                    for t in trades: equity.append(equity[-1] * (1 + t))
                    equity = np.array(equity)
                    total = equity[-1] - 1
                    peak = np.maximum.accumulate(equity)
                    dd = (equity - peak) / peak
                    max_dd = abs(dd.min())
                    annual_trades = len(trades) / days * 365
                    
                    results.append(("RSI双边", 
                        f"RSI{rsi_buy}/{rsi_sell} 止{stop_pct:.1%} 持{hold//4}h",
                        len(trades), annual_trades, wr, pf, total, max_dd))

# 策略2: MA趋势+RSI
for ma_fast in [20, 50]:
    for ma_slow in [50, 100]:
        for rsi_buy in [40, 45]:
            for rsi_sell in [55, 60]:
                for stop_pct in [0.01, 0.015]:
                    for hold in [24, 48]:
                        trades = ma_trend_rsi_15m(p, h, l, ma_fast, ma_slow, rsi_buy, rsi_sell, stop_pct, hold)
                        if len(trades) >= 30:
                            wins = [t for t in trades if t > 0]
                            losses = [t for t in trades if t < 0]
                            wr = len(wins) / len(trades)
                            pf = abs(sum(wins)/sum(losses)) if losses else 999
                            equity = [1.0]
                            for t in trades: equity.append(equity[-1] * (1 + t))
                            equity = np.array(equity)
                            total = equity[-1] - 1
                            peak = np.maximum.accumulate(equity)
                            dd = (equity - peak) / peak
                            max_dd = abs(dd.min())
                            annual_trades = len(trades) / days * 365
                            
                            results.append(("MA趋势+RSI",
                                f"MA{ma_fast}/{ma_slow} RSI{rsi_buy}/{rsi_sell} 止{stop_pct:.1%} 持{hold//4}h",
                                len(trades), annual_trades, wr, pf, total, max_dd))

print(f"测试了 {len(results)} 个参数组合")
print()
print(f"{'策略':<15} {'参数':<38} {'交易数':>8} {'年交易':>8} {'胜率':>8} {'PF':>8} {'总收益':>10} {'DD':>8}")
print("-"*110)

results.sort(key=lambda x: -x[3])
for name, params, trades_cnt, annual, wr, pf, total, max_dd in results[:30]:
    marker = "✅" if wr >= 0.50 and pf >= 1.5 else "🟡" if wr >= 0.48 else "❌"
    print(f"{marker}{name:<13} {params:<38} {trades_cnt:>8} {annual:>7.0f} {wr:>7.1%} {pf:>8.2f} {total:>+10.1%} {max_dd:>7.1%}")

print()
print("★ 结论：15分钟K线，59天数据约产生100-500笔交易")
print(f"  如果59天有{trades_cnt}笔，年化约{annual:.0f}笔")
print()
print("★ 注意：59天数据有限，年化交易数的置信度较低")
print("  需要更长时间的数据（6个月-1年）才能确定策略真实表现")