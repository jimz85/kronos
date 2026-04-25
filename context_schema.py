#!/usr/bin/env python3
"""
factor_context.json Schema Definition
====================================
Kronos 双层架构：MiniMax(小时) → gemma4(3分钟) 战略上下文传递

Schema 版本: v1.0 | 2026-04-21
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime
from typing import Optional
import json, os

# ========== 常量 ==========
CTX_FILE = os.path.expanduser("~/.hermes/cron/output/factor_context.json")
EMERGENCY_FILE = os.path.expanduser("~/.hermes/cron/output/emergency_stop.json")
AUDIT_FILE = os.path.expanduser("~/.hermes/cron/output/audit_log.jsonl")
SCHEMA_VERSION = "1.0"

# 允许的枚举值（用于Schema Validation）
VALID_REGIMES = {"bull", "bear", "neutral", "volatile", "neutral/volatile"}
VALID_DIRECTIONS = {"long", "short", "both", "none"}
VALID_ULTRADANGER_LEVELS = {"none", "watch", "elevated", "high", "ultra"}
VALID_FACTOR_STATUS = {"active", "inactive", "degraded", "unknown"}


# ========== 数据模型 ==========

@dataclass
class FactorStatus:
    """单个因子的状态"""
    status: str = "unknown"          # active/inactive/degraded/unknown
    ic: float = 0.0                  # IC值
    confidence: float = 0.5           # 置信度 0-1
    note: str = ""


@dataclass
class PositionContext:
    """持仓上下文摘要"""
    coin: str = ""
    direction: str = ""               # long/short
    pnl_pct: float = 0.0
    sl_distance_pct: float = 0.0
    holding_hours: int = 0
    status: str = "unknown"           # healthy/warning/critical


@dataclass
class EmergencyStop:
    """紧急干预指令"""
    level: str = "none"              # none/watch/elevated/high/ultra
    reason: str = ""
    affected_coins: list = field(default_factory=list)
    action: str = "none"             # none/pause_new/close_all/close_affected
    until_ts: int = 0                # 过期时间戳


# ========== 主Schema ==========

@dataclass
class FactorContext:
    """
    factor_context.json 的完整结构
    由 gemma4_hourly_review.py 写入
    由 kronos_multi_coin.py 读取
    """
    # === 元信息 ===
    schema_version: str = SCHEMA_VERSION
    generated_at: str = ""           # ISO格式时间戳
    generated_by: str = "MiniMax-M2.7"
    next_update_at: str = ""         # 下次更新时间（用于检测空窗期）

    # === 市场环境 ===
    market_regime: str = "neutral"   # bull/bear/neutral/volatile
    regime_confidence: float = 0.5    # 0-1
    btc_trend: str = "neutral"       # 来自 get_btc_regime()
    eth_trend: str = "neutral"
    overall_confidence: float = 0.5   # MiniMax对自己判断的置信度

    # === 因子健康状态 ===
    factor_status: dict = field(default_factory=dict)
    # 格式: {"vol_ratio": FactorStatus, "rsi": FactorStatus, ...}

    # === 方向建议 ===
    primary_direction: str = "both"  # long/short/both/none（none=不建议开仓）
    direction_confidence: float = 0.5
    forbidden_actions: list = field(default_factory=list)
    # 格式: ["short_btc", "long_avax_4h"] 等

    # === 仓位上下文 ===
    current_positions: list = field(default_factory=list)
    # 格式: [PositionContext, ...]
    total_exposure_pct: float = 0.0   # 总保证金占用%
    max_total_leverage: float = 1.5   # 允许的最大总杠杆

    # === 战略建议 ===
    strategic_hint: str = ""         # 自然语言提示给gemma4
    confidence: float = 0.5           # 整体置信度（用于gemma4调整激进程度）

    # === 紧急干预 ===
    emergency_stop: EmergencyStop = field(default_factory=EmergencyStop)

    # === 审计信息 ===
    audit_id: str = ""               # 本次上下文的唯一ID（用于追溯）

    def to_dict(self) -> dict:
        d = asdict(self)
        # 嵌套对象需要手动序列化
        d['emergency_stop'] = asdict(self.emergency_stop)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> 'FactorContext':
        es_data = d.pop('emergency_stop', {})
        fc = cls(**d)
        fc.emergency_stop = EmergencyStop(**es_data) if es_data else EmergencyStop()
        return fc

    def save(self, path: str = CTX_FILE):
        self.generated_at = datetime.now().isoformat()
        next_h = (datetime.now().timestamp() + 3600) * 1000
        self.next_update_at = str(int(next_h))
        self.audit_id = f"ctx_{datetime.now().strftime('%Y%m%d%H%M%S')}"
        with open(path, 'w') as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str = CTX_FILE) -> 'FactorContext':
        try:
            with open(path) as f:
                d = json.load(f)
            fc = cls.from_dict(d)
            # 空窗期检测：超过90分钟未更新（使用 generated_at）
            if fc.generated_at:
                import time
                gen_ts = datetime.fromisoformat(fc.generated_at).timestamp()
                age_hours = (time.time() - gen_ts) / 3600
                if age_hours > 1.5:  # 超过90分钟
                    fc.regime_confidence = max(0.0, fc.regime_confidence - 0.5)
                    fc.strategic_hint = f"[WARN] 上下文已过期{age_hours:.1f}h，使用保守策略"
                    fc.confidence = max(0.0, fc.confidence - 0.4)
            return fc
        except:
            return cls()  # 返回默认空上下文

    @classmethod
    def get_default(cls) -> 'FactorContext':
        """安全的默认上下文（永远不崩溃）"""
        return cls(
            market_regime="neutral",
            regime_confidence=0.0,
            overall_confidence=0.0,
            strategic_hint="[FALLBACK] 无有效上下文，使用保守均值回归策略",
            primary_direction="both",
            direction_confidence=0.0,
        )


# ========== Schema Validation（防止结构化幻觉） ==========

def validate_context(ctx: FactorContext) -> tuple[bool, str]:
    """验证上下文字段合法性，返回 (is_valid, error_msg)"""
    if ctx.market_regime not in VALID_REGIMES:
        return False, f"Invalid regime: {ctx.market_regime}"
    if ctx.primary_direction not in VALID_DIRECTIONS:
        return False, f"Invalid direction: {ctx.primary_direction}"
    if ctx.emergency_stop.level not in VALID_ULTRADANGER_LEVELS:
        return False, f"Invalid emergency level: {ctx.emergency_stop.level}"
    if not (0 <= ctx.confidence <= 1):
        return False, f"Confidence out of range: {ctx.confidence}"
    return True, ""


def load_context_with_validation(path: str = CTX_FILE) -> FactorContext:
    """带验证的加载，失败时返回默认上下文"""
    ctx = FactorContext.load(path)
    valid, err = validate_context(ctx)
    if not valid:
        print(f"[WARN] factor_context.json validation failed: {err}，使用默认上下文")
        return FactorContext.get_default()
    return ctx


# ========== Emergency Stop ==========

def read_emergency_stop(path: str = EMERGENCY_FILE) -> Optional[EmergencyStop]:
    """读取紧急干预状态，返回None表示无紧急干预"""
    try:
        if not os.path.exists(path):
            return None
        with open(path) as f:
            d = json.load(f)
        es = EmergencyStop(**d)
        # 检查是否过期
        if es.until_ts and es.until_ts < datetime.now().timestamp() * 1000:
            os.remove(path)
            return None
        return es
    except:
        return None


def write_emergency_stop(es: EmergencyStop, path: str = EMERGENCY_FILE):
    """写入紧急干预指令"""
    with open(path, 'w') as f:
        json.dump(asdict(es), f, ensure_ascii=False)


def clear_emergency_stop(path: str = EMERGENCY_FILE):
    """清除紧急干预"""
    if os.path.exists(path):
        os.remove(path)


# ========== 审计日志 ==========

def append_audit(audit_entry: dict, path: str = AUDIT_FILE):
    """追加审计日志（JSONL格式，每行一条）"""
    try:
        import os
        with open(path, 'a') as f:
            f.write(json.dumps(audit_entry, ensure_ascii=False) + '\n')
    except:
        pass
