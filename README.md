# Kronos 趋势跟踪策略 — 最终状态

## 引擎
- **UnifiedBacktester**：`backtest_engine.py`
  - 0.2% FEE+滑点
  - 动态出场：1.5xATR止损 / 3xATR触发→保本 / 24h强制
  - 2h冷却去重

## 策略参数

### v1.0_stable
- Rolling窗口：10 | ADX>20 | RSI<68
- 信号数：143 | 胜率33.6% | 盈亏比1.75 | 收益-9.4%

### 模式过滤（WARMUP=40后启用）
- 白名单：`check_whitelist(rsi, adx)`
  - RSI 35-45 + ADX>30：WR=66.7%（3笔）
  - RSI 55-65 + ADX 30-40：WR=58.8%（17笔）
  - RSI 45-55 + ADX 20-30：WR=50.0%（5笔）
- 过滤后：56笔 | 胜率44.6% | 收益+9.5%

## 文件结构
```
kronos/
  backtest_engine.py       # 统一回测引擎
  run_comparison.py       # 三策略对比
  lookup_pattern.py        # 模式查表+白名单过滤
  pattern_db_live.json     # 实盘自学习库
  pattern_library_trend.json  # 回测模式库
  comparison_results.json  # 对比结果
```

## 核心函数
```python
from lookup_pattern import check_whitelist, update_pattern_db, PatternLookup

# 开仓前检查
result = check_whitelist(rsi=38, adx=31)
if result["allowed"]:
    print("可开仓:", result["reason"])

# 交易结束后自学习
update_pattern_db({
    "pattern_key": "long_RSI35-45_ADX30-100",
    "is_long": True,
    "entry_price": 74000,
    "exit_price": 74850,
    "max_drawdown": -0.012,
    "exit_time": 1715000000,
})

# 查询历史统计
lookup = PatternLookup()
lookup.print_report(rsi=38, adx=31, direction="long")
```

## 状态：等待实盘接入
- 前3个月：预热期，记录数据
- 3个月后：启用白名单过滤
- 预期收益：+9.5%/2年 ≈ 年化4%（无杠杆）
