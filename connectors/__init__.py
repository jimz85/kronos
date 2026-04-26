#!/usr/bin/env python3
"""
Kronos Connectors Package
=========================

Consolidated OKX exchange connectors.

Exports:
    OKXRESTClient: OKX REST API client with proper error handling and rate limiting

Version: 1.0.0
"""

from .okx_rest import OKXRESTClient

__all__ = ["OKXRESTClient"]
__version__ = "1.0.0"
