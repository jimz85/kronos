#!/usr/bin/env python3
"""
multi_timeframe_trading_system.py
多周期综合交易系统

使用方法：
python3 multi_timeframe_trading_system.py BTC-USD

逻辑：
1. 多周期趋势确认 → 确定方向
2. 入场时机 → 等待回调
3. 风险管理 → 固定止损+跟踪止盈
4. 杠杆 → 2x放大收益
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
# 多周期分析
# ═══════════════════════════════════════════════════════════

def get_timeframe_analysis(df, name):
    """分析单个周期的市场状态"""
    if df is None or df.empty:
        return None
    
    p = np.asarray(df["Close"].values).flatten()
    h = np.asarray(df["High"].values).flatten()
    l = np.asarray(df["Low"].values).flatten()
    
    ma20 = calc_ma(p, 20)
    ma50 = calc_ma(p, 50)
    rsi = calc_rsi(p)
    atr = calc_atr(h, l, p)
    ma, bb_up, bb_low = calc_bollinger(p)
    
    current_price = float(p[-1])
    current_rsi = float(rsi.iloc[-1])
    current_ma20 = float(ma20.iloc[-1])
    current_ma50 = float(ma50.iloc[-1]) if len(ma50) >= 50 else current_ma20
    current_bb_up = float(bb_up.iloc[-1])
    current_bb_low = float(bb_low.iloc[-1])
    current_atr = float(atr.iloc[-1])
    
    # 趋势
    if current_price > current_ma20 and current_ma20 > current_ma50:
        trend = "UP"
    elif current_price < current_ma20 and current_ma20 < current_ma50:
        trend = "DOWN"
    else:
        trend = "RANGE"
    
    # 得分
    score = 0
    if current_price > current_ma20 > current_ma50:
        score += 20
    elif current_price > current_ma20:
        score += 5
    elif current_price < current_ma20 < current_ma50:
        score -= 20
    elif current_price < current_ma20:
        score -= 5
    
    if current_rsi < 30:
        score += 15
    elif current_rsi < 40:
        score += 5
    elif current_rsi > 70:
        score -= 15
    elif current_rsi > 60:
        score -= 5
    
    bb_pos = (current_price - current_bb_low) / (current_bb_up - current_bb_low + 1e-10) * 100
    if bb_pos < 20:
        score += 10
    elif bb_pos > 80:
        score -= 10
    
    return {
        "name": name,
        "price": current_price,
        "rsi": current_rsi,
        "trend": trend,
        "bb_position": bb_pos,
        "atr": current_atr,
        "atr_pct": current_atr / current_price * 100,
        "score": max(-100, min(100, score)),
        "ma20": current_ma20,
        "ma50": current_ma50,
    }

def get_multi_timeframe_data(coin):
    """获取多周期数据"""
    t = yf.Ticker(coin)
    
    data = {}
    try:
        data["15m"] = t.history(period="10d", interval="15m")
        data["1h"] = t.history(period="30d", interval="1h")
        data["4h"] = t.history(period="60d", interval="4h")
        data["1d"] = t.history(period="1y", interval="1d")
    except:
        return None
    
    return data

def analyze(coin="BTC-USD"):
    """完整的多周期分析"""
    
    data = get_multi_timeframe_data(coin)
    if not data:
        return None
    
    # 分析各周期
    tf_data = {}
    for tf_name, df in data.items():
        if df is not None and not df.empty:
            tf_data[tf_name] = get_timeframe_analysis(df, tf_name)
    
    if not tf_data:
        return None
    
    # 权重: 大周期更重要
    weights = {"1d": 0.35, "4h": 0.30, "1h": 0.25, "15m": 0.10}
    
    # 计算综合得分
    total_score = 0
    total_weight = 0
    trend_votes = {"UP": 0, "DOWN": 0, "RANGE": 0}
    
    for tf, weight in weights.items():
        if tf in tf_data:
            a = tf_data[tf]
            total_score += a["score"] * weight
            total_weight += weight
            trend_votes[a["trend"]] += weight
    
    final_score = total_score / total_weight if total_weight > 0 else 0
    
    # 趋势方向 = 多数周期方向
    if trend_votes["UP"] > trend_votes["DOWN"]:
        direction = "LONG"
        confidence = trend_votes["UP"] / (trend_votes["UP"] + trend_votes["DOWN"]) * 100
    elif trend_votes["DOWN"] > trend_votes["UP"]:
        direction = "SHORT"
        confidence = trend_votes["DOWN"] / (trend_votes["UP"] + trend_votes["DOWN"]) * 100
    else:
        direction = "NEUTRAL"
        confidence = 50
    
    # 得分阈值判断
    if final_score > 15 and direction == "LONG":
        signal = "STRONG_LONG"
    elif final_score > 5 and direction == "LONG":
        signal = "LONG"
    elif final_score < -15 and direction == "SHORT":
        signal = "STRONG_SHORT"
    elif final_score < -5 and direction == "SHORT":
        signal = "SHORT"
    else:
        signal = "WAIT"
    
    return {
        "coin": coin,
        "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "signal": signal,
        "direction": direction,
        "confidence": confidence,
        "score": final_score,
        "timeframes": {tf: {
            "price": f"${a['price']:.0f}",
            "rsi": f"{a['rsi']:.1f}",
            "trend": a["trend"],
            "bb_position": f"{a['bb_position']:.0f}%",
            "atr_pct": f"{a['atr_pct']:.2f}%",
            "score": a["score"]
        } for tf, a in tf_data.items()},
        "trade_plan": _generate_trade_plan(signal, tf_data, direction)
    }

def _generate_trade_plan(signal, tf_data, direction):
    """根据信号生成交易计划"""
    
    if signal == "WAIT":
        return {
            "action": "观望",
            "reason": "多周期方向不一致，等待确认",
            "entry": None,
            "stop": None,
            "target": None,
            "leverage": None,
            "position_size": None
        }
    
    # 获取各周期数据
    d_1d = tf_data.get("1d", {})
    d_4h = tf_data.get("4h", {})
    d_1h = tf_data.get("1h", {})
    d_15m = tf_data.get("15m", {})
    
    # 计算入场、止损、目标
    price = d_15m.get("price", 0)
    atr_pct = d_4h.get("atr_pct", 1.5)  # 用4H的ATR
    
    if signal in ["STRONG_LONG", "LONG"]:
        # 做多
        entry = price
        stop = price * (1 - atr_pct / 100 * 2)  # 2倍ATR止损
        target = price * (1 + atr_pct / 100 * 4)  # 4倍ATR目标
        risk_pct = atr_pct / 100 * 2  # 2%风险
        leverage = 2
        
    elif signal in ["STRONG_SHORT", "SHORT"]:
        # 做空
        entry = price
        stop = price * (1 + atr_pct / 100 * 2)
        target = price * (1 - atr_pct / 100 * 4)
        risk_pct = atr_pct / 100 * 2
        leverage = 2
    else:
        return None
    
    # 仓位计算：假设账户1万U，每笔风险2%
    account = 10000
    risk_amount = account * risk_pct
    position_size = risk_amount / risk_pct  # = account
    margin_required = position_size / leverage
    
    return {
        "action": "做多" if "LONG" in signal else "做空",
        "signal_strength": "强" if "STRONG" in signal else "中",
        "entry": f"${entry:.0f}",
        "stop": f"${stop:.0f}",
        "target": f"${target:.0f}",
        "risk_reward": f"1:{4 if 'STRONG' in signal else 3}",
        "leverage": f"{leverage}x",
        "atr_stop": f"{atr_pct*2:.1f}%",
        "position_size_usdt": f"{position_size:.0f}U",
        "margin_required": f"{margin_required:.0f}U",
        "max_loss": f"{risk_amount:.0f}U"
    }

def format_report(result):
    """格式化报告"""
    if not result:
        return "分析失败"
    
    lines = []
    lines.append("="*60)
    lines.append(f"  多周期交易分析 - {result['coin']}")
    lines.append(f"  {result['timestamp']}")
    lines.append("="*60)
    
    # 各周期状态
    lines.append("\n【各周期状态】")
    lines.append("-"*60)
    
    tf_order = ["1d", "4h", "1h", "15m"]
    trend_icons = {"UP": "📈", "DOWN": "📉", "RANGE": "📊"}
    
    for tf in tf_order:
        if tf in result["timeframes"]:
            t = result["timeframes"][tf]
            trend_icon = trend_icons.get(t["trend"], "❓")
            lines.append(f"  {tf:>4}: {t['price']:>12} | {trend_icon} {t['trend']:>6} | "
                        f"RSI={t['rsi']:>5} | BB={t['bb_position']:>5} | ATR={t['atr_pct']}")
    
    # 综合判断
    lines.append("\n【综合判断】")
    lines.append("-"*60)
    
    sig = result["signal"]
    sig_icons = {
        "STRONG_LONG": "🟢💪",
        "LONG": "🟢",
        "STRONG_SHORT": "🔴💪",
        "SHORT": "🔴",
        "WAIT": "⚪"
    }
    
    lines.append(f"  信号: {sig_icons.get(sig, '❓')} {sig}")
    lines.append(f"  方向: {result['direction']} (置信度: {result['confidence']:.0f}%)")
    lines.append(f"  得分: {result['score']:+.0f} (-100到+100)")
    
    # 交易计划
    tp = result.get("trade_plan", {})
    if tp:
        lines.append("\n【交易计划】")
        lines.append("-"*60)
        lines.append(f"  操作: {tp.get('action', 'N/A')} {tp.get('signal_strength', '')}")
        lines.append(f"  入场: {tp.get('entry', 'N/A')}")
        lines.append(f"  止损: {tp.get('stop', 'N/A')} (风险: {tp.get('atr_stop', 'N/A')})")
        lines.append(f"  目标: {tp.get('target', 'N/A')} (R:R = {tp.get('risk_reward', 'N/A')})")
        
        if tp.get("leverage"):
            lines.append(f"  杠杆: {tp['leverage']}")
            lines.append(f"  仓位: {tp.get('position_size_usdt', 'N/A')}")
            lines.append(f"  保证金: {tp.get('margin_required', 'N/A')}")
            lines.append(f"  最大亏损: {tp.get('max_loss', 'N/A')}")
    
    return "\n".join(lines)

# ═══════════════════════════════════════════════════════════
# 主程序
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    
    coin = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"
    
    print(f"\n正在分析 {coin} ...")
    result = analyze(coin)
    
    if result:
        print(format_report(result))
        
        # 保存
        result_file = KRONOS / f"trade_plan_{coin.replace('-', '_')}.json"
        with open(result_file, "w") as f:
            json.dump(result, f, indent=2, default=str)
        print(f"\n✅ 结果已保存: {result_file}")
    else:
        print("❌ 分析失败")