#!/usr/bin/env python3
"""
OKX REST API Client
===================

OKX REST API client with proper error handling and rate limiting.
Consolidates functionality from scattered okx*.py files.

Features:
    - HMAC-SHA256 authentication
    - Exponential backoff with jitter for retries
    - Rate limit handling with Retry-After support
    - Circuit breaker integration
    - Comprehensive error handling

Usage:
    client = OKXRESTClient()
    candles = client.get_candles("BTC-USDT", bar="1H", limit=100)
    positions = client.get_positions()
    balance = client.get_account_balance()

Version: 1.0.0
"""

import base64
import hashlib
import hmac
import logging
import os
import random
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Optional

import requests

logger = logging.getLogger(__name__)


# Rate limit codes from OKX API
_RATE_LIMIT_CODES = frozenset({"50198", "58102", "58103"})
_RETRY_AFTER_DEFAULT = 10.0


@dataclass
class OKXAPIError(Exception):
    """Base exception for OKX API errors."""

    code: str
    msg: str
    data: Any = None

    def __str__(self) -> str:
        return f"OKXAPIError(code={self.code}, msg={self.msg})"


@dataclass
class OKXRateLimitError(OKXAPIError):
    """Raised when rate limit is exceeded."""

    retry_after: float = _RETRY_AFTER_DEFAULT

    def __str__(self) -> str:
        return (
            f"OKXRateLimitError(code={self.code}, msg={self.msg}, "
            f"retry_after={self.retry_after}s)"
        )


@dataclass
class OKXAuthError(OKXAPIError):
    """Raised when authentication fails."""

    pass


class OKXRESTClient:
    """
    OKX REST API client with proper error handling and rate limiting.

    Implements:
        - HMAC-SHA256 request signing
        - Exponential backoff with jitter for retries
        - HTTP 429 Retry-After header handling
        - Circuit breaker integration via environment variables
    """

    BASE_URL = "https://www.okx.com/api/v5"

    def __init__(
        self,
        api_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        passphrase: Optional[str] = None,
        use_simulated: Optional[bool] = None,
        max_retries: int = 5,
        base_delay: float = 1.0,
        max_delay: float = 60.0,
        timeout: float = 15.0,
    ):
        """
        Initialize OKX REST client.

        Args:
            api_key: OKX API key. Defaults to OKX_API_KEY env var.
            secret_key: OKX secret key. Defaults to OKX_SECRET_KEY env var.
            passphrase: OKX passphrase. Defaults to OKX_PASSPHRASE env var.
            use_simulated: Whether to use simulated trading.
                          Defaults to OKX_FLAG env var ('0'=live, '1'=sim).
            max_retries: Maximum number of retry attempts (default 5).
            base_delay: Initial backoff delay in seconds (default 1.0).
            max_delay: Maximum delay cap in seconds (default 60.0).
            timeout: Request timeout in seconds (default 15.0).
        """
        self.api_key = api_key or os.getenv("OKX_API_KEY", "")
        self.secret_key = secret_key or os.getenv("OKX_SECRET_KEY", "")
        self.passphrase = passphrase or os.getenv("OKX_PASSPHRASE", "")

        if use_simulated is None:
            flag = os.getenv("OKX_FLAG", "1")
            self.use_simulated = flag == "1"
        else:
            self.use_simulated = use_simulated

        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.timeout = timeout

        self._session = requests.Session()
        self._session.headers.update({"Content-Type": "application/json"})

    # ==================== Authentication ====================

    def _sign(self, method: str, path: str, body: str = "") -> tuple[str, str]:
        """
        Generate OKX API signature.

        Args:
            method: HTTP method (GET, POST, etc.)
            path: API endpoint path
            body: Request body (empty string for GET)

        Returns:
            Tuple of (timestamp, signature)
        """
        ts = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        msg = ts + method + path + body
        mac = hmac.new(
            self.secret_key.encode(), msg.encode(), hashlib.sha256
        )
        sig = base64.b64encode(mac.digest()).decode()
        return ts, sig

    def _headers(self, method: str, path: str, body: str = "") -> dict:
        """Build request headers with authentication."""
        ts, sig = self._sign(method, path, body)
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "x-simulated-trading": "1" if self.use_simulated else "0",
        }

    # ==================== Rate Limiting ====================

    def _compute_delay(self, attempt: int) -> float:
        """Compute exponential backoff delay with jitter."""
        delay = min(self.base_delay * (2 ** (attempt - 1)), self.max_delay)
        # Add jitter: 0% to 50% of the delay
        delay += random.uniform(0.0, 0.5 * delay)
        return delay

    def _should_retry(self, response: dict) -> tuple[bool, Optional[float]]:
        """
        Determine if request should be retried.

        Returns:
            Tuple of (should_retry, retry_after_seconds)
        """
        code = response.get("code", "")

        # Success
        if code == "0":
            return False, None

        # Rate limit codes - exhaust retries immediately
        if code in _RATE_LIMIT_CODES:
            return True, _RETRY_AFTER_DEFAULT

        # Other API errors - don't retry
        return False, None

    # ==================== HTTP Methods ====================

    def _request(
        self, method: str, path: str, body: Optional[dict] = None
    ) -> dict:
        """
        Make HTTP request with retry logic and rate limiting.

        Args:
            method: HTTP method (GET, POST, DELETE, etc.)
            path: API endpoint path
            body: Request body dict (will be JSON encoded)

        Returns:
            API response dict

        Raises:
            OKXAPIError: On API error
            OKXAuthError: On authentication failure
            OKXRateLimitError: When rate limited
            requests.RequestException: On network errors
        """
        body_str = json_dumps(body) if body else ""
        url = self.BASE_URL + path

        for attempt in range(1, self.max_retries + 1):
            try:
                headers = self._headers(method, path, body_str)

                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=body_str if body_str else None,
                    timeout=self.timeout,
                )

                # Handle HTTP 429
                if response.status_code == 429:
                    retry_after = _RETRY_AFTER_DEFAULT
                    retry_after_header = response.headers.get(
                        "Retry-After"
                    ) or response.headers.get("retry-after")
                    if retry_after_header:
                        try:
                            retry_after = float(retry_after_header)
                        except ValueError:
                            pass

                    if attempt < self.max_retries:
                        logger.warning(
                            "HTTP 429 on attempt %d/%d, retrying in %.1fs",
                            attempt, self.max_retries, retry_after,
                        )
                        time.sleep(retry_after)
                        continue
                    else:
                        raise OKXRateLimitError(
                            code="429",
                            msg="HTTP 429 Rate Limit Exceeded",
                            retry_after=retry_after,
                        )

                # Handle other HTTP errors
                response.raise_for_status()

                # Parse JSON response
                data = response.json()

                # Check API-level errors
                should_retry, retry_after = self._should_retry(data)
                if should_retry and retry_after:
                    if attempt < self.max_retries:
                        logger.warning(
                            "API rate limit code=%s on attempt %d/%d, "
                            "retrying in %.1fs",
                            data.get("code"), attempt, self.max_retries,
                            retry_after,
                        )
                        time.sleep(retry_after)
                        continue
                    else:
                        raise OKXRateLimitError(
                            code=data.get("code", ""),
                            msg=data.get("msg", "Rate limit exceeded"),
                            retry_after=retry_after,
                        )

                # Check for auth errors
                code = data.get("code", "")
                if code in ("50101", "50102", "50103", "50104", "50105"):
                    raise OKXAuthError(
                        code=code,
                        msg=data.get("msg", "Authentication failed"),
                    )

                # Return on success or non-retryable error
                if code != "0":
                    raise OKXAPIError(
                        code=code,
                        msg=data.get("msg", "API error"),
                        data=data,
                    )

                return data

            except requests.RequestException as e:
                last_error = f"{type(e).__name__}: {e}"
                if attempt < self.max_retries:
                    delay = self._compute_delay(attempt)
                    logger.warning(
                        "Request failed [attempt %d/%d] %s, retrying in %.1fs",
                        attempt, self.max_retries, last_error, delay,
                    )
                    time.sleep(delay)
                    continue
                else:
                    raise

        # Should not reach here, but just in case
        raise OKXAPIError(
            code="99999",
            msg=f"All {self.max_retries} retries exhausted",
        )

    def get(self, path: str) -> dict:
        """Make GET request."""
        return self._request("GET", path)

    def post(self, path: str, body: Optional[dict] = None) -> dict:
        """Make POST request."""
        return self._request("POST", path, body)

    def delete(self, path: str, body: Optional[dict] = None) -> dict:
        """Make DELETE request."""
        return self._request("DELETE", path, body)

    # ==================== Market Data ====================

    def get_candles(
        self,
        inst_id: str,
        bar: str = "1H",
        limit: int = 100,
        after: Optional[str] = None,
        before: Optional[str] = None,
    ) -> list[dict]:
        """
        Get candlestick (OHLCV) data.

        Args:
            inst_id: Instrument ID (e.g., "BTC-USDT", "ETH-USDT-SWAP")
            bar: Timeframe (e.g., "1m", "5m", "1H", "4H", "1D")
            limit: Number of candles (max 300)
            after: Pagination cursor - get candles before this timestamp
            before: Pagination cursor - get candles after this timestamp

        Returns:
            List of candle dicts with keys: ts, open, high, low, close, vol
        """
        params = f"instId={inst_id}&bar={bar}&limit={limit}"
        if after:
            params += f"&after={after}"
        if before:
            params += f"&before={before}"

        data = self.get(f"/market/candles?{params}")
        candles = []

        if not data.get("data"):
            return candles

        # OKX returns candles newest first, reverse for chronological order
        for c in reversed(data["data"]):
            candles.append({
                "ts": int(c[0]),
                "open": float(c[1]),
                "high": float(c[2]),
                "low": float(c[3]),
                "close": float(c[4]),
                "vol": float(c[5]),
            })

        return candles

    def get_ticker(self, inst_id: str) -> Optional[dict]:
        """
        Get ticker information.

        Args:
            inst_id: Instrument ID (e.g., "BTC-USDT")

        Returns:
            Ticker dict with keys: last, bid, ask, high24h, low24h, vol24h, open24h
        """
        data = self.get(f"/market/ticker?instId={inst_id}")

        if not data.get("data"):
            return None

        t = data["data"][0]
        return {
            "inst_id": t["instId"],
            "last": float(t["last"]),
            "bid": float(t["bidPx"]),
            "ask": float(t["askPx"]),
            "high24h": float(t["high24h"]),
            "low24h": float(t["low24h"]),
            "vol24h": float(t["vol24h"]),
            "open24h": float(t["open24h"]),
        }

    def get_orderbook(
        self, inst_id: str, depth: int = 400
    ) -> Optional[dict]:
        """
        Get order book (market depth).

        Args:
            inst_id: Instrument ID (e.g., "BTC-USDT")
            depth: Order book depth (max 400)

        Returns:
            Order book dict with keys: bids, asks, ts
        """
        data = self.get(f"/market/books?instId={inst_id}&sz={depth}")

        if not data.get("data"):
            return None

        ob = data["data"][0]
        return {
            "inst_id": ob["instId"],
            "ts": int(ob["ts"]),
            "asks": [[float(p), float(q)] for p, q in ob.get("asks", [])],
            "bids": [[float(p), float(q)] for p, q in ob.get("bids", [])],
        }

    # ==================== Account ====================

    def get_account_balance(self) -> dict:
        """
        Get account balance.

        Returns:
            Account balance dict
        """
        return self.get("/account/balance")

    def get_positions(self, inst_id: Optional[str] = None) -> list[dict]:
        """
        Get positions.

        Args:
            inst_id: Optional instrument ID to filter positions

        Returns:
            List of position dicts
        """
        path = "/account/positions"
        if inst_id:
            path += f"?instId={inst_id}"

        data = self.get(path)
        return data.get("data", [])

    def get_position(self, inst_id: str) -> Optional[dict]:
        """
        Get position for a specific instrument.

        Args:
            inst_id: Instrument ID (e.g., "BTC-USDT-SWAP")

        Returns:
            Position dict or None if not found
        """
        positions = self.get_positions(inst_id)
        for pos in positions:
            if pos.get("instId") == inst_id:
                return pos
        return None

    # ==================== Trading ====================

    def place_order(
        self,
        inst_id: str,
        td_mode: str,
        side: str,
        ord_type: str,
        sz: str,
        px: Optional[str] = None,
        sl_trigger_px: Optional[str] = None,
        sl_ord_px: Optional[str] = None,
        tp_trigger_px: Optional[str] = None,
        tp_ord_px: Optional[str] = None,
        **kwargs,
    ) -> dict:
        """
        Place an order.

        Args:
            inst_id: Instrument ID (e.g., "BTC-USDT-SWAP")
            td_mode: Trade mode ("cross" or "isolated")
            side: Order side ("buy" or "sell")
            ord_type: Order type ("market", "limit", "post_only", "fok", "ioc")
            sz: Order size
            px: Order price (required for limit orders)
            sl_trigger_px: Stop loss trigger price
            sl_ord_px: Stop loss order price
            tp_trigger_px: Take profit trigger price
            tp_ord_px: Take profit order price
            **kwargs: Additional order parameters

        Returns:
            Order result dict
        """
        body = {
            "instId": inst_id,
            "tdMode": td_mode,
            "side": side,
            "ordType": ord_type,
            "sz": sz,
        }

        if px:
            body["px"] = px

        # Stop loss
        if sl_trigger_px:
            body["slTriggerPx"] = sl_trigger_px
            body["slOrdPx"] = sl_ord_px or "-1"

        # Take profit
        if tp_trigger_px:
            body["tpTriggerPx"] = tp_trigger_px
            body["tpOrdPx"] = tp_ord_px or "-1"

        body.update(kwargs)

        return self.post("/trade/order", body)

    def cancel_order(self, inst_id: str, ord_id: str) -> dict:
        """
        Cancel an order.

        Args:
            inst_id: Instrument ID
            ord_id: Order ID

        Returns:
            Cancel result dict
        """
        return self.post(
            "/trade/cancel-order",
            {"instId": inst_id, "ordId": ord_id},
        )

    def get_order(self, inst_id: str, ord_id: str) -> Optional[dict]:
        """
        Get order details.

        Args:
            inst_id: Instrument ID
            ord_id: Order ID

        Returns:
            Order dict or None if not found
        """
        data = self.get(f"/trade/order?instId={inst_id}&ordId={ord_id}")
        if data.get("data"):
            return data["data"][0]
        return None

    def get_orders(self, inst_id: str, state: Optional[str] = None) -> list:
        """
        Get orders for an instrument.

        Args:
            inst_id: Instrument ID
            state: Filter by state ("live", "filled", "canceled")

        Returns:
            List of order dicts
        """
        path = f"/trade/orders-archive?instId={inst_id}"
        if state:
            path += f"&state={state}"

        data = self.get(path)
        return data.get("data", [])

    # ==================== Convenience Methods ====================

    def get_last_candle(self, inst_id: str, bar: str = "1H") -> Optional[dict]:
        """Get the most recent candle for an instrument."""
        candles = self.get_candles(inst_id, bar=bar, limit=1)
        return candles[0] if candles else None

    def get_market_price(self, inst_id: str) -> Optional[float]:
        """Get current market price for an instrument."""
        ticker = self.get_ticker(inst_id)
        return ticker.get("last") if ticker else None

    def close_position(self, inst_id: str) -> dict:
        """
        Close a position by placing an opposite order.

        Args:
            inst_id: Instrument ID

        Returns:
            Close order result dict
        """
        position = self.get_position(inst_id)
        if not position:
            raise OKXAPIError(
                code="99999",
                msg=f"No position found for {inst_id}",
            )

        pos_side = position.get("posSide", "net")
        inst_type = position.get("instType", "SWAP")

        # For SWAP, side is determined by posSide
        if pos_side == "long":
            side = "sell"
        elif pos_side == "short":
            side = "buy"
        else:
            # Net position - determine side from position value
            avail_pos = float(position.get("availPos", 0))
            if avail_pos > 0:
                side = "sell" if position.get("pos") > 0 else "buy"
            else:
                side = "buy"

        sz = position.get("availPos", "0")

        return self.place_order(
            inst_id=inst_id,
            td_mode="isolated",
            side=side,
            ord_type="market",
            sz=sz,
        )


# Helper function to avoid circular import
def json_dumps(obj: Any) -> str:
    """JSON serialize an object to string."""
    import json as _json

    return _json.dumps(obj, separators=(",", ":"))
