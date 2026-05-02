# Kronos 日志系统分析报告

## 一、日志现状总表

### 1.1 Kronos 日志文件

| 文件路径 | 记录内容 | 记录频率 | 文件大小 | 存在的问题 |
|---------|---------|---------|---------|-----------|
| `decision_journal.jsonl` | LLM决策上下文（持仓快照、候选币种、equity、LLM输出、解析结果） | 每次决策循环（约5分钟） | 1.2MB/753条 | 缺少order_id关联；execution_result字段常为空 |
| `logs/kronos_pilot.log` | 系统日志（信号、订单执行、SL/TP、错误） | 实时 | 418KB（轮转10MB×5份） | 文本格式难以解析；与结构化日志割裂 |
| `data/trade_journal.jsonl` | 交易记录（开仓/平仓、pnl、close_reason） | 每笔交易开仓/平仓时 | 21KB | 缺少signal_id；平仓匹配依赖symbol+direction |
| `audit_log.jsonl` | 审计日志 | 稀疏 | 1.8KB | 内容不完整 |
| `kronos_journal.json` | 日报分析结果 | 每日/每小时 | - | 手动生成，非自动化 |
| `paper_trades.json` | 纸质交易记录 | 纸质交易时 | - | 格式与trade_journal不统一 |

### 1.2 Miracle System 日志文件

| 文件路径 | 记录内容 | 记录频率 | 文件大小 | 存在的问题 |
|---------|---------|---------|---------|-----------|
| `miracle.log` | 主系统日志 | 实时 | 1.4MB | 混合多种日志级别；文本+结构化混杂 |
| `logs/trades.json` | 交易记录 | 每笔交易 | 1.5KB | 格式简单，缺少完整交易链路 |
| `logs/slippage.json` | 滑点记录 | 交易执行时 | 6.6KB | 独立记录，未关联到交易日志 |
| `data/archived_snapshot_history_*.jsonl` | 持仓快照历史 | 每分钟 | 大量小文件 | 文件名含时间戳，难以追溯 |
| `logs/coordinator.log` | 协调器日志 | 运行时 | 629B | 内容稀少 |

---

## 二、日志断链节点分析

### 2.1 执行路径图

```
信号生成 (IC分析) 
    ↓ [signal_id 生成]
信号评估 (五层确认、预交易模拟)
    ↓ [信号通过]
订单发送 (okx_place_order)
    ↓ [order_id / algo_id]
OKX订单确认 (fill_px, ordId)
    ↓
持仓建立 (positions snapshot)
    ↓
持仓监控 (real_monitor/heartbeat)
    ↓
平仓触发 (SL/TP/手动)
    ↓ [symbol + direction 匹配]
PnL计算 (record_trade_outcome)
    ↓
权益更新 (equity snapshot)
```

### 2.2 断链节点列表

| # | 断链节点 | 缺失关联 | 影响 |
|---|---------|---------|------|
| 1 | **信号 → 订单** | decision_journal无order_id | 无法追溯信号→下单的转化率 |
| 2 | **订单 → 持仓** | okx_place_order结果未写journal | 丢失订单确认价格、滑点 |
| 3 | **持仓 → 平仓** | trade_journal平仓匹配用symbol+direction | 多持仓同币种时匹配错误 |
| 4 | **PnL → 权益** | pnl计算结果未写入equity日志 | 权益变化链条断裂 |
| 5 | **日志 → 飞书** | 执行结果execution_result常为空 | 无法确认飞书通知是否成功 |

### 2.3 具体证据

**decision_journal.jsonl 样本分析：**
```json
{
  "ts": "2026-04-23T21:08:13",
  "equity": 68320.15,
  "positions_snapshot": {"BNB_short": {...}, "LINK_short": {...}},
  "candidates_snapshot": [{"coin": "AVAX", "direction": "做空", "score": 102}],
  "decision_parsed": {"coin": "", "decision": "hold", "reason": "..."},
  "execution_result": "LLM调用失败",   // ← 常为空或模糊
  "execution_ok": false
}
```

**trade_journal.jsonl 开仓记录（缺少信号追溯）：**
```json
{
  "trade_id": "uuid",
  "symbol": "AVAX-USDT-SWAP",
  "side": "short",
  "entry_price": 9.279,
  "status": "open"
  // 缺少: signal_id, signal_source, ic_score, confidence
}
```

---

## 三、关键代码位置

### 3.1 信号生成

| 文件 | 函数 | 职责 |
|------|------|------|
| `kronos_pilot.py:442` | `get_cross_layer_signals()` | 五层信号采集 |
| `ic_monitor.py:216` | `get_adaptive_signal()` | IC自适应信号 |
| `strategies/engine_alpha.py` | `AlphaEngine` | Alpha信号引擎 |
| `strategies/engine_beta.py` | `BetaEngine` | Beta信号引擎 |

### 3.2 订单执行

| 文件 | 函数 | 职责 |
|------|------|------|
| `kronos_pilot.py:691` | `okx_place_order()` | OKX下单（含SL/TP） |
| `kronos_pilot.py:920` | `_place_sl_tp_algo()` | OCO止盈止损 |
| `kronos_pilot.py:865` | `_okx_market_close()` | 市价平仓 |
| `miracle_kronos.py` | `place_order()` | 统一订单接口 |

### 3.3 持仓与PnL

| 文件 | 函数 | 职责 |
|------|------|------|
| `kronos_heartbeat.py:64` | `record_trade_outcome()` | 交易结果记录 |
| `kronos_journal.py:118` | `build_journal_entry()` | 日记账条目 |
| `real_monitor.py` | `get_real_positions()` | 真实持仓查询 |
| `miracle_kronos.py` | `get_positions()` | 统一持仓查询 |

---

## 四、改进方案建议

### 4.1 统一交易ID贯穿全链路

```
trade_id = uuid5(signal_id, timestamp)
```

**字段清单：**
```python
TradeRecord = {
    # 核心ID
    "trade_id": str,           # 全局唯一交易ID
    "signal_id": str,          # 来源信号ID
    "order_id": str,           # OKX订单ID
    
    # 信号阶段
    "signal_source": str,      # ic_monitor/alpha/beta/manual
    "signal_time": str,        # ISO格式
    "coin": str,
    "direction": str,          # long/short
    "ic_score": float,
    "confidence": float,
    "entry_conditions": dict,  # 触发条件快照
    
    # 订单阶段
    "order_time": str,
    "order_type": str,         # market/limit
    "requested_price": float,
    "requested_size": float,
    "leverage": float,
    
    # 成交阶段
    "fill_time": str,
    "fill_price": float,
    "fill_slippage": float,    # 滑点
    "fill_size": float,
    "actual_notional": float,
    
    # 持仓阶段
    "sl_price": float,
    "tp_price": float,
    "oco_algo_id": str,
    
    # 平仓阶段
    "close_time": str,
    "close_price": float,
    "close_slippage": float,
    "close_reason": str,       # sl_triggered/tp_triggered/manual/force_close
    "realized_pnl": float,
    "hold_hours": float,
    
    # 权益
    "equity_at_open": float,
    "equity_at_close": float,
    "equity_change": float,
    
    # 状态
    "status": str,             # pending/open/closed/cancelled
    "error": str,              # 错误信息
}
```

### 4.2 日志文件结构化建议

| 日志类型 | 文件名 | 格式 | 写入时机 |
|---------|-------|------|---------|
| 信号日志 | `signals_{date}.jsonl` | 结构化 | 信号生成时 |
| 订单日志 | `orders_{date}.jsonl` | 结构化 | 订单发送/确认时 |
| 持仓快照 | `positions_{date}.jsonl` | 结构化 | 定时（每分钟） |
| 权益快照 | `equity_{date}.jsonl` | 结构化 | 定时（每分钟） |
| PnL日志 | `pnl_{date}.jsonl` | 结构化 | 平仓时 |

### 4.3 核心改进代码

```python
# 改进1: 信号生成时创建signal_id
def generate_signal_with_id(coin, direction, score, conditions):
    signal_id = str(uuid4())
    signal_record = {
        "signal_id": signal_id,
        "coin": coin,
        "direction": direction,
        "score": score,
        "conditions": conditions,
        "signal_time": datetime.now().isoformat(),
        "status": "pending"
    }
    # 写入信号日志
    append_to_jsonl(f"signals_{date}.jsonl", signal_record)
    return signal_id

# 改进2: 订单执行时关联signal_id
def execute_order_with_trace(signal_id, signal_record, ...):
    trade_id = str(uuid5(signal_id, datetime.now().isoformat()))
    order_result = okx_place_order(...)
    
    order_record = {
        "trade_id": trade_id,
        "signal_id": signal_id,
        "order_id": order_result.get("ordId"),
        "fill_price": order_result.get("fillPx"),
        "status": "filled" if order_result.get("code") == "0" else "failed",
        ...
    }
    append_to_jsonl(f"orders_{date}.jsonl", order_record)
    return trade_id

# 改进3: 平仓时通过trade_id精确匹配
def close_trade_by_id(trade_id, close_reason, close_price):
    trade = get_trade_by_id(trade_id)  # 精确查找
    trade["close_time"] = datetime.now().isoformat()
    trade["close_price"] = close_price
    trade["close_reason"] = close_reason
    trade["realized_pnl"] = calculate_pnl(trade)
    trade["status"] = "closed"
    append_to_jsonl(f"pnl_{date}.jsonl", trade)
```

### 4.4 日志完整性检查

```python
def validate_log_chain(trade_id):
    """验证交易日志完整性"""
    signal = get_signal_by_trade_id(trade_id)
    order = get_order_by_trade_id(trade_id)
    position = get_position_by_trade_id(trade_id)
    pnl = get_pnl_by_trade_id(trade_id)
    
    missing = []
    if not signal: missing.append("signal")
    if not order: missing.append("order")
    if not position: missing.append("position")
    if not pnl: missing.append("pnl")
    
    if missing:
        logger.error(f"Trade {trade_id} log chain broken: {missing}")
        return False
    return True
```

---

## 五、总结

### 当前问题

1. **日志分散**：决策、订单、持仓、PnL分散在多个文件
2. **ID断链**：signal_id、order_id、trade_id未形成链条
3. **匹配模糊**：平仓时用symbol+direction匹配，可能出错
4. **执行结果未知**：execution_result字段常为空

### 改进收益

1. **可追溯**：任意交易可从信号→订单→持仓→PnL完整回放
2. **可分析**：信号转化率、订单执行质量、滑点分析
3. **可审计**：权益变化完整链路，失误可定位
4. **可优化**：基于完整数据的策略迭代

---

*报告生成时间: 2026-05-03*
*分析范围: ~/kronos, ~/miracle_system*
