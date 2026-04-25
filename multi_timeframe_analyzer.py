#!/usr/bin/env python3
"""
multi_timeframe_analyzer.py
多周期综合分析系统

三层确认逻辑：
1. 大周期（日线/4小时）：确定趋势方向
2. 中周期（1小时）：寻找入场时机
3. 小周期（15分钟）：精确入场点位

核心理念：
- 不预测方向，只确认方向
- 大周期决定做什么（做多/做空/观望）
- 小周期决定怎么做（何时入场）
"""

import numpy as np
import pandas as pd
import yfinance as yf
import json
from datetime import datetime
from pathlib import Path
import warnings
warnings.filterwarnings("ignore")

KRONOS = Path.home() / "kronos"

# ═══════════════════════════════════════════════════════════
# 指标计算
# ═══════════════════════════════════════════════════════════

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

def calc_bollinger(prices, period=20, std_mult=2.0):
    prices = np.asarray(prices).flatten()
    ma = pd.Series(prices).rolling(period).mean()
    std = pd.Series(prices).rolling(period).std()
    return ma, ma + std_mult * std, ma - std_mult * std

# ═══════════════════════════════════════════════════════════
# 获取多周期数据
# ═══════════════════════════════════════════════════════════

def get_multi_timeframe_data(coin="BTC-USD"):
    """获取15m, 1h, 4h, 1d四个周期的数据"""
    t = yf.Ticker(coin)
    
    data = {}
    
    # 1天 = 96个15分钟K线，30天 = ~2880根
    # 1天 = 24根1小时K线
    # 1天 = 6根4小时K线
    # 获取足够历史数据
    
    try:
        # 15分钟K线
        df_15m = t.history(period="30d", interval="15m")
        data["15m"] = df_15m
        
        # 1小时K线
        df_1h = t.history(period="60d", interval="1h")
        data["1h"] = df_1h
        
        # 4小时K线
        df_4h = t.history(period="90d", interval="4h")
        data["4h"] = df_4h
        
        # 日K线
        df_1d = t.history(period="2y", interval="1d")
        data["1d"] = df_1d
        
    except Exception as e:
        print(f"数据获取错误: {e}")
        return None
    
    return data

# ═══════════════════════════════════════════════════════════
# 分析单个周期
# ═══════════════════════════════════════════════════════════

def analyze_timeframe(df, name):
    """分析单个周期的市场状态"""
    if df is None or df.empty:
        return None
    
    p = np.asarray(df["Close"].values).flatten()
    h = np.asarray(df["High"].values).flatten()
    l = np.asarray(df["Low"].values).flatten()
    
    # 均线
    ma20 = calc_ma(p, 20)
    ma50 = calc_ma(p, 50)
    
    # RSI
    rsi = calc_rsi(p)
    
    # 布林带
    ma, bb_up, bb_low = calc_bollinger(p)
    
    # ATR
    atr = calc_atr(h, l, p)
    
    # 最新值
    current_price = float(p[-1])
    current_rsi = float(rsi.iloc[-1])
    current_ma20 = float(ma20.iloc[-1])
    current_ma50 = float(ma50.iloc[-1]) if len(ma50) >= 50 else current_ma20
    current_bb_up = float(bb_up.iloc[-1])
    current_bb_low = float(bb_low.iloc[-1])
    current_atr = float(atr.iloc[-1])
    
    # 趋势判断
    if current_price > current_ma20 and current_ma20 > current_ma50:
        trend = "UP"
    elif current_price < current_ma20 and current_ma20 < current_ma50:
        trend = "DOWN"
    else:
        trend = "RANGE"
    
    # 相对位置 (0-100%, 50% = 布林带中轨)
    bb_pos = (current_price - current_bb_low) / (current_bb_up - current_bb_low + 1e-10) * 100
    
    # RSI状态
    if current_rsi < 30:
        rsi_state = "OVERSOLD"
    elif current_rsi > 70:
        rsi_state = "OVERBOUGHT"
    elif current_rsi < 45:
        rsi_state = "BEARISH"
    elif current_rsi > 55:
        rsi_state = "BULLISH"
    else:
        rsi_state = "NEUTRAL"
    
    return {
        "name": name,
        "price": current_price,
        "rsi": current_rsi,
        "rsi_state": rsi_state,
        "trend": trend,
        "ma20": current_ma20,
        "ma50": current_ma50,
        "bb_up": current_bb_up,
        "bb_low": current_bb_low,
        "bb_position": bb_pos,
        "atr": current_atr,
        "atr_pct": current_atr / current_price * 100,
        "bull_bear_score": _calc_bull_bear_score(current_price, current_ma20, current_ma50, current_rsi, bb_pos)
    }

def _calc_bull_bear_score(price, ma20, ma50, rsi, bb_pos):
    """
    计算多空得分 (-100 到 +100)
    正数 = 看多，负数 = 看空
    """
    score = 0
    
    # 均线排列 (+20/-20)
    if price > ma20 > ma50:
        score += 20
    elif price > ma20 and ma20 <= ma50:
        score += 10
    elif price < ma20 < ma50:
        score -= 20
    elif price < ma20 and ma20 >= ma50:
        score -= 10
    
    # RSI (+20/-20)
    if rsi < 30:
        score += 15
    elif rsi < 40:
        score += 5
    elif rsi > 70:
        score -= 15
    elif rsi > 60:
        score -= 5
    
    # 布林带位置 (+10/-10)
    if bb_pos < 20:
        score += 10
    elif bb_pos < 40:
        score += 3
    elif bb_pos > 80:
        score -= 10
    elif bb_pos > 60:
        score -= 3
    
    return max(-100, min(100, score))

# ═══════════════════════════════════════════════════════════
# 多周期综合判断
# ═══════════════════════════════════════════════════════════

def multi_timeframe_analysis(coin="BTC-USD"):
    """
    多周期综合分析
    
    决策逻辑：
    1. 大周期趋势决定操作方向
    2. 中周期形态确认入场时机
    3. 小周期点位精确执行
    """
    print("="*70)
    print(f"  多周期综合分析 - {coin}")
    print(f"  时间: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("="*70)
    
    data = get_multi_timeframe_data(coin)
    if data is None:
        print("数据获取失败")
        return None
    
    # 分析各周期
    tf_analysis = {}
    for tf_name, df in data.items():
        if df is not None and not df.empty:
            tf_analysis[tf_name] = analyze_timeframe(df, tf_name)
    
    if not tf_analysis:
        print("没有可用的周期数据")
        return None
    
    # 打印各周期分析
    print("\n【各周期状态】")
    print("-"*70)
    
    tf_priority = ["1d", "4h", "1h", "15m"]
    for tf in tf_priority:
        if tf in tf_analysis:
            a = tf_analysis[tf]
            trend_icon = {"UP": "📈", "DOWN": "📉", "RANGE": "📊"}.get(a["trend"], "❓")
            rsi_icon = {"OVERSOLD": "🟢", "OVERBOUGHT": "🔴", "BEARISH": "🔴", "BULLISH": "🟢", "NEUTRAL": "🟡"}.get(a["rsi_state"], "🟡")
            
            print(f"  {tf:>4}: ${a['price']:>10.0f} | {trend_icon} {a['trend']:>6} | "
                  f"RSI={a['rsi']:>5.1f}{rsi_icon} | BB位={a['bb_position']:>5.0f}% | "
                  f"多空分={a['bull_bear_score']:>+5.0f}")
    
    # 综合判断
    print("\n【综合判断】")
    print("-"*70)
    
    # 大周期权重
    weights = {"1d": 0.40, "4h": 0.30, "1h": 0.20, "15m": 0.10}
    
    # 加权多空得分
    weighted_score = 0
    total_weight = 0
    trend_confirmation = 0
    
    for tf, weight in weights.items():
        if tf in tf_analysis:
            a = tf_analysis[tf]
            weighted_score += a["bull_bear_score"] * weight
            total_weight += weight
            
            # 趋势确认度
            if a["trend"] == "UP":
                trend_confirmation += weight
            elif a["trend"] == "DOWN":
                trend_confirmation -= weight
    
    final_score = weighted_score / total_weight if total_weight > 0 else 0
    trend_direction = "UP" if trend_confirmation > 0.3 else "DOWN" if trend_confirmation < -0.3 else "RANGE"
    
    # 方向确认度
    confirmation_pct = abs(trend_confirmation) * 100
    
    print(f"  趋势方向: {trend_direction} (确认度: {confirmation_pct:.0f}%)")
    print(f"  多空得分: {final_score:+.0f} (范围 -100 到 +100)")
    print(f"  趋势确认: 多头排列 +{trend_confirmation*100:.0f}% / 空头排列 {abs(trend_confirmation)*100:.0f}%")
    
    # 交易信号判断
    print("\n【交易信号】")
    print("-"*70)
    
    signals = []
    
    # 方向信号
    if trend_direction == "UP" and final_score > 10:
        signals.append(("做多信号", "STRONG", f"多周期确认上涨，得分{final_score:.0f}"))
    elif trend_direction == "UP" and final_score > 0:
        signals.append(("偏多", "BULLISH", f"方向向上，得分{final_score:.0f}"))
    elif trend_direction == "DOWN" and final_score < -10:
        signals.append(("做空信号", "STRONG", f"多周期确认下跌，得分{final_score:.0f}"))
    elif trend_direction == "DOWN" and final_score < 0:
        signals.append(("偏空", "BEARISH", f"方向向下，得分{final_score:.0f}"))
    else:
        signals.append(("观望", "NEUTRAL", f"方向不明，等待确认"))
    
    # RSI极值信号
    if "1h" in tf_analysis:
        rsi_1h = tf_analysis["1h"]["rsi"]
        rsi_state = tf_analysis["1h"]["rsi_state"]
        if rsi_state == "OVERSOLD" and trend_direction == "UP":
            signals.append(("回踩买入", "BUY", f"1H RSI={rsi_1h:.1f}超卖，上升趋势中"))
        elif rsi_state == "OVERBOUGHT" and trend_direction == "DOWN":
            signals.append(("反弹做空", "SELL", f"1H RSI={rsi_1h:.1f}超买，下降趋势中"))
    
    # 布林带信号
    if "15m" in tf_analysis:
        bb_pos = tf_analysis["15m"]["bb_position"]
        if bb_pos < 15:
            signals.append(("15M超卖", "BUY", f"BB位置={bb_pos:.0f}%"))
        elif bb_pos > 85:
            signals.append(("15M超买", "SELL", f"BB位置={bb_pos:.0f}%"))
    
    # 打印信号
    for signal_name, signal_type, signal_desc in signals:
        type_icon = {"STRONG": "✅", "BUY": "🟢", "BULLISH": "🟡", 
                     "SELL": "🔴", "BEARISH": "🟡", "NEUTRAL": "⚪"}.get(signal_type, "❓")
        print(f"  {type_icon} {signal_name}: {signal_desc}")
    
    # 入场/出场建议
    print("\n【操作建议】")
    print("-"*70)
    
    primary_signal = signals[0] if signals else None
    
    if primary_signal:
        name, stype, desc = primary_signal
        
        if stype in ["STRONG", "BUY"]:
            # 做多建议
            if "15m" in tf_analysis:
                entry = tf_analysis["15m"]["price"]
                stop = entry * (1 - tf_analysis["15m"]["atr_pct"] / 100 * 1.5)
                target = entry * (1 + tf_analysis["15m"]["atr_pct"] / 100 * 3)
                print(f"  🟢 做多")
                print(f"  建议入场: ${entry:.0f}")
                print(f"  止损: ${stop:.0f} ({-((1-stop/entry))*100:.1f}%)")
                print(f"  目标: ${target:.0f} ({((target/entry)-1)*100:.1f}%)")
                print(f"  仓位: 建议20-30%本金，2x杠杆")
        elif stype in ["BEARISH", "SELL"]:
            if "15m" in tf_analysis:
                entry = tf_analysis["15m"]["price"]
                stop = entry * (1 + tf_analysis["15m"]["atr_pct"] / 100 * 1.5)
                target = entry * (1 - tf_analysis["15m"]["atr_pct"] / 100 * 3)
                print(f"  🔴 做空")
                print(f"  建议入场: ${entry:.0f}")
                print(f"  止损: ${stop:.0f} ({((stop/entry)-1)*100:.1f}%)")
                print(f"  目标: ${target:.0f} ({-((1-target/entry))*100:.1f}%)")
                print(f"  仓位: 建议20-30%本金，2x杠杆")
        else:
            print(f"  ⚪ 观望，等待明确信号")
    
    # 风险提示
    print("\n【风险提示】")
    print("-"*70)
    
    # 检查各周期是否一致
    up_count = sum(1 for tf, a in tf_analysis.items() if a["trend"] == "UP")
    down_count = sum(1 for tf, a in tf_analysis.items() if a["trend"] == "DOWN")
    
    if up_count >= 3:
        print("  ⚠️ 多周期高度一致，看涨过于集中，注意回调风险")
    elif down_count >= 3:
        print("  ⚠️ 多周期高度一致，看跌过于集中，注意反弹风险")
    
    if abs(trend_confirmation) < 0.5:
        print("  ⚠️ 周期方向不一致，建议观望或轻仓")
    
    # 保存分析结果
    result = {
        "coin": coin,
        "timestamp": datetime.now().isoformat(),
        "timeframes": {tf: {
            "price": a["price"],
            "rsi": a["rsi"],
            "trend": a["trend"],
            "bb_position": a["bb_position"],
            "bull_bear_score": a["bull_bear_score"]
        } for tf, a in tf_analysis.items()},
        "summary": {
            "trend_direction": trend_direction,
            "confirmation": confirmation_pct,
            "score": final_score,
            "primary_signal": primary_signal[1] if primary_signal else "NEUTRAL"
        }
    }
    
    return result

# ═══════════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    if len(sys.argv) > 1:
        coin = sys.argv[1]
    else:
        coin = "BTC-USD"
    
    result = multi_timeframe_analysis(coin)
    
    if result:
        # 保存结果
        result_file = KRONOS / "multi_tf_analysis.json"
        with open(result_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n✅ 分析结果已保存到 {result_file}")