#!/usr/bin/env python3
"""
Kronos 交易反馈闭环脚本 v1.3
=======================
每天自动运行，从 paper_trades.json 读取真实交易结果
更新 ic_weights_latest.json 的因子权重

v1.3: 不再硬编码排除，改用动态per-coin WLR判断（WLR<0.4的币种才排除）
     recovered不再是排除项；realized_pnl+unrealized_pnl联合计算

用法: python3 compute_trade_ic.py
"""
import os, sys, json
from pathlib import Path
from datetime import datetime, timedelta
from collections import defaultdict

PAPER_TRADES = Path.home() / '.hermes/cron/output/paper_trades.json'
# P2 Fix: 写入kronos_ic_weights.json（voting_system读这个），不是orphaned的ic_weights_latest.json
IC_WEIGHTS = Path.home() / '.hermes/kronos_ic_weights.json'
COIN_STRATEGY = Path(__file__).parent / 'coin_strategy_map.json'

# 因子名称规范化：paper_trades因子名 -> IC系统因子名（必须与voting_system.py完全一致）
# IC系统实际使用的key：RSI, ADX, Bollinger, Vol, MACD, BTC, Gemma, Sentiment
FACTOR_MAP = {
    # 旧系统硬编码（兼容）
    'rsi_inv': 'RSI',
    'rsi': 'RSI',
    # P2 Fix: 新系统有意义的best_factor名
    'RSI均值回归': 'RSI',
    'RSI超卖': 'RSI',
    'RSI超买': 'RSI',
    'ADX趋势': 'ADX',
    '波动率': 'Vol',
    # IC系统因子名
    'vol': 'Vol',
    'vol_ratio': 'Vol',
    'adx': 'ADX',
    'bollinger': 'Bollinger',
    'bollingerbands': 'Bollinger',
    'macd': 'MACD',
    'gemma': 'Gemma',
    'btc': 'BTC',
    'sentiment': 'Sentiment',
    # 常见错误映射（防止历史遗留名称）
    'vol_ratio_signal': 'Vol',
    'atr': 'Vol',
}

# 不再硬编码排除，改用动态WLR判断（per-coin WLR < 0.4 才排除）

def load_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except:
        return default if default is not None else {}

def main():
    print(f"=== Kronos交易反馈闭环 {datetime.now().strftime('%Y-%m-%d %H:%M')} ===")

    # 1. 读取paper_trades
    trades = load_json(PAPER_TRADES, [])
    if not trades:
        print("无交易记录，跳过")
        return

    # 2. 读取coin_strategy_map获取per-coin最优因子和排除币种
    coin_strategy = load_json(COIN_STRATEGY, {})
    coin_factor = {}
    excluded_coins_set = set()  # P2 Fix: 从coin_strategy_map读取excluded字段
    for c in coin_strategy.get('coins', []):
        coin_factor[c['symbol']] = c.get('optimal_strategy', 'RSI')
        if c.get('excluded'):  # 被标记为排除的币种不参与WLR统计
            excluded_coins_set.add(c['symbol'])

    # 3. 读取当前IC权重
    ic_data = load_json(IC_WEIGHTS, {})
    current_weights = ic_data.get('weights', {})

    # v1.3: 不再硬编码排除，改用动态per-coin WLR判断
    # 4. 计算近30天交易的WLR
    # v1.3: 收集per-coin WLR，动态排除WLR<0.4的币种
    cutoff = datetime.now() - timedelta(days=30)
    wins, losses = defaultdict(lambda: {'win':0,'loss':0,'pnl':0.0}), defaultdict(lambda: {'win':0,'loss':0,'pnl':0.0})
    coin_stats = defaultdict(lambda: {'win':0,'loss':0,'pnl':0.0})  # v1.3: per-coin统计
    excluded_factors = {'rsi_inv', '', None, 'recovered'}  # recovered是旧系统遗留best_factor，无意义
    seen_keys = set()
    trade_count = 0
    for t in trades:
        # 解析时间
        open_time = t.get('open_time', '')
        try:
            ts = datetime.fromisoformat(open_time.replace('Z','+00:00'))
            if ts < cutoff:
                continue
        except:
            pass

        coin = t.get('coin', 'UNKNOWN')
        # P0 Fix: 跳过手动开仓的交易（避免手动仓位污染IC统计）
        if '历史手动开仓' in t.get('open_reason', ''):
            continue
        # P2 Fix: 跳过coin_strategy_map中标记为excluded的币种
        if coin in excluded_coins_set:
            continue
        # v1.3: 优先用realized_pnl，没有则用pnl字段
        realized = t.get('realized_pnl', 0) or 0
        unrealized = t.get('unrealized_pnl', 0) or 0
        pnl = realized + unrealized if realized or unrealized else (t.get('pnl', 0) or 0)
        factor_raw = t.get('best_factor', 'recovered')
        factor = FACTOR_MAP.get(factor_raw, factor_raw)

        # v1.3: recovered不再是排除项（它是有效因子）
        if factor_raw in excluded_factors:
            continue
        # v1.3: 跳过未平仓的交易（pnl未实现，不应计入WLR）
        if t.get('status') not in ('CLOSED', 'FAILED'):
            continue
        # v1.2: 跳过重复记录
        key = (coin, open_time, factor_raw)
        if key in seen_keys:
            continue
        seen_keys.add(key)

        # v1.3: 收集per-coin统计（用于动态排除）
        if pnl > 0:
            coin_stats[coin]['win'] += 1
            coin_stats[coin]['pnl'] += pnl
            wins[factor]['win'] += 1
            wins[factor]['pnl'] += pnl
        else:
            coin_stats[coin]['loss'] += 1
            coin_stats[coin]['pnl'] += pnl
            losses[factor]['loss'] += 1
            losses[factor]['pnl'] += pnl
        trade_count += 1

    # v1.3: 动态排除WLR<0.4的币种
    excluded_coins = set()
    for coin, stats in coin_stats.items():
        total = stats['win'] + stats['loss']
        if total >= 3:  # 至少3笔交易才统计可靠
            wlr = stats['win'] / total
            if wlr < 0.4:
                excluded_coins.add(coin)
                print(f"  ⚠️  动态排除 {coin}: WLR={wlr:.2f} ({stats['win']}W/{stats['loss']}L)")

    # 过滤掉被排除币种的因子统计
    for coin in excluded_coins:
        cs = coin_stats[coin]
        if cs['win'] > 0:
            wins[factor] = {'win': wins.get(factor,{}).get('win',0) - cs['win'],
                            'pnl': wins.get(factor,{}).get('pnl',0) - cs['pnl']}
        if cs['loss'] > 0:
            losses[factor] = {'loss': losses.get(factor,{}).get('loss',0) - cs['loss'],
                              'pnl': losses.get(factor,{}).get('pnl',0) - cs['pnl']}

    print(f"\n近30天有效交易: {trade_count}笔")
    print("\n因子WLR统计:")
    all_factors = set(wins.keys()) | set(losses.keys())
    factor_wlr = {}
    for f in all_factors:
        w_count = wins[f]['win']
        l_count = losses[f]['loss']
        total = w_count + l_count
        if total == 0:
            wlr = 0.5
        else:
            wlr = w_count / total
        pnl_total = wins[f]['pnl'] + losses[f]['pnl']
        factor_wlr[f] = wlr
        print(f"  {f}: W={w_count} L={l_count} WLR={wlr:.2f} PnL={pnl_total:+.2f}")

    # 5. 计算新权重（基于WLR）
    # P2 Fix: 要求至少3条有效交易才更新权重（原5笔，系统交易频率低，经常达不到）
    # P2 Fix: 要求至少2条有效交易才更新权重（原5笔太高，原3笔仍偏保守）
    # 系统交易频率低(约2-3次/周)，改为2笔即可触发IC更新
    if trade_count < 2:
        print(f"\n⚠️  有效交易仅{trade_count}笔 (< 2)，不更新权重，保持现状")
        return
    print(f"\n✅ {trade_count}笔有效交易，开始更新权重...")

    # WLR > 0.6: 提高权重; WLR 0.4-0.6: 保持; WLR < 0.4: 降低权重
    # P3 Fix: BTC/Gemma设置上限（各20%），防止它们无限增长挤压技术因子
    BTC_GEMMA_MAX = 0.20
    new_weights = current_weights.copy() if current_weights else {'RSI': 0.3, 'Vol': 0.3, 'ADX': 0.2, 'Gemma': 0.2}
    for factor, wlr in factor_wlr.items():
        if factor not in new_weights:
            continue
        current = new_weights[factor]
        if wlr >= 0.6:
            adjustment = 1.2  # 提高20%
        elif wlr >= 0.4:
            adjustment = 1.0  # 保持
        else:
            adjustment = 0.5  # 降低50%
        new_weights[factor] = min(1.0, current * adjustment)

    # P3 Fix: BTC/Gemma权重上限，防止技术因子被彻底挤出
    for cap_factor in ('BTC', 'Gemma'):
        if cap_factor in new_weights and new_weights[cap_factor] > BTC_GEMMA_MAX:
            excess = new_weights[cap_factor] - BTC_GEMMA_MAX
            new_weights[cap_factor] = BTC_GEMMA_MAX
            # 把多余的权重平均分配给技术因子
            tech_factors = [f for f in new_weights if f not in ('BTC', 'Gemma')]
            if tech_factors:
                boost = excess / len(tech_factors)
                for f in tech_factors:
                    new_weights[f] += boost
            print(f"  [{cap_factor}] 权重被限制到{BTC_GEMMA_MAX:.0%}，超额{excess:.1%}分配给技术因子")

    # v1.3: 不再硬编码DOGE排除，改用动态判断
    # 归一化
    total = sum(new_weights.values())
    if total > 0:
        new_weights = {k: v/total for k, v in new_weights.items()}

    print("\n新旧权重对比:")
    for f in sorted(set(new_weights.keys()) | set(current_weights.keys())):
        old = current_weights.get(f, 0)
        new = new_weights.get(f, 0)
        diff = new - old
        sign = '+' if diff >= 0 else ''
        bar = '█' * int(abs(diff) * 50)
        print(f"  {f:12s}: {old:.1%} → {new:.1%} ({sign}{diff:.1%}) {bar}")

    # 7. 保存到kronos_ic_weights.json（与ICTracker格式兼容）
    #    保留ICTracker的历史数据（ic_history/strategy_quality/adjusted_by），
    #    只更新weights字段（合并trade反馈权重和IC权重）
    existing_data = load_json(IC_WEIGHTS, {})  # 保留ICTracker的完整结构
    result = {
        'ic_history': existing_data.get('ic_history', {}),
        'weights': new_weights,  # 合并后的权重
        'last_update': datetime.now().isoformat(),
        'strategy_quality': existing_data.get('strategy_quality', {}),
        'adjusted_by': existing_data.get('adjusted_by', []) + ['compute_trade_ic'],
        'source': str(PAPER_TRADES),
        'trade_count': trade_count,
        'prev_weights': current_weights,
    }
    tmp = IC_WEIGHTS.with_suffix('.tmp')
    with open(tmp, 'w') as f:
        json.dump(result, f, indent=2)
    tmp.replace(IC_WEIGHTS)
    print(f"\n✅ 已更新 {IC_WEIGHTS}")

    # 8. 飞书通知
    try:
        msg = f"【IC权重反馈更新 {datetime.now().strftime('%m-%d %H:%M')}】\n"
        for f in sorted(new_weights.keys(), key=lambda x: -new_weights[x])[:4]:
            msg += f"{f}: {new_weights[f]:.1%}\n"
        msg += f"来源: {trade_count}笔真实交易"
        sys.path.insert(0, str(Path(__file__).parent))
        from kronos_pilot import push_feishu
        push_feishu(msg)
    except Exception as e:
        print(f"  飞书通知失败: {e}")

if __name__ == '__main__':
    main()
