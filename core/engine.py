"""
Kronos v5.0 — 主引擎
=======================
运行：python3 core/engine.py
"""

import os, sys, json, time
from datetime import datetime
from pathlib import Path

ROOT = Path.home() / "kronos"
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)


def load_modules():
    """尝试加载各层模块"""
    results = {}
    try:
        from strategies.regime_classifier import RegimeClassifier, RegimeType
        results["regime"] = {"cls": RegimeClassifier, "type": RegimeType}
    except Exception as e:
        results["regime"] = {"error": str(e)}

    try:
        from models.confidence_scorer import ConfidenceScorer
        results["scorer"] = {"cls": ConfidenceScorer}
    except Exception as e:
        results["scorer"] = {"error": str(e)}

    try:
        from data.evolution_engine import EvolutionEngine
        results["evolution"] = {"cls": EvolutionEngine}
    except Exception as e:
        results["evolution"] = {"error": str(e)}

    try:
        from data.atr_watchlist import ATRWatchlist
        results["atr"] = {"cls": ATRWatchlist}
    except Exception as e:
        results["atr"] = {"error": str(e)}

    try:
        from risk.circuit_breaker import CircuitBreaker
        results["circuit"] = {"cls": CircuitBreaker}
    except Exception as e:
        results["circuit"] = {"error": str(e)}

    try:
        from risk.dynamic_trailing import DynamicTrailingStop
        results["trailing"] = {"cls": DynamicTrailingStop}
    except Exception as e:
        results["trailing"] = {"error": str(e)}

    return results


def get_btc_price() -> float:
    """获取BTC实时价格"""
    try:
        import urllib.request
        url = "https://www.okx.com/api/v5/market/ticker?instId=BTC-USDT-SWAP"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as r:
            data = json.loads(r.read())
        if data.get("code") == "0" and data.get("data"):
            return float(data["data"][0]["last"])
    except Exception:
        pass
    return None


def get_positions() -> list:
    """从本地状态文件读取当前持仓"""
    files = [
        ROOT / "dual_strategy_state.json",
        ROOT / "multi_direction_state.json",
        Path.home() / ".hermes/cron/output/kronos_position_state.json",
    ]
    for f in files:
        if f.exists():
            try:
                with open(f) as fh:
                    d = json.load(fh)
                positions = d.get("positions", {})
                if positions:
                    return [{"coin": k, **v} for k, v in positions.items()]
            except Exception:
                pass
    return []


class KronosEngine:
    """
    Kronos v5.0 全自动交易引擎。
    每3分钟被cron调用一次。
    """

    def __init__(self):
        self.modules = load_modules()
        self.stats = {"runs": 0, "trades_today": 0}

    def run(self) -> dict:
        start = time.time()
        self.stats["runs"] += 1

        btc_price = get_btc_price()
        positions = get_positions()
        elapsed = int((time.time() - start) * 1000)

        return {
            "status": "OK",
            "version": "v5.0",
            "timestamp": datetime.now().isoformat(),
            "btc_price": btc_price,
            "positions": positions,
            "modules_loaded": {
                k: "OK" if "cls" in v else f"ERR: {v.get('error','?')}"
                for k, v in self.modules.items()
            },
            "stats": self.stats,
            "elapsed_ms": elapsed,
        }


# ── Demo ────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    print("Kronos v5.0 Engine")
    print("=" * 50)

    engine = KronosEngine()
    print("\n模块加载状态：")
    for name, info in engine.modules.items():
        icon = "✅" if "cls" in info else "❌"
        print(f"  {icon} {name}: {info}")

    print()
    result = engine.run()
    print(json.dumps(result, indent=2, ensure_ascii=False))
