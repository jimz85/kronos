"""
高频率合约策略 - 最终版
关键洞察: leavaraged compounding 是毁灭性的
解决方案: 固定风险(每笔最多亏1%) + 复利增长
"""
import vectorbt as vbt
import pandas as pd
import numpy as np
import warnings
warnings.filterwarnings('ignore')

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'

def calc_rsi(close, n=14):
    d = np.diff(close, prepend=close.iloc[0])
    g = np.where(d > 0, d, 0.0)
    l = np.where(d < 0, -d, 0.0)
    ag = pd.Series(g).rolling(n).mean()
    al = pd.Series(l).rolling(n).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))

def calc_ema(close, n):
    return close.ewm(span=n, adjust=False).mean()

def calc_adx(high, low, close, n=14):
    tr1 = high - low
    tr2 = np.abs(high - close.shift())
    tr3 = np.abs(low - close.shift())
    tr = pd.DataFrame({'tr1':tr1,'tr2':tr2,'tr3':tr3}).max(axis=1)
    up = high.diff()
    dn = -low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=low.index)
    atr = tr.rolling(n).mean()
    pdi = 100 * (pdm.rolling(n).mean() / atr)
    mdi = 100 * (mdm.rolling(n).mean() / atr)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
    return dx.rolling(n).mean()

def load_data(coin='BTC'):
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    cols = df.columns.tolist()
    new_cols = []
    seen = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn not in seen:
            new_cols.append(c)
            seen[cn] = cn
    df = df[new_cols][['datetime_utc','open','high','low','close','volume']]
    df['ts'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('ts').sort_index()
    df = df[df['close'] > 0]
    return df

def resample_tfs(df):
    """生成多个时间框架"""
    tfs = {}
    for tf_name, rule in [('5m', '5min'), ('15m', '15min'), ('1h', '1h'), ('4h', '4h')]:
        ohlc = df[['open','high','low','close']].resample(rule).agg({
            'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
        }).dropna()
        tfs[tf_name] = ohlc
    return tfs

def manual_backtest(c, h, l, entries_long, exits_long, entries_short, exits_short, 
                    init_cap=10000, risk_pct=0.02, lev=3, fee_pct=0.04):
    """
    手动回测: 固定风险模式
    每笔交易最多亏损 risk_pct * 当前资金
    """
    # 计算每笔交易的ATR止损
    atr = ((h - l).rolling(14).mean() / c).fillna(0.01)
    
    # 资金曲线
    equity = init_cap
    equity_curve = [equity]
    trades = []
    
    # 持仓状态
    position = None  # None, 'long', 'short'
    entry_price = 0
    entry_idx = 0
    
    # 时间索引
    idx_list = c.index.tolist()
    
    for i in range(20, len(c) - 1):
        t = idx_list[i]
        t_next = idx_list[i + 1]
        
        price = float(c.iloc[i])
        price_next_open = float(c.iloc[i + 1]) if i + 1 < len(c) else price
        
        eL = int(entries_long.iloc[i]) if i < len(entries_long) else 0
        xL = int(exits_long.iloc[i]) if i < len(exits_long) else 0
        eS = int(entries_short.iloc[i]) if i < len(entries_short) else 0
        xS = int(exits_short.iloc[i]) if i < len(exits_short) else 0
        
        sl_pct = float(atr.iloc[i]) if not np.isnan(float(atr.iloc[i])) else 0.02
        
        if position is None:
            # 无持仓
            if eL:
                # 做多
                risk_usd = equity * risk_pct
                size = (risk_usd / sl_pct) * lev
                position = 'long'
                entry_price = price_next_open * (1 + fee_pct)
                entry_idx = i
                entry_equity = equity
            elif eS:
                # 做空
                risk_usd = equity * risk_pct
                size = (risk_usd / sl_pct) * lev
                position = 'short'
                entry_price = price_next_open * (1 - fee_pct)
                entry_idx = i
                entry_equity = equity
        else:
            # 有持仓, 检查止损/止盈/退出
            if position == 'long':
                sl_price = entry_price * (1 - sl_pct)
                tp_price = entry_price * (1 + sl_pct * 2)  # 1:2 赔率
                
                hit_sl = price < sl_price
                hit_tp = price > tp_price
                hit_exit = xL or eS
                
                if hit_sl:
                    pnl = equity * risk_pct * (-1)
                    equity += pnl
                    trades.append({'dir': 'long', 'pnl': pnl, 'pnl_pct': -risk_pct*100, 'reason': 'stop', 'holding': i - entry_idx})
                    position = None
                elif hit_tp or hit_exit:
                    pnl = risk_pct * 2 * equity  # 1:2
                    equity += pnl
                    trades.append({'dir': 'long', 'pnl': pnl, 'pnl_pct': risk_pct*2*100, 'reason': 'exit', 'holding': i - entry_idx})
                    position = None
            elif position == 'short':
                sl_price = entry_price * (1 + sl_pct)
                tp_price = entry_price * (1 - sl_pct * 2)
                
                hit_sl = price > sl_price
                hit_tp = price < tp_price
                hit_exit = xS or eL
                
                if hit_sl:
                    pnl = equity * risk_pct * (-1)
                    equity += pnl
                    trades.append({'dir': 'short', 'pnl': pnl, 'pnl_pct': -risk_pct*100, 'reason': 'stop', 'holding': i - entry_idx})
                    position = None
                elif hit_tp or hit_exit:
                    pnl = risk_pct * 2 * equity
                    equity += pnl
                    trades.append({'dir': 'short', 'pnl': pnl, 'pnl_pct': risk_pct*2*100, 'reason': 'exit', 'holding': i - entry_idx})
                    position = None
        
        equity_curve.append(equity)
    
    return equity, equity_curve, trades

print("="*72)
print("高频率合约策略 - 固定风险模式")
print("每笔最多亏2%资金, 1:2赔率, 3x杠杆")
print("="*72)

df = load_data('BTC')
tfs = resample_tfs(df)

results = []

for tf_name, ohlc in tfs.items():
    c = ohlc['close']
    h = ohlc['high']
    l = ohlc['low']
    
    # 计算指标
    rsi = calc_rsi(c, 14)
    ema10 = calc_ema(c, 10)
    ema30 = calc_ema(c, 30)
    adx = calc_adx(h, l, c, 14)
    adx_avg = adx.rolling(3).mean()
    
    # ========== 策略1: RSI均值回归 + 固定风险 ==========
    for rsi_long_th in [25, 30, 35]:
        for rsi_short_th in [70, 65]:
            rsi_ma = rsi.rolling(5).mean()
            
            # LONG: RSI超卖 + RSI反弹 + EMA多头
            rsi_bounce = (rsi > rsi_ma) & (rsi.shift(1) <= rsi_ma)
            eL = ((rsi < rsi_long_th) & rsi_bounce & (ema10 > ema30 * 0.99)).astype(int)
            xL = ((rsi > 55) | (ema10 < ema30 * 0.99)).astype(int)
            
            # SHORT: RSI超买 + RSI死叉 + EMA空头
            rsi_drop = (rsi < rsi_ma) & (rsi.shift(1) >= rsi_ma)
            eS = ((rsi > rsi_short_th) & rsi_drop & (ema10 < ema30 * 1.01)).astype(int)
            xS = ((rsi < 45) | (ema10 > ema30 * 1.01)).astype(int)
            
            total = int(eL.sum()) + int(eS.sum())
            if total < 50:
                continue
            
            # 只用2020-2023年训练
            train_c = c.loc['2020-01-01':'2023-12-31']
            train_h = h.loc['2020-01-01':'2023-12-31']
            train_l = l.loc['2020-01-01':'2023-12-31']
            train_eL = eL.loc['2020-01-01':'2023-12-31']
            train_xL = xL.loc['2020-01-01':'2023-12-31']
            train_eS = eS.loc['2020-01-01':'2023-12-31']
            train_xS = xS.loc['2020-01-01':'2023-12-31']
            
            if len(train_c) < 100:
                continue
            
            final_equity, equity_curve, trades = manual_backtest(
                train_c, train_h, train_l,
                train_eL, train_xL, train_eS, train_xS,
                init_cap=10000, risk_pct=0.02, lev=3, fee_pct=0.0004
            )
            
            if len(trades) < 10:
                continue
            
            total_ret = (final_equity - 10000) / 10000 * 100
            peak = np.maximum.accumulate(equity_curve)
            dd = np.min((np.array(equity_curve) - peak) / peak) * 100
            wins = [t for t in trades if t['pnl'] > 0]
            losses = [t for t in trades if t['pnl'] <= 0]
            wr = len(wins) / len(trades) * 100 if trades else 0
            avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
            avg_loss = np.mean([t['pnl'] for t in losses]) if losses else 0
            pf = avg_win / abs(avg_loss) if avg_loss != 0 else 0
            
            # 年化
            n_days = len(train_c) / (24 if tf_name == '1h' else (4 if tf_name == '4h' else (3 if tf_name == '15m' else 1)))
            ann_ret = ((final_equity / 10000) ** (365 / max(n_days, 1)) - 1) * 100 if final_equity > 0 else -99
            
            results.append({
                'tf': tf_name,
                'rsi_long': rsi_long_th,
                'rsi_short': rsi_short_th,
                'total_ret': total_ret,
                'ann_ret': ann_ret,
                'dd': dd,
                'wr': wr,
                'pf': pf,
                'n_trades': len(trades),
                'signals_per_day': len(trades) / max(n_days, 1),
                'wins': len(wins),
                'losses': len(losses),
                'avg_win': avg_win,
                'avg_loss': avg_loss,
                'final_equity': final_equity,
            })

# 排序
if results:
    results_df = pd.DataFrame(results)
    results_df = results_df.sort_values('ann_ret', ascending=False)
    
    print(f"\n{'TF':>4} {'RSI_L':>6} {'RSI_S':>6} {'年化':>8} {'总收益':>8} {'DD':>7} {'胜率':>6} {'PF':>5} {'交易':>5} {'日均信号':>8}")
    print("-"*70)
    
    for _, r in results_df.head(20).iterrows():
        mark = '🏆' if r['ann_ret'] > 100 else ('✅' if r['ann_ret'] > 20 else ('⚠️' if r['ann_ret'] > 0 else '❌'))
        print(f"  {r['tf']:>3} {r['rsi_long']:>5} {r['rsi_short']:>5} {r['ann_ret']:>+7.0f}% {r['total_ret']:>+7.0f}% {r['dd']:>6.1f}% {r['wr']:>5.0f}% {r['pf']:>5.2f} {r['n_trades']:>5} {r['signals_per_day']:>7.1f} {mark}")

print()
print("="*72)
print("最优策略: 验证集测试")
print("="*72)

# 用最优参数在验证集上测试
best = results_df.iloc[0]
print(f"\n最优参数: {best['tf']} RSI_L={best['rsi_long']} RSI_S={best['rsi_short']}")
print(f"训练集表现: 年化{best['ann_ret']:+.0f}% DD={best['dd']:.1f}% PF={best['pf']:.2f}")

# 在验证集上测试
tf_name = best['tf']
ohlc = tfs[tf_name]
c = ohlc['close']
h = ohlc['high']
l = ohlc['low']
rsi = calc_rsi(c, 14)
ema10 = calc_ema(c, 10)
ema30 = calc_ema(c, 30)
rsi_long_th = int(best['rsi_long'])
rsi_short_th = int(best['rsi_short'])
rsi_ma = rsi.rolling(5).mean()
eL = ((rsi < rsi_long_th) & (rsi > rsi_ma) & (rsi.shift(1) <= rsi_ma) & (ema10 > ema30 * 0.99)).astype(int)
xL = ((rsi > 55) | (ema10 < ema30 * 0.99)).astype(int)
eS = ((rsi > rsi_short_th) & (rsi < rsi_ma) & (rsi.shift(1) >= rsi_ma) & (ema10 < ema30 * 1.01)).astype(int)
xS = ((rsi < 45) | (ema10 > ema30 * 1.01)).astype(int)

# 验证集
val_c = c.loc['2024-01-01':'2025-06-30']
val_h = h.loc['2024-01-01':'2025-06-30']
val_l = l.loc['2024-01-01':'2025-06-30']
val_eL = eL.loc['2024-01-01':'2025-06-30']
val_xL = xL.loc['2024-01-01':'2025-06-30']
val_eS = eS.loc['2024-01-01':'2025-06-30']
val_xS = xS.loc['2024-01-01':'2025-06-30']

print(f"\n验证集信号: LONG={int(val_eL.sum())}次 SHORT={int(val_eS.sum())}次")

for lev in [3, 5]:
    final_equity, equity_curve, trades = manual_backtest(
        val_c, val_h, val_l,
        val_eL, val_xL, val_eS, val_xS,
        init_cap=10000, risk_pct=0.02, lev=lev, fee_pct=0.0004
    )
    
    if len(trades) < 5:
        print(f"\n{lev}x杠杆 验证集: 交易太少({len(trades)}次)")
        continue
    
    total_ret = (final_equity - 10000) / 10000 * 100
    peak = np.maximum.accumulate(equity_curve)
    dd = np.min((np.array(equity_curve) - peak) / peak) * 100
    wins = [t for t in trades if t['pnl'] > 0]
    losses = [t for t in trades if t['pnl'] <= 0]
    wr = len(wins) / len(trades) * 100 if trades else 0
    avg_win = np.mean([t['pnl'] for t in wins]) if wins else 0
    avg_loss = np.mean([t['pnl'] for t in losses]) if losses else 0
    pf = avg_win / abs(avg_loss) if avg_loss != 0 else 0
    
    n_days = len(val_c) / (24 if tf_name == '1h' else 4)
    ann_ret = ((final_equity / 10000) ** (365 / max(n_days, 1)) - 1) * 100
    
    mark = '✅' if ann_ret > 0 and dd > -30 else '❌'
    print(f"\n{lev}x杠杆 验证集:")
    print(f"  收益: {total_ret:+.1f}% | 年化: {ann_ret:+.0f}% | DD: {dd:.1f}%")
    print(f"  交易: {len(trades)}次 | 胜率: {wr:.0f}% | PF: {pf:.2f}")
    print(f"  均盈利: {avg_win:+.0f} | 均亏损: {avg_loss:+.0f} {mark}")

# 分年份验证
print(f"\n验证集分年份:")
for year in [2024, 2025]:
    yr_c = c.loc[f'{year}-01-01':f'{year}-12-31']
    yr_h = h.loc[f'{year}-01-01':f'{year}-12-31']
    yr_l = l.loc[f'{year}-01-01':f'{year}-12-31']
    yr_eL = eL.loc[f'{year}-01-01':f'{year}-12-31']
    yr_xL = xL.loc[f'{year}-01-01':f'{year}-12-31']
    yr_eS = eS.loc[f'{year}-01-01':f'{year}-12-31']
    yr_xS = xS.loc[f'{year}-01-01':f'{year}-12-31']
    
    if len(yr_c) < 50:
        continue
    
    final_equity, equity_curve, trades = manual_backtest(
        yr_c, yr_h, yr_l, yr_eL, yr_xL, yr_eS, yr_xS,
        init_cap=10000, risk_pct=0.02, lev=3, fee_pct=0.0004
    )
    
    total_ret = (final_equity - 10000) / 10000 * 100
    peak = np.maximum.accumulate(equity_curve)
    dd = np.min((np.array(equity_curve) - peak) / peak) * 100
    wins = sum(1 for t in trades if t['pnl'] > 0)
    wr = wins / len(trades) * 100 if trades else 0
    
    mark = '✅' if total_ret > 0 else '❌'
    print(f"  {year}: 收益={total_ret:+.1f}% DD={dd:.1f}% 交易={len(trades)}次 胜率={wr:.0f}% {mark}")
