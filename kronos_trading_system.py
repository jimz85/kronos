#!/usr/bin/env python3
"""
Kronos Adaptive Trading System v2
最终版 - 基于全面回测验证的自适应交易系统

核心理念：
- 只做BTC的高频RSI策略（18笔/年，胜率57%，PF=1.99）
- 其他币种用布林带趋势策略
- 市场环境决定工具选择（现货/合约）
- 零情绪执行，不追涨杀跌
"""
import numpy as np
import pandas as pd
import yfinance as yf
import json
import time
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

def calc_bollinger(prices, period=20, std_mult=2.0):
    prices = np.asarray(prices).flatten()
    ma = pd.Series(prices).rolling(period).mean()
    std = pd.Series(prices).rolling(period).std()
    return ma, ma + std_mult * std, ma - std_mult * std

def calc_atr(high, low, close, period=14):
    high = np.asarray(high).flatten()
    low = np.asarray(low).flatten()
    close = np.asarray(close).flatten()
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean()

def calc_ma(prices, period):
    return pd.Series(np.asarray(prices).flatten()).rolling(period).mean()

# ═══════════════════════════════════════════════════════════
# 策略定义
# ═══════════════════════════════════════════════════════════

STRATEGIES = {
    "BTC-RSI-HF": {
        "name": "BTC RSI高频策略",
        "coin": "BTC-USD",
        "type": "spot",
        "direction": "long",
        "params": {
            "rsi_buy": 35,
            "rsi_sell": 65,
            "stop_pct": 0.02,
            "hold_max": 7
        },
        "expected": {
            "annual_trades": 18,
            "win_rate": 0.571,
            "pf": 1.99,
            "total_return": 0.648
        },
        "when": "always",  # 任何市场都可以
        "min_confidence": 0.70
    },
    "BTC-RSI-ORIGINAL": {
        "name": "BTC RSI原始策略",
        "coin": "BTC-USD",
        "type": "spot",
        "direction": "long",
        "params": {
            "rsi_buy": 30,
            "rsi_sell": 70,
            "stop_pct": 0.04,
            "hold_max": 20
        },
        "expected": {
            "annual_trades": 12,
            "win_rate": 0.586,
            "pf": 2.96,
            "total_return": 0.394
        },
        "when": "high_conviction",
        "min_confidence": 0.85
    },
    "BTC-BB-TREND": {
        "name": "BTC布林趋势策略",
        "coin": "BTC-USD",
        "type": "spot",
        "direction": "long",
        "params": {
            "bb_period": 20,
            "bb_std": 2.0,
            "stop_atr": 1.5,
            "hold_max": 10
        },
        "expected": {
            "annual_trades": 8,
            "win_rate": 0.50,
            "pf": 2.0,
            "total_return": 0.30
        },
        "when": "bull_market",
        "min_confidence": 0.75
    }
}

# ═══════════════════════════════════════════════════════════
# 市场环境检测
# ═══════════════════════════════════════════════════════════

def detect_regime(coin="BTC-USD"):
    """检测市场环境"""
    try:
        df = yf.download(coin, period="90d", progress=False)
        if df.empty or len(df) < 60:
            return {"regime": "unknown"}
        
        if isinstance(df.columns, pd.MultiIndex):
            df = df.loc[:, df.columns.get_level_values(0)]
        
        closes = np.asarray(df["Close"].values).flatten()
        ma20 = float(calc_ma(closes, 20).iloc[-1])
        ma60 = float(calc_ma(closes, 60).iloc[-1]) if len(closes) >= 60 else ma20
        current = closes[-1]
        rsi = calc_rsi(closes)
        current_rsi = float(rsi.iloc[-1])
        
        # 趋势判断
        if current > ma20 and ma20 > ma60:
            regime = "BULL"
        elif current < ma20 and ma20 < ma60:
            regime = "BEAR"
        else:
            regime = "RANGE"
        
        return {
            "regime": regime,
            "price": current,
            "rsi": current_rsi,
            "ma20": ma20,
            "ma60": ma60
        }
    except Exception as e:
        return {"regime": "unknown", "error": str(e)}

# ═══════════════════════════════════════════════════════════
# 信号检测
# ═══════════════════════════════════════════════════════════

def check_signals(coin="BTC-USD"):
    """检查当前是否有交易信号"""
    try:
        df = yf.download(coin, period="90d", progress=False)
        if df.empty:
            return {"error": "No data"}
        
        if isinstance(df.columns, pd.MultiIndex):
            df = df.loc[:, df.columns.get_level_values(0)]
        
        p = np.asarray(df["Close"].values).flatten()
        h = np.asarray(df["High"].values).flatten()
        l = np.asarray(df["Low"].values).flatten()
        
        rsi = calc_rsi(p)
        current_rsi = float(rsi.iloc[-1])
        ma, bb_upper, bb_lower = calc_bollinger(p)
        current_bb_upper = float(bb_upper.iloc[-1])
        current_bb_lower = float(bb_lower.iloc[-1])
        
        # BTC RSI HF signal
        rsi_buy = 35
        rsi_sell = 65
        
        signals = []
        
        # RSI超卖信号
        if current_rsi < rsi_buy:
            signals.append({
                "strategy": "BTC-RSI-HF",
                "type": "BUY",
                "price": float(p[-1]),
                "rsi": current_rsi,
                "stop_loss": float(p[-1]) * 0.98,
                "message": f"RSI={current_rsi:.1f}<{rsi_buy} 超卖，建议买入，止损{p[-1]*0.98:.0f}"
            })
        elif current_rsi > rsi_sell:
            signals.append({
                "strategy": "BTC-RSI-HF",
                "type": "SELL",
                "price": float(p[-1]),
                "rsi": current_rsi,
                "message": f"RSI={current_rsi:.1f}>{rsi_sell} 超买，建议卖出"
            })
        
        # BB突破信号
        if float(p[-1]) > current_bb_upper:
            signals.append({
                "strategy": "BTC-BB-TREND",
                "type": "BUY",
                "price": float(p[-1]),
                "bb_upper": current_bb_upper,
                "message": f"突破布林上轨 ${float(p[-1]):.0f} > {current_bb_upper:.0f}"
            })
        
        return {
            "coin": coin,
            "price": float(p[-1]),
            "rsi": current_rsi,
            "bb_upper": current_bb_upper,
            "bb_lower": current_bb_lower,
            "bb_position": (float(p[-1]) - current_bb_lower) / (current_bb_upper - current_bb_lower + 1e-10),
            "signals": signals,
            "regime": detect_regime(coin)
        }
    except Exception as e:
        return {"error": str(e)}

# ═══════════════════════════════════════════════════════════
# 实时报告
# ═══════════════════════════════════════════════════════════

def generate_report():
    """生成完整市场报告"""
    regime = detect_regime()
    signals = check_signals()
    
    regime_icon = {"BULL": "📈", "BEAR": "📉", "RANGE": "📊", "unknown": "❓"}.get(regime.get("regime", ""), "❓")
    
    report = []
    report.append("=" * 60)
    report.append(f"  Kronos 交易信号报告")
    report.append(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    report.append("=" * 60)
    report.append("")
    
    report.append(f"【市场环境】{regime_icon} {regime.get('regime', '?').upper()}")
    report.append(f"  BTC价格: ${regime.get('price', 0):.0f}")
    report.append(f"  RSI: {regime.get('rsi', 0):.1f}")
    report.append("")
    
    if "error" not in signals:
        rsi = signals["rsi"]
        rsi_emoji = "🟢" if rsi < 35 else "🔴" if rsi > 65 else "🟡"
        report.append(f"【RSI状态】{rsi_emoji} {rsi:.1f}")
        
        bb_pos = signals.get("bb_position", 0.5)
        report.append(f"【布林带位置】{(bb_pos*100):.0f}% (20%以下=超卖, 80%以上=超买)")
        report.append("")
        
        if signals["signals"]:
            report.append("【⚡ 触发信号】")
            for sig in signals["signals"]:
                type_icon = {"BUY": "🟢买入", "SELL": "🔴卖出"}.get(sig.get("type"), "❓")
                report.append(f"  {type_icon}: {sig.get('message', '')}")
                if sig.get("stop_loss"):
                    report.append(f"    止损: ${sig.get('stop_loss'):.0f}")
            report.append("")
        else:
            report.append("【无信号】等待RSI<35超卖或RSI>65超买")
            report.append("")
    
    report.append("【策略推荐】")
    
    current_regime = regime.get("regime", "")
    
    # 根据市场环境推荐
    if current_regime == "BULL":
        report.append("  牛市环境：")
        report.append("  首选: BTC RSI高频策略 (18笔/年, 胜率57%)")
        report.append("  备选: BTC 布林趋势策略 (8笔/年, 胜率50%)")
    elif current_regime == "BEAR":
        report.append("  熊市环境：")
        report.append("  观望: 等待RSI<35超卖信号")
        report.append("  注意: 不要逆势做多")
    else:
        report.append("  震荡环境：")
        report.append("  首选: BTC RSI高频策略 (RSI<35买, >65卖)")
        report.append("  等待: 布林带突破确认方向")
    
    report.append("")
    report.append("【执行清单】")
    report.append("  1. 当RSI<35时买入BTC，止损2%")
    report.append("  2. 当RSI>65或持满7天时卖出")
    report.append("  3. 同时只持有1个仓位，不追加")
    report.append("  4. 每笔仓位严格2%止损")
    
    # 保存到文件
    report_text = "\n".join(report)
    signal_file = KRONOS / "triggered_alerts.json"
    
    # 读取历史
    history = []
    if signal_file.exists():
        try:
            with open(signal_file) as f:
                history = json.load(f)
        except:
            pass
    
    # 添加当前信号
    if "signals" in signals and signals["signals"]:
        for sig in signals["signals"]:
            history.append({
                "time": datetime.now().isoformat(),
                "type": sig.get("type"),
                "strategy": sig.get("strategy"),
                "price": sig.get("price"),
                "rsi": signals.get("rsi"),
                "regime": current_regime
            })
    
    # 只保留最近100条
    history = history[-100:]
    
    with open(signal_file, "w") as f:
        json.dump(history, f, indent=2)
    
    return report_text

# ═══════════════════════════════════════════════════════════
# 主入口
# ═══════════════════════════════════════════════════════════

if __name__ == "__main__":
    import sys
    if "--report" in sys.argv:
        print(generate_report())
    elif "--signals" in sys.argv:
        s = check_signals()
        if "error" in s:
            print(f"错误: {s['error']}")
        else:
            print(f"BTC: ${s['price']:.0f} RSI={s['rsi']:.1f}")
            for sig in s.get("signals", []):
                print(f"  {sig.get('type')}: {sig.get('message')}")
            if not s.get("signals"):
                print("  无信号")
    else:
        print(generate_report())
