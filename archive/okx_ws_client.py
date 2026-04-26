#!/usr/bin/env python3
"""
OKX WebSocket 实时市场数据模块
为 market_sense.py 提供实时数据流

功能：
- 实时价格（ticker）- 每秒更新
- 订单簿深度（books5）- 每100ms更新
- 成交量（fills）- 实时成交

无需 API Key（公开频道）
"""
import threading
import time
import json
import websocket
import pandas as pd
import numpy as np
from collections import deque
from datetime import datetime

# ==================== 配置 ====================
OKX_WS_PUBLIC = "wss://ws.okx.com:8443/ws/v5/public"

# 订阅频道
TICKER_CHANNEL = "tickers"      # 实时价格
ORDERBOOK_CHANNEL = "books5"     # 5档订单簿
TRADES_CHANNEL = "trades"       # 实时成交

# 默认交易对
DEFAULT_INST = "SOL-USDT-SWAP"  # 主力交易对

# ==================== 数据缓存 ====================
class MarketDataBuffer:
    """
    实时市场数据缓存
    线程安全，支持多用户读取
    """

    def __init__(self, max_ticks=1000, max_trades=500):
        self._lock = threading.RLock()
        self.last_price = None
        self.price_change_1m = 0.0
        self.price_change_5m = 0.0
        self.price_change_1h = 0.0
        self.vol_24h = 0.0
        self.high_24h = 0.0
        self.low_24h = 0.0
        self.open_24h = 0.0
        self.bid_price = 0.0
        self.ask_price = 0.0
        self.bid_vol = 0.0
        self.ask_vol = 0.0
        self.orderbook_imbalance = 0.0  # (bid_vol - ask_vol) / (bid_vol + ask_vol)

        # 历史价格（用于计算波动率和变化率）
        self._price_history = deque(maxlen=3600)  # 最近3600个价格点（按推送频率，约30分钟）
        self._price_timestamps = deque(maxlen=3600)

        # 成交量历史（按分钟）
        self._vol_per_minute = deque(maxlen=60)  # 最近60分钟的成交量

        # 实时成交
        self._recent_trades = deque(maxlen=max_trades)

        # 订单簿历史
        self._ob_history = deque(maxlen=100)

        # 累计值（用于计算变化率）
        self._price_at_1m_ago = None

        # 时间
        self.last_update = time.time()
        self.connected = False

    def update_ticker(self, data):
        """更新 ticker 数据"""
        with self._lock:
            ts = data.get('ts', data.get('unixTime', time.time() * 1000))
            price = float(data.get('last', 0))
            if price == 0:
                return

            now = time.time()
            self.last_price = price
            self.last_update = now

            # 24h 数据
            self.price_change_1m = data.get('change24h', 0)  # OKX ticker 有这些字段
            # 24h 数据
            open_24h = float(data.get('open24h', 0))
            last_px = float(data.get('last', 0))

            self.price_change_1h = (last_px - open_24h) / open_24h * 100 if open_24h > 0 else 0

            # 高低
            self.high_24h = float(data.get('high24h', 0))
            self.low_24h = float(data.get('low24h', 0))
            self.open_24h = open_24h
            self.vol_24h = float(data.get('vol24h', 0))

            # 记录价格历史
            self._price_history.append(last_px)
            self._price_timestamps.append(ts)

            # 计算1m/5m/1h变化率（从ticker累积的历史）
            if len(self._price_history) >= 60:
                self.price_change_1m = (self._price_history[-1] - self._price_history[-60]) / self._price_history[-60] * 100
            if len(self._price_history) >= 300:
                self.price_change_5m = (self._price_history[-1] - self._price_history[-300]) / self._price_history[-300] * 100

    def update_orderbook(self, data):
        """更新订单簿"""
        with self._lock:
            bids = data.get('bids', [])
            asks = data.get('asks', [])

            if not bids or not asks:
                return

            # 最佳买卖价和量
            self.bid_price = float(bids[0][0])
            self.ask_price = float(asks[0][0])
            self.bid_vol = float(bids[0][1])
            self.ask_vol = float(asks[0][1])

            # 订单簿不平衡度
            total_bid_vol = sum(float(b[1]) for b in bids[:5])
            total_ask_vol = sum(float(a[1]) for a in asks[:5])
            self.orderbook_imbalance = (total_bid_vol - total_ask_vol) / (total_bid_vol + total_ask_vol + 1e-10)

            # 记录历史
            self._ob_history.append({
                'time': time.time(),
                'bid_price': self.bid_price,
                'ask_price': self.ask_price,
                'bid_vol': total_bid_vol,
                'ask_vol': total_ask_vol,
                'imbalance': self.orderbook_imbalance,
            })

    def update_trade(self, data):
        """更新成交"""
        with self._lock:
            for trade in data if isinstance(data, list) else [data]:
                self._recent_trades.append({
                    'time': trade.get('ts', time.time() * 1000),
                    'price': float(trade.get('px', 0)),
                    'volume': float(trade.get('sz', 0)),
                    'side': trade.get('side', ''),  # buy/sell
                    'trade_id': trade.get('tradeId', ''),
                })

    def get_features(self):
        """提取盘感特征"""
        with self._lock:
            if not self.last_price or self.last_price == 0:
                return None

            now = time.time()
            features = {}

            # 价格
            features['last_price'] = self.last_price
            features['price_change_1m'] = self.price_change_1m
            features['price_change_5m'] = self.price_change_5m
            features['price_change_1h'] = self.price_change_1h
            features['vol_24h'] = self.vol_24h

            # 波动率（最近60个价格点的标准差）
            if len(self._price_history) >= 5:
                prices = np.array(list(self._price_history))
                returns = np.diff(np.log(prices))
                features['vol_1m'] = float(np.std(returns[-5:]) * 100) if len(returns) >= 5 else 0
                features['vol_5m'] = float(np.std(returns[-60:]) * 100) if len(returns) >= 60 else features['vol_1m']
            else:
                features['vol_1m'] = 0
                features['vol_5m'] = 0

            # 波动率趋势
            if len(self._price_history) >= 120:
                early_vol = np.std(list(self._price_history)[-120:-60])
                late_vol = np.std(list(self._price_history)[-60:])
                features['vol_accelerating'] = late_vol > early_vol * 1.3
                features['vol_contracting'] = late_vol < early_vol * 0.7
            else:
                features['vol_accelerating'] = False
                features['vol_contracting'] = False

            # 订单簿
            features['bid_price'] = self.bid_price
            features['ask_price'] = self.ask_price
            features['bid_vol'] = self.bid_vol
            features['ask_vol'] = self.ask_vol
            features['orderbook_imbalance'] = self.orderbook_imbalance
            features['spread'] = (self.ask_price - self.bid_price) / self.last_price * 100

            # 成交量异常
            if len(self._recent_trades) >= 10:
                recent_vol = sum(t['volume'] for t in list(self._recent_trades)[-10:])
                avg_vol = recent_vol / 10
                features['volume_spike'] = recent_vol > avg_vol * 3
                features['volume_dry'] = recent_vol < avg_vol * 0.3
            else:
                features['volume_spike'] = False
                features['volume_dry'] = False

            # 大单检测（单笔成交 > 10倍平均）
            if len(self._recent_trades) >= 20:
                avg_trade_vol = np.mean([t['volume'] for t in list(self._recent_trades)[-20:]])
                big_trades = [t for t in self._recent_trades if t['volume'] > avg_trade_vol * 10]
                features['big_trade_count'] = len(big_trades)
                features['big_trade_alert'] = len(big_trades) >= 3
            else:
                features['big_trade_count'] = 0
                features['big_trade_alert'] = False

            # 瞬时涨跌（最近5笔成交）
            if len(self._recent_trades) >= 5:
                recent_5 = list(self._recent_trades)[-5:]
                first_px = recent_5[0]['price']
                last_px = recent_5[-1]['price']
                pulse = (last_px - first_px) / first_px * 100
                features['sudden_pump'] = pulse > 0.5
                features['sudden_dump'] = pulse < -0.5
            else:
                features['sudden_pump'] = False
                features['sudden_dump'] = False

            # 订单簿变化速度（最近10个状态的订单簿不平衡度变化）
            if len(self._ob_history) >= 10:
                imbalances = [h['imbalance'] for h in list(self._ob_history)[-10:]]
                features['ob_instability'] = float(np.std(imbalances))
            else:
                features['ob_instability'] = 0

            features['last_update'] = now
            return features

    def get_summary(self):
        """获取简要状态"""
        with self._lock:
            return {
                'price': self.last_price,
                'change_1h': self.price_change_1h,
                'vol': self.vol_24h,
                'ob_imbalance': self.orderbook_imbalance,
                'spread_pct': (self.ask_price - self.bid_price) / self.last_price * 100 if self.last_price > 0 else 0,
                'connected': self.connected,
                'data_age': time.time() - self.last_update if self.last_update else 999,
            }


# ==================== WebSocket 客户端 ====================
class OKXWebSocket:
    """
    OKX WebSocket 客户端
    连接到公共频道获取实时市场数据
    """

    def __init__(self, inst_id=DEFAULT_INST, on_data=None):
        self.inst_id = inst_id
        self.on_data = on_data  # 回调函数
        self.buffer = MarketDataBuffer()
        self.ws = None
        self._running = False
        self._thread = None
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60

    def _make_subscribe(self, channels):
        """生成订阅消息"""
        return {
            'op': 'subscribe',
            'args': [{'channel': ch, 'instId': self.inst_id} for ch in channels]
        }

    def _on_message(self, ws, message):
        try:
            data = json.loads(message)

            # 处理心跳
            if data.get('event') in ('subscribe', 'error'):
                return

            # 解析数据
            arg = data.get('arg', {})
            channel = arg.get('channel', '')
            items = data.get('data', [])

            if not items:
                return

            for item in items:
                if channel == 'tickers':
                    self.buffer.update_ticker(item)
                elif channel == 'books5':
                    self.buffer.update_orderbook(item)
                elif channel == 'trades':
                    self.buffer.update_trade(item)

                # 触发回调
                if self.on_data:
                    self.on_data(channel, item, self.buffer.get_features())

        except json.JSONDecodeError:
            pass
        except Exception as e:
            pass  # 静默处理，避免断开

    def _on_error(self, ws, error):
        pass  # 不打印错误，避免噪音

    def _on_close(self, ws, close_status_code, close_msg):
        self.buffer.connected = False
        if self._running:
            self._reconnect()

    def _on_open(self, ws):
        self.buffer.connected = True
        self._reconnect_delay = 1  # 重置退避

        # 订阅 ticker + 订单簿 + 成交
        subscribe = self._make_subscribe(['tickers', 'books5', 'trades'])
        ws.send(json.dumps(subscribe))

    def _reconnect(self):
        """自动重连"""
        if not self._running:
            return
        time.sleep(self._reconnect_delay)
        self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)
        self.connect()

    def connect(self):
        """启动 WebSocket 连接"""
        if self._thread and self._thread.is_alive():
            return

        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def disconnect(self):
        """断开连接"""
        self._running = False
        if self.ws:
            try:
                self.ws.close()
            except:
                pass

    def _run(self):
        while self._running:
            try:
                self.ws = websocket.WebSocketApp(
                    OKX_WS_PUBLIC,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                    on_open=self._on_open,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                pass
            if self._running:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)


# ==================== 便捷接口 ====================
# 全局单例
_global_ws = None
_global_buffer = None

def start_real_time(coin='SOL-USDT-SWAP'):
    """启动实时数据流"""
    global _global_ws, _global_buffer
    if _global_ws is None:
        _global_buffer = MarketDataBuffer()
        _global_ws = OKXWebSocket(inst_id=coin, on_data=None)
        _global_ws.connect()
        time.sleep(2)  # 等待首次数据
    return _global_ws, _global_buffer

def get_current_features(coin='SOL-USDT-SWAP'):
    """获取当前市场特征（需要先 start_real_time）"""
    global _global_ws, _global_buffer
    if _global_buffer is None:
        start_real_time(coin)
    return _global_buffer.get_features()

def stop_real_time():
    """停止实时数据流"""
    global _global_ws, _global_buffer
    if _global_ws:
        _global_ws.disconnect()
        _global_ws = None
        _global_buffer = None


# ==================== 测试 ====================
if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser(description='OKX WebSocket 实时数据')
    parser.add_argument('--coin', default='SOL-USDT-SWAP', help='交易对')
    parser.add_argument('--duration', type=int, default=30, help='运行秒数')
    args = parser.parse_args()

    print(f"启动 OKX WebSocket: {args.coin}")
    print("-" * 50)

    ws = OKXWebSocket(inst_id=args.coin)
    ws.connect()

    print(f"连接中... (等待 {args.duration} 秒)")
    time.sleep(args.duration)

    # 输出最新数据
    features = ws.buffer.get_features()
    if features:
        print(f"\n实时特征 ({args.coin}):")
        print(f"  当前价格: ${features['last_price']:.4f}")
        print(f"  1h涨跌: {features['price_change_1h']:+.2f}%")
        print(f"  5m涨跌: {features['price_change_5m']:+.2f}%")
        print(f"  1m波动率: {features['vol_1m']:.4f}%")
        print(f"  5m波动率: {features['vol_5m']:.4f}%")
        print(f"  波动加速: {features['vol_accelerating']}")
        print(f"  波动收缩: {features['vol_contracting']}")
        print(f"  订单簿不平衡: {features['orderbook_imbalance']:+.3f}")
        print(f"  买卖价差: {features['spread']:.4f}%")
        print(f"  大单预警: {features['big_trade_alert']} ({features['big_trade_count']}笔)")
        print(f"  瞬时急拉: {features['sudden_pump']}, 急砸: {features['sudden_dump']}")
        print(f"  数据延迟: {time.time() - features['last_update']:.1f}秒")
    else:
        print("未获取到数据，请检查网络")

    ws.disconnect()
    print("\n已断开连接")
