# OKX/Binance 双交易所适配层

## 概述

`exchange_adapter.py` 提供了一个统一的交易所接口，支持OKX和Binance两个交易所的无缝切换。

## 架构

```
ExchangeAdapter (抽象基类)
    ├── OKXAdapter       - OKX交易所实现
    └── BinanceAdapter   - Binance交易所实现
```

## 统一接口

| 方法 | 说明 |
|------|------|
| `get_ticker(symbol)` | 获取24小时行情 |
| `get_candles(symbol, bar, limit)` | 获取K线数据 |
| `get_balance()` | 获取账户余额 |
| `get_positions()` | 获取当前持仓 |
| `place_order(symbol, side, type, size, price)` | 下单 |
| `cancel_order(symbol, order_id)` | 取消订单 |
| `get_orderbook(symbol, limit)` | 获取订单簿 |

## 数据结构

- **Ticker**: 行情数据 (last, open_24h, high_24h, low_24h, vol_24h, price_change_pct)
- **Candle**: K线数据 (timestamp, open, high, low, close, volume)
- **OrderBook**: 订单簿 (asks, bids)
- **Balance**: 账户余额 (total, available, locked)
- **Position**: 持仓信息 (symbol, side, size, avg_price, unrealized_pnl)
- **Order**: 订单信息 (order_id, symbol, side, type, price, size, status)

## 使用示例

```python
from execution.exchange_adapter import (
    create_exchange_adapter,
    ExchangeType,
)

# 创建OKX适配器
okx = create_exchange_adapter(
    ExchangeType.OKX,
    api_key="your_key",
    secret_key="your_secret",
    passphrase="your_passphrase",
    testnet=True,
)

# 创建Binance适配器
binance = create_exchange_adapter(
    ExchangeType.BINANCE,
    api_key="your_key",
    secret_key="your_secret",
    testnet=True,
)

# 统一调用 - 无需关心底层交易所
ticker = okx.get_ticker("BTC-USDT")
print(f"BTC价格: ${ticker.last:.2f}")

candles = binance.get_candles("BTCUSDT", bar="1h", limit=100)
```

## 环境变量

### OKX
- `OKX_API_KEY`
- `OKX_SECRET_KEY`
- `OKX_PASSPHRASE`
- `OKX_FLAG` (1=模拟, 0=真实)

### Binance
- `BINANCE_API_KEY`
- `BINANCE_SECRET_KEY`
- `BINANCE_TESTNET` (true/false)

## 便捷函数

```python
from execution.exchange_adapter import (
    get_default_okx_adapter,
    get_default_binance_adapter,
)

# 自动从环境变量创建
okx = get_default_okx_adapter()
binance = get_default_binance_adapter()
```

## 注意事项

1. **Binance Testnet**: 在某些地区可能不可用 (返回451错误)
2. **公开行情**: 不需要API密钥即可获取
3. **交易操作**: 需要有效的API密钥和正确的权限

## 文件结构

```
kronos/execution/
├── __init__.py                 # 模块导出
├── order_executor.py           # 订单执行器 (重试、限流)
├── exchange_adapter.py         # 交易所适配层
└── examples/
    └── exchange_usage_example.py  # 使用示例
```