"""
BTC/BCH 趋势跟踪策略 - 模拟盘执行引擎
策略: ADX>23纯多 | EMA(10/30)金叉 | ATR止损1.5x | 5%仓位

模拟盘规则:
- 入场: 下一根K线开盘价 + 0.3%滑点
- 止损: 1.5倍ATR
- 仓位: 5%总资金/币
- 无信号不开仓
"""
import pandas as pd
import numpy as np
import json
import os
from datetime import datetime, timedelta

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
LOG_FILE = os.path.expanduser('~/.hermes/cron/output/sim_trades.log')
os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

COINS = ['BTC']  # BCH已在Walk-Forward中验证为持续亏损，放弃
SIZE_PCT = 0.05  # 5%总资金/币
INITIAL_CAPITAL = 10000  # 初始资金 $10000
ADX_TH = 23
SL_ATR = 1.5

def calc_adx(high, low, close, n=14):
    tr1 = high - low
    tr2 = np.abs(high - close.shift())
    tr3 = np.abs(low - close.shift())
    tr = pd.DataFrame({'tr1': tr1, 'tr2': tr2, 'tr3': tr3}).max(axis=1)
    up = high.diff()
    dn = -low.diff()
    pdm = pd.Series(np.where((up > dn) & (up > 0), up, 0.0), index=high.index)
    mdm = pd.Series(np.where((dn > up) & (dn > 0), dn, 0.0), index=low.index)
    atr = tr.rolling(n).mean()
    pdi = 100 * (pdm.rolling(n).mean() / atr)
    mdi = 100 * (mdm.rolling(n).mean() / atr)
    dx = 100 * np.abs(pdi - mdi) / (pdi + mdi + 1e-10)
    return dx.rolling(n).mean()

def load_data(coin):
    """加载并预处理数据"""
    df = pd.read_csv(f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv')
    
    # 处理重复列名问题
    cols = df.columns.tolist()
    new_cols = []
    seen = {}
    for c in cols:
        cn = c.split('.')[0]
        if cn not in seen:
            new_cols.append(c)
            seen[cn] = cn
    df = df[new_cols]
    
    df = df[['datetime_utc', 'open', 'high', 'low', 'close', 'volume']]
    df['timestamp'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('timestamp').sort_index()
    
    # 过滤无效行
    df = df[df['close'] > 0]
    
    # 4H聚合
    ohlc_4h = df[['open', 'high', 'low', 'close']].resample('4h').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    })
    # 删除4H中的NaN行
    ohlc_4h = ohlc_4h.dropna()
    
    # 日线聚合
    ohlc_1d = df[['open', 'high', 'low', 'close']].resample('1d').agg({
        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
    })
    ohlc_1d = ohlc_1d.dropna()
    
    return ohlc_4h, ohlc_1d

def get_live_data(coin):
    """获取最新实时数据(从CSV读取模拟)"""
    ohlc_4h, ohlc_1d = load_data(coin)
    return ohlc_4h, ohlc_1d

def scan_signal(ohlc_4h, ohlc_1d):
    """
    扫描当前是否有做多信号
    返回: (can_long, signal_info)
    """
    close = ohlc_4h['close']
    high = ohlc_4h['high']
    low = ohlc_4h['low']
    close_1d = ohlc_1d['close']
    
    adx = calc_adx(high, low, close, 14)
    adx_avg = adx.rolling(3).mean()
    
    # 日线EMA20/50趋势
    ema20_1d = close_1d.ewm(span=20, adjust=False).mean()
    ema50_1d = close_1d.ewm(span=50, adjust=False).mean()
    daily_trend_up = bool((ema20_1d.iloc[-1] > ema50_1d.iloc[-1]))
    
    # 4H EMA金叉
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema30 = close.ewm(span=30, adjust=False).mean()
    
    # 最后一根K线是否金叉
    bull_cross_now = bool(
        (ema10.iloc[-1] > ema30.iloc[-1]) and 
        (ema10.iloc[-2] <= ema30.iloc[-2])
    )
    
    # ATR止损
    atr_pct = ((high - low).rolling(14).mean()) / close
    sl_pct = float(atr_pct.iloc[-1] * SL_ATR) if not np.isnan(atr_pct.iloc[-1]) else 0.02
    
    # 当前价格
    current_price = float(close.iloc[-1])
    
    # 当前ADX
    current_adx = float(adx_avg.iloc[-1])
    
    can_long = (
        current_adx > ADX_TH and
        daily_trend_up and
        bull_cross_now
    )
    
    return can_long, {
        'adx': current_adx,
        'price': current_price,
        'ema10': float(ema10.iloc[-1]),
        'ema30': float(ema30.iloc[-1]),
        'sl_pct': sl_pct,
        'daily_trend_up': daily_trend_up,
        'bull_cross': bull_cross_now,
        'timestamp': str(close.index[-1]),
    }

def simulate_trade(coin, entry_price, size_usd, sl_pct, exit_price, exit_reason):
    """模拟交易收益计算"""
    # 入场: 下一根K线开盘价 + 0.3%滑点
    entry_with_slippage = entry_price * 1.003
    # 止损价格
    sl_price = entry_with_slippage * (1 - sl_pct)
    
    # 计算份额
    shares = size_usd / entry_with_slippage
    
    # 出场
    if exit_reason == 'stop':
        exit_with_slippage = exit_price * 0.997  # 止损滑向不利方向
    else:
        exit_with_slippage = exit_price * 1.003
    
    pnl_pct = (exit_with_slippage - entry_with_slippage) / entry_with_slippage
    pnl_usd = pnl_pct * size_usd
    
    return {
        'coin': coin,
        'entry': entry_with_slippage,
        'exit': exit_with_slippage,
        'sl_price': sl_price,
        'shares': shares,
        'size_usd': size_usd,
        'pnl_pct': pnl_pct * 100,
        'pnl_usd': pnl_usd,
        'exit_reason': exit_reason,
    }

def backtest_signal(ohlc_4h, ohlc_1d, coin, capital=INITIAL_CAPITAL):
    """
    完整回测(逐根K线模拟真实成交)
    """
    close = ohlc_4h['close']
    high = ohlc_4h['high']
    low = ohlc_4h['low']
    open_ = ohlc_4h['open']
    close_1d = ohlc_1d['close']
    
    adx = calc_adx(high, low, close, 14)
    adx_avg = adx.rolling(3).mean()
    
    ema20_1d = close_1d.ewm(span=20, adjust=False).mean()
    ema50_1d = close_1d.ewm(span=50, adjust=False).mean()
    
    ema10 = close.ewm(span=10, adjust=False).mean()
    ema30 = close.ewm(span=30, adjust=False).mean()
    
    atr_pct = ((high - low).rolling(14).mean()) / close
    
    trades = []
    equity_curve = [capital]
    timestamps = []
    
    position = None  # 当前持仓
    equity = capital
    
    for i in range(50, len(close) - 1):  # 留最后一根K线给未来
        t = close.index[i]
        t_next = close.index[i + 1]
        
        price_now = float(close.iloc[i])
        price_next_open = float(open_.iloc[i + 1])
        price_high = float(high.iloc[i])
        price_low = float(low.iloc[i])
        
        adx_val = float(adx_avg.iloc[i])
        daily_up = bool(ema20_1d.iloc[i // 6] > ema50_1d.iloc[i // 6] if i % 6 == 0 else 
                       (ema20_1d.iloc[(i-1) // 6] > ema50_1d.iloc[(i-1) // 6]))
        ema10_val = float(ema10.iloc[i])
        ema10_prev = float(ema10.iloc[i - 1])
        ema30_val = float(ema30.iloc[i])
        ema30_prev = float(ema30.iloc[i - 1])
        
        bull_cross = bool((ema10_val > ema30_val) and (ema10_prev <= ema30_prev))
        
        sl_pct = float(atr_pct.iloc[i] * SL_ATR) if not np.isnan(float(atr_pct.iloc[i])) else 0.02
        
        can_long = adx_val > ADX_TH and daily_up and bull_cross
        
        if position is None:
            # 无持仓
            if can_long:
                entry_price = price_next_open * 1.003  # 0.3%滑点
                sl_price = entry_price * (1 - sl_pct)
                position = {
                    'entry_price': entry_price,
                    'sl_price': sl_price,
                    'size_usd': equity * SIZE_PCT,
                    'entry_time': t_next,
                }
        else:
            # 有持仓, 检查止损
            hit_stop = price_low < position['sl_price']
            
            if hit_stop:
                # 止损出局
                exit_price = position['sl_price'] * 0.997
                pnl_pct = (exit_price - position['entry_price']) / position['entry_price']
                pnl_usd = pnl_pct * position['size_usd']
                equity += pnl_usd
                
                trades.append({
                    'coin': coin,
                    'entry_time': str(position['entry_time']),
                    'exit_time': str(t),
                    'entry': position['entry_price'],
                    'exit': exit_price,
                    'pnl_pct': pnl_pct * 100,
                    'pnl_usd': pnl_usd,
                    'reason': 'stop',
                })
                position = None
            elif bull_cross and adx_val < 15:
                # 反向金叉出场(ADX极低)
                exit_price = price_next_open * 0.997
                pnl_pct = (exit_price - position['entry_price']) / position['entry_price']
                pnl_usd = pnl_pct * position['size_usd']
                equity += pnl_usd
                
                trades.append({
                    'coin': coin,
                    'entry_time': str(position['entry_time']),
                    'exit_time': str(t),
                    'entry': position['entry_price'],
                    'exit': exit_price,
                    'pnl_pct': pnl_pct * 100,
                    'pnl_usd': pnl_usd,
                    'reason': 'adx_exit',
                })
                position = None
        
        equity_curve.append(equity)
        timestamps.append(t)
    
    # 最终平仓
    if position is not None:
        last_close = float(close.iloc[-1])
        exit_price = last_close * 0.997
        pnl_pct = (exit_price - position['entry_price']) / position['entry_price']
        pnl_usd = pnl_pct * position['size_usd']
        equity += pnl_usd
        trades.append({
            'coin': coin,
            'entry_time': str(position['entry_time']),
            'exit_time': str(close.index[-1]),
            'entry': position['entry_price'],
            'exit': exit_price,
            'pnl_pct': pnl_pct * 100,
            'pnl_usd': pnl_usd,
            'reason': 'final_close',
        })
    
    return {
        'trades': trades,
        'equity_curve': equity_curve,
        'timestamps': timestamps,
        'final_equity': equity,
        'total_return': (equity - capital) / capital * 100,
        'n_trades': len(trades),
    }

def generate_report(results, coin):
    """生成交易报告"""
    trades = results['trades']
    if not trades:
        return f"{coin}: 无交易"
    
    wins = [t for t in trades if t['pnl_usd'] > 0]
    losses = [t for t in trades if t['pnl_usd'] <= 0]
    
    total_pnl = sum(t['pnl_usd'] for t in trades)
    avg_win = np.mean([t['pnl_usd'] for t in wins]) if wins else 0
    avg_loss = np.mean([t['pnl_usd'] for t in losses]) if losses else 0
    pf = abs(avg_win / avg_loss) if avg_loss != 0 else 0
    wr = len(wins) / len(trades) * 100 if trades else 0
    
    # 最大回撤
    equity = results['equity_curve']
    peak = np.maximum.accumulate(equity)
    dd = np.min((equity - peak) / peak) * 100
    
    report = f"""
{coin} 模拟盘报告:
  交易次数: {len(trades)}次
  胜率: {wr:.0f}%
  PF: {pf:.2f}
  总收益: {total_pnl:+.2f} ({results['total_return']:+.1f}%)
  最大回撤: {dd:.1f}%
  平均盈利: {avg_win:+.2f}
  平均亏损: {avg_loss:+.2f}
"""
    return report

def run_full_backtest():
    """完整回测+滑点模拟"""
    print("="*60)
    print("模拟盘回测 (0.3%滑点, 真实逐K线成交)")
    print("="*60)
    print(f"初始资金: ${INITIAL_CAPITAL:,}")
    print(f"仓位: {SIZE_PCT*100:.0f}%/币")
    print(f"策略: ADX>{ADX_TH} | EMA(10/30)金叉 | ATR止损{SL_ATR}x")
    print()
    
    all_results = {}
    
    for coin in COINS:
        print(f"\n加载 {coin} 数据...")
        ohlc_4h, ohlc_1d = load_data(coin)
        close = ohlc_4h['close']
        print(f"  数据范围: {close.index[0].date()} ~ {close.index[-1].date()}")
        print(f"  总K线: {len(close)}")
        
        results = backtest_signal(ohlc_4h, ohlc_1d, coin)
        all_results[coin] = results
        
        print(f"\n{generate_report(results, coin)}")
        
        # 分年份统计
        print(f"\n{coin} 分年份:")
        print(f"  {'年份':>4} {'交易':>5} {'收益':>8} {'胜率':>6}")
        
        years = {}
        for t in results['trades']:
            yr = t['entry_time'][:4]
            if yr not in years:
                years[yr] = {'trades': [], 'pnl': 0}
            years[yr]['trades'].append(t)
            years[yr]['pnl'] += t['pnl_usd']
        
        for yr in sorted(years.keys()):
            data = years[yr]
            wins = sum(1 for t in data['trades'] if t['pnl_usd'] > 0)
            wr = wins / len(data['trades']) * 100 if data['trades'] else 0
            mark = '🟢' if data['pnl'] > 0 else '🔴'
            print(f"  {yr}: {len(data['trades']):>3}次  {data['pnl']:>+8.2f}  {wr:>5.0f}% {mark}")
    
    return all_results

def run_live_scan():
    """实时信号扫描"""
    print("="*60)
    print(f"实时信号扫描 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    for coin in COINS:
        try:
            ohlc_4h, ohlc_1d = get_live_data(coin)
            can_long, info = scan_signal(ohlc_4h, ohlc_1d)
            
            print(f"\n{coin}:")
            print(f"  最新价格: ${info['price']:,.0f}")
            print(f"  4H ADX: {info['adx']:.1f} (阈值>{ADX_TH})")
            print(f"  日线趋势: {'多头' if info['daily_trend_up'] else '空头'}")
            print(f"  4H金叉: {'是' if info['bull_cross'] else '否'}")
            print(f"  做多信号: {'✅ 是' if can_long else '❌ 否'}")
            print(f"  ATR止损: {info['sl_pct']*100:.1f}%")
            print(f"  数据时间: {info['timestamp']}")
            
        except Exception as e:
            print(f"\n{coin}: 扫描失败 - {e}")

if __name__ == '__main__':
    import sys
    
    if len(sys.argv) > 1 and sys.argv[1] == '--scan':
        run_live_scan()
    else:
        results = run_full_backtest()
        
        # 保存结果
        out_file = os.path.expanduser('~/.hermes/cron/output/sim_backtest_results.json')
        serializable = {}
        for coin, res in results.items():
            serializable[coin] = {
                'trades': res['trades'],
                'final_equity': res['final_equity'],
                'total_return': res['total_return'],
                'n_trades': res['n_trades'],
            }
        with open(out_file, 'w') as f:
            json.dump(serializable, f, indent=2, default=str)
        print(f"\n结果已保存到: {out_file}")
