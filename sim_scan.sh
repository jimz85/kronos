#!/bin/bash
# BTC/BCH 趋势跟踪策略 - 模拟盘定时扫描
# 每6小时运行一次
cd ~/kronos && source venv/bin/activate && python3 sim_engine.py --scan 2>/dev/null
