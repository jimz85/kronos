#!/usr/bin/env python3
"""
live_monitor.py
实时市场监控 + 信号触发

每分钟检查市场状态和信号：
1. 获取最新价格和RSI
2. 检测市场状态（趋势/震荡）
3. 当BTC RSI < 35时发出买入告警
4. 当BTC RSI > 65时发出卖出告警
5. 所有告警记录到文件 + 打印到终端
"""
import yfinance as yf
import numpy as np
import pandas as pd
import time
import json
from datetime import datetime
from pathlib import Path

# ─── 指标计算 ───────────────────────────────────────────────
def calc_rsi(prices, period=14):
    prices = np.asarray(prices).flatten()
    deltas = np.diff(prices, prepend=prices[0])
    gains = np.where(deltas > 0, deltas, 0)
    losses = np.where(deltas < 0, -deltas, 0)
    avg_gain = pd.Series(gains).rolling(period).mean()
    avg_loss = pd.Series(losses).rolling(period).mean()
    rs = avg_gain / (avg_loss + 1e-10)
    return 100 - (100 / (1 + rs))

def calc_bollinger(prices, period=20, std_mult=2.5):
    ma = pd.Series(prices).rolling(period).mean()
    std = pd.Series(prices).rolling(period).std()
    return ma, ma + std_mult * std, ma - std_mult * std

def detect_regime(df, lookback=60):
    """检测市场状态"""
    closes = df["close"].values
    if len(closes) < lookback:
        return "unknown"
    
    ma20 = np.mean(closes[-20:])
    ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else ma20
    current = closes[-1]
    
    # 趋势检测
    if current > ma20 > ma60:
        trend = "up"
    elif current < ma20 < ma60:
        trend = "down"
    else:
        trend = "neutral"
    
    # 波动率检测
    returns = np.diff(np.log(closes[-20:]))
    vol = np.std(returns)
    hist_vol = np.std(returns)
    vol_ratio = vol / (hist_vol + 1e-10)
    
    if vol_ratio > 1.5:
        vol_regime = "high"
    elif vol_ratio < 0.5:
        vol_regime = "low"
    else:
        vol_regime = "normal"
    
    # 震荡检测（布林带宽度）
    ma, bb_upper, bb_lower = calc_bollinger(closes)
    if len(closes) >= 60:
        recent_closes = closes[-60:]
        bb_width = (bb_upper.iloc[-1] - bb_lower.iloc[-1]) / ma.iloc[-1]
        avg_width = np.mean([(calc_bollinger(closes[:i])[0] - calc_bollinger(closes[:i])[2]).iloc[-1] / np.mean(closes[:i]) for i in range(60, len(closes))])
        range_bound = bb_width / (avg_width + 1e-10)
    else:
        range_bound = 0.5
    
    if range_bound > 1.3:
        regime = "RANGE_BOUND"
    elif trend == "up":
        regime = "TREND_UP"
    elif trend == "down":
        regime = "TREND_DOWN"
    else:
        regime = "NEUTRAL"
    
    return regime, trend, vol_regime, range_bound

# ─── 获取市场数据 ────────────────────────────────────────────
def get_market_data(coins=["BTC-USD", "ETH-USD", "BNB-USD", "SOL-USD"]):
    """获取多个币种的数据"""
    data = {}
    for coin in coins:
        try:
            t = yf.Ticker(coin)
            df = t.history(period="90d")
            if df.empty:
                continue
            df = df[["Close"]].copy()
            df.columns = ["close"]
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
            data[coin] = df
        except Exception as e:
            print(f"  数据获取失败 {coin}: {e}")
    return data

# ─── 计算信号 ────────────────────────────────────────────────
def calculate_signals(data):
    """计算各币种的信号"""
    signals = {}
    for coin, df in data.items():
        closes = df["close"].values
        rsi = calc_rsi(closes)
        current_rsi = float(rsi.iloc[-1])
        current_price = float(closes[-1])
        
        ma, bb_upper, bb_lower = calc_bollinger(closes)
        current_bb_upper = float(bb_upper.iloc[-1])
        current_bb_lower = float(bb_lower.iloc[-1])
        
        # 状态
        regime_info = detect_regime(df)
        if isinstance(regime_info, tuple):
            regime, trend, vol, range_bound = regime_info
        else:
            regime = regime_info
            trend = "neutral"
            vol = "normal"
            range_bound = 0.5
        
        signals[coin] = {
            "price": current_price,
            "rsi": current_rsi,
            "bb_upper": current_bb_upper,
            "bb_lower": current_bb_lower,
            "bb_position": (current_price - current_bb_lower) / (current_bb_upper - current_bb_lower + 1e-10),
            "regime": regime,
            "trend": trend,
            "volatility": vol,
            "range_bound": range_bound
        }
    return signals

# ─── 信号判断 ────────────────────────────────────────────────
def check_signals(signals):
    """检查是否有触发信号"""
    alerts = []
    
    for coin, s in signals.items():
        # RSI超卖信号
        if s["rsi"] < 35:
            alerts.append({
                "type": "RSI_OVERSOLD",
                "coin": coin,
                "level": "BUY",
                "price": s["price"],
                "rsi": s["rsi"],
                "message": f"RSI 超卖: {s['rsi']:.1f} < 35，当前价 {s['price']:.2f}"
            })
        # RSI超买信号
        elif s["rsi"] > 65:
            alerts.append({
                "type": "RSI_OVERBOUGHT",
                "coin": coin,
                "level": "SELL",
                "price": s["price"],
                "rsi": s["rsi"],
                "message": f"RSI 超买: {s['rsi']:.1f} > 65，当前价 {s['price']:.2f}"
            })
        # 中性信号
        else:
            pass  # 不告警
        
        # 布林带突破信号
        if s["price"] > s["bb_upper"]:
            alerts.append({
                "type": "BB_UPPER_BREAK",
                "coin": coin,
                "level": "WARN",
                "price": s["price"],
                "bb_upper": s["bb_upper"],
                "message": f"突破布林上轨: {s['price']:.2f} > {s['bb_upper']:.2f}"
            })
        elif s["price"] < s["bb_lower"]:
            alerts.append({
                "type": "BB_LOWER_BREAK",
                "coin": coin,
                "level": "WARN",
                "price": s["price"],
                "bb_lower": s["bb_lower"],
                "message": f"突破布林下轨: {s['price']:.2f} < {s['bb_lower']:.2f}"
            })
    
    return alerts

# ─── 保存告警 ────────────────────────────────────────────────
def save_alert(alert):
    """保存告警到文件"""
    alert_file = Path(__file__).parent / "triggered_alerts.json"
    alerts = []
    if alert_file.exists():
        with open(alert_file) as f:
            alerts = json.load(f)
    alert["timestamp"] = datetime.now().isoformat()
    alert["id"] = f"{alert['coin']}_{alert['type']}_{int(time.time())}"
    alerts.append(alert)
    # 只保留最近100条
    alerts = alerts[-100:]
    with open(alert_file, "w") as f:
        json.dump(alerts, f, indent=2, ensure_ascii=False)

# ─── 主监控循环 ───────────────────────────────────────────────
def run_monitor(check_interval=60):
    """运行监控循环"""
    print("="*60)
    print("  Kronos 实时市场监控")
    print("="*60)
    print(f"  监控币种: BTC-USD, ETH-USD, BNB-USD, SOL-USD")
    print(f"  检查间隔: {check_interval}秒")
    print(f"  启动时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*60)
    
    last_alert_times = {}  # 冷却追踪
    
    while True:
        try:
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            
            # 获取数据
            data = get_market_data()
            if not data:
                print(f"[{ts}] 数据获取失败，10秒后重试...")
                time.sleep(10)
                continue
            
            # 计算信号
            signals = calculate_signals(data)
            
            # 检查信号
            alerts = check_signals(signals)
            
            # 过滤冷却期内的告警（同一币种同一类型，冷却1小时）
            now = time.time()
            filtered_alerts = []
            for a in alerts:
                key = f"{a['coin']}_{a['type']}"
                last_time = last_alert_times.get(key, 0)
                if now - last_time > 3600:  # 1小时冷却
                    filtered_alerts.append(a)
                    last_alert_times[key] = now
                    save_alert(a)
            
            # 打印状态
            print(f"\n[{ts}]")
            for coin, s in signals.items():
                regime_icon = {"TREND_UP": "↑↑", "TREND_DOWN": "↓↓", "RANGE_BOUND": "↔", "NEUTRAL": "～"}.get(s["regime"], "??")
                rsi_color = "🟢" if s["rsi"] < 35 else "🔴" if s["rsi"] > 65 else "🟡"
                print(f"  {coin}: {s['price']:>10.2f}  RSI={s['rsi']:>5.1f}{rsi_color}  状态={regime_icon} {s['regime']}")
            
            # 打印新告警
            for a in filtered_alerts:
                level_icon = {"BUY": "🟢买入信号", "SELL": "🔴卖出信号", "WARN": "⚠️告警"}.get(a["level"], "❓")
                print(f"\n  {level_icon}: {a['message']}")
            
            if not filtered_alerts:
                print("  (无新信号)")
            
        except KeyboardInterrupt:
            print("\n监控已停止")
            break
        except Exception as e:
            print(f"\n错误: {e}")
        
        time.sleep(check_interval)

# ─── 单次检查（不循环）───────────────────────────────────────
def check_once():
    """只检查一次，不循环"""
    data = get_market_data()
    if not data:
        print("数据获取失败")
        return
    
    signals = calculate_signals(data)
    alerts = check_signals(signals)
    
    print("="*60)
    print("  市场状态检查")
    print("="*60)
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"  检查时间: {ts}")
    print()
    
    for coin, s in signals.items():
        regime_icon = {"TREND_UP": "↑↑", "TREND_DOWN": "↓↓", "RANGE_BOUND": "↔", "NEUTRAL": "～"}.get(s["regime"], "??")
        rsi_color = "🟢" if s["rsi"] < 35 else "🔴" if s["rsi"] > 65 else "🟡"
        vol_icon = {"high": "⚡", "low": "～", "normal": "·"}.get(s["volatility"], "?")
        
        regime_str = {
            "TREND_UP": "上升趋势",
            "TREND_DOWN": "下降趋势",
            "RANGE_BOUND": "震荡市场",
            "NEUTRAL": "中性"
        }.get(s["regime"], s["regime"])
        
        print(f"{coin}:")
        print(f"  价格: {s['price']:.2f}")
        print(f"  RSI:  {s['rsi']:.1f}{rsi_color} ({'超卖' if s['rsi']<35 else '超买' if s['rsi']>65 else '中性'})")
        print(f"  布林: 上={s['bb_upper']:.2f} 下={s['bb_lower']:.2f}")
        print(f"  状态: {regime_icon} {regime_str} {vol_icon}")
        print()
    
    if alerts:
        print("触发信号:")
        for a in alerts:
            level_icon = {"BUY": "🟢买入", "SELL": "🔴卖出", "WARN": "⚠️"}.get(a["level"], "❓")
            print(f"  {level_icon}: {a['message']}")
    else:
        print("无触发信号")

if __name__ == "__main__":
    import sys
    if "--loop" in sys.argv:
        run_monitor()
    else:
        check_once()
