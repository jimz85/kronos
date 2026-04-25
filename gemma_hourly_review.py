#!/usr/bin/env python3
"""
Gemma因子小时评估脚本
=====================
每小时运行一次，用gemma4-heretic评估市场状态
更新IC权重文件中的Gemma IC值

用法:
  python3 gemma_hourly_review.py
"""

import os, sys, json, time
from datetime import datetime
from pathlib import Path
import signal
import threading

sys.path.insert(0, str(Path(__file__).parent))
from voting_system import GemmaVoter, ICTracker

def vote_with_timeout(gv, timeout=20):
    """带超时的投票，20秒超时（macOS兼容）"""
    result_holder = [None]
    error_holder = [None]

    def target():
        try:
            result_holder[0] = gv.vote()
        except Exception as e:
            error_holder[0] = e

    t = threading.Thread(target=target)
    t.daemon = True
    t.start()
    t.join(timeout=timeout)
    if t.is_alive():
        return {'vote': 0, 'confidence': 0, 'reason': f'评估超时({timeout}s)跳过', 'raw_response': 'TIMEOUT'}
    if error_holder[0]:
        return {'vote': 0, 'confidence': 0, 'reason': f'评估错误跳过', 'raw_response': 'ERROR'}
    return result_holder[0] or {'vote': 0, 'confidence': 0, 'reason': '无结果', 'raw_response': 'EMPTY'}

# 要评估的币种
COINS = ['AVAX', 'ETH', 'BTC', 'SOL', 'DOGE', 'ADA', 'DOT', 'LINK']

def get_market_data(coin):
    """获取基础市场数据"""
    try:
        from kronos_multi_coin import get_ohlcv, get_price, calc_rsi, calc_adx
        
        price = get_price(coin)
        c1 = get_ohlcv(coin, '1H', 72)
        if not c1:
            return None
        
        rsi = calc_rsi(c1)
        adx = calc_adx(c1)
        
        # vol_ratio
        if len(c1) >= 20:
            recent_vol = sum(c['volume'] for c in c1[-5:]) / 5
            avg_vol = sum(c['volume'] for c in c1[-20:]) / 20
            vol_ratio = recent_vol / (avg_vol + 1e-10)
        else:
            vol_ratio = 1.0
        
        # BTC数据
        try:
            btc_c = get_ohlcv('BTC', '1H', 72)
            btc_rsi = calc_rsi(btc_c) if btc_c else 50
            btc_dir = 'overbought' if btc_rsi > 70 else ('oversold' if btc_rsi < 30 else 'neutral')
        except:
            btc_dir = 'neutral'
        
        return {
            'coin': coin,
            'price': price,
            'rsi_1h': rsi,
            'adx_1h': adx,
            'vol_ratio': vol_ratio,
            'btc_direction': btc_dir,
        }
    except Exception as e:
        return None

def main():
    print(f"=== Gemma小时审查 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")
    
    all_results = {}
    tracker = ICTracker()
    
    for coin in COINS:
        print(f"\n[{coin}] Gemma评估中...", end=" ", flush=True)
        
        md = get_market_data(coin)
        if not md:
            print("❌ 数据获取失败")
            continue
        
        # 长和短方向各评估一次
        results = {}
        for direction in ['long', 'short']:
            gv = GemmaVoter(coin, md, direction)
            result = vote_with_timeout(gv, timeout=20)
            results[direction] = result
        
        all_results[coin] = {
            'md': md,
            'gemma': results,
            'timestamp': datetime.now().isoformat(),
        }
        
        # 打印结果
        for direction, result in results.items():
            vote = result['vote']
            conf = result['confidence']
            reason = result['reason']
            raw = result.get('raw_response', '')[:80]
            print(f"  {direction}: vote={vote:+.2f} conf={conf} {reason}")
            if 'ERROR' in raw:
                print(f"    ⚠️ {raw}")
    
    # 保存结果
    out_path = Path(__file__).parent / 'gemma_hourly_latest.json'
    with open(out_path, 'w') as f:
        json.dump(all_results, f, indent=2)
    
    print(f"\n✅ 已保存: {out_path}")
    
    # 推送到飞书
    try:
        msg = f"【Gemma小时审查 {datetime.now().strftime('%m-%d %H:%M')}】\n"
        for coin, data in all_results.items():
            for direction, result in data['gemma'].items():
                if abs(result['vote']) >= 0.5:
                    sign = '+' if result['vote'] > 0 else ''
                    msg += f"{coin} {direction}: {sign}{result['vote']:.2f} {result['reason']}\n"
        if msg:
            from hermes_gateway import send_feishu_alert
            send_feishu_alert(msg)
    except:
        pass
    
    print("\n=== 完成 ===")

if __name__ == '__main__':
    main()
