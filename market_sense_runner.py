#!/usr/bin/env python3
"""
Kronos 盘感批量训练器
========================
任务：多空双向 + 赔率优先 + 全周期扫描top10主流币
要求：稳定运行、断点续跑、每批次保存结果

运行方式（每次跑一批，分3轮）：
  Round 1: BTC ETH BNB SOL DOGE
  Round 2: XRP ADA DOT AVAX LINK
  Round 3: 重新跑有潜力的品种（根据前两轮结果选）
"""
import os, sys, json, time, traceback
import numpy as np
import pandas as pd
import talib

# ====== 配置 ======
DATA_DIR = os.path.expanduser("~/Desktop/crypto_data_Pre5m")
OUT_DIR = os.path.expanduser("~/kronos/market_sense_results")
os.makedirs(OUT_DIR, exist_ok=True)

COINS = ["BTC", "ETH", "BNB", "SOL", "DOGE", "XRP", "ADA", "DOT", "AVAX", "LINK"]
TIMEFRAMES = ["1h", "4h"]
KLINE_LIMIT = 200_000  # 每次最多加载K线数

# ====== v1.0 策略参数 ======
L_RSI_THRESH    = 45
L_ADX_THRESH    = 20
S_RSI_THRESH    = 55
S_ADX_THRESH    = 25
STOP_ATR        = 1.5
PROFIT_ATR      = 2.5
COOLDOWN_HOURS  = 2   # 小时
FEE             = 0.002  # 0.2%

# ====== 工具函数 ======
def load_coin_data(coin, limit=KLINE_LIMIT):
    """加载单个币种数据，返回5分钟df"""
    # 找文件（文件名格式多样：BTC_USDT_5m, BTCUSDT, etc）
    pattern = None
    coin_uc = coin.upper()
    for f in os.listdir(DATA_DIR):
        f_uc = f.upper()
        if coin_uc in f_uc and ('USDT' in f_uc or '5M' in f_uc):
            pattern = f"{DATA_DIR}/{f}"
            break
    if not pattern or not os.path.exists(pattern):
        return None
    
    try:
        # 读CSV，处理不同格式
        with open(pattern, 'rb') as f:
            raw = f.read(200)
        
        # 检测BOM
        if raw.startswith(b'\xef\xbb\xbf'):
            pattern = pattern  # UTF-8 BOM, read_csv handles it
        elif raw.startswith(b'<'):
            print(f"  ⚠️ {coin} 文件可能是HTML，不是CSV")
            return None
        
        df = pd.read_csv(pattern, nrows=limit)
        
        # 统一列名
        cols_lower = {c.lower().strip(): c for c in df.columns}
        
        # 找时间列
        ts_col = None
        for name in ['datetime_utc', 'datetime', 'timestamp', 'date', 'time']:
            if name in cols_lower:
                ts_col = cols_lower[name]
                break
        if ts_col is None:
            # 取第一列
            ts_col = df.columns[0]
        
        df["ts"] = pd.to_datetime(df[ts_col], errors="coerce").dt.tz_localize(None)
        df = df.dropna(subset=["ts"])
        
        # 找价格列（兼容不同命名）
        result = pd.DataFrame(index=df.index)
        result["ts"] = df["ts"]  # 保留ts列
        for col_name, std_name in [('open','open'), ('high','high'), ('low','low'), ('close','close')]:
            if col_name in cols_lower:
                result[std_name] = df[cols_lower[col_name]].values
            elif std_name in cols_lower:
                result[std_name] = df[cols_lower[std_name]].values
        
        # 找成交量（多种命名）
        vol_col = None
        for name in ['volume', 'vol', 'volccy']:
            if name in cols_lower:
                vol_col = cols_lower[name]
                break
        result["volume"] = df[vol_col].values if vol_col else 0.0
        
        result = result.set_index("ts").sort_index()
        return result.tail(limit)
    except Exception as e:
        print(f"  ⚠️ {coin} 加载失败: {e}")
        traceback.print_exc()
        return None

def compute_indicators(df_5m):
    """计算所有指标，返回5m df + 1h df"""
    close = df_5m["close"].values
    
    # RSI 5m
    rsi_5m = talib.RSI(close, timeperiod=14)
    df_5m["rsi_5m"] = rsi_5m
    
    # Resample 到 15m 和 1h
    for tf in ["15min", "1h", "4h"]:
        resampled = df_5m.resample(tf).agg({
            "open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"
        }).dropna()
        
        if len(resampled) < 230:
            continue
        
        rsi = talib.RSI(resampled["close"].values, timeperiod=14)
        adx = talib.ADX(resampled["high"].values, resampled["low"].values, resampled["close"].values, timeperiod=14)
        plus_di = talib.PLUS_DI(resampled["high"].values, resampled["low"].values, resampled["close"].values, timeperiod=14)
        minus_di = talib.MINUS_DI(resampled["high"].values, resampled["low"].values, resampled["close"].values, timeperiod=14)
        atr = talib.ATR(resampled["high"].values, resampled["low"].values, resampled["close"].values, timeperiod=14)
        ma20 = talib.MA(resampled["close"].values, timeperiod=20, matype=0)
        ma50 = talib.MA(resampled["close"].values, timeperiod=50, matype=0)
        ma200 = talib.MA(resampled["close"].values, timeperiod=200, matype=0)
        
        prefix = tf.replace("min", "m").replace("h", "h")
        resampled[f"rsi_{prefix}"] = rsi
        resampled[f"adx_{prefix}"] = adx
        resampled[f"di_plus_{prefix}"] = plus_di
        resampled[f"di_minus_{prefix}"] = minus_di
        resampled[f"atr_{prefix}"] = atr
        resampled[f"ma20_{prefix}"] = ma20
        resampled[f"ma50_{prefix}"] = ma50
        resampled[f"ma200_{prefix}"] = ma200
        
        # 写回到5m（用于信号生成）
        lag_map = {"1h": 12, "4h": 48, "15min": 3}
        lag = lag_map.get(tf, 12)
        cols_1h = [f"rsi_{prefix}", f"adx_{prefix}", f"di_plus_{prefix}", f"di_minus_{prefix}",
                    f"atr_{prefix}", f"ma20_{prefix}", f"ma50_{prefix}", f"ma200_{prefix}"]
        cols_4h = [f"rsi_{prefix}", f"adx_{prefix}"]
        cols_15min = [f"rsi_{prefix}"]
        target_cols = cols_1h if tf == "1h" else (cols_4h if tf == "4h" else cols_15min)
        for col in target_cols:
            if col in resampled.columns:
                df_5m[col] = resampled[col].reindex(df_5m.index, method="ffill").shift(lag)
    
    return df_5m

def generate_signals(df):
    """生成做多做空信号（v1.0趋势跟踪逻辑）"""
    close = df["close"]
    
    # 做多：MA20>MA50>MA200 且 RSI<45 且 ADX>20
    major_up = (df["ma20_1h"] > df["ma50_1h"]) & (df["ma50_1h"] > df["ma200_1h"])
    major_down = (df["ma20_1h"] < df["ma50_1h"]) & (df["ma50_1h"] < df["ma200_1h"])
    
    long_signal = (
        major_up & 
        (df["rsi_15m"] < L_RSI_THRESH) & 
        (df["adx_1h"] > L_ADX_THRESH)
    )
    
    short_signal = (
        major_down & 
        (df["rsi_15m"] > S_RSI_THRESH) & 
        (df["adx_1h"] > S_ADX_THRESH)
    )
    
    df["long_signal"] = long_signal.fillna(False)
    df["short_signal"] = short_signal.fillna(False)
    
    return df

def backtest_direction(df, direction="long", stop_atr=STOP_ATR, profit_atr=PROFIT_ATR, fee=FEE):
    """回测单个方向，返回交易统计"""
    signal_col = f"{direction}_signal"
    if signal_col not in df.columns:
        return None
    
    signals = df[df[signal_col]].index
    if len(signals) == 0:
        return {
            "direction": direction, "signal_count": 0,
            "win_rate": 0, "avg_return": 0, "profit_factor": 0,
            "win_loss_ratio": 0, "total_return": 0, "trades": []
        }
    
    trades = []
    for idx in signals:
        # 找持仓期（下一根K线到COOLDOWN_HOURS后的第一根）
        exit_time = idx + pd.Timedelta(hours=COOLDOWN_HOURS)
        period = df.loc[idx:exit_time]
        if len(period) < 2:
            continue
        
        entry_price = period.iloc[0]["close"]
        atr = period.iloc[0].get("atr_1h", entry_price * 0.01)
        if pd.isna(atr) or atr == 0:
            atr = entry_price * 0.01
        
        stop = atr * stop_atr
        target = atr * profit_atr
        
        # 模拟持仓期内的最高/最低价
        if direction == "long":
            high_price = period["high"].max()
            low_price = period["low"].min()
            pnl = (high_price - entry_price) / entry_price  # 这里先按holder计算
            # 实际：用期内最低/最高
            pnl = (low_price - entry_price) / entry_price  # 止损触发的最低点
            # 动态止盈止损
            best_price = high_price if direction == "long" else low_price
            if best_price > 0:
                pnl = (best_price - entry_price) / entry_price
        
        # 简化：固定持有COOLDOWN_HOURS，用最后价格
        exit_price = period.iloc[-1]["close"]
        ret = (exit_price - entry_price) / entry_price - fee
        if direction == "short":
            ret = -ret - fee
        
        win = ret > 0
        trades.append({
            "entry_time": str(idx), "entry_price": entry_price,
            "exit_price": exit_price, "return": ret, "win": win,
            "atr": atr
        })
    
    if not trades:
        return {
            "direction": direction, "signal_count": len(signals),
            "win_rate": 0, "avg_return": 0, "profit_factor": 0,
            "win_loss_ratio": 0, "total_return": 0, "trades": []
        }
    
    wins = [t for t in trades if t["win"]]
    losses = [t for t in trades if not t["win"]]
    total_ret = sum(t["return"] for t in trades)
    avg_ret = np.mean([t["return"] for t in trades])
    win_rate = len(wins) / len(trades) if trades else 0
    
    avg_win = np.mean([t["return"] for t in wins]) if wins else 0
    avg_loss = abs(np.mean([t["return"] for t in losses])) if losses else 0.001
    win_loss_ratio = avg_win / avg_loss if avg_loss > 0 else 0
    profit_factor = sum(t["return"] for t in wins) / (sum(t["return"] for t in losses) + 0.0001)
    
    return {
        "direction": direction,
        "signal_count": len(signals),
        "win_rate": round(win_rate, 4),
        "avg_return": round(avg_ret, 6),
        "profit_factor": round(profit_factor, 3),
        "win_loss_ratio": round(win_loss_ratio, 3),
        "total_return": round(total_ret, 4),
        "avg_win": round(avg_win, 6) if wins else 0,
        "avg_loss": round(avg_loss, 6) if losses else 0,
        "trades_count": len(trades),
        "wins": len(wins), "losses": len(losses)
    }

def run_round(round_name, coins, save_every=2):
    """运行一轮扫描，分批保存"""
    results = {}
    total = len(coins)
    
    print(f"\n{'='*60}")
    print(f"  Round: {round_name}")
    print(f"  Coins: {coins}")
    print(f"  Timeframes: {TIMEFRAMES}")
    print(f"{'='*60}")
    
    checkpoint_file = f"{OUT_DIR}/{round_name}_checkpoint.json"
    
    # 读断点
    if os.path.exists(checkpoint_file):
        with open(checkpoint_file) as f:
            results = json.load(f)
        print(f"📂 断点续跑，已完成: {len(results)} 个任务")
    
    for i, coin in enumerate(coins):
        for tf in TIMEFRAMES:
            task_id = f"{coin}_{tf}"
            if task_id in results:
                print(f"  ⏭️  [{i+1}/{total}] {task_id} 已完成，跳过")
                continue
            
            print(f"  🔄 [{i+1}/{total}] {task_id}...", end=" ", flush=True)
            try:
                start = time.time()
                
                # 加载数据
                df = load_coin_data(coin)
                if df is None or len(df) < 1000:
                    print(f"❌ 数据不足")
                    results[task_id] = {"error": "insufficient data"}
                    continue
                
                # 计算指标
                warmup = 230
                df = compute_indicators(df)
                
                # 生成信号
                df = generate_signals(df)
                df = df.dropna(subset=["rsi_15m", "adx_1h"])
                
                if len(df) < warmup:
                    print(f"❌ 指标数据不足")
                    results[task_id] = {"error": "indicator warmup failed"}
                    continue
                
                # 回测
                long_result = backtest_direction(df, "long")
                short_result = backtest_direction(df, "short")
                
                elapsed = time.time() - start
                result = {
                    "coin": coin, "timeframe": tf,
                    "data_rows": len(df),
                    "long": long_result, "short": short_result,
                    "elapsed_sec": round(elapsed, 1)
                }
                results[task_id] = result
                
                lw = long_result
                sw = short_result
                print(f"✅ long({lw.get('signal_count',0)}信号 胜率{lw.get('win_rate',0)*100:.1f}% WLR{lw.get('win_loss_ratio',0):.2f}) "
                      f"short({sw.get('signal_count',0)}信号 胜率{sw.get('win_rate',0)*100:.1f}% WLR{sw.get('win_loss_ratio',0):.2f}) "
                      f"[{elapsed:.1f}s]")
                
                # 每2个保存一次断点
                if len(results) % save_every == 0:
                    with open(checkpoint_file, "w") as f:
                        json.dump(results, f)
                    print(f"  💾 断点已保存")
                
            except Exception as e:
                print(f"❌ 异常: {e}")
                results[task_id] = {"error": str(e)}
                traceback.print_exc()
    
    # 最终保存
    with open(checkpoint_file, "w") as f:
        json.dump(results, f)
    
    # 保存汇总
    summary = summarize_results(results)
    summary_file = f"{OUT_DIR}/{round_name}_summary.json"
    with open(summary_file, "w") as f:
        json.dump(summary, f, indent=2, default=str)
    
    print(f"\n📊 Round {round_name} 完成!")
    print_summary(summary)
    
    return results, summary

def summarize_results(results):
    """汇总所有结果"""
    summary = {"long": [], "short": [], "overall": {}}
    
    long_signals = sum(r.get("long", {}).get("signal_count", 0) for r in results.values() if "error" not in r)
    short_signals = sum(r.get("short", {}).get("signal_count", 0) for r in results.values() if "error" not in r)
    total_long_ret = sum(r.get("long", {}).get("total_return", 0) for r in results.values() if "error" not in r)
    total_short_ret = sum(r.get("short", {}).get("total_return", 0) for r in results.values() if "error" not in r)
    
    # 按coin汇总
    by_coin = {}
    for task_id, r in results.items():
        if "error" in r:
            continue
        coin = r["coin"]
        if coin not in by_coin:
            by_coin[coin] = {"long_signals": 0, "short_signals": 0, "long_ret": 0, "short_ret": 0,
                             "long_wr": [], "short_wr": [], "tasks": []}
        by_coin[coin]["long_signals"] += r["long"].get("signal_count", 0)
        by_coin[coin]["short_signals"] += r["short"].get("signal_count", 0)
        by_coin[coin]["long_ret"] += r["long"].get("total_return", 0)
        by_coin[coin]["short_ret"] += r["short"].get("total_return", 0)
        if r["long"].get("win_rate", 0) > 0:
            by_coin[coin]["long_wr"].append(r["long"]["win_rate"])
        if r["short"].get("win_rate", 0) > 0:
            by_coin[coin]["short_wr"].append(r["short"]["win_rate"])
        by_coin[coin]["tasks"].append(task_id)
    
    for coin, d in by_coin.items():
        avg_long_wr = np.mean(d["long_wr"]) if d["long_wr"] else 0
        avg_short_wr = np.mean(d["short_wr"]) if d["short_wr"] else 0
        print(f"\n  {coin}: long={d['long_signals']}信号({avg_long_wr*100:.1f}%胜率, {d['long_ret']*100:.1f}%总收益) "
              f"short={d['short_signals']}信号({avg_short_wr*100:.1f}%胜率, {d['short_ret']*100:.1f}%总收益)")
    
    summary = {
        "total_tasks": len(results),
        "total_long_signals": long_signals,
        "total_short_signals": short_signals,
        "total_long_return": round(total_long_ret, 4),
        "total_short_return": round(total_short_ret, 4),
        "by_coin": by_coin,
        "details": results
    }
    return summary

def print_summary(summary):
    """打印汇总"""
    print(f"\n{'='*60}")
    print(f"  汇总")
    print(f"  做多总信号: {summary['total_long_signals']} | 总收益: {summary['total_long_return']*100:.1f}%")
    print(f"  做空总信号: {summary['total_short_signals']} | 总收益: {summary['total_short_return']*100:.1f}%")
    print(f"{'='*60}")

if __name__ == "__main__":
    round_name = sys.argv[1] if len(sys.argv) > 1 else "round1"
    coin_arg = sys.argv[2] if len(sys.argv) > 2 else ""
    
    # Round分配
    ROUNDS = {
        "round1": ["BTC", "ETH", "BNB", "SOL", "DOGE"],
        "round2": ["XRP", "ADA", "DOT", "AVAX", "LINK"],
        "round3": [],  # 根据前两轮结果动态决定
    }
    
    coins = ROUNDS.get(round_name, [])
    if coin_arg:
        coins = coin_arg.split(",")
    
    if not coins:
        print("用法: python market_sense_runner.py <round1|round2|round3> [BTC,ETH,...]")
        print(f"可用轮次: {list(ROUNDS.keys())}")
        sys.exit(1)
    
    print(f"🚀 开始 {round_name}，币种: {coins}")
    results, summary = run_round(round_name, coins)
    print(f"\n✅ {round_name} 完成！结果保存在 {OUT_DIR}/")
