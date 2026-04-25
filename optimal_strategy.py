#!/usr/bin/env python3
"""
最优参数策略文件 - 2026-04-16 最终版
=====================================
Walk-Forward验证过的稳健参数
"""
import os

# ====== 最终稳健参数（2026-04-16 Walk-Forward验证）======

OPTIMAL_PARAMS = {

    # ========== Walk-Forward 100%稳健 ==========

    # BCH: 4/4 Walk-Forward通过，信号680个/7.3年
    # RSI<45 ADX>15 MA确认 持仓48h → +245% (基准ADX>15)
    # 注意：BCH持仓48h最优，72h无额外收益
    "BCH": {
        "direction": "trend_only",
        "long": {
            "rsi_entry": 45,
            "adx_min": 15,
            "hold_hours": 48,
        "stop_atr": 0,       # 不用止损！ATR止损破坏收益
        "profit_atr": 0,     # 持仓到固定时间出场
            "max_pos": 0.8,
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": 2.452,
        "walkforward_pass_rate": "4/4 (100%)",
    },

    # BTC: 5/5 Walk-Forward通过，信号305个/8.3年
    # RSI<35 ADX>15 持仓72h → +44.6%
    # 注意：BTC需要RSI<35（不是<45）且持仓72h（不是48h）
    "BTC": {
        "direction": "trend_only",
        "long": {
            "rsi_entry": 35,
            "adx_min": 15,
            "hold_hours": 72,
        "stop_atr": 0,       # 不用止损！ATR止损破坏收益
        "profit_atr": 0,     # 持仓到固定时间出场
            "max_pos": 0.8,
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": 0.446,
        "walkforward_pass_rate": "5/5 (100%)",
    },

    # AVAX: 2/2 Walk-Forward通过，数据5.5年
    # RSI<35 ADX>15 持仓72h → +130%
    "AVAX": {
        "direction": "trend_only",
        "long": {
            "rsi_entry": 35,
            "adx_min": 15,
            "hold_hours": 72,
        "stop_atr": 0,       # 不用止损！ATR止损破坏收益
        "profit_atr": 0,     # 持仓到固定时间出场
            "max_pos": 0.8,
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": 1.301,
        "walkforward_pass_rate": "2/2 (100%)",
    },

    # ETH: 4/5 Walk-Forward通过
    # RSI<35 ADX>15 持仓72h → +32.3%
    "ETH": {
        "direction": "trend_only",
        "long": {
            "rsi_entry": 35,
            "adx_min": 15,
            "hold_hours": 72,
        "stop_atr": 0,       # 不用止损！ATR止损破坏收益
        "profit_atr": 0,     # 持仓到固定时间出场
            "max_pos": 0.8,
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": 0.323,
        "walkforward_pass_rate": "4/5 (80%)",
    },

    # DOGE: 均値回归（RSI<30）70%胜率
    # 趋势跟踪: RSI<45 ADX>15 持仓48h
    "DOGE": {
        "direction": "both",
        "long": {
            "trend": {"rsi_entry": 45, "adx_min": 15, "hold_hours": 48, "stop_atr": 2.0, "profit_atr": 3.0, "max_pos": 0.8},
            "mean_rev": {"rsi_entry": 30, "hold_hours": 48, "max_pos": 0.8},
            "use": "trend_first",
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": None,
    },

    # AAVE: 2/2 Walk-Forward通过，DeFi相对独立
    # RSI<35 ADX>15 持仓72h
    "AAVE": {
        "direction": "trend_only",
        "long": {
            "rsi_entry": 35,
            "adx_min": 15,
            "hold_hours": 72,
        "stop_atr": 0,       # 不用止损！ATR止损破坏收益
        "profit_atr": 0,     # 持仓到固定时间出场
            "max_pos": 0.8,
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": None,
        "walkforward_pass_rate": "2/2 (100%)",
    },

    # CRV: 2/2 Walk-Forward通过
    "CRV": {
        "direction": "trend_only",
        "long": {
            "rsi_entry": 35,
            "adx_min": 15,
            "hold_hours": 72,
        "stop_atr": 0,       # 不用止损！ATR止损破坏收益
        "profit_atr": 0,     # 持仓到固定时间出场
            "max_pos": 0.8,
        },
        "short": None,
        "ma_confirm": True,
        "timeframe": "1h",
        "expected_return": None,
        "walkforward_pass_rate": "2/2 (100%)",
    },

    # ========== 重要教训 ==========

    # BTC均値回归：8年全负（RSI<30后继续下跌概率60.9%）
    # BNB趋势跟踪：波动2.7%，趋势无效
    # BAT趋势跟踪：Walk-Forward 33%稳健率
    # AXS/CVX/AR趋势跟踪：系统性亏损
}


def get_params(coin):
    """获取币种的最优参数"""
    coin = coin.upper()
    if coin in OPTIMAL_PARAMS:
        return OPTIMAL_PARAMS[coin]
    return None


if __name__ == "__main__":
    for coin in ["BTC", "ETH", "BCH", "AVAX", "DOGE", "AAVE", "CRV"]:
        p = get_params(coin)
        if p:
            print(f"{coin}: RSI<{p['long']['rsi_entry']} ADX>{p['long']['adx_min']} 持{p['long']['hold_hours']}h WF={p.get('walkforward_pass_rate','N/A')}")
