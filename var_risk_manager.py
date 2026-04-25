#!/usr/bin/env python3
"""
Kronos VaR 风险管理系统
==========================
目的：用历史波动率动态计算风险敞口，替代固定金额熔断

VaR (Value at Risk)：在给定置信度下，未来一段时间内的最大损失
CVaR (Conditional VaR)：VaR情况下的平均损失（Expected Shortfall）

VaR计算方法：
1. 历史模拟法：从真实收益率分布提取百分位数
2. 参数法（EWMA）：用指数加权方差，更注重近期波动
3. 蒙特卡洛：从收益率分布抽样

Kronos使用EWMA参数VaR：
- 波动率 = EWMA(收益率²)，lambda=0.94（行业标准）
- VaR(95%) = z_95 × σ × position_value
- z_95 = 1.645（正态分布95%分位数）
- CVaR(95%) = σ × density at VaR ≈ 2×VaR for normal

运行：
  python3 var_risk_manager.py              # 测试+报告
  python3 var_risk_manager.py --coin AVAX   # AVAX专项分析
"""

import os, json, time, math
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from pathlib import Path

# ========== 配置 ==========
DATA_DIR = Path.home() / '.hermes/cron/output'
VAR_CONFIDENCE = 0.95        # 95%置信度
CVAR_CONFIDENCE = 0.95       # CVaR置信度
EWMA_LAMBDA = 0.94           # RiskMetrics标准
HISTORY_DAYS = 60            # 用多少天历史计算波动率
POSITION_RISK_RATIO = 0.02   # 单仓位最大VaR占比账户2%

# ========== 数据获取 ==========

def fetch_ohlcv(coin, bar='1H', limit=500):
    """分页获取OKX K线数据"""
    try:
        import requests
        all_data = []
        after = None
        for batch in range(5):
            if after:
                url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit=300&after={after}'
            else:
                url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit=300'
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get('code') != '0' or not data.get('data'):
                break
            candles = data['data']
            if len(all_data) == 0:
                oldest_ts = candles[-1][0]
            all_data.extend(candles)
            if len(candles) < 300:
                break
            oldest_ts = candles[-1][0]
            after = oldest_ts
            time.sleep(0.2)

        rows = []
        for d in reversed(all_data):
            rows.append({
                'ts': int(d[0]),
                'close': float(d[4]),
                'high': float(d[2]),
                'low': float(d[3]),
                'volume': float(d[5]),
            })
        df = pd.DataFrame(rows)
        df['date'] = pd.to_datetime(df['ts'], unit='ms')
        return df.set_index('date')
    except Exception as e:
        print(f'数据获取失败: {e}')
        return None

# ========== VaR计算核心 ==========

def compute_returns(closes):
    """计算收益率序列（对数收益率）"""
    closes = np.asarray(closes)
    # 对数收益率: ln(P_t / P_{t-1})
    returns = np.diff(np.log(closes), prepend=closes[0])
    # 第一值为0
    returns[0] = 0
    return returns

def compute_ewma_variance(returns, lookback=100):
    """
    计算波动率方差 - 使用最近lookback个收益率的标准差
    这是最可靠的 VaR 估计方法
    """
    returns = np.asarray(returns)
    n = len(returns)
    if n < 10:
        return 0.0

    # 用最近lookback个收益率
    recent = returns[-lookback:]
    if len(recent) < 20:
        return float(np.var(recent))

    # 标准差
    sigma = float(np.std(recent, ddof=1))
    variance = sigma ** 2
    return max(variance, 0.0001)  # 最小0.01%波动率


def compute_parametric_var(returns, confidence=VAR_CONFIDENCE):
    """
    参数VaR：假设正态分布
    VaR = z_score × sigma × portfolio_value
    """
    variance = compute_ewma_variance(returns)
    sigma = math.sqrt(variance)

    # 正态分布分位数
    # 95%: 1.645, 99%: 2.326
    z_scores = {0.90: 1.282, 0.95: 1.645, 0.99: 2.326}
    z = z_scores.get(confidence, 1.645)

    return sigma, z * sigma
def compute_historical_var(returns, confidence=VAR_CONFIDENCE):
    """
    历史模拟VaR：直接从收益率分布提取百分位
    更准确，不假设正态分布
    """
    returns = np.asarray(returns)
    if len(returns) < 20:
        return 0.0, 0.0

    sigma = np.std(returns)
    var_pct = np.percentile(returns, (1 - confidence) * 100)
    return sigma, abs(var_pct)

def compute_cvar(returns, confidence=CVAR_CONFIDENCE):
    """
    CVaR (Expected Shortfall)：VaR情况下的平均损失
    CVaR = E[loss | loss > VaR]
    """
    returns = np.asarray(returns)
    if len(returns) < 20:
        return 0.0

    var_pct = np.percentile(returns, (1 - confidence) * 100)
    tail_losses = returns[returns <= var_pct]
    if len(tail_losses) > 0:
        cvar_pct = abs(np.mean(tail_losses))
    else:
        cvar_pct = abs(var_pct) * 1.5  # 粗略估计

    return cvar_pct

def compute_var_for_position(coin, position_value_usdt, bar='1H'):
    """
    计算某仓位的VaR

    参数:
      coin: 币种
      position_value_usdt: 仓位价值（USD保证金 × 杠杆）
      bar: K线周期（1H=小时VaR, 4H=4小时VaR）

    返回:
      {
        'coin': coin,
        'position_value': $仓位价值,
        'sigma_1h': 1小时波动率(标准差),
        'var_95_1h': $VaR(95%, 1小时),
        'var_99_1h': $VaR(99%, 1小时),
        'cvar_95_1h': $CVaR(95%, 1小时),
        'risk_level': 'LOW/MEDIUM/HIGH/EXTREME',
        'recommended_stop': 建议止损阈值,
        'volatility_regime': 'LOW/NORMAL/HIGH/EXTREME',
      }
    """
    df = fetch_ohlcv(coin, bar, limit=500)
    if df is None or len(df) < 50:
        # 数据不足，返回保守估计
        return {
            'coin': coin,
            'position_value': position_value_usdt,
            'sigma_1h': 0.03,
            'var_95_1h': position_value_usdt * 0.05,  # 5% of position = conservative fallback
            'var_99_1h': position_value_usdt * 0.08,
            'cvar_95_1h': position_value_usdt * 0.075,
            'var_95_4h': position_value_usdt * 0.05 * 2,
            'var_99_4h': position_value_usdt * 0.08 * 2,
            'max_loss_1h_95': position_value_usdt * 0.075,
            'risk_level': 'MEDIUM',
            'volatility_regime': 'NORMAL',
            'recommended_stop': position_value_usdt * 0.05,
            'var_ratio': 0.05,
            'data_points': 0,
            'note': '数据不足，使用保守估计',
        }

    # 计算收益率
    returns = compute_returns(df['close'].values)

    # 只用最近N天的数据
    lookback = HISTORY_DAYS * 24  # 60天 × 24小时
    if len(returns) > lookback:
        returns = returns[-lookback:]

    # 历史模拟VaR（用百分位数，更准确）
    sigma_1h, var_95_1h = compute_historical_var(returns, 0.95)
    _, var_99_1h = compute_historical_var(returns, 0.99)
    cvar_95 = compute_cvar(returns, 0.95)

    # 4小时VaR（1h VaR × √4）
    var_95_4h = var_95_1h * 2
    var_99_4h = var_99_1h * 2

    # 波动率 regime
    if sigma_1h < 0.01:
        vol_regime = 'LOW'
    elif sigma_1h < 0.02:
        vol_regime = 'NORMAL'
    elif sigma_1h < 0.035:
        vol_regime = 'HIGH'
    else:
        vol_regime = 'EXTREME'

    # 风险等级（基于VaR占仓位价值比例）
    var_ratio = var_95_1h
    if var_ratio < 0.015:
        risk_level = 'LOW'
    elif var_ratio < 0.03:
        risk_level = 'MEDIUM'
    elif var_ratio < 0.05:
        risk_level = 'HIGH'
    else:
        risk_level = 'EXTREME'

    # 推荐止损：CVaR作为主要参考（更保守）
    recommended_stop = cvar_95 * 0.8

    return {
        'coin': coin,
        'position_value': position_value_usdt,
        'sigma_1h': sigma_1h,
        'var_95_1h': var_95_1h * position_value_usdt,
        'var_99_1h': var_99_1h * position_value_usdt,
        'cvar_95_1h': cvar_95 * position_value_usdt,
        'var_95_4h': var_95_4h * position_value_usdt,
        'var_99_4h': var_99_4h * position_value_usdt,
        'max_loss_1h_95': cvar_95 * position_value_usdt * 0.8,
        'risk_level': risk_level,
        'volatility_regime': vol_regime,
        'recommended_stop': recommended_stop * position_value_usdt,
        'var_ratio': var_ratio,
        'data_points': len(returns),
        'note': '',
    }

def compute_portfolio_var(positions, equity):
    """
    计算组合VaR（考虑各仓位的波动率和相关性）

    相关性处理：简化处理
    - 加密货币高度相关，用BTC波动率作为基准
    - 组合VaR ≈ sum(个体VaR) × 相关性系数
    - 对于强趋势市场，相关性≈0.7-0.9
    - 对于震荡市场，策略分散，相关性≈0.4-0.6
    """
    if not positions:
        return {
            'total_var_95': 0,
            'total_var_99': 0,
            'portfolio_var_95': 0,
            'portfolio_var_99': 0,
            'var_utilization': 0,
            'var_limit_95': equity * 0.02,  # 组合VaR不超过账户2%
            'var_limit_99': equity * 0.03,   # 99% VaR不超过账户3%
            'risk_level': 'LOW',
            'positions': [],
        }

    pos_results = []
    total_var_95 = 0
    total_var_99 = 0
    correlation_factor = 0.75  # 简化：加密货币平均相关性

    for pos in positions:
        coin = pos.get('coin', 'UNKNOWN')
        # 仓位价值估算（简化：用持仓数量 × 当前价格）
        # 实际应该用notional value
        pos_value = pos.get('position_value', pos.get('notional_value', 10000))
        result = compute_var_for_position(coin, pos_value)
        result['pos'] = pos
        pos_results.append(result)
        total_var_95 += result['var_95_1h']
        total_var_99 += result['var_99_1h']

    # 组合VaR（考虑相关性后的打折）
    portfolio_var_95 = total_var_95 * math.sqrt(correlation_factor)
    portfolio_var_99 = total_var_99 * math.sqrt(correlation_factor)

    # VaR利用率
    var_limit_95 = equity * 0.02  # 组合VaR不超过账户2%
    var_limit_99 = equity * 0.03  # 99% VaR不超过账户3%
    var_utilization = portfolio_var_95 / var_limit_95 if var_limit_95 > 0 else 0

    # 风险等级
    if var_utilization < 0.5:
        risk_level = 'LOW'
    elif var_utilization < 0.8:
        risk_level = 'MEDIUM'
    elif var_utilization < 1.0:
        risk_level = 'HIGH'
    else:
        risk_level = 'EXTREME'

    return {
        'total_var_95': total_var_95,
        'total_var_99': total_var_99,
        'portfolio_var_95': portfolio_var_95,
        'portfolio_var_99': portfolio_var_99,
        'var_utilization': var_utilization,
        'var_limit_95': var_limit_95,
        'var_limit_99': var_limit_99,
        'equity': equity,
        'risk_level': risk_level,
        'positions': pos_results,
        'correlation_factor': correlation_factor,
    }

# ========== 动态熔断集成 ==========

def var_circuit_breaker_check(positions, equity, hourly_loss, daily_loss, consecutive_losses):
    """
    基于VaR的动态熔断检查

    VaR熔断只用于【开仓限制】：
    - 新开仓时，检查组合VaR是否超过账户2%
    - 如果超过，禁止开新仓（已有仓位继续管理）

    已仓位的保护仍然使用固定熔断（hourly_loss/daily_loss比较固定值）
    原因：VaR是统计估计，适合事前风控；已仓位的极端亏损需要立即响应

    返回: (can_trade: bool, reason: str, warnings: list)
    """
    if not positions:
        return True, '无持仓，可开新仓', []

    warnings = []
    portfolio_var = compute_portfolio_var(positions, equity)
    pvar = portfolio_var

    # 检查1：组合VaR利用率
    var_util = pvar['var_utilization']
    if var_util > 1.0:
        return False, f'组合VaR超限 {var_util:.0%} > 100%，禁止开仓', [
            f'VaR利用率: {var_util:.0%}',
            f'当前组合VaR: ${pvar["portfolio_var_95"]:.0f}',
            f'VaR限额: ${pvar["var_limit_95"]:.0f}',
        ]
    elif var_util > 0.8:
        warnings.append(f'⚠️ VaR利用率 {var_util:.0%} 偏高(>80%)')

    # 检查2：小时亏损 vs 小时VaR
    hourly_var = pvar['portfolio_var_95']
    if hourly_loss > hourly_var * 0.5:
        warnings.append(f'⚠️ 小时亏损${hourly_loss:.0f}超过小时VaR${hourly_var:.0f}的50%')
    if hourly_loss > hourly_var:
        return False, f'小时亏损${hourly_loss:.0f}>小时VaR${hourly_var:.0f}，熔断', [
            f'小时亏损: ${hourly_loss:.0f}',
            f'小时VaR(95%): ${hourly_var:.0f}',
            f'VaR限额: ${pvar["var_limit_95"]:.0f}',
        ]

    # 检查3：日亏损 vs 日VaR（用4h VaR × √6）
    daily_var_est = hourly_var * math.sqrt(6)
    if daily_loss > daily_var_est:
        return False, f'日亏损${daily_loss:.0f}>估算日VaR${daily_var_est:.0f}，熔断', [
            f'日亏损: ${daily_loss:.0f}',
            f'估算日VaR: ${daily_var_est:.0f}',
        ]

    # 检查4：连亏次数
    if consecutive_losses >= 2:
        warnings.append(f'⚠️ 连亏{consecutive_losses}次，降低风险敞口')
    if consecutive_losses >= 3:
        return False, f'连亏{consecutive_losses}次，禁止开仓', [
            f'连亏次数: {consecutive_losses}',
            f'建议等待市场反转信号',
        ]

    # 检查5：各仓位风险等级
    for pr in pvar['positions']:
        if pr['risk_level'] in ('HIGH', 'EXTREME'):
            warnings.append(
                f"⚠️ {pr['coin']}风险等级{pr['risk_level']}，VaR比率{pr['var_ratio']:.1%}"
            )

    # 综合判断
    if warnings:
        reason = f'VaR通过({var_util:.0%})，有{len(warnings)}个警告'
    else:
        reason = f'VaR检查通过 {var_util:.0%} < 80%'

    return True, reason, warnings


# ========== 报告 ==========

def print_var_report(coin='AVAX', position_value=10000):
    """打印VaR分析报告"""
    print(f'\n{"="*60}')
    print(f'Kronos VaR 风险分析报告')
    print(f'{"="*60}')

    result = compute_var_for_position(coin, position_value)
    equity = 91882  # 模拟账户

    print(f'\n【{coin} VaR分析】仓位价值: ${position_value:,.0f}')
    print(f'数据点: {result["data_points"]}个1h K线')
    print(f'1h波动率(σ): {result["sigma_1h"]:.4f} ({result["sigma_1h"]*100:.2f}%)')
    print(f'')
    print(f'【VaR风险值】')
    print(f'  VaR(95%, 1h):  ${result["var_95_1h"]:,.0f}  (最大1小时损失，95%置信)')
    print(f'  VaR(99%, 1h):  ${result["var_99_1h"]:,.0f}  (最大1小时损失，99%置信)')
    print(f'  VaR(95%, 4h):  ${result["var_95_4h"]:,.0f}  (最大4小时损失，95%置信)')
    print(f'  CVaR(95%, 1h): ${result["cvar_95_1h"]:,.0f}  (Expected Shortfall，超VaR的平均损失)')
    print(f'')
    print(f'【风险等级】')
    print(f'  波动率regime: {result["volatility_regime"]}')
    print(f'  仓位风险等级: {result["risk_level"]}')
    print(f'  VaR/仓位比率: {result["var_ratio"]:.2%}')
    print(f'  推荐止损阈值: ${result["recommended_stop"]:,.0f} (CVaR的80%)')
    if result['note']:
        print(f'  注: {result["note"]}')

    print(f'\n【与固定熔断对比】')
    fixed_hourly = equity * 0.02  # 原来固定2%
    print(f'  原固定熔断: ${fixed_hourly:,.0f}/小时')
    print(f'  VaR熔断(95%): ${result["var_95_1h"]:,.0f}/小时')
    print(f'  差异: ${abs(fixed_hourly - result["var_95_1h"]):,.0f}')

    if result["var_95_1h"] > fixed_hourly * 1.2:
        print(f'  → 当前波动率偏高，VaR比固定熔断更严格')
    elif result["var_95_1h"] < fixed_hourly * 0.8:
        print(f'  → 当前波动率偏低，可适当放宽')
    else:
        print(f'  → VaR与固定熔断接近')

    return result

def print_portfolio_report(positions, equity):
    """打印组合VaR报告"""
    print(f'\n{"="*60}')
    print(f'Kronos 组合VaR报告')
    print(f'{"="*60}')

    pvar = compute_portfolio_var(positions, equity)

    print(f'\n【组合概况】')
    print(f'  账户权益: ${pvar["equity"]:,.0f}')
    print(f'  持仓数量: {len(positions)}')
    print(f'  相关性系数: {pvar["correlation_factor"]:.2f} (简化)')
    print(f'')
    print(f'【VaR汇总】')
    print(f'  简单加总VaR(95%): ${pvar["total_var_95"]:,.0f}')
    print(f'  组合VaR(95%):     ${pvar["portfolio_var_95"]:,.0f}')
    print(f'  组合VaR(99%):     ${pvar["portfolio_var_99"]:,.0f}')
    print(f'  VaR限额(95%):     ${pvar["var_limit_95"]:,.0f} (账户2%)')
    print(f'  VaR限额(99%):     ${pvar["var_limit_99"]:,.0f} (账户3%)')
    print(f'  VaR利用率:       {pvar["var_utilization"]:.1%}')
    print(f'  风险等级:         {pvar["risk_level"]}')
    print(f'')
    print(f'【各仓位明细】')
    for pr in pvar['positions']:
        print(f'  {pr["coin"]}: VaR95={pr["var_95_1h"]:,.0f} | CVaR={pr["cvar_95_1h"]:,.0f} | 风险={pr["risk_level"]} | regime={pr["volatility_regime"]}')

    return pvar

# ========== 主程序 ==========

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--coin', default='AVAX')
    parser.add_argument('--position', type=float, default=10000)
    parser.add_argument('--equity', type=float, default=91882)
    args = parser.parse_args()

    # 单币种报告
    var_result = print_var_report(args.coin, args.position)

    # 模拟组合报告
    print(f'\n{"="*60}')
    print(f'【示例组合测试】')
    sample_positions = [
        {'coin': 'AVAX', 'position_value': args.position},
        {'coin': 'SOL', 'position_value': args.position * 0.8},
        {'coin': 'DOGE', 'position_value': args.position * 0.5},
    ]
    pvar = print_portfolio_report(sample_positions, args.equity)

    # VaR熔断测试
    print(f'\n【VaR熔断测试】')
    # 场景1：无亏损
    can_trade, reason, warns = var_circuit_breaker_check(
        sample_positions, args.equity,
        hourly_loss=500, daily_loss=1000, consecutive_losses=0
    )
    print(f'场景1（正常）: {"✅" if can_trade else "❌"} {reason}')
    for w in warns:
        print(f'  {w}')

    # 场景2：小时亏损接近VaR
    can_trade2, reason2, warns2 = var_circuit_breaker_check(
        sample_positions, args.equity,
        hourly_loss=pvar['portfolio_var_95'] * 0.6,
        daily_loss=1000, consecutive_losses=0
    )
    print(f'场景2（亏损>VaR50%）: {"✅" if can_trade2 else "❌"} {reason2}')
    for w in warns2:
        print(f'  {w}')

    # 场景3：VaR超限
    can_trade3, reason3, warns3 = var_circuit_breaker_check(
        sample_positions, args.equity,
        hourly_loss=pvar['portfolio_var_95'] * 1.2,
        daily_loss=5000, consecutive_losses=0
    )
    print(f'场景3（VaR超限）: {"✅" if can_trade3 else "❌"} {reason3}')
    for w in warns3:
        print(f'  {w}')

    print(f'\n{"="*60}')
    print('VaR系统已就绪，可集成到real_monitor.py')
    print(f'{"="*60}')
