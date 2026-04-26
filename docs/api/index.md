# Kronos API Documentation

Welcome to the Kronos API documentation. Kronos is a trading platform providing comprehensive APIs for market data, order execution, and portfolio management.

## Documentation Structure

- [Core Modules API Reference](./core.md) - Core system components including configuration, logging, and data models
- [Trading Modules API Reference](./trading.md) - Trading-specific APIs for order management, execution, and market data

## Quick Start

```python
from kronos import KronosClient

client = KronosClient(api_key="your_api_key")
```

## Authentication

All API requests require authentication via API key passed in the `X-API-Key` header.

## Rate Limits

| Tier | Requests/minute | Requests/day |
|------|-----------------|--------------|
| Free | 60 | 1,000 |
| Pro | 600 | 50,000 |
| Enterprise | 6,000 | Unlimited |

## Support

- Documentation: https://docs.kronos.trade
- API Status: https://status.kronos.trade
- Support: support@kronos.trade
