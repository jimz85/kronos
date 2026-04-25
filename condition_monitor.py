#!/usr/bin/env python3
"""
condition_monitor.py
实时市场条件监控 + 触发告警

不是自动交易，而是：
- 实时监控市场状态变化
- 当特定条件满足时，通知我（Hermes）
- 我来判断是否执行交易

条件触发类型：
1. 市场状态变化（TREND → RANGE 等）
2. 信号触发（RSI超卖、BB突破）
3. 异常预警（波动率急剧变化、成交量异常）
4. 机会窗口（多个指标共振）

每次触发都记录原因，不带情绪地执行。
"""
import time
import threading
import json
from datetime import datetime
from pathlib import Path
from collections import deque

# ─── 配置 ────────────────────────────────────────────────────
ALERT_LOG_FILE = Path(__file__).parent / "alert_log.json"
TRIGGERED_ALERTS_FILE = Path(__file__).parent / "triggered_alerts.json"

# ─── 告警级别 ───────────────────────────────────────────────
class AlertLevel(Enum):
    INFO = "info"           # 信息，不紧急
    SIGNAL = "signal"       # 交易信号，需要关注
    URGENT = "urgent"       # 紧急，需要立即处理
    CRITICAL = "critical"   # 极端，可能需要止损

from enum import Enum

# ─── 告警记录 ────────────────────────────────────────────────
class Alert:
    def __init__(self, level, symbol, alert_type, message, details=None):
        self.level = level
        self.symbol = symbol
        self.type = alert_type
        self.message = message
        self.details = details or {}
        self.timestamp = datetime.now().isoformat()
        self.id = f"{symbol}_{alert_type}_{int(time.time())}"
    
    def to_dict(self):
        return {
            "id": self.id,
            "level": self.level.value,
            "symbol": self.symbol,
            "type": self.type,
            "message": self.message,
            "details": self.details,
            "timestamp": self.timestamp
        }

# ─── 条件触发器基类 ─────────────────────────────────────────
class ConditionTrigger:
    def __init__(self, name, symbol, params=None):
        self.name = name
        self.symbol = symbol
        self.params = params or {}
        self.last_triggered = None
        self.trigger_count = 0
        self.cooldown_seconds = params.get("cooldown", 3600)  # 默认1小时冷却
    
    def check(self, market_data) -> Alert | None:
        """检查是否触发，返回Alert或None"""
        raise NotImplementedError
    
    def can_trigger(self):
        """检查是否在冷却期"""
        if self.last_triggered is None:
            return True
        return time.time() - self.last_triggered > self.cooldown_seconds
    
    def record_trigger(self):
        self.last_triggered = time.time()
        self.trigger_count += 1

# ─── 具体触发器实现 ─────────────────────────────────────────

class RSIOversoldTrigger(ConditionTrigger):
    """RSI超卖触发器"""
    def __init__(self, symbol, rsi_threshold=35, cooldown=7200):
        super().__init__("RSI_OVERSOLD", symbol, {"rsi_threshold": rsi_threshold, "cooldown": cooldown})
    
    def check(self, market_data) -> Alert | None:
        rsi = market_data.get("rsi_14")
        if rsi is None:
            return None
        
        if rsi < self.params["rsi_threshold"] and self.can_trigger():
            self.record_trigger()
            return Alert(
                AlertLevel.SIGNAL,
                self.symbol,
                "RSI_OVERSOLD",
                f"RSI 超卖: {rsi:.1f} < {self.params['rsi_threshold']}",
                {"rsi": rsi, "threshold": self.params["rsi_threshold"]}
            )
        return None

class BBBreakoutTrigger(ConditionTrigger):
    """布林带突破触发器"""
    def __init__(self, symbol, cooldown=7200):
        super().__init__("BB_BREAKOUT", symbol, {"cooldown": cooldown})
    
    def check(self, market_data) -> Alert | None:
        price = market_data.get("close")
        bb_upper = market_data.get("bb_upper")
        bb_lower = market_data.get("bb_lower")
        
        if None in [price, bb_upper, bb_lower]:
            return None
        
        # 上轨突破
        if price > bb_upper and self.can_trigger():
            self.record_trigger()
            return Alert(
                AlertLevel.SIGNAL,
                self.symbol,
                "BB_UPPER_BREAK",
                f"价格突破布林上轨: {price:.2f} > {bb_upper:.2f}",
                {"price": price, "bb_upper": bb_upper}
            )
        
        # 下轨突破
        if price < bb_lower and self.can_trigger():
            self.record_trigger()
            return Alert(
                AlertLevel.SIGNAL,
                self.symbol,
                "BB_LOWER_BREAK",
                f"价格突破布林下轨: {price:.2f} < {bb_lower:.2f}",
                {"price": price, "bb_lower": bb_lower}
            )
        
        return None

class VolatilitySpikeTrigger(ConditionTrigger):
    """波动率急剧变化触发器"""
    def __init__(self, symbol, spike_threshold=2.0, cooldown=3600):
        super().__init__("VOL_SPIKE", symbol, {"spike_threshold": spike_threshold, "cooldown": cooldown})
    
    def check(self, market_data) -> Alert | None:
        current_vol = market_data.get("vol_current")
        avg_vol = market_data.get("vol_avg")
        
        if None in [current_vol, avg_vol] or avg_vol == 0:
            return None
        
        ratio = current_vol / avg_vol
        
        if ratio > self.params["spike_threshold"] and self.can_trigger():
            self.record_trigger()
            return Alert(
                AlertLevel.URGENT,
                self.symbol,
                "VOL_SPIKE",
                f"波动率飙升: {ratio:.1f}x 历史均值",
                {"current_vol": current_vol, "avg_vol": avg_vol, "ratio": ratio}
            )
        
        return None

class RegimeChangeTrigger(ConditionTrigger):
    """市场状态变化触发器"""
    def __init__(self, symbol, cooldown=7200):
        super().__init__("REGIME_CHANGE", symbol, {"cooldown": cooldown})
        self.last_regime = None
    
    def check(self, market_data) -> Alert | None:
        regime = market_data.get("market_regime")
        if regime is None or regime == "unknown":
            return None
        
        if self.last_regime is not None and regime != self.last_regime and self.can_trigger():
            self.record_trigger()
            level = AlertLevel.URGENT if regime in ["trend_down", "high_vol"] else AlertLevel.INFO
            return Alert(
                level,
                self.symbol,
                "REGIME_CHANGE",
                f"市场状态变化: {self.last_regime} → {regime}",
                {"from": self.last_regime, "to": regime}
            )
        
        self.last_regime = regime
        return None

# ─── 监控器主类 ─────────────────────────────────────────────
class ConditionMonitor:
    def __init__(self, symbols=["BTC-USDT", "ETH-USDT", "BNB-USDT", "SOL-USDT"]):
        self.symbols = symbols
        self.triggers = []
        self.alert_history = []
        self.running = False
        self._lock = threading.Lock()
        
        # 初始化触发器
        self._setup_triggers()
    
    def _setup_triggers(self):
        """设置所有触发器"""
        for symbol in self.symbols:
            # RSI 超卖
            self.triggers.append(RSIOversoldTrigger(symbol, rsi_threshold=35))
            # 布林带突破
            self.triggers.append(BBBreakoutTrigger(symbol))
            # 波动率飙升
            self.triggers.append(VolatilitySpikeTrigger(symbol, spike_threshold=2.0))
            # 市场状态变化
            self.triggers.append(RegimeChangeTrigger(symbol))
    
    def check_all(self, market_data_by_symbol) -> list[Alert]:
        """检查所有触发器，返回触发告警列表"""
        alerts = []
        for symbol, data in market_data_by_symbol.items():
            for trigger in self.triggers:
                if trigger.symbol == symbol:
                    alert = trigger.check(data)
                    if alert:
                        alerts.append(alert)
        return alerts
    
    def process_alerts(self, alerts):
        """处理告警：记录 + 通知"""
        with self._lock:
            for alert in alerts:
                self.alert_history.append(alert)
                # 记录到文件
                self._save_alert(alert)
                # 打印通知
                self._notify(alert)
    
    def _save_alert(self, alert):
        """保存告警到文件"""
        path = TRIGGERED_ALERTS_FILE
        alerts = []
        if path.exists():
            with open(path) as f:
                alerts = json.load(f)
        alerts.append(alert.to_dict())
        # 只保留最近100条
        alerts = alerts[-100:]
        with open(path, "w") as f:
            json.dump(alerts, f, indent=2)
    
    def _notify(self, alert):
        """通知：有新的告警触发"""
        level_str = {
            "info": "ℹ️",
            "signal": "📋",
            "urgent": "⚠️",
            "critical": "🚨"
        }.get(alert.level.value, "?")
        
        print(f"\n{level_str} [{alert.symbol}] {alert.type}")
        print(f"   {alert.message}")
        if alert.details:
            for k, v in alert.details.items():
                print(f"   {k}: {v}")
        print()

# ─── 主循环 ─────────────────────────────────────────────────
def run_monitor_loop(monitor, check_interval=60):
    """
    运行监控循环
    check_interval: 检查间隔（秒）
    """
    print(f"\n🔔 Condition Monitor 启动")
    print(f"   监控币种: {', '.join(monitor.symbols)}")
    print(f"   检查间隔: {check_interval}秒\n")
    
    monitor.running = True
    
    while monitor.running:
        try:
            # TODO: 接入实时数据
            # market_data = get_market_data(monitor.symbols)
            # alerts = monitor.check_all(market_data)
            # monitor.process_alerts(alerts)
            pass
        except Exception as e:
            print(f"监控错误: {e}")
        
        time.sleep(check_interval)

if __name__ == "__main__":
    monitor = ConditionMonitor()
    run_monitor_loop(monitor)
