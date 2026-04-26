# Trading Modules API Reference

This document covers the trading-specific APIs for Kronos.

## Trading Client

### TradingClient

Main client for interacting with trading functionality.

```python
from kronos.trading import TradingClient

client = TradingClient(config=kronos_config)
```

### Methods

#### place_order()

Submit a new order to the exchange.

```python
def place_order(
    self,
    symbol: str,
    side: OrderSide,
    quantity: float,
    order_type: OrderType,
    price: Optional[float] = None,
    time_in_force: TimeInForce = TimeInForce.DAY
) -> Order
```

**Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| symbol | str | Yes | Trading pair symbol (e.g., "BTC-USD") |
| side | OrderSide | Yes | BUY or SELL |
| quantity | float | Yes | Order quantity |
| order_type | OrderType | Yes | Order type |
| price | float | Conditional | Limit price (required for LIMIT orders) |
| time_in_force | TimeInForce | No | Time in force (default: DAY) |

**Returns:** `Order` object

**Example:**

```python
order = client.place_order(
    symbol="BTC-USD",
    side=OrderSide.BUY,
    quantity=0.1,
    order_type=OrderType.LIMIT,
    price=50000.00
)
```

#### cancel_order()

Cancel an existing order.

```python
def cancel_order(self, order_id: str) -> bool
```

#### get_order()

Retrieve order details by ID.

```python
def get_order(self, order_id: str) -> Order
```

#### list_orders()

List all orders with optional filtering.

```python
def list_orders(
    self,
    symbol: Optional[str] = None,
    status: Optional[OrderStatus] = None,
    limit: int = 100
) -> List[Order]
```

#### get_positions()

Get all open positions.

```python
def get_positions(self) -> List[Position]
```

#### get_portfolio()

Get portfolio summary.

```python
def get_portfolio(self) -> Portfolio
```

## Market Data

### MarketDataClient

Client for accessing market data.

```python
from kronos.trading import MarketDataClient

market_client = MarketDataClient(config=kronos_config)
```

### Methods

#### get_quote()

Get real-time quote for a symbol.

```python
def get_quote(self, symbol: str) -> Quote
```

#### get_historical_bars()

Get historical OHLCV data.

```python
def get_historical_bars(
    self,
    symbol: str,
    start: datetime,
    end: datetime,
    resolution: str = "1D"
) -> List[Bar]
```

## Order Types

### OrderType

- `MARKET` - Market order
- `LIMIT` - Limit order
- `STOP` - Stop order
- `STOP_LIMIT` - Stop limit order

## WebSocket Streaming

### StreamingClient

Real-time data streaming via WebSocket.

```python
from kronos.trading import StreamingClient

stream = StreamingClient(config=kronos_config)
```

**Subscribe to updates:**

```python
# Subscribe to order updates
stream.subscribe("orders", callback=order_callback)

# Subscribe to market data
stream.subscribe("quotes:BTC-USD", callback=quote_callback)
```

**Disconnect:**

```python
stream.disconnect()
```
