#!/bin/bash
# run_live_monitor.sh - 启动实时交易监控
# 每次运行：python3 adaptive_trading_v3.py --mode live --coin ALL
# 监控间隔：5分钟检查一次信号
cd /Users/jimingzhang/kronos
echo "启动实时监控..."
echo "BTC/ETH: RSI均值回归策略"
echo "BNB/SOL: 布林趋势策略"
echo "DOGE: 不交易"
echo ""
python3 adaptive_trading_v3.py --mode live --coin ALL
