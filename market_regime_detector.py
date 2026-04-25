#!/usr/bin/env python3
"""
market_regime_detector.py
市场状态识别模块

目标：识别当前市场处于什么状态，配合对应的策略

市场状态分类：
- TREND_UP: 明显上升趋势
- TREND_DOWN: 明显下降趋势  
- RANGE_BOUND: 震荡市场（无明显方向）
- HIGH_VOL: 高波动市场
- LOW_VOL: 低波动市场

每个状态对应不同的策略：
- TREND: 趋势跟随策略（BB趋势）
- RANGE: 均值回归策略（RSI）
- HIGH_VOL: 缩小仓位，提高止损
- LOW_VOL: 正常仓位

不只是价格方向，而是理解市场当前在什么模式。
"""
import numpy as np
import pandas as pd
from enum import Enum

class MarketRegime(Enum):
    UNKNOWN = "unknown"
    TREND_UP = "trend_up"
    TREND_DOWN = "trend_down"
    RANGE_BOUND = "range_bound"
    HIGH_VOL = "high_vol"
    LOW_VOL = "low_vol"

class MarketRegimeDetector:
    def __init__(self):
        self.regime = MarketRegime.UNKNOWN
        self.confidence = 0.0
        self._cache = {}
    
    def detect(self, df_daily, df_hourly=None):
        """
        检测市场状态
        df_daily: 日线数据 (需要至少60天)
        df_hourly: 小时线数据 (可选，更精确)
        
        Returns: (regime, confidence, details)
        """
        if df_daily is None or len(df_daily) < 60:
            return MarketRegime.UNKNOWN, 0.0, {"reason": "数据不足"}
        
        details = {}
        
        # 1. 趋势检测：用 ADX + 移动均线
        trend = self._detect_trend(df_daily)
        details["trend"] = trend
        
        # 2. 波动率检测：用 ATR 相对历史
        vol = self._detect_volatility(df_daily)
        details["volatility"] = vol
        
        # 3. 区间震荡检测：用布林带宽度
        range_score = self._detect_range_bound(df_daily)
        details["range_score"] = range_score
        
        # 综合判断
        if vol == "high":
            regime = MarketRegime.HIGH_VOL
            confidence = 0.8
        elif vol == "low":
            regime = MarketRegime.LOW_VOL
            confidence = 0.7
        elif trend == "up" and range_score < 0.3:
            regime = MarketRegime.TREND_UP
            confidence = 0.75
        elif trend == "down" and range_score < 0.3:
            regime = MarketRegime.TREND_DOWN
            confidence = 0.75
        elif range_score > 0.6:
            regime = MarketRegime.RANGE_BOUND
            confidence = 0.7
        else:
            regime = MarketRegime.UNKNOWN
            confidence = 0.5
        
        self.regime = regime
        self.confidence = confidence
        
        return regime, confidence, details
    
    def _detect_trend(self, df, lookback=60):
        """检测趋势方向"""
        closes = df["close"].values[-lookback:]
        
        # 简单方法：价格 vs 移动均线
        ma20 = np.mean(closes[-20:])
        ma60 = np.mean(closes[-60:]) if len(closes) >= 60 else ma20
        
        current_price = closes[-1]
        
        if current_price > ma20 > ma60:
            return "up"
        elif current_price < ma20 < ma60:
            return "down"
        else:
            return "neutral"
    
    def _detect_volatility(self, df, lookback=20):
        """检测波动率水平"""
        if len(df) < lookback:
            return "normal"
        
        closes = df["close"].values[-lookback:]
        returns = np.diff(np.log(closes))
        
        current_vol = np.std(returns[-5:])  # 最近5天波动
        historical_vol = np.std(returns)     # 历史波动
        
        if current_vol > historical_vol * 1.5:
            return "high"
        elif current_vol < historical_vol * 0.5:
            return "low"
        else:
            return "normal"
    
    def _detect_range_bound(self, df, lookback=60):
        """
        检测是否震荡市场
        返回 0-1，1=最强震荡
        """
        if len(df) < lookback:
            return 0.5
        
        closes = df["close"].values[-lookback:]
        
        # 布林带宽度
        ma = np.mean(closes)
        std = np.std(closes)
        
        # 带宽 = (上轨 - 下轨) / 中轨
        bb_width = (2 * std) / ma if ma > 0 else 0
        
        # 归一化：历史平均带宽
        all_returns = np.diff(np.log(closes))
        avg_vol = np.mean(np.abs(all_returns))
        
        # 带宽/波动率比例
        ratio = bb_width / (avg_vol * 2) if avg_vol > 0 else 1
        
        return min(ratio / 2, 1.0)  # 归一化到 0-1
    
    def get_strategy_for_regime(self, regime):
        """
        根据市场状态推荐策略
        返回: (primary_strategy, secondary_strategy, position_size_modifier)
        """
        strategy_map = {
            MarketRegime.TREND_UP: {
                "primary": "BB_TREND",
                "secondary": "BB_TREND",
                "position": 1.0,
                "stop_loss": "2ATR",
                "description": "上升趋势，趋势策略为主"
            },
            MarketRegime.TREND_DOWN: {
                "primary": "CASH",
                "secondary": None,
                "position": 0.0,
                "stop_loss": None,
                "description": "下降趋势，不持仓"
            },
            MarketRegime.RANGE_BOUND: {
                "primary": "RSI",
                "secondary": "RSI",
                "position": 0.8,
                "stop_loss": "3%",
                "description": "震荡市场，均值回归策略"
            },
            MarketRegime.HIGH_VOL: {
                "primary": "RSI",
                "secondary": None,
                "position": 0.5,
                "stop_loss": "5%",
                "description": "高波动，降低仓位"
            },
            MarketRegime.LOW_VOL: {
                "primary": "RSI",
                "secondary": "BB_TREND",
                "position": 1.0,
                "stop_loss": "3%",
                "description": "低波动，正常操作"
            },
            MarketRegime.UNKNOWN: {
                "primary": "RSI",
                "secondary": None,
                "position": 0.5,
                "stop_loss": "3%",
                "description": "市场状态不明，半仓"
            }
        }
        return strategy_map.get(regime, strategy_map[MarketRegime.UNKNOWN])
    
    def format_report(self, regime, confidence, details):
        """格式化状态报告"""
        strategy = self.get_strategy_for_regime(regime)
        return f"""
市场状态报告
═══════════════════════════════════════
当前状态: {regime.value.upper()} (置信度: {confidence:.0%})
趋势: {details.get('trend', '?')}
波动: {details.get('volatility', '?')}
震荡指数: {details.get('range_score', 0):.2f} (0=趋势, 1=震荡)

推荐策略: {strategy['primary']}
仓位调整: {strategy['position']:.0%}
止损设置: {strategy['stop_loss'] or '无'}
说明: {strategy['description']}
═══════════════════════════════════════
"""

def detect_market_regime(df_daily):
    """快速检测函数"""
    detector = MarketRegimeDetector()
    regime, confidence, details = detector.detect(df_daily)
    return detector, regime, confidence, details

# ─── 测试 ────────────────────────────────────────────────────
if __name__ == "__main__":
    import yfinance as yf
    
    print("检测市场状态...")
    
    # 下载 BTC 数据
    df = yf.download("BTC-USD", start="2024-01-01", end="2026-04-01", progress=False)
    df = df[['Close']].copy()
    df.columns = ['close']
    
    detector = MarketRegimeDetector()
    regime, confidence, details = detector.detect(df)
    
    print(detector.format_report(regime, confidence, details))
