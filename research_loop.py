#!/usr/bin/env python3
"""
research_loop.py
Ralph-style 策略研究循环 - 主动淘汰无效策略

每次迭代：
1. 选一个待研究的策略假设
2. 构造统计测试：能不能找到它失效的场景
3. 跨市场/跨时间验证
4. 更新策略池：promoting / eliminating
5. 记录 learnings

不是"找最优"，而是"找边界"——知道策略什么时候会失效比知道它什么时候有效更重要。
"""
import json
import os
import sys
from datetime import datetime
from pathlib import Path

# ─── 路径配置 ───────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
POOL_FILE = SCRIPT_DIR / "strategy_pool.json"
PROGRESS_FILE = SCRIPT_DIR / "research_progress.txt"

# ─── 加载策略池 ──────────────────────────────────────────────
def load_pool():
    if POOL_FILE.exists():
        with open(POOL_FILE) as f:
            return json.load(f)
    return {"strategies": [], "progress": {"total": 0, "promising": 0, "eliminated": 0, "inconclusive": 0, "pending": 0}}

def save_pool(pool):
    with open(POOL_FILE, "w") as f:
        json.dump(pool, f, indent=2, ensure_ascii=False)

# ─── 添加策略假设 ────────────────────────────────────────────
def add_strategy(title, hypothesis, logic_anchor, test_params=None):
    pool = load_pool()
    for s in pool["strategies"]:
        if s["hypothesis"] == hypothesis:
            print(f"策略已存在: {hypothesis[:50]}...")
            return s["id"]
    sid = f"STR-{len(pool['strategies'])+1:03d}"
    strategy = {
        "id": sid, "title": title, "hypothesis": hypothesis,
        "logic_anchor": logic_anchor, "status": "pending",
        "test_result": None, "elimination_reason": None,
        "test_params": test_params or {}, "created_at": datetime.now().isoformat(), "notes": ""
    }
    pool["strategies"].append(strategy)
    pool["progress"]["total"] = len(pool["strategies"])
    save_pool(pool)
    print(f"添加策略: [{sid}] {title}")
    return sid

# ─── 运行回测 ────────────────────────────────────────────────
def run_strategy_backtest(strategy):
    """运行策略回测，返回结果"""
    from backtest_engine import run_backtest
    
    sid = strategy["id"]
    title = strategy["title"]
    
    # 根据策略标题决定测试什么
    if "BTC RSI" in title:
        result = run_backtest("BTC-USD", "RSI", {"rsi_buy": 35, "rsi_sell": 65, "stop_pct": 0.03, "hold_max": 15})
    elif "ETH RSI" in title:
        result = run_backtest("ETH-USD", "RSI", {"rsi_buy": 35, "rsi_sell": 65, "stop_pct": 0.04, "hold_max": 15})
    elif "BNB BB" in title:
        result = run_backtest("BNB-USD", "BB", {"bb_period": 20, "bb_std": 2.5, "stop_atr": 2.0})
    elif "SOL BB" in title:
        result = run_backtest("SOL-USD", "BB", {"bb_period": 20, "bb_std": 2.5, "stop_atr": 2.0})
    elif "相关性" in title:
        result = run_backtest("BTC-USD", "CORR", {"ref_symbol": "ETH-USD", "corr_lookback": 20, "threshold": 0.7})
    elif "波动率" in title:
        result = run_backtest("BTC-USD", "VOL", {"vol_lookback": 20})
    else:
        return None
    
    return result

# ─── 更新策略状态 ────────────────────────────────────────────
def update_strategy(sid, status, test_result=None, elimination_reason=None, notes=""):
    pool = load_pool()
    for s in pool["strategies"]:
        if s["id"] == sid:
            s["status"] = status
            if test_result:
                s["test_result"] = test_result
            if elimination_reason:
                s["elimination_reason"] = elimination_reason
            if notes:
                s["notes"] = notes
            s["updated_at"] = datetime.now().isoformat()
            break
    pool["progress"]["promising"] = sum(1 for s in pool["strategies"] if s["status"] == "promising")
    pool["progress"]["eliminated"] = sum(1 for s in pool["strategies"] if s["status"] == "eliminated")
    pool["progress"]["inconclusive"] = sum(1 for s in pool["strategies"] if s["status"] == "inconclusive")
    pool["progress"]["pending"] = sum(1 for s in pool["strategies"] if s["status"] == "pending")
    save_pool(pool)

# ─── 记录研究进度 ────────────────────────────────────────────
def append_progress(sid, what, learnings):
    with open(PROGRESS_FILE, "a") as f:
        f.write(f"\n## {datetime.now().isoformat()} - {sid}\n")
        f.write(f"- {what}\n")
        f.write(f"- **Learnings:** {learnings}\n")
        f.write("---\n")

# ─── 打印状态 ────────────────────────────────────────────────
def show_status():
    pool = load_pool()
    p = pool["progress"]
    print("\n" + "="*60)
    print(f"Kronos 策略研究池状态")
    print("="*60)
    print(f"总计: {p['total']} | 有望: {p['promising']} | 淘汰: {p['eliminated']} | 待定: {p['inconclusive']} | 待测: {p['pending']}")
    print()
    for s in pool["strategies"]:
        status_icon = {"pending": "⏳", "in_test": "🔬", "promising": "✅", "eliminated": "❌", "inconclusive": "⚠️"}.get(s["status"], "?")
        print(f"  {status_icon} [{s['id']}] {s['title']}")
        if s.get("test_result"):
            tr = s["test_result"]
            print(f"       胜率={tr.get('avg_win_rate','?'):.0%} PF={tr.get('avg_profit_factor','?'):.2f} 年化={tr.get('avg_cagr','?'):.1%}")
        if s.get("elimination_reason"):
            print(f"       ❌ {s['elimination_reason']}")
    print()

# ─── 主循环 ─────────────────────────────────────────────────
def run_research_loop(max_iterations=10):
    print(f"\n🔬 Kronos Research Loop 开始 (max={max_iterations})\n")
    
    # 导入回测引擎
    sys.path.insert(0, str(SCRIPT_DIR))
    
    for i in range(1, max_iterations + 1):
        print(f"\n{'='*60}")
        print(f"  Research 迭代 {i}/{max_iterations}")
        print(f"{'='*60}")
        
        pool = load_pool()
        strategy = next((s for s in pool["strategies"] if s["status"] == "pending"), None)
        
        if not strategy:
            print("没有待测策略了")
            break
        
        print(f"\n📋 测试策略: [{strategy['id']}] {strategy['title']}")
        print(f"   假设: {strategy['hypothesis']}")
        print(f"   逻辑锚点: {strategy['logic_anchor']}")
        
        # 更新状态为测试中
        update_strategy(strategy["id"], "in_test")
        
        # 运行回测
        result = run_strategy_backtest(strategy)
        
        if result is None:
            print("   错误: 无法运行回测")
            update_strategy(strategy["id"], "pending", notes="回测引擎未适配")
            continue
        
        # 判断结果
        if result.get("eliminated"):
            status = "eliminated"
            reason = result.get("elimination_reason", "未知原因")
            learnings = f"策略被淘汰: {reason}"
            print(f"\n   ❌ 淘汰 - {reason}")
        elif result.get("periods", {}).get("全周期", {}).get("trades", 0) < 20:
            status = "inconclusive"
            reason = f"交易次数太少({result.get('periods', {}).get('全周期', {}).get('trades', 0)}<20)"
            learnings = f"样本不足: {reason}"
            print(f"\n   ⚠️ 待定 - {reason}")
        else:
            status = "promising"
            learnings = f"策略通过验证: 胜率{result.get('avg_win_rate', 0):.1%}, PF={result.get('avg_profit_factor', 0):.2f}"
            print(f"\n   ✅ 有望 - 胜率{result.get('avg_win_rate', 0):.1%}, PF={result.get('avg_profit_factor', 0):.2f}")
        
        # 更新策略池
        test_result_summary = {
            "avg_win_rate": result.get("avg_win_rate", 0),
            "avg_profit_factor": result.get("avg_profit_factor", 0),
            "avg_cagr": result.get("avg_cagr", 0),
            "max_drawdown": result.get("max_drawdown", 0)
        }
        update_strategy(strategy["id"], status, test_result=test_result_summary, elimination_reason=reason if status == "eliminated" else None, notes=learnings)
        append_progress(strategy["id"], strategy["title"], learnings)
        
        show_status()
    
    print("\n✅ Research Loop 完成")
    show_status()

# ─── CLI ────────────────────────────────────────────────────
if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Kronos Research Loop")
    parser.add_argument("--add", nargs=3, metavar=("TITLE", "HYPOTHESIS", "LOGIC_ANCHOR"), help="添加新策略假设")
    parser.add_argument("--status", action="store_true", help="显示状态")
    parser.add_argument("--run", action="store_true", help="运行研究循环")
    parser.add_argument("--max", type=int, default=10, help="最大迭代次数")
    args = parser.parse_args()
    
    if args.add:
        add_strategy(args.add[0], args.add[1], args.add[2])
    if args.status:
        show_status()
    if args.run:
        run_research_loop(max_iterations=args.max)
    if not (args.add or args.status or args.run):
        show_status()
