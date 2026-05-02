#!/usr/bin/env python3
"""
trade_logger.py - 统一交易溯源日志系统
=========================================

解决核心问题：信号→订单→持仓→PnL→权益 全链路断链

使用 trade_id 贯穿每个交易生命周期，所有关键事件都记录到结构化JSONL文件。
每个交易可完整追溯。

核心文件：
  ~/kronos/logs/trades_{date}.jsonl     - 所有交易全生命周期
  ~/kronos/logs/equity_{date}.jsonl     - 账户权益快照
  ~/kronos/logs/signals_{date}.jsonl     - 信号记录

用法：
  from trade_logger import log_signal, log_order_filled, log_position_update, log_trade_close, log_equity

Version: 1.0.0
"""

import os, sys, json, uuid, logging
from pathlib import Path
from datetime import datetime, date
from typing import Optional, Dict, Any, List
from collections import defaultdict

# ─────────────────────────────────────────────────────────────
# 路径配置
# ─────────────────────────────────────────────────────────────
ROOT = Path.home() / "kronos"
LOGS_DIR = ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)

# ─────────────────────────────────────────────────────────────
# 日志器配置
# ─────────────────────────────────────────────────────────────
logger = logging.getLogger("kronos.trade_logger")
if not logger.handlers:
    _ch = logging.StreamHandler()
    _ch.setLevel(logging.INFO)
    _fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
    _ch.setFormatter(_fmt)
    logger.addHandler(_ch)
    logger.setLevel(logging.INFO)

# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────

def _ts() -> str:
    """返回当前UTC时间ISO格式（毫秒精度）"""
    return datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _date_tag() -> str:
    """返回日期标签，用于日志文件名"""
    return datetime.utcnow().strftime("%Y-%m-%d")


def _jpath(basename: str) -> Path:
    """返回带日期的日志文件路径"""
    return LOGS_DIR / f"{basename}_{_date_tag()}.jsonl"


# ─────────────────────────────────────────────────────────────
# 核心：append_to_jsonl（所有日志的写入函数）
# ─────────────────────────────────────────────────────────────

def append_to_jsonl(path: Path, record: Dict[str, Any]) -> None:
    """原子写入JSONL：写入临时文件再rename，防止crash导致日志损坏"""
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        tmp.replace(path)
    except Exception as e:
        logger.error(f"写入日志失败 {path}: {e}")
        # 回退：直接append（可能损坏但不丢日志）
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            pass


# ─────────────────────────────────────────────────────────────
# 信号日志
# ─────────────────────────────────────────────────────────────

def log_signal(
    coin: str,
    direction: str,           # "long" | "short"
    score: float,
    confidence: float,
    signal_source: str,        # "ic_monitor" | "alpha" | "beta" | "manual" | "gemma4"
    entry_conditions: Dict[str, Any],
    reason: str = "",
    equity: float = 0.0,
    market_regime: str = "",
    candidates: List[Dict] = None,
) -> str:
    """
    记录信号生成事件。

    返回 signal_id（用于关联后续订单/持仓记录）
    """
    signal_id = str(uuid.uuid4())
    record = {
        # 核心ID
        "event": "signal_generated",
        "signal_id": signal_id,
        "ts": _ts(),
        "coin": coin,
        "direction": direction,
        "score": score,
        "confidence": confidence,
        "signal_source": signal_source,
        "entry_conditions": entry_conditions,
        "reason": reason,
        # 上下文快照
        "equity_at_signal": equity,
        "market_regime": market_regime,
        "candidates_snapshot": candidates or [],
        # 状态
        "status": "pending",  # pending → executed / rejected / expired
    }
    append_to_jsonl(_jpath("signals"), record)
    logger.info(f"[SIGNAL] {coin} {direction} score={score} signal_id={signal_id[:8]}")
    return signal_id


def update_signal_status(signal_id: str, status: str, reason: str = "") -> None:
    """更新信号状态（executed / rejected / expired）"""
    # 注意：JSONL不支持原地更新，这里只记录状态变更事件
    # 完整信号链通过 signal_id 在日志中串联
    record = {
        "event": "signal_status_changed",
        "signal_id": signal_id,
        "ts": _ts(),
        "new_status": status,
        "reason": reason,
    }
    append_to_jsonl(_jpath("signals"), record)


# ─────────────────────────────────────────────────────────────
# 订单日志
# ─────────────────────────────────────────────────────────────

def log_order_placed(
    signal_id: str,
    coin: str,
    direction: str,
    order_type: str,          # "market" | "limit"
    requested_price: float,
    requested_size: float,
    leverage: int,
    order_side: str,          # "buy" (开多) | "sell" (开空)
    pos_side: str,            # "long" | "short"
    equity: float,
) -> str:
    """
    记录订单发送事件。

    返回 trade_id
    """
    trade_id = str(uuid.uuid4())
    record = {
        "event": "order_placed",
        "trade_id": trade_id,
        "signal_id": signal_id,
        "ts": _ts(),
        "coin": coin,
        "direction": direction,
        "order_type": order_type,
        "requested_price": requested_price,
        "requested_size": requested_size,
        "leverage": leverage,
        "order_side": order_side,    # buy/sell
        "pos_side": pos_side,        # long/short
        "equity_at_order": equity,
        "status": "pending",         # pending → filled / failed / cancelled
        "order_id": None,           # 后续更新
        "fill_price": None,
        "fill_time": None,
        "fill_slippage": None,
    }
    append_to_jsonl(_jpath("trades"), record)
    logger.info(f"[ORDER] {coin} {direction} sz={requested_size} trade_id={trade_id[:8]}")
    return trade_id


def log_order_filled(
    trade_id: str,
    order_id: str,
    fill_price: float,
    fill_size: float,
    fill_time: str = None,
    slippage: float = 0.0,
    algo_ids: Dict[str, str] = None,  # {"sl": "...", "tp": "..."}
) -> None:
    """
    记录订单成交事件（最关键：连接信号和持仓）
    """
    record = {
        "event": "order_filled",
        "trade_id": trade_id,
        "ts": _ts(),
        "order_id": order_id,
        "fill_price": fill_price,
        "fill_size": fill_size,
        "fill_time": fill_time or _ts(),
        "fill_slippage": slippage,
        "algo_ids": algo_ids or {},
        "status": "open",
    }
    append_to_jsonl(_jpath("trades"), record)
    logger.info(f"[FILLED] trade_id={trade_id[:8]} order_id={order_id} fill_price={fill_price}")


def log_order_failed(
    trade_id: str,
    order_id: str,
    error_code: str,
    error_msg: str,
    equity: float,
) -> None:
    """记录订单失败事件"""
    record = {
        "event": "order_failed",
        "trade_id": trade_id,
        "ts": _ts(),
        "order_id": order_id,
        "error_code": error_code,
        "error_msg": error_msg,
        "equity_at_failure": equity,
        "status": "failed",
    }
    append_to_jsonl(_jpath("trades"), record)
    logger.error(f"[ORDER_FAIL] trade_id={trade_id[:8]} code={error_code} msg={error_msg}")


# ─────────────────────────────────────────────────────────────
# 持仓快照
# ─────────────────────────────────────────────────────────────

def log_position_update(
    positions: Dict[str, Dict],   # {coin: {size, entry, price, pnl, side, ...}}
    equity: float,
    note: str = "",
) -> None:
    """
    记录定时持仓快照（每分钟或每次决策循环调用）
    注意：这是全量快照，不是增量
    """
    record = {
        "event": "positions_snapshot",
        "ts": _ts(),
        "equity": equity,
        "positions": positions,
        "note": note,
    }
    append_to_jsonl(_jpath("positions"), record)


# ─────────────────────────────────────────────────────────────
# 平仓日志（核心PnL记录）
# ─────────────────────────────────────────────────────────────

def log_trade_close(
    trade_id: str,
    coin: str,
    direction: str,
    close_price: float,
    close_size: float,
    close_reason: str,         # "sl_triggered" | "tp_triggered" | "manual" | "force_close" | "liquidation"
    realized_pnl: float,
    close_time: str = None,
    hold_hours: float = 0.0,
    equity_before: float = 0.0,
    equity_after: float = 0.0,
    fees: float = 0.0,
    sl_price: float = None,
    tp_price: float = None,
    algo_ids_closed: Dict[str, str] = None,
) -> None:
    """
    记录平仓事件（完整PnL链路终点）
    """
    record = {
        "event": "trade_closed",
        "trade_id": trade_id,
        "ts": _ts(),
        "close_time": close_time or _ts(),
        "coin": coin,
        "direction": direction,
        "close_price": close_price,
        "close_size": close_size,
        "close_reason": close_reason,
        "realized_pnl": realized_pnl,
        "hold_hours": hold_hours,
        "fees": fees,
        "sl_price": sl_price,
        "tp_price": tp_price,
        "equity_before": equity_before,
        "equity_after": equity_after,
        "equity_change": equity_after - equity_before,
        "algo_ids_closed": algo_ids_closed or {},
        "status": "closed",
    }
    append_to_jsonl(_jpath("trades"), record)
    logger.info(
        f"[CLOSE] {coin} {direction} {close_reason} "
        f"PnL={realized_pnl:+.2f} equity={equity_before:.0f}→{equity_after:.0f}"
    )


# ─────────────────────────────────────────────────────────────
# 权益快照
# ─────────────────────────────────────────────────────────────

def log_equity(
    equity: float,
    positions: Dict[str, Dict] = None,
    unrealized_pnl: float = 0.0,
    hourly_pnl: float = 0.0,
    daily_pnl: float = 0.0,
    note: str = "",
) -> None:
    """
    记录账户权益快照（每小时/每次决策前调用）
    """
    record = {
        "event": "equity_snapshot",
        "ts": _ts(),
        "equity": equity,
        "unrealized_pnl": unrealized_pnl,
        "hourly_pnl": hourly_pnl,
        "daily_pnl": daily_pnl,
        "positions_summary": {
            coin: {
                "size": p.get("size"),
                "direction": p.get("side"),
                "entry": p.get("entry"),
                "pnl": p.get("unrealized_pnl", 0),
            }
            for coin, p in (positions or {}).items()
        },
        "note": note,
    }
    append_to_jsonl(_jpath("equity"), record)


# ─────────────────────────────────────────────────────────────
# 查询接口（用于日志分析和完整性验证）
# ─────────────────────────────────────────────────────────────

def load_trades_for_date(d: str = None) -> List[Dict]:
    """加载指定日期的所有交易事件（用于回放和分析）"""
    if d is None:
        d = _date_tag()
    path = LOGS_DIR / f"trades_{d}.jsonl"
    if not path.exists():
        return []
    events = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    pass
    return events


def build_trade_chain(trade_id: str, date_str: str = None) -> Dict[str, Any]:
    """
    根据 trade_id 构建完整交易链：
    order_placed → order_filled → trade_closed
    返回各阶段记录和断链信息
    """
    events = load_trades_for_date(date_str)
    chain = {"trade_id": trade_id, "stages": {}, "missing": []}

    for e in events:
        if e.get("trade_id") == trade_id:
            event_type = e["event"]
            chain["stages"][event_type] = e

    required = ["order_placed", "order_filled", "trade_closed"]
    for stage in required:
        if stage not in chain["stages"]:
            chain["missing"].append(stage)

    return chain


def validate_trade_chain(trade_id: str, date_str: str = None) -> tuple[bool, List[str]]:
    """
    验证交易链路完整性。
    返回 (is_complete, missing_stages)
    """
    chain = build_trade_chain(trade_id, date_str)
    is_ok = len(chain["missing"]) == 0
    return is_ok, chain["missing"]


def get_trade_summary(date_str: str = None) -> Dict[str, Any]:
    """
    获取指定日期的交易汇总（用于日报）
    """
    events = load_trades_for_date(date_str)

    signals = [e for e in events if e["event"] == "signal_generated"]
    orders_placed = [e for e in events if e["event"] == "order_placed"]
    orders_filled = [e for e in events if e["event"] == "order_filled"]
    orders_failed = [e for e in events if e["event"] == "order_failed"]
    closes = [e for e in events if e["event"] == "trade_closed"]

    realized_pnls = [e["realized_pnl"] for e in closes]
    total_pnl = sum(realized_pnls)

    return {
        "date": date_str or _date_tag(),
        "signals_generated": len(signals),
        "orders_placed": len(orders_placed),
        "orders_filled": len(orders_filled),
        "orders_failed": len(orders_failed),
        "trades_closed": len(closes),
        "total_realized_pnl": total_pnl,
        "win_count": sum(1 for p in realized_pnls if p > 0),
        "loss_count": sum(1 for p in realized_pnls if p < 0),
        "break_even_count": sum(1 for p in realized_pnls if p == 0),
    }


# ─────────────────────────────────────────────────────────────
# 测试入口
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=== Trade Logger 自检 ===")

    # 测试信号日志
    sid = log_signal("DOGE", "long", 88, 0.82, "ic_monitor",
                     {"rsi_1h": 32, "adx_1h": 28},
                     "RSI超卖+趋势确认", equity=70000,
                     market_regime="bull",
                     candidates=[{"coin": "DOGE", "score": 88}])
    print(f"signal_id: {sid}")

    # 测试订单日志
    tid = log_order_placed(sid, "DOGE", "long", "market",
                           0.0987, 1000, 3, "buy", "long", 70000)
    print(f"trade_id: {tid}")

    # 模拟成交
    log_order_filled(tid, "ORD123456", 0.0988, 1000, slippage=0.001,
                      algo_ids={"sl": "ALGO001", "tp": "ALGO002"})

    # 模拟平仓
    log_trade_close(tid, "DOGE", "long", 0.102, 1000,
                    "tp_triggered", 35.0, hold_hours=2.5,
                    equity_before=70000, equity_after=70035,
                    fees=2.5, sl_price=0.095, tp_price=0.102)

    # 权益快照
    log_equity(70035, {"DOGE": {"size": 0, "side": "long", "entry": 0.0988, "unrealized_pnl": 0}},
                unrealized_pnl=0, hourly_pnl=35, daily_pnl=-50)

    # 验证链
    is_ok, missing = validate_trade_chain(tid)
    print(f"链完整性: {is_ok}, 缺失: {missing}")

    # 汇总
    summary = get_trade_summary()
    print(f"今日汇总: {summary}")

    print("\n=== 自检完成 ===")
    print(f"日志文件位置: {LOGS_DIR}")
    import os
    for f in sorted(LOGS_DIR.glob("*.jsonl")):
        size = os.path.getsize(f)
        print(f"  {f.name}: {size} bytes")
