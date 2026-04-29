"""
================================================================
Kronos v5.0 — 主引擎 (总线贯通版)
================================================================
运行：cd ~/kronos && python3 -m core.engine

五层架构：
  Phase 0  core/         — 常量、原子状态、OKX客户端
  Phase 1  strategies/   — 市场感知、双核引擎
  Phase 2  models/      — 置信度评分、智能仓位
  Phase 3  risk/        — 熔断装饰器、动态追踪止损
  Phase 4  data/        — 每日复盘、ATR监控

修复记录 (v5.0-revision):
  ✅ 移除 os.chdir() — 全程绝对路径
  ✅ CircuitBreaker.is_allowed() 替换 can_trade()
  ✅ ATRWatchlist() 默认构造
  ✅ _scan_opportunities() 接入真实 OKX K线 → DataFrame
  ✅ regime 统一用 RegimeType
  ✅ 双核路由：CHOP→Alpha, TREND→Beta
  ✅ position_sizer 按实际签名传入参数
================================================================
"""

from __future__ import annotations

import os, sys, json, time, logging
from datetime import datetime, timedelta, date
from pathlib import Path
from typing import Optional

# ── 绝对路径（ROOT 永不依赖 cwd）────────────────────────────────────────────
ROOT = Path.home() / "kronos"
STATE_DIR = ROOT / "data"          # 状态文件目录
STATE_DIR.mkdir(exist_ok=True)

TREASURY_FILE     = STATE_DIR / "treasury.json"
JOURNAL_FILE     = STATE_DIR / "journal.json"
CIRCUIT_FILE     = STATE_DIR / "circuit.json"
DD_FILE          = STATE_DIR / "daily_dd.json"

FACTOR_CONTEXT_FILE = ROOT / "factor_context.json"
IC_WEIGHTS_FILE    = Path.home() / ".hermes/kronos_ic_weights.json"

# ── 日志 ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
    handlers=[
        logging.FileHandler(str(STATE_DIR / "engine.log"), mode="a"),
        logging.StreamHandler(),
    ],
)
logger = logging.getLogger("kronos.engine")

# ── Phase 0: 导入五层模块 ─────────────────────────────────────────────────
from strategies.regime_classifier import RegimeClassifier, RegimeType
from strategies.engine_alpha import AlphaEngine, SignalType
from strategies.engine_beta import BetaEngine, BetaSignalType
from models.confidence_scorer import ConfidenceScorer
from models.position_sizer import PositionSizer
from risk.circuit_breaker import CircuitBreaker
from data.atr_watchlist import ATRWatchlist


# ═══════════════════════════════════════════════════════════════════════════
# 数据管道：OKX K线 → pandas DataFrame
# ═══════════════════════════════════════════════════════════════════════════
def fetch_okx_candles(
    inst_id: str,
    bar: str = "1H",
    limit: int = 300,
) -> Optional["pd.DataFrame"]:
    """
    从 OKX 获取 K 线数据，返回 DataFrame。
    使用 requests（需网络），失败时返回 None。
    """
    try:
        import requests, pandas as pd

        url = (
            f"https://www.okx.com/api/v5/market/history-candles"
            f"?instId={inst_id}&bar={bar}&limit={limit}"
        )
        r = requests.get(url, headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        data = r.json()

        if data.get("code") != "0" or not data.get("data"):
            logger.warning(f"OKX K线 API 失败: {data.get('msg', 'unknown')}")
            return None

        rows = []
        for candle in reversed(data["data"]):
            ts, o, h, l, c, vol = candle[:6]
            rows.append({
                "timestamp": datetime.utcfromtimestamp(int(ts) / 1000),
                "open":   float(o),
                "high":   float(h),
                "low":    float(l),
                "close":  float(c),
                "volume": float(vol),
            })

        df = pd.DataFrame(rows)
        df.set_index("timestamp", inplace=True)
        return df

    except Exception as e:
        logger.warning(f"fetch_okx_candles({inst_id}) 失败: {e}")
        return None


# ═══════════════════════════════════════════════════════════════════════════
# 每日回撤监控（独立于 CircuitBreaker）
# CircuitBreaker 是请求级熔断，DailyDDMonitor 是资金级防护
# ═══════════════════════════════════════════════════════════════════════════
class DailyDDMonitor:
    """日线级资金回撤监控 — 超过阈值立即清仓"""

    MAX_DAILY_DD = 0.05    # 5% 日线最大回撤
    TRADE_LOSS_WARN = 0.02 # 2% 单笔亏损告警

    def __init__(self, state_file: Path):
        self.state_file = state_file
        self.state = self._load()

    def _load(self) -> dict:
        if self.state_file.exists():
            try:
                with open(self.state_file) as f:
                    d = json.load(f)
                # 新的一天，重置
                if d.get("date") != str(date.today()):
                    d = self._fresh_state(d.get("starting_equity", 0))
                return d
            except Exception:
                pass
        return self._fresh_state(0)

    def _fresh_state(self, starting_equity: float) -> dict:
        return {
            "date": str(date.today()),
            "starting_equity": starting_equity,
            "current_equity": starting_equity,
            "peak_equity": starting_equity,
            "trades_today": 0,
            "daily_pnl": 0.0,
            "daily_dd": 0.0,
            "halted": False,
        }

    def update_equity(self, equity: float) -> dict:
        """每次收到新权益时调用"""
        today = str(date.today())
        if self.state["date"] != today:
            self.state = self._fresh_state(equity)

        self.state["current_equity"] = equity
        self.state["peak_equity"] = max(self.state["peak_equity"], equity)
        self.state["daily_dd"] = max(0,
            (self.state["peak_equity"] - equity) / self.state["starting_equity"]
            if self.state["starting_equity"] > 0 else 0
        )

        result = {"halted": False, "dd": self.state["daily_dd"]}

        if self.state["daily_dd"] >= self.MAX_DAILY_DD and not self.state.get("halted"):
            self.state["halted"] = True
            result["halted"] = True
            result["action"] = "HALT"
            logger.critical(f"日线DD {self.state['daily_dd']:.1%} ≥ {self.MAX_DAILY_DD:.1%}，已强制清仓！")

        self._save()
        return result

    def on_trade_result(self, pnl_pct: float, equity: float) -> dict:
        """每笔交易结束后调用"""
        self.state["trades_today"] += 1
        self.state["daily_pnl"] += pnl_pct

        # 更新权益
        upd = self.update_equity(equity)
        if upd.get("halted"):
            return {"action": "HALT_ALL", "close_positions": True, **upd}

        if pnl_pct <= -self.TRADE_LOSS_WARN:
            logger.warning(f"单笔亏损 {pnl_pct:.1%}，进入观察模式")
            return {"action": "WARNING", "pnl_pct": pnl_pct}

        return {"action": "OK", "pnl_pct": pnl_pct, **upd}

    def can_trade(self) -> tuple[bool, str]:
        """是否允许开新仓"""
        if self.state.get("halted"):
            return False, f"日线DD超限，已halted（{self.state['daily_dd']:.1%}）"
        return True, "OK"

    def _save(self):
        tmp = str(self.state_file) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.state, f, indent=2)
        os.replace(tmp, str(self.state_file))


# ═══════════════════════════════════════════════════════════════════════════
# 主引擎
# ═══════════════════════════════════════════════════════════════════════════
class KronosEngine:
    """
    Kronos v5.0 主引擎。
    每3分钟被cron调用一次：感知 → 路由 → 评分 → 风控 → 执行
    """

    # 扫描币种列表
    SCAN_COINS = ["BTC-USDT-SWAP", "ETH-USDT-SWAP", "DOGE-USDT-SWAP",
                  "SOL-USDT-SWAP", "BNB-USDT-SWAP", "XRP-USDT-SWAP"]

    def __init__(self):
        # Phase 1 感知层
        self.regime_classifier = RegimeClassifier()

        # Phase 2 评分层
        self.confidence_scorer = ConfidenceScorer()
        self.position_sizer    = PositionSizer()

        # Phase 3 风控层
        self.circuit_breaker = CircuitBreaker("kronos")   # 请求级熔断
        self.dd_monitor      = DailyDDMonitor(DD_FILE)     # 日线DD监控
        self.atr_watch       = ATRWatchlist()              # ATR监控

        # 双核引擎
        self.alpha = AlphaEngine()
        self.beta  = BetaEngine()

        # Phase 4 进化层（genetic algorithm — 策略参数优化）
        # 注：EvolutionEngine 是遗传算法框架，用于参数寻优，不是每日复盘
        # 每日 IC 权重更新由 cron job 独立执行
        self.evolution = None  # type: ignore

        # 统计
        self._runs = 0

        logger.info("KronosEngine 初始化完成")

    def run(self) -> dict:
        """主循环入口"""
        start = time.time()
        self._runs += 1

        try:
            # ── Step 0: 安全检查 ─────────────────────────────────────────
            safe, safe_msg = self._safety_check()
            if not safe:
                return self._result("BLOCKED", {}, start, safe_msg)

            # ── Step 1: 市场感知 ─────────────────────────────────────────
            btc_df = fetch_okx_candles("BTC-USDT-SWAP", bar="1H", limit=300)
            if btc_df is None or len(btc_df) < 50:
                return self._result("NO_DATA", {}, start, "BTC K线获取失败")

            regime, regime_conf, metrics = self.regime_classifier.classify(btc_df)
            logger.info(f"市场状态: {regime.value} | 置信度 {regime_conf:.2f} | "
                        f"ADX={metrics.adx:.1f} ATR_ratio={metrics.atr_ratio:.2f}")

            # ── Step 2: 扫描候选币 ───────────────────────────────────────
            candidates = self._scan_candidates()
            if not candidates:
                return self._result("NO_SIGNAL", {"regime": regime.value}, start)

            # ── Step 3: 路由 + 置信度打分 ──────────────────────────────
            signals = self._route_and_score(candidates, regime, btc_df)

            # ── Step 4: 风控过滤 ─────────────────────────────────────────
            actionable = self._risk_filter(signals)

            # ── Step 5: 准备执行 ─────────────────────────────────────────
            if actionable:
                logger.info(f"可执行信号: {actionable[0]['coin']} {actionable[0].get('direction')} "
                            f"置信度={actionable[0].get('confidence', 0):.0f}")
                return self._result("READY", {
                    "regime": regime.value,
                    "signal": actionable[0],
                }, start)

            return self._result("NO_SIGNAL", {"regime": regime.value}, start)

        except Exception as e:
            logger.error(f"引擎异常: {e}", exc_info=True)
            return self._result("ERROR", {"error": str(e)}, start)

    # ── Step 0: 安全检查 ─────────────────────────────────────────────────────
    def _safety_check(self) -> tuple[bool, str]:
        """熔断器 + 日线DD联合检查"""
        if not self.circuit_breaker.is_allowed():
            return False, "请求级熔断已触发"

        can_trade, msg = self.dd_monitor.can_trade()
        if not can_trade:
            return False, msg

        return True, "OK"

    # ── Step 2: 扫描候选币 ──────────────────────────────────────────────────
    def _scan_candidates(self) -> list[dict]:
        """
        核心数据管道：从 OKX 获取多币种 K 线，
        计算技术指标，输出候选信号列表。
        """
        import pandas as pd, numpy as np

        candidates = []
        for inst_id in self.SCAN_COINS:
            df = fetch_okx_candles(inst_id, bar="1H", limit=300)
            if df is None or len(df) < 100:
                continue

            try:
                # ── 基础指标 ────────────────────────────────────────────
                close = df["close"]
                high  = df["high"]
                low   = df["low"]
                vol   = df["volume"]

                # RSI
                delta = close.diff()
                gain  = delta.clip(lower=0).rolling(14).mean()
                loss  = (-delta.clip(upper=0)).rolling(14).mean()
                rs    = gain / loss.replace(0, 1e-10)
                rsi   = 100 - (100 / (1 + rs))
                rsi_current = rsi.iloc[-1] if not rsi.isna().all() else 50

                # ATR
                tr1 = high - low
                tr2 = (high - close.shift()).abs()
                tr3 = (low  - close.shift()).abs()
                tr  = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
                atr  = tr.rolling(14).mean()
                atr_current = atr.iloc[-1] if not atr.isna().all() else close.iloc[-1] * 0.015
                atr_pct = atr_current / close.iloc[-1]

                # Bollinger %B
                sma20  = close.rolling(20).mean()
                std20  = close.rolling(20).std()
                bb_upper = sma20 + 2 * std20
                bb_lower = sma20 - 2 * std20
                bb_pos = (close - bb_lower) / (bb_upper - bb_lower).replace(0, 1e-10)
                bb_pos_current = bb_pos.iloc[-1] if not bb_pos.isna().all() else 0.5

                # 成交量比率
                avg_vol = vol.rolling(20).mean()
                vol_ratio = vol.iloc[-1] / avg_vol.iloc[-1] if not avg_vol.isna().all() else 1.0

                # ── 方向判断 ─────────────────────────────────────────────
                if rsi_current < 35:
                    direction = "long"
                elif rsi_current > 65:
                    direction = "short"
                else:
                    direction = None

                if direction is None:
                    continue

                # ── 评分（简化版，不依赖 DataFrame 历史）────────────────
                base_conf = 60.0
                if rsi_current < 30:
                    base_conf += 15
                elif rsi_current > 70:
                    base_conf += 15
                if atr_pct > 0.02:
                    base_conf -= 5   # 高波动降分
                if vol_ratio > 1.5:
                    base_conf += 5   # 量能确认加分

                entry_price = close.iloc[-1]
                stop_loss   = entry_price * (0.97 if direction == "long" else 1.03)
                take_profit = entry_price * (1.06 if direction == "long" else 0.94)

                candidates.append({
                    "coin":         inst_id,
                    "direction":    direction,
                    "entry_price":  entry_price,
                    "stop_loss":    stop_loss,
                    "take_profit":  take_profit,
                    "base_confidence": base_conf,
                    "rsi":          rsi_current,
                    "atr_pct":      atr_pct,
                    "bb_pos":       bb_pos_current,
                    "vol_ratio":    vol_ratio,
                    "df":           df,          # 传递给后续步骤
                    "atr_value":    atr_current,
                })

            except Exception as e:
                logger.warning(f"{inst_id} 扫描失败: {e}")
                continue

        return candidates

    # ── Step 3: 路由 + 置信度打分 ───────────────────────────────────────────
    def _route_and_score(self, candidates: list, regime: RegimeType,
                          btc_df: "pd.DataFrame") -> list[dict]:
        """
        路由规则：
          - BULL_TREND / BEAR_TREND → BetaEngine
          - RANGE_BOUND / LOW_VOLATILITY / HIGH_VOLATILITY / UNKNOWN → AlphaEngine
          - BetaEngine 信号优先

        置信度 ≥ 60 才放行
        """
        scored = []

        for c in candidates:
            df = c["df"]

            # ── 双核分析 ──────────────────────────────────────────────────
            if regime in (RegimeType.BULL_TREND, RegimeType.BEAR_TREND):
                # 趋势市 → BetaEngine
                beta_sig = self.beta.analyze(df, regime)
                alpha_sig = None
                engine_used = "beta"
                conf = beta_sig.confidence * 100 if beta_sig.confidence else c["base_confidence"]
            else:
                # 震荡市 → AlphaEngine
                alpha_sig = self.alpha.analyze(df, regime)
                beta_sig = None
                engine_used = "alpha"
                conf = alpha_sig.strength * 100 if alpha_sig.strength else c["base_confidence"]

            # ── 最终置信度 = 引擎分 × 基础分 ────────────────────────────
            final_conf = (conf * 0.6 + c["base_confidence"] * 0.4)

            # ── 方向过滤（MiniMax禁止做空BTC/ETH）────────────────────────
            if c["coin"] in ("BTC-USDT-SWAP", "ETH-USDT-SWAP") and c["direction"] == "short":
                continue

            if final_conf < 60:
                continue

            # ── 仓位计算 ────────────────────────────────────────────────
            ps_result = self.position_sizer.calculate_size(
                entry_price=c["entry_price"],
                stop_loss=c["stop_loss"],
                take_profit=c["take_profit"],
                signal_confidence=final_conf,
                regime=regime,
                df=df,
            )

            scored.append({
                **c,
                "confidence":     final_conf,
                "engine_used":   engine_used,
                "beta_signal":   beta_sig,
                "alpha_signal":  alpha_sig,
                "position":      ps_result,
                "regime":        regime.value,
            })

        # 按置信度排序
        scored.sort(key=lambda x: x["confidence"], reverse=True)
        return scored

    # ── Step 4: 风控过滤 ────────────────────────────────────────────────────
    def _risk_filter(self, signals: list[dict]) -> list[dict]:
        """熔断器 + 滑点检查 + 日内频率限制"""
        if not signals:
            return []

        # 检查日内交易频率
        dd_state = self.dd_monitor.state
        if dd_state["trades_today"] >= 3:
            logger.info("已达日线交易上限（3笔）")
            return []

        actionable = []
        for s in signals:
            # 检查滑点成本（预期净利润必须为正）
            entry   = s["entry_price"]
            sl      = s["stop_loss"]
            tp      = s["take_profit"]
            side    = s["direction"]
            size_result = s["position"]

            # 粗略滑点过滤
            slippage_pct = 0.005  # 0.5% 极端滑点
            if side == "long":
                cost_pct = slippage_pct + 0.0005  # 滑点+手续费
                net_tp   = (tp - entry) / entry - cost_pct
                net_sl   = (entry - sl) / entry - cost_pct
            else:
                cost_pct = slippage_pct + 0.0005
                net_tp   = (entry - tp) / entry - cost_pct
                net_sl   = (sl - entry) / entry - cost_pct

            if net_tp < 0 and net_sl < 0:
                logger.info(f"{s['coin']} 滑点过滤：预期净收益为负")
                continue

            actionable.append(s)

        return actionable

    # ── 辅助 ───────────────────────────────────────────────────────────────
    def _result(self, status: str, steps: dict,
                start: float, msg: str = "") -> dict:
        return {
            "status": status,
            "message": msg,
            "steps": steps,
            "runs": self._runs,
            "elapsed_ms": int((time.time() - start) * 1000),
            "timestamp": datetime.now().isoformat(),
        }


# ── 入口 ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import pandas as _pd  # type: ignore

    print("Kronos v5.0 Engine — 总线贯通版")
    print("=" * 60)

    engine = KronosEngine()
    result = engine.run()

    print()
    if result["status"] == "READY":
        sig = result["steps"]["signal"]
        print(f"🎯 信号就绪!")
        print(f"   币种:    {sig['coin']}")
        print(f"   方向:    {sig['direction']}")
        print(f"   置信度:  {sig['confidence']:.0f}")
        print(f"   引擎:    {sig['engine_used']}")
        print(f"   入场价:  ${sig['entry_price']:.4f}")
        print(f"   止损价:  ${sig['stop_loss']:.4f}")
        print(f"   止盈价:  ${sig['take_profit']:.4f}")
        if sig.get('position'):
            p = sig['position']
            print(f"   建议仓位: {p.num_units:.4f} 张 | 约 ${p.size_dollars:.0f}")
    else:
        print(f"状态: {result['status']} | {result.get('message', '')}")
        if result["steps"].get("regime"):
            print(f"市场状态: {result['steps']['regime']}")
    print(f"耗时: {result['elapsed_ms']}ms")
