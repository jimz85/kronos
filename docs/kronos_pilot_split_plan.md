# kronos_pilot.py 拆分计划 (2026-04-26)

## 现状
- 文件: kronos_pilot.py
- 总行数: 2618
- 函数数量: 53

## 拆分方案

### config.py (~180行)
```
  44: log_info
  47: log_warn
  50: log_error
  54: _ts
  98: get_okx_pos_mode
 148: _get_allowed_coins
202: _atomic_write_json
```

### blacklist.py (~100行)
```
188: load_blacklist
209: save_blacklist
212: is_blacklisted
251: add_to_blacklist
```

### risk.py (~120行)
```
267: _get_volatility_stop
319: auto_position_sizing
360: log_skipped_signal
```

### execution.py (~480行)
```
554: _okx_request_with_retry
628: okx_place_order
779: _okx_market_close
828: _place_sl_tp_algo
934: _get_position_entry_price
963: _set_leverage
990: okx_get_positions
1029: okx_close_position
```

### paper_trading.py (~400行)
```
342: load_paper_log
348: load_skip_log
354: save_paper_log
357: save_skip_log
1094: open_paper_trade
1287: close_paper_trade
1349: check_stop_take_profit
1383: get_performance_stats
```

### signals.py (~700行)
```
391: get_cross_layer_signals
1438: get_okx_prices
1463: rsi
1469: adx
1479: compute_ic
1499: analyze_multi_timeframe
1585: generate_signals
1767: push_feishu
```

### reports.py (~450行)
```
1797: run_full_report
1990: save_ic_snapshot
2016: compute_ic_weights
2107: get_ic_weights
2124: format_ic_weights_report
2156: analyze_factor_weights
2261: compute_per_coin_performance
2298: compute_per_coin_allocation
2424: get_per_coin_allocation
2437: format_allocation_report
2483: run_ic_collection
2491: kronos_confirm
2513: show_status
2538: show_log
```

## 实施步骤

### Phase 1: 基础设施 (P0)
1. 创建 config.py（constants, logging, _ts, API keys）
2. 创建 blacklist.py（黑名单逻辑）
3. 更新 kronos_pilot.py 导入

### Phase 2: 交易执行 (P0)
4. 创建 execution.py（所有OKX API调用）
5. 验证: python3 kronos_pilot.py --status

### Phase 3: 核心策略 (P1)
6. 创建 indicators.py（RSI/ADX/ATR计算）
7. 创建 signals.py（信号生成+L1-L5数据）
8. 创建 paper_trading.py（纸质交易记录）

### Phase 4: 报告 (P2)
9. 创建 reports.py（飞书推送+IC分析+报告）

### Phase 5: 清理 (P0)
10. 精简 kronos_pilot.py 为纯入口（~100行）
11. 全量测试: python3 kronos_pilot.py --full

## 风险
- 53个函数签名必须保持完全一致
- 导入路径变更必须同步更新
- 每次修改后必须运行 --status 验证
- 建议用 Claude Code 多agent并行执行

## 验证清单
- [ ] python3 kronos_pilot.py --status 正常
- [ ] python3 kronos_pilot.py --full 正常
- [ ] 纸仓记录正确
- [ ] OKX真实持仓同步正常
- [ ] 飞书推送正常
