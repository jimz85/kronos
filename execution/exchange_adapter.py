#!/usr/bin/env python3
"""
exchange_adapter.py - OKX/Binance双交易所适配层
================================================

统一接口设计，支持OKX和Binance两个交易所的行情、订单操作。

架构：
  ExchangeAdapter (抽象基类)
      ├── OKXAdapter    - OKX交易所实现
      └── BinanceAdapter - Binance交易所实现

统一接口：
  - get_ticker()        获取行情
  - get_candles()       获取K线
  - get_balance()       获取账户余额
  - place_order()       市价/限价下单
  - cancel_order()      取消订单
  - get_orderbook()     获取订单簿
  - get_positions()     获取当前持仓

Usage:
    from execution.exchange_adapter import create_exchange_adapter, ExchangeType

    # 创建适配器
    adapter = create_exchange_adapter(ExchangeType.OKX, api_key, secret_key, passphrase)

    # 统一调用
    ticker = adapter.get_ticker("BTC-USDT")
    candles = adapter.get_candles("BTC-USDT", bar="1H", limit=100)
    balance = adapter.get_balance()
    adapter.place_order("BTC-USDT", "buy", "market", size=0.001)

Version: 1.0.0
"""

from __future__ import annotations

import os
import json
import time
import hmac
import hashlib
import base64
import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Optional, Any
from datetime import datetime
from pathlib import Path

import requests

logger = logging.getLogger('kronos.exchange')


# ═══════════════════════════════════════════════════════════════════════════
# 数据结构定义
# ═══════════════════════════════════════════════════════════════════════════

class ExchangeType(Enum):
    """支持的交易所类型"""
    OKX = "okx"
    BINANCE = "binance"


class OrderSide(Enum):
    """订单方向"""
    BUY = "buy"
    SELL = "sell"


class OrderType(Enum):
    """订单类型"""
    MARKET = "market"
    LIMIT = "limit"


@dataclass
class Ticker:
    """行情数据结构"""
    symbol: str              # 交易对符号
    last: float              # 最新价格
    open_24h: float          # 24h开盘价
    high_24h: float          # 24h最高价
    low_24h: float           # 24h最低价
    vol_24h: float           # 24h成交量
    price_change_pct: float  # 24h价格变化百分比
    timestamp: int = 0       # 时间戳


@dataclass
class Candle:
    """K线数据结构"""
    timestamp: int           # 时间戳（毫秒）
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass
class OrderBookEntry:
    """订单簿条目"""
    price: float
    quantity: float


@dataclass
class OrderBook:
    """订单簿结构"""
    symbol: str
    asks: list[OrderBookEntry]  # 卖盘 [price, quantity]
    bids: list[OrderBookEntry]  # 买盘 [price, quantity]
    timestamp: int = 0


@dataclass
class Balance:
    """账户余额"""
    total: float             # 总权益
    available: float         # 可用余额
    locked: float = 0.0      # 冻结/锁定


@dataclass
class Position:
    """持仓信息"""
    symbol: str              # 交易对
    side: str                # long/short
    size: float              # 持仓数量
    avg_price: float         # 平均价格
    unrealized_pnl: float = 0.0  # 未实现盈亏
    liquidation_price: float = 0.0  # 强平价格


@dataclass
class Order:
    """订单信息"""
    order_id: str
    symbol: str
    side: OrderSide
    order_type: OrderType
    price: Optional[float]
    size: float
    filled_size: float = 0.0
    status: str = "pending"
    timestamp: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# 抽象基类
# ═══════════════════════════════════════════════════════════════════════════

class ExchangeAdapter(ABC):
    """
    交易所适配器抽象基类
    
    所有交易所适配器必须实现以下接口：
    - get_ticker()        获取行情
    - get_candles()       获取K线
    - get_balance()       获取账户余额
    - place_order()       下单
    - cancel_order()      取消订单
    - get_orderbook()     获取订单簿
    - get_positions()     获取持仓
    """

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        testnet: bool = True,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.passphrase = passphrase
        self.testnet = testnet
        self._session = requests.Session()
        self._session.headers.update({"User-Agent": "Mozilla/5.0"})

    @property
    @abstractmethod
    def exchange_type(self) -> ExchangeType:
        """返回交易所类型"""
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """返回交易所名称"""
        pass

    @abstractmethod
    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """
        获取单个币种行情
        
        Args:
            symbol: 交易对符号，如 "BTC-USDT" (OKX) 或 "BTCUSDT" (Binance)
            
        Returns:
            Ticker对象，失败返回None
        """
        pass

    @abstractmethod
    def get_candles(
        self,
        symbol: str,
        bar: str = "1H",
        limit: int = 100,
    ) -> list[Candle]:
        """
        获取K线数据
        
        Args:
            symbol: 交易对符号
            bar: K线周期，如 "1m", "5m", "1H", "1D"
            limit: 返回数量
            
        Returns:
            Candle列表
        """
        pass

    @abstractmethod
    def get_balance(self) -> Optional[Balance]:
        """获取账户余额"""
        pass

    @abstractmethod
    def get_positions(self) -> list[Position]:
        """获取当前持仓"""
        pass

    @abstractmethod
    def place_order(
        self,
        symbol: str,
        side: str,  # "buy" or "sell"
        order_type: str,  # "market" or "limit"
        size: float,
        price: Optional[float] = None,
    ) -> Optional[Order]:
        """
        下单
        
        Args:
            symbol: 交易对
            side: 买卖方向 "buy" / "sell"
            order_type: 订单类型 "market" / "limit"
            size: 数量
            price: 限价单价格（市价单不需要）
            
        Returns:
            Order对象，失败返回None
        """
        pass

    @abstractmethod
    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """取消订单"""
        pass

    @abstractmethod
    def get_orderbook(self, symbol: str, limit: int = 5) -> Optional[OrderBook]:
        """获取订单簿"""
        pass

    # ── 通用工具方法 ─────────────────────────────────────────────

    def normalize_symbol(self, symbol: str) -> str:
        """
        标准化交易对符号
        子类可根据交易所要求覆盖
        """
        return symbol.replace("-", "").upper()

    def _parse_timestamp(self, ts: Any) -> int:
        """解析时间戳为毫秒"""
        if isinstance(ts, int):
            return ts
        if isinstance(ts, str):
            try:
                return int(ts)
            except ValueError:
                pass
        return int(time.time() * 1000)

    def close(self):
        """关闭会话"""
        self._session.close()


# ═══════════════════════════════════════════════════════════════════════════
# OKX交易所适配器
# ═══════════════════════════════════════════════════════════════════════════

class OKXAdapter(ExchangeAdapter):
    """
    OKX交易所适配器
    
    API文档: https://www.okx.com/docs-v5/
    """

    BASE_URL = "https://www.okx.com"
    TESTNET_URL = "https://www.okx.com"

    # OKX K线周期映射
    BAR_MAP = {
        "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
        "1H": "1H", "4H": "4H", "8H": "8H", "1D": "1D",
        "1w": "1w", "1mo": "1mo",
    }

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        testnet: bool = True,
    ):
        super().__init__(api_key, secret_key, passphrase, testnet)
        self.base_url = self.TESTNET_URL if testnet else self.BASE_URL

    @property
    def exchange_type(self) -> ExchangeType:
        return ExchangeType.OKX

    @property
    def name(self) -> str:
        return "OKX" + (" Testnet" if self.testnet else "")

    # ── 签名 ─────────────────────────────────────────────────────────────

    def _sign(self, method: str, path: str, body: str = "") -> dict:
        """OKX API签名"""
        ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
        msg = ts + method + path + (body if body else "")
        sig = base64.b64encode(hmac.new(
            self.secret_key.encode(), msg.encode(), hashlib.sha256
        ).digest()).decode()
        return {
            "OK-ACCESS-KEY": self.api_key,
            "OK-ACCESS-SIGN": sig,
            "OK-ACCESS-TIMESTAMP": ts,
            "OK-ACCESS-PASSPHRASE": self.passphrase,
            "Content-Type": "application/json",
            "x-simulated-trading": "1" if self.testnet else "0",
        }

    def _request(
        self,
        method: str,
        path: str,
        params: dict = None,
        body: dict = None,
    ) -> Optional[dict]:
        """发送API请求"""
        try:
            headers = self._sign(method, path, json.dumps(body) if body else "")
            url = self.base_url + path

            if method == "GET":
                r = self._session.get(url, headers=headers, params=params, timeout=10)
            else:
                r = self._session.post(url, headers=headers, json=body, timeout=10)

            result = r.json()
            code = result.get("code", "-1")
            if code != "0":
                logger.warning(f"OKX API error: {code} {result.get('msg', '')}")
                return None
            return result

        except Exception as e:
            logger.error(f"OKX request failed: {e}")
            return None

    # ── 行情接口 ─────────────────────────────────────────────────────────

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """获取OKX行情"""
        try:
            # OKX使用 instId 格式如 "BTC-USDT-SWAP"
            inst_id = symbol if "-" in symbol else f"{symbol}-USDT-SWAP"
            path = f"/api/v5/market/ticker?instId={inst_id}"

            # 公开行情不需要签名
            ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
            msg = ts + "GET" + path
            sig = base64.b64encode(hmac.new(
                self.secret_key.encode(), msg.encode(), hashlib.sha256
            ).digest()).decode()
            headers = {
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": sig,
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }

            url = self.base_url + path
            r = self._session.get(url, headers=headers, timeout=10)
            data = r.json()

            if not data.get("data"):
                return None

            t = data["data"][0]
            last = float(t.get("last", 0))
            open_24h = float(t.get("open24h", last))
            change_pct = (last - open_24h) / open_24h * 100 if open_24h else 0

            return Ticker(
                symbol=inst_id,
                last=last,
                open_24h=open_24h,
                high_24h=float(t.get("high24h", 0)),
                low_24h=float(t.get("low24h", 0)),
                vol_24h=float(t.get("volCcy24h", 0)),
                price_change_pct=change_pct,
                timestamp=self._parse_timestamp(t.get("ts", 0)),
            )
        except Exception as e:
            logger.warning(f"get_ticker({symbol}) failed: {e}")
            return None

    def get_candles(self, symbol: str, bar: str = "1H", limit: int = 100) -> list[Candle]:
        """获取OKX K线数据"""
        try:
            inst_id = symbol if "-" in symbol else f"{symbol}-USDT-SWAP"
            okx_bar = self.BAR_MAP.get(bar, bar)
            path = f"/api/v5/market/history-candles?instId={inst_id}&bar={okx_bar}&limit={limit}"

            ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
            msg = ts + "GET" + path
            sig = base64.b64encode(hmac.new(
                self.secret_key.encode(), msg.encode(), hashlib.sha256
            ).digest()).decode()
            headers = {
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": sig,
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }

            url = self.base_url + path
            r = self._session.get(url, headers=headers, timeout=10)
            data = r.json()

            if data.get("code") != "0" or not data.get("data"):
                return []

            candles = []
            for c in reversed(data["data"]):
                candles.append(Candle(
                    timestamp=int(c[0]),
                    open=float(c[1]),
                    high=float(c[2]),
                    low=float(c[3]),
                    close=float(c[4]),
                    volume=float(c[5]),
                ))
            return candles

        except Exception as e:
            logger.warning(f"get_candles({symbol}) failed: {e}")
            return []

    def get_orderbook(self, symbol: str, limit: int = 5) -> Optional[OrderBook]:
        """获取OKX订单簿"""
        try:
            inst_id = symbol if "-" in symbol else f"{symbol}-USDT-SWAP"
            path = f"/api/v5/market/books-lite?instId={inst_id}&sz={limit}"

            ts = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%S.000Z')
            msg = ts + "GET" + path
            sig = base64.b64encode(hmac.new(
                self.secret_key.encode(), msg.encode(), hashlib.sha256
            ).digest()).decode()
            headers = {
                "OK-ACCESS-KEY": self.api_key,
                "OK-ACCESS-SIGN": sig,
                "OK-ACCESS-TIMESTAMP": ts,
                "OK-ACCESS-PASSPHRASE": self.passphrase,
                "Content-Type": "application/json",
            }

            url = self.base_url + path
            r = self._session.get(url, headers=headers, timeout=10)
            data = r.json()

            if not data.get("data"):
                return None

            ob = data["data"][0]
            asks = [OrderBookEntry(float(p), float(s)) for p, s in ob.get("asks", [])[:limit]]
            bids = [OrderBookEntry(float(p), float(s)) for p, s in ob.get("bids", [])[:limit]]

            return OrderBook(
                symbol=inst_id,
                asks=asks,
                bids=bids,
                timestamp=self._parse_timestamp(ob.get("ts", 0)),
            )

        except Exception as e:
            logger.warning(f"get_orderbook({symbol}) failed: {e}")
            return None

    # ── 账户接口 ─────────────────────────────────────────────────────────

    def get_balance(self) -> Optional[Balance]:
        """获取OKX账户余额"""
        try:
            result = self._request("GET", "/api/v5/account/balance")
            if not result or not result.get("data"):
                return None

            data = result["data"][0]
            total = 0.0
            available = 0.0

            for details in data.get("details", []):
                ccy = details.get("ccy", "")
                eq = float(details.get("eq", 0))
                avail = float(details.get("availEq", 0))
                total += eq
                available += avail

            return Balance(total=total, available=available)

        except Exception as e:
            logger.warning(f"get_balance failed: {e}")
            return None

    def get_positions(self) -> list[Position]:
        """获取OKX持仓"""
        try:
            result = self._request("GET", "/api/v5/account/positions")
            if not result or not result.get("data"):
                return []

            positions = []
            for pos in result["data"]:
                inst_id = pos.get("instId", "")
                pos_side = pos.get("posSide", "")  # long/short
                if not inst_id or not pos_side:
                    continue

                # 获取持仓数量
                avail_pos = float(pos.get("availPos", 0))
                if avail_pos <= 0:
                    continue

                positions.append(Position(
                    symbol=inst_id,
                    side=pos_side,
                    size=avail_pos,
                    avg_price=float(pos.get("avgPx", 0)),
                    unrealized_pnl=float(pos.get("upl", 0)),
                    liquidation_price=float(pos.get("liqPx", 0)),
                ))
            return positions

        except Exception as e:
            logger.warning(f"get_positions failed: {e}")
            return []

    # ── 交易接口 ─────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        price: Optional[float] = None,
    ) -> Optional[Order]:
        """OKX下单"""
        try:
            inst_id = symbol if "-" in symbol else f"{symbol}-USDT-SWAP"
            body = {
                "instId": inst_id,
                "tdMode": "cash",
                "side": side,
                "ordType": order_type,
                "sz": str(size),
            }
            if order_type == "limit" and price:
                body["px"] = str(price)

            result = self._request("POST", "/api/v5/trade/order", body=body)
            if not result or not result.get("data"):
                return None

            order_data = result["data"][0]
            return Order(
                order_id=order_data.get("ordId", ""),
                symbol=inst_id,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType.MARKET if order_type == "market" else OrderType.LIMIT,
                price=price,
                size=size,
                filled_size=float(order_data.get("filledSq", 0)),
                status=order_data.get("state", "pending"),
                timestamp=self._parse_timestamp(order_data.get("cTime", 0)),
            )

        except Exception as e:
            logger.warning(f"place_order failed: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """取消OKX订单"""
        try:
            inst_id = symbol if "-" in symbol else f"{symbol}-USDT-SWAP"
            body = {
                "instId": inst_id,
                "ordId": order_id,
            }
            result = self._request("POST", "/api/v5/trade/cancel-order", body=body)
            return result is not None

        except Exception as e:
            logger.warning(f"cancel_order failed: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════
# Binance交易所适配器
# ═══════════════════════════════════════════════════════════════════════════

class BinanceAdapter(ExchangeAdapter):
    """
    Binance交易所适配器
    
    API文档: https://developers.binance.com/
    """

    BASE_URL = "https://api.binance.com"
    TESTNET_URL = "https://testnet.binance.vision"
    # Binance Futures Testnet
    FUTURES_TESTNET_URL = "https://testnet.binancefuture.com"

    def __init__(
        self,
        api_key: str = "",
        secret_key: str = "",
        passphrase: str = "",
        testnet: bool = True,
        use_futures: bool = False,
    ):
        super().__init__(api_key, secret_key, passphrase, testnet)
        self.use_futures = use_futures
        if use_futures:
            self.base_url = self.FUTURES_TESTNET_URL if testnet else "https://fapi.binance.com"
        else:
            self.base_url = self.TESTNET_URL if testnet else self.BASE_URL

    @property
    def exchange_type(self) -> ExchangeType:
        return ExchangeType.BINANCE

    @property
    def name(self) -> str:
        suffix = " Futures" if self.use_futures else ""
        return f"Binance{suffix}" + (" Testnet" if self.testnet else "")

    def normalize_symbol(self, symbol: str) -> str:
        """Binance使用无连字符格式如 BTCUSDT"""
        return symbol.replace("-", "").upper()

    # ── 签名 ─────────────────────────────────────────────────────────────

    def _sign(self, params: dict) -> str:
        """Binance API签名"""
        query = "&".join(f"{k}={v}" for k, v in params.items())
        signature = hmac.new(
            self.secret_key.encode(),
            query.encode(),
            hashlib.sha256
        ).hexdigest()
        return signature

    def _request(
        self,
        method: str,
        path: str,
        params: dict = None,
        signed: bool = False,
    ) -> Optional[dict]:
        """发送Binance API请求"""
        try:
            params = params or {}
            if signed:
                params["timestamp"] = int(time.time() * 1000)
                params["signature"] = self._sign(params)

            headers = {}
            if signed and self.api_key:
                headers["X-MBX-APIKEY"] = self.api_key

            url = self.base_url + path

            if method == "GET":
                r = self._session.get(url, headers=headers, params=params, timeout=10)
            else:
                r = self._session.post(url, headers=headers, params=params, timeout=10)

            if r.status_code != 200:
                logger.warning(f"Binance API error: status={r.status_code}")
                return None

            return r.json()

        except Exception as e:
            logger.error(f"Binance request failed: {e}")
            return None

    # ── 行情接口 ─────────────────────────────────────────────────────────

    def get_ticker(self, symbol: str) -> Optional[Ticker]:
        """获取Binance 24h行情"""
        try:
            sym = self.normalize_symbol(symbol)
            path = "/api/v3/ticker/24hr"
            params = {"symbol": sym}

            data = self._request("GET", path, params)
            if not data or "symbol" not in data:
                return None

            return Ticker(
                symbol=sym,
                last=float(data.get("lastPrice", 0)),
                open_24h=float(data.get("openPrice", 0)),
                high_24h=float(data.get("highPrice", 0)),
                low_24h=float(data.get("lowPrice", 0)),
                vol_24h=float(data.get("volume", 0)),
                price_change_pct=float(data.get("priceChangePercent", 0)),
                timestamp=int(time.time() * 1000),
            )

        except Exception as e:
            logger.warning(f"get_ticker({symbol}) failed: {e}")
            return None

    def get_candles(self, symbol: str, bar: str = "1H", limit: int = 100) -> list[Candle]:
        """获取Binance K线数据"""
        try:
            sym = self.normalize_symbol(symbol)
            # Binance K线周期映射
            interval_map = {
                "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
                "1H": "1h", "4H": "4h", "8H": "8h", "1D": "1d",
                "1w": "1w", "1mo": "1M",
            }
            interval = interval_map.get(bar, bar)

            path = "/api/v3/klines"
            params = {"symbol": sym, "interval": interval, "limit": limit}

            data = self._request("GET", path, params)
            if not data or not isinstance(data, list):
                return []

            candles = []
            for c in data:
                candles.append(Candle(
                    timestamp=int(c[0]),
                    open=float(c[1]),
                    high=float(c[2]),
                    low=float(c[3]),
                    close=float(c[4]),
                    volume=float(c[5]),
                ))
            return candles

        except Exception as e:
            logger.warning(f"get_candles({symbol}) failed: {e}")
            return []

    def get_orderbook(self, symbol: str, limit: int = 5) -> Optional[OrderBook]:
        """获取Binance订单簿"""
        try:
            sym = self.normalize_symbol(symbol)
            path = "/api/v3/depth"
            params = {"symbol": sym, "limit": limit}

            data = self._request("GET", path, params)
            if not data:
                return None

            asks = [OrderBookEntry(float(p), float(q)) for p, q in data.get("asks", [])[:limit]]
            bids = [OrderBookEntry(float(p), float(q)) for p, q in data.get("bids", [])[:limit]]

            return OrderBook(
                symbol=sym,
                asks=asks,
                bids=bids,
                timestamp=int(time.time() * 1000),
            )

        except Exception as e:
            logger.warning(f"get_orderbook({symbol}) failed: {e}")
            return None

    # ── 账户接口 ─────────────────────────────────────────────────────────

    def get_balance(self) -> Optional[Balance]:
        """获取Binance账户余额"""
        if not self.api_key or not self.secret_key:
            logger.warning("Binance requires API key for balance")
            return None

        try:
            if self.use_futures:
                path = "/fapi/v2/balance"
            else:
                path = "/api/v3/account"

            data = self._request("GET", path, signed=True)
            if not data:
                return None

            total = 0.0
            available = 0.0

            if self.use_futures:
                for asset in data:
                    if asset.get("symbol") in ("USDT", "BUSD", "USD"):
                        total += float(asset.get("walletBalance", 0))
                        available += float(asset.get("availableBalance", 0))
            else:
                for balance in data.get("balances", []):
                    free = float(balance.get("free", 0))
                    locked = float(balance.get("locked", 0))
                    total += free + locked
                    available += free

            return Balance(total=total, available=available)

        except Exception as e:
            logger.warning(f"get_balance failed: {e}")
            return None

    def get_positions(self) -> list[Position]:
        """获取Binance持仓"""
        if not self.api_key or not self.secret_key:
            return []

        try:
            if self.use_futures:
                path = "/fapi/v2/positionRisk"
                data = self._request("GET", path, signed=True)
                if not data:
                    return []

                positions = []
                for pos in data:
                    sym = pos.get("symbol", "")
                    pos_amt = float(pos.get("positionAmt", 0))
                    if pos_amt == 0:
                        continue

                    side = "long" if pos_amt > 0 else "short"
                    positions.append(Position(
                        symbol=sym,
                        side=side,
                        size=abs(pos_amt),
                        avg_price=float(pos.get("entryPrice", 0)),
                        unrealized_pnl=float(pos.get("unrealizedProfit", 0)),
                        liquidation_price=float(pos.get("liquidationPrice", 0)),
                    ))
                return positions
            else:
                # 现货没有持仓概念，返回空
                return []

        except Exception as e:
            logger.warning(f"get_positions failed: {e}")
            return []

    # ── 交易接口 ─────────────────────────────────────────────────────────

    def place_order(
        self,
        symbol: str,
        side: str,
        order_type: str,
        size: float,
        price: Optional[float] = None,
    ) -> Optional[Order]:
        """Binance下单"""
        if not self.api_key or not self.secret_key:
            logger.warning("Binance requires API key for trading")
            return None

        try:
            sym = self.normalize_symbol(symbol)
            if self.use_futures:
                path = "/fapi/v1/order"
            else:
                path = "/api/v3/order"

            params = {
                "symbol": sym,
                "side": side.upper(),
                "type": order_type.upper(),
                "quantity": size,
            }
            if order_type == "limit" and price:
                params["price"] = price
                params["timeInForce"] = "GTC"

            data = self._request("POST", path, params=params, signed=True)
            if not data:
                return None

            return Order(
                order_id=str(data.get("orderId", "")),
                symbol=sym,
                side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
                order_type=OrderType.MARKET if order_type == "market" else OrderType.LIMIT,
                price=price,
                size=size,
                filled_size=float(data.get("executedQty", 0)),
                status=data.get("status", "NEW"),
                timestamp=int(data.get("transactTime", 0)),
            )

        except Exception as e:
            logger.warning(f"place_order failed: {e}")
            return None

    def cancel_order(self, symbol: str, order_id: str) -> bool:
        """取消Binance订单"""
        if not self.api_key or not self.secret_key:
            return False

        try:
            sym = self.normalize_symbol(symbol)
            if self.use_futures:
                path = "/fapi/v1/order"
            else:
                path = "/api/v3/order"

            params = {
                "symbol": sym,
                "orderId": order_id,
            }
            data = self._request("DELETE", path, params=params, signed=True)
            return data is not None

        except Exception as e:
            logger.warning(f"cancel_order failed: {e}")
            return False


# ═══════════════════════════════════════════════════════════════════════════
# 工厂函数
# ═══════════════════════════════════════════════════════════════════════════

def create_exchange_adapter(
    exchange_type: ExchangeType,
    api_key: str = "",
    secret_key: str = "",
    passphrase: str = "",
    testnet: bool = True,
    **kwargs,
) -> ExchangeAdapter:
    """
    工厂函数：创建交易所适配器实例

    Args:
        exchange_type: 交易所类型 (OKX / BINANCE)
        api_key: API密钥
        secret_key: 密钥
        passphrase: 口令（OKX需要，Binance忽略）
        testnet: 是否使用测试网
        **kwargs: 其他参数（如 use_futures for Binance）

    Returns:
        ExchangeAdapter实例

    Example:
        # 创建OKX适配器
        okx = create_exchange_adapter(
            ExchangeType.OKX,
            api_key="xxx",
            secret_key="yyy",
            passphrase="zzz"
        )

        # 创建Binance期货测试网适配器
        binance = create_exchange_adapter(
            ExchangeType.BINANCE,
            api_key="xxx",
            secret_key="yyy",
            testnet=True,
            use_futures=True
        )
    """
    if exchange_type == ExchangeType.OKX:
        return OKXAdapter(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            testnet=testnet,
        )
    elif exchange_type == ExchangeType.BINANCE:
        return BinanceAdapter(
            api_key=api_key,
            secret_key=secret_key,
            passphrase=passphrase,
            testnet=testnet,
            use_futures=kwargs.get("use_futures", False),
        )
    else:
        raise ValueError(f"Unsupported exchange type: {exchange_type}")


# ═══════════════════════════════════════════════════════════════════════════
# 便捷函数
# ═══════════════════════════════════════════════════════════════════════════

def get_default_okx_adapter() -> OKXAdapter:
    """
    从环境变量创建默认OKX适配器

    环境变量:
        OKX_API_KEY
        OKX_SECRET_KEY
        OKX_PASSPHRASE
        OKX_FLAG (1=模拟, 0=真实)
    """
    from dotenv import load_dotenv
    load_dotenv(Path.home() / '.hermes' / '.env', override=True)

    return OKXAdapter(
        api_key=os.getenv('OKX_API_KEY', ''),
        secret_key=os.getenv('OKX_SECRET_KEY', ''),
        passphrase=os.getenv('OKX_PASSPHRASE', ''),
        testnet=os.getenv('OKX_FLAG', '1') == '1',
    )


def get_default_binance_adapter(use_futures: bool = False) -> BinanceAdapter:
    """
    从环境变量创建默认Binance适配器

    环境变量:
        BINANCE_API_KEY
        BINANCE_SECRET_KEY
        BINANCE_TESTNET (true/false)
    """
    from dotenv import load_dotenv
    load_dotenv(Path.home() / '.hermes' / '.env', override=True)

    return BinanceAdapter(
        api_key=os.getenv('BINANCE_API_KEY', ''),
        secret_key=os.getenv('BINANCE_SECRET_KEY', ''),
        testnet=os.getenv('BINANCE_TESTNET', 'true').lower() == 'true',
        use_futures=use_futures,
    )


# ═══════════════════════════════════════════════════════════════════════════
# 统一接口示例
# ═══════════════════════════════════════════════════════════════════════════

def example_unified_usage():
    """
    统一接口使用示例

    无论使用哪个交易所，API调用方式都是一样的
    """
    # 创建适配器（可以从配置/环境变量读取）
    okx = create_exchange_adapter(
        ExchangeType.OKX,
        api_key="your_okx_key",
        secret_key="your_okx_secret",
        passphrase="your_passphrase",
        testnet=True,
    )

    binance = create_exchange_adapter(
        ExchangeType.BINANCE,
        api_key="your_binance_key",
        secret_key="your_binance_secret",
        testnet=True,
    )

    # 统一调用方式
    symbols = ["BTC-USDT", "ETH-USDT"]

    for adapter in [okx, binance]:
        print(f"\n{'='*50}")
        print(f"交易所: {adapter.name}")
        print(f"{'='*50}")

        # 获取多个行情
        for symbol in symbols:
            ticker = adapter.get_ticker(symbol)
            if ticker:
                print(f"  {symbol}: ${ticker.last:.2f} ({ticker.price_change_pct:+.2f}%)")

        # 获取K线
        candles = adapter.get_candles("BTC-USDT", bar="1H", limit=10)
        print(f"  K线数量: {len(candles)}")

        # 获取余额
        balance = adapter.get_balance()
        if balance:
            print(f"  余额: ${balance.available:.2f} (可用)")

        # 获取持仓
        positions = adapter.get_positions()
        print(f"  持仓数: {len(positions)}")


if __name__ == "__main__":
    print("OKX/Binance 双交易所适配层 v1.0.0")
    print("=" * 50)
    print("\n使用方法:")
    print("  from execution.exchange_adapter import create_exchange_adapter, ExchangeType")
    print()
    print("  # OKX")
    print("  okx = create_exchange_adapter(ExchangeType.OKX, api_key, secret_key, passphrase)")
    print()
    print("  # Binance")
    print("  binance = create_exchange_adapter(ExchangeType.BINANCE, api_key, secret_key)")
    print()
    print("\n统一接口:")
    print("  adapter.get_ticker('BTC-USDT')   # 行情")
    print("  adapter.get_candles('BTC-USDT')   # K线")
    print("  adapter.get_balance()            # 余额")
    print("  adapter.get_positions()          # 持仓")
    print("  adapter.place_order(...)          # 下单")
    print("  adapter.cancel_order(...)         # 取消")
    print("  adapter.get_orderbook(...)       # 订单簿")