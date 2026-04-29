# Kronos API Documentation

Welcome to the Kronos API documentation. Kronos is a trading platform providing comprehensive APIs for market data, order execution, and portfolio management.

## Documentation Structure

- [Core Modules API Reference](./core.md) - Core system components including configuration, logging, and data models
- [Trading Modules API Reference](./trading.md) - Trading-specific APIs for order management, execution, and market data
- [WebUI API Specification](./openapi.yaml) - OpenAPI 3.0 specification for the Flask WebUI endpoints (Swagger UI available at `/api/docs`)
- [Prometheus Metrics API](./prometheus_metrics.yaml) - OpenAPI specification for Prometheus metrics exporter

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

## Swagger UI

Interactive API documentation is available at:

- **WebUI API**: `http://localhost:7070/api/docs` (when running the webui server)
- **Prometheus Metrics**: `http://localhost:9090/api/docs` (when running prometheus_metrics.py)

## OpenAPI Specifications

- [WebUI API (YAML)](./openapi.yaml) - Kronos WebUI REST API
- [WebUI API (JSON)](./openapi.json) - Kronos WebUI REST API (JSON format)
- [Prometheus Metrics API](./prometheus_metrics.yaml) - Monitoring metrics endpoints

## Support

- Documentation: https://docs.kronos.trade
- API Status: https://status.kronos.trade
- Support: support@kronos.trade
