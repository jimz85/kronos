#!/bin/bash
# 数据更新脚本 - 每15分钟更新一次关键币种数据
source ~/.zshrc 2>/dev/null || source ~/.bashrc 2>/dev/null
cd /Users/jimingzhang/kronos
source venv/bin/activate
python3 data_fetcher.py DOT 2>&1 | tail -1
python3 data_fetcher.py AVAX 2>&1 | tail -1
python3 data_fetcher.py AAVE 2>&1 | tail -1
