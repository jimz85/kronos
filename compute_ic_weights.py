#!/usr/bin/env python3
"""
Kronos IC权重批量计算脚本
============================
每天自动运行，计算所有币种的因子IC，更新权重
用法: python3 compute_ic_weights.py
      python3 compute_ic_weights.py --coins AVAX ETH BTC DOGE
"""

import os, sys, json, time
from datetime import datetime
from pathlib import Path

# 添加kronos目录
sys.path.insert(0, str(Path(__file__).parent))
from voting_system import ICTracker, compute_factor_ic_batch

COINS = ['AVAX', 'ETH', 'BTC', 'SOL', 'DOGE', 'ADA', 'DOT', 'LINK', 'BNB', 'XRP']
CACHE_FILE = os.path.expanduser('~/.hermes/kronos_ic_weights.json')

def main():
    print(f"=== IC权重批量计算 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    
    tracker = ICTracker()
    all_results = {}
    
    for coin in COINS:
        print(f"\n[{coin}] 计算中...", end=" ", flush=True)
        try:
            ics = compute_factor_ic_batch(coin, '1H', 500)
            
            if not ics:
                print("❌ 数据不足")
                continue
            
            # 记录每个因子的IC
            for factor, ic in ics.items():
                tracker.record_ic(factor, ic)
            
            # 取该币RSI的IC作为该币的代表IC
            rsi_ic = ics.get('RSI', 0)
            all_results[coin] = {'rsi_ic': rsi_ic, 'factors': ics}
            print(f"✅ RSI_IC={rsi_ic:+.4f}")
            
        except Exception as e:
            print(f"❌ {str(e)[:50]}")
            continue
    
    # 计算新权重
    print(f"\n=== 计算权重 ===")
    weights = tracker.compute_weights()
    
    print("因子权重:")
    for factor, w in sorted(weights.items(), key=lambda x: -x[1]):
        bar = '█' * int(w * 50)
        print(f"  {factor:12s} {w:.2%} {bar}")
    
    total = sum(weights.values())
    print(f"总和: {total:.2%} (应≈100%)")
    
    # 保存结果摘要
    summary = {
        'updated': datetime.now().isoformat(),
        'coins': COINS,
        'weights': weights,
        'sample_ics': all_results,
    }
    
    # P2 Fix: 保存到kronos_ic_weights.json（voting_system读这个），不是orphaned的ic_weights_latest.json
    # 
    # 重要：不要覆盖MiniMax已调整的weights字段！
    # MiniMax每小时更新weights（基于战略情绪），
    # compute_ic_weights每天更新ic_history（基于历史IC数据）。
    # 两者独立存储，由ICTracker的compute_weights()在运行时合并。
    existing = {}
    if Path(CACHE_FILE).exists():
        try:
            existing = json.loads(Path(CACHE_FILE).read_text())
        except:
            pass
    ic_data = {
        # IC历史数据（供ICTracker.compute_weights使用）
        'ic_history': existing.get('ic_history', {}),
        # 权重由MiniMax调整（apply_minimax_adjustment）和IC计算共同决定
        # 不在这里覆盖weights，保留MiniMax的调整结果
        'weights': existing.get('weights', weights),  # 保存IC计算结果作为后备
        'last_update': datetime.now().isoformat(),
        'strategy_quality': existing.get('strategy_quality', {}),
        'adjusted_by': existing.get('adjusted_by', []) + ['compute_ic_weights'],
        # IC计算的原始结果（供诊断）
        'ic_computed_weights': weights,
        'ic_sample_results': all_results,
    }
    Path(CACHE_FILE).write_text(json.dumps(ic_data, indent=2))
    
    out_path = CACHE_FILE
    
    print(f"\n✅ 已保存到 {out_path}")
    
    # 推送到飞书（如果有的话）
    try:
        from hermes_gateway import send_feishu_alert
        msg = f"【IC权重更新 {datetime.now().strftime('%m-%d %H:%M')}】\n"
        for factor, w in sorted(weights.items(), key=lambda x: -x[1])[:5]:
            if w > 0:
                msg += f"{factor}: {w:.1%}\n"
        send_feishu_alert(msg)
    except:
        pass  # 静默
    
    print("\n=== 完成 ===")

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--coins', nargs='+', default=COINS)
    args = parser.parse_args()
    
    # 临时覆盖COINS
    if args.coins:
        globals()['COINS'] = args.coins
    
    main()
