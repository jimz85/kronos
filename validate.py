
import numpy as np
import pandas as pd
import yfinance as yf
import warnings
warnings.filterwarnings("ignore")

def calc_rsi(p, n=14):
    d = np.diff(p, prepend=p[0])
    g = np.where(d>0, d, 0); l = np.where(d<0, -d, 0)
    ag = pd.Series(g).rolling(n).mean(); al = pd.Series(l).rolling(n).mean()
    return 100 - (100/(1 + ag/(al+1e-10)))

# 最优参数表（基于之前搜索）
configs = {
    "BTC-USD": {"rsi_buy": 40, "rsi_sell": 75, "stop": 0.04, "target": 0.02, "hold": 24},
    "ETH-USD": {"rsi_buy": 20, "rsi_sell": 75, "stop": 0.02, "target": 0.03, "hold": 24},
    "SOL-USD": {"rsi_buy": 20, "rsi_sell": 75, "stop": 0.05, "target": 0.03, "hold": 12},
    "BNB-USD": {"rsi_buy": 20, "rsi_sell": 65, "stop": 0.02, "target": 0.02, "hold": 12},
    "DOGE-USD": {"rsi_buy": 40, "rsi_sell": 60, "stop": 0.03, "target": 0.04, "hold": 12},
}

def run_backtest(p, rsi, cfg, leverage=10):
    trades = []
    pos = None
    
    for i in range(20, len(p) - cfg["hold"] - 1):
        rv = float(rsi.iloc[i])
        
        if pos is None:
            if rv < cfg["rsi_buy"]:
                pos = "long"; entry = p[i]
            elif rv > cfg["rsi_sell"]:
                pos = "short"; entry = p[i]
        else:
            if pos == "long":
                ret = (p[i] - entry) / entry * leverage
                if p[i] <= entry * (1 - cfg["stop"]) or rv > 50:
                    trades.append(ret); pos = None
                elif p[i] >= entry * (1 + cfg["target"]):
                    trades.append(ret); pos = None
            else:
                ret = (entry - p[i]) / entry * leverage
                if p[i] >= entry * (1 + cfg["stop"]) or rv < 50:
                    trades.append(ret); pos = None
                elif p[i] <= entry * (1 - cfg["target"]):
                    trades.append(ret); pos = None
    
    if not trades:
        return None
    
    wins = [t for t in trades if t > 0]
    losses = [t for t in trades if t < 0]
    wr = len(wins) / len(trades)
    pf = abs(sum(wins)/sum(losses)) if losses else 999
    
    equity = 1.0
    for t in trades:
        equity *= (1 + t/100)
    total = equity - 1
    
    return {
        "trades": len(trades),
        "wr": wr,
        "pf": pf,
        "total": total,
        "wins": len(wins),
        "losses": len(losses)
    }

print("="*70)
print("  样本内验证 + 样本外测试")
print("="*70)

final_results = []

for coin, cfg in configs.items():
    print(f"\n--- {coin} ---")
    
    # 样本内: 180天数据
    t = yf.Ticker(coin)
    df_in = t.history(period="180d", interval="1h")
    p_in = np.asarray(df_in["Close"].values).flatten()
    rsi_in = calc_rsi(p_in)
    days_in = (df_in.index[-1] - df_in.index[0]).days
    
    result_in = run_backtest(p_in, rsi_in, cfg)
    
    if result_in:
        weekly_in = result_in["trades"] / days_in * 7
        print(f"  样本内(180天): 周{weekly_in:.1f}次 胜率{result_in['wr']:.1%} PF={result_in['pf']:.2f} 收益{result_in['total']:+.1%}")
    
    # 样本外: 更早的180天数据
    try:
        df_out = t.history(period="360d", interval="1h")
        # 取前180天
        df_out = df_out.iloc[:int(len(df_out)/2)] if len(df_out) > 100 else df_out
        p_out = np.asarray(df_out["Close"].values).flatten()
        rsi_out = calc_rsi(p_out)
        days_out = (df_out.index[-1] - df_out.index[0]).days
        
        result_out = run_backtest(p_out, rsi_out, cfg)
        
        if result_out:
            weekly_out = result_out["trades"] / days_out * 7
            print(f"  样本外(前180天): 周{weekly_out:.1f}次 胜率{result_out['wr']:.1%} PF={result_out['pf']:.2f} 收益{result_out['total']:+.1%}")
            
            final_results.append({
                "coin": coin,
                "cfg": cfg,
                "in_sample": result_in,
                "out_sample": result_out,
                "weekly_in": weekly_in,
                "weekly_out": weekly_out
            })
    except Exception as e:
        print(f"  样本外测试错误: {e}")
        final_results.append({
            "coin": coin,
            "cfg": cfg,
            "in_sample": result_in,
            "out_sample": None
        })

print()
print("="*70)
print("  最终汇总")
print("="*70)

all_pass = True
for r in final_results:
    cfg = r["cfg"]
    ins = r["in_sample"]
    outs = r.get("out_sample")
    
    marker = "✅" if ins and ins["wr"] >= 0.60 and ins["pf"] > 1.0 else "❌"
    
    print(f"{marker} {r['coin']}: RSI<{cfg['rsi_buy']}/{cfg['rsi_sell']} 止{cfg['stop']:.0%} 目{cfg['target']:.0%} 持{cfg['hold']}h")
    print(f"    样本内: 周{r['weekly_in']:.1f}次 胜率{ins['wr']:.1%} PF={ins['pf']:.2f}")
    if outs:
        print(f"    样本外: 周{r['weekly_out']:.1f}次 胜率{outs['wr']:.1%} PF={outs['pf']:.2f}")
    else:
        print(f"    样本外: N/A")
    
    if ins and ins["wr"] < 0.60:
        all_pass = False

print()
if all_pass:
    print("★ 全部通过样本内测试!")
else:
    print("★ 部分配置未达标，继续优化...")
