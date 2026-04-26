# Core Modules API Reference

This document covers the core module APIs for Kronos.

## Configuration

### KronosConfig

Configuration class for initializing the Kronos client.

```python
class KronosConfig:
    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.kronos.trade",
        timeout: int = 30,
        max_retries: int = 3
    ) -> None
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| api_key | str | required | Your API key |
| base_url | str | "https://api.kronos.trade" | API base URL |
| timeout | int | 30 | Request timeout in seconds |
| max_retries | int | 3 | Maximum retry attempts |

## Logging

### Logger

```python
from kronos.core.logging import Logger

logger = Logger(name: str, level: str = "INFO")
```

Log levels: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`

## Data Models

### Order

```python
@dataclass
class Order:
    order_id: str
    symbol: str
    side: OrderSide
    quantity: float
    price: Optional[float]
    status: OrderStatus
    created_at: datetime
    updated_at: datetime
```

### Portfolio

```python
@dataclass
class Portfolio:
    portfolio_id: str
    account_id: str
    positions: List[Position]
    cash_balance: float
    total_value: float
```

### Position

```python
@dataclass
class Position:
    symbol: str
    quantity: float
    average_price: float
    current_price: float
    unrealized_pnl: float
```

## Enums

### OrderSide

- `BUY` - Buy order
- `SELL` - Sell order

### OrderStatus

- `PENDING` - Order submitted, not yet filled
- `PARTIAL` - Order partially filled
- `FILLED` - Order completely filled
- `CANCELLED` - Order cancelled
- `REJECTED` - Order rejected

### TimeInForce

- `DAY` - Day order
- `GTC` - Good till cancelled
- `IOC` - Immediate or cancel
- `FOK` - Fill or kill
