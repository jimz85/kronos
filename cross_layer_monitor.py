#!/usr/bin/env python3
"""
五层数据采集器 v1.0
===================================
Layer 1: OKX实时价格（公开API）
Layer 2: 技术指标（yfinance RSI/ADX/IC因子）
Layer 3: Fear & Greed情绪指数（免费API）
Layer 4: DeFiLlama资金流向（免费API）
Layer 5: BTC市值占比 + 链上数据

所有数据统一带时间戳，用于跨层时间差分析。
"""

import requests, json, time
from datetime import datetime

BASE_URL = 'https://api.alternative.me'
DEFI_URL = 'https://api.llama.fi'
COINGECKO_URL = 'https://api.coingecko.com/api/v1'

def get_fear_greed():
    """Layer3: Fear & Greed Index"""
    try:
        r = requests.get(f'{BASE_URL}/fng/', timeout=10)
        d = r.json()['data'][0]
        return {
            'value': int(d['value']),
            'classification': d['value_classification'],
            'timestamp': int(d['timestamp']),
            'time': datetime.fromtimestamp(int(d['timestamp'])).strftime('%Y-%m-%d %H:%M')
        }
    except:
        return None

def get_defi_tvl():
    """Layer4: DeFiLlama TVL资金流向"""
    try:
        # 各协议TVL（/tvl端点挂了，用/protocols）
        r3 = requests.get('https://api.llama.fi/protocols', timeout=10)
        protocols = r3.json()
        
        # 找CEX的TVL
        cex_data = {}
        for p in protocols:
            name = p.get('name', '')
            sym = name.lower()
            tvl = p.get('tvl', 0) or 0
            c7d = p.get('change_7d', 0) or 0
            c1d = p.get('change_1d', 0) or 0
            
            # 只保留主要CEX
            cex_keywords = ['okx', 'binance cex', 'bybit', 'bitfinex', 'robinhood', 'coinbase bridge']
            if any(kw in sym for kw in cex_keywords):
                cex_data[name] = {'tvl': tvl, 'change_1d': c1d, 'change_7d': c7d}
        
        return {
            'cex_tvl': cex_data,
            'protocol_count': len(protocols),
            'time': datetime.now().strftime('%Y-%m-%d %H:%M')
        }
    except Exception as e:
        return {'error': str(e)}

def get_coingecko_global():
    """Layer1+5: CoinGecko全球市场数据"""
    try:
        r = requests.get(f'{COINGECKO_URL}/global', timeout=10)
        d = r.json().get('data', {})
        
        mcp = d.get('market_cap_percentage', {})
        btc_dominance = mcp.get('btc', 0) or 0
        eth_dominance = mcp.get('eth', 0) or 0
        
        tmc = d.get('total_market_cap', {})
        tvol = d.get('total_volume', {})
        
        return {
            'total_market_cap': tmc.get('usd', 0) if isinstance(tmc, dict) else 0,
            'total_volume_24h': tvol.get('usd', 0) if isinstance(tvol, dict) else 0,
            'btc_dominance': btc_dominance,
            'eth_dominance': eth_dominance,
            'alt_dominance': 100 - btc_dominance - eth_dominance,
            'active_cryptocurrencies': d.get('active_cryptocurrencies', 0),
            'market_cap_change_24h': d.get('market_cap_change_percentage_24h_usd', 0),
            'time': datetime.now().strftime('%Y-%m-%d %H:%M')
        }
    except:
        return None

def get_layer5_hints():
    """Layer5: 鲸鱼和链上数据线索（从公开来源）"""
    hints = []
    
    # Fear & Greed极端值（低于25=极度恐惧，往往是鲸鱼抄底时机）
    fg = get_fear_greed()
    if fg and fg['value'] < 30:
        hints.append({
            'signal': 'EXTREME_FEAR',
            'value': fg['value'],
            'interpretation': '极度恐惧区(25)，历史上往往是鲸鱼逆势买入时机',
            'action': 'LONG',
            'confidence': min(100, (30 - fg['value']) * 5)
        })
    elif fg and fg['value'] > 75:
        hints.append({
            'signal': 'EXTREME_GREED',
            'value': fg['value'],
            'interpretation': '极度贪婪区(75)，历史上往往是市场顶部区域',
            'action': 'SHORT',
            'confidence': min(100, (fg['value'] - 70) * 5)
        })
    
    return fg, hints

def analyze_cross_layer():
    """跨层综合分析"""
    ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f'[{ts}] 五层数据采集...')
    
    # 并行获取所有层数据
    fg = get_fear_greed()
    defi = get_defi_tvl()
    global_data = get_coingecko_global()
    
    print(f'  Fear&Greed: {fg["value"]} ({fg["classification"]})' if fg else '  Fear&Greed: 获取失败')
    print(f'  总DeFi TVL: ${defi.get("total_tvl",0)/1e9:.1f}B' if defi and 'error' not in defi else '  DeFiLlama: 获取失败')
    print(f'  BTC市值占比: {global_data.get("btc_dominance",0):.1f}%' if global_data else '  CoinGecko: 获取失败')
    
    # CEX资金流向
    if defi and 'cex_tvl' in defi:
        print(f'  CEX资金流:')
        for name, data in defi['cex_tvl'].items():
            arrow = '↑' if data['change_7d'] > 0 else '↓'
            print(f'    {name}: ${data["tvl"]/1e9:.1f}B {arrow}{abs(data["change_7d"]):.1f}%/7d')
    
    # 生成跨层信号
    signals = []
    
    # Layer3信号：Fear & Greed
    if fg:
        fg_value = fg['value']
        if fg_value < 25:
            signals.append({
                'layer': 'L3_Sentiment',
                'action': 'LONG',
                'confidence': (30 - fg_value) * 4,
                'reason': f'Fear&Greed={fg_value}极度恐惧，历史上逆势买入信号'
            })
        elif fg_value > 75:
            signals.append({
                'layer': 'L3_Sentiment',
                'action': 'SHORT',
                'confidence': (fg_value - 70) * 4,
                'reason': f'Fear&Greed={fg_value}极度贪婪，历史上市场顶部区域'
            })
    
    # Layer4信号：CEX大资金异动
    if defi and 'cex_tvl' in defi:
        for name, data in defi['cex_tvl'].items():
            if data['change_7d'] > 30:
                signals.append({
                    'layer': 'L4_FundFlow',
                    'action': 'LONG',
                    'confidence': min(90, data['change_7d'] * 2),
                    'reason': f'{name} TVL 7天+{data["change_7d"]:.0f}%，大资金逆势抄底信号'
                })
            elif data['change_7d'] < -20:
                signals.append({
                    'layer': 'L4_FundFlow',
                    'action': 'SHORT',
                    'confidence': min(90, abs(data['change_7d']) * 2),
                    'reason': f'{name} TVL 7天{data["change_7d"]:.0f}%，大资金撤离信号'
                })
    
    # Layer5信号：BTC主导权
    if global_data:
        btc_dom = global_data.get('btc_dominance', 0)
        if btc_dom > 60:
            signals.append({
                'layer': 'L5_MarketStructure',
                'action': 'OBSERVE',
                'confidence': (btc_dom - 55) * 4,
                'reason': f'BTC主导率{btc_dom:.1f}%，市场集中度高，需谨慎'
            })
    
    # 跨层共振检测
    long_signals = [s for s in signals if s['action'] == 'LONG']
    short_signals = [s for s in signals if s['action'] == 'SHORT']
    
    共振强度 = len(long_signals) * 100 + sum(s['confidence'] for s in long_signals)
    
    print(f'\n  跨层信号分析:')
    for s in signals:
        print(f'    [{s["layer"]}] {s["action"]} 置信{s["confidence"]}%: {s["reason"]}')
    
    print(f'\n  共振强度: LONG×{len(long_signals)}层 = {共振强度}分 | SHORT×{len(short_signals)}层')
    
    return {
        'fear_greed': fg,
        'defi': defi,
        'global': global_data,
        'signals': signals,
        '共振强度': 共振强度,
        'timestamp': ts
    }

if __name__ == '__main__':
    result = analyze_cross_layer()
    print('\n=== 汇总 ===')
    print(json.dumps({
        'fear_greed': result['fear_greed'],
        'global_market': result['global'],
        'cross_layer_signals': result['signals'],
        '共振强度': result['共振强度'],
    }, indent=2, ensure_ascii=False))
