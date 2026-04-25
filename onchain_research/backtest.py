#!/usr/bin/env python3
"""链上假设 IC 回测

复刻 crypto-kol-quant 的 backtest.py 逻辑。
对每个注册的假设因子，计算它与未来收益的 Spearman IC。
"""
import sys, os, json
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.dirname(__file__))
from capabilities import CAP_REGISTRY

BASE = os.path.dirname(__file__)


def run_hypotheses(feats: pd.DataFrame) -> pd.DataFrame:
    """评估所有注册的假设，返回分数面板。"""
    out = {}
    for cid, meta in CAP_REGISTRY.items():
        try:
            res = meta['fn'](feats)
            if hasattr(res, 'score'):
                score = res.score
            elif isinstance(res, dict):
                score = res.get('score', 0)
            else:
                score = res
            if hasattr(score, '__len__') and len(score) == len(feats):
                out[cid] = np.asarray(score, dtype=float)
            else:
                out[cid] = np.full(len(feats), float(score) if np.isscalar(score) else 0.0)
        except Exception as e:
            out[cid] = np.zeros(len(feats))
    return pd.DataFrame(out, index=feats.index)


def run_ic_analysis(panel: pd.DataFrame) -> pd.DataFrame:
    """对每个假设因子计算 IC（Spearman）"""
    factor_cols = [c for c in panel.columns if c.startswith('hyp_') or c.startswith('emg_')]
    results = []

    for fc in factor_cols:
        if fc not in CAP_REGISTRY:
            continue
        meta = CAP_REGISTRY[fc]
        row = {
            'factor': fc,
            'type': meta['type'],
            'impl': meta['impl'],
            'bias': meta['bias_default'],
        }

        for h_name, h_col in [('1d', 'fwd_ret_1d'), ('7d', 'fwd_ret_7d'), ('30d', 'fwd_ret_30d')]:
            df = panel[[fc, h_col]].dropna()
            if len(df) < 20 or df[fc].nunique() < 2:
                row[f'ic_{h_name}'] = np.nan
                row[f'n_{h_name}'] = 0
                row[f'pval_{h_name}'] = np.nan
                continue
            ic, pval = stats.spearmanr(df[fc], df[h_col])
            row[f'ic_{h_name}'] = ic
            row[f'pval_{h_name}'] = pval
            row[f'n_{h_name}'] = len(df)

        # 信号触发统计
        df_30 = panel[[fc, 'fwd_ret_30d']].dropna()
        row['trig_count'] = int((df_30[fc].abs() > 0.01).sum()) if not df_30.empty else 0

        results.append(row)

    return pd.DataFrame(results)


def main():
    features_path = f'{BASE}/data/features.parquet'
    if not os.path.exists(features_path):
        print(f'[WARN] {features_path} not found. Using simulated data.')
        panel = generate_demo_panel()
    else:
        panel = pd.read_parquet(features_path)

    print(f'Features: {panel.shape}')

    # 对每个币种跑因子
    all_results = []
    for sym in panel.index.get_level_values(0).unique():
        sub = panel.loc[sym].copy()
        fac = run_hypotheses(sub)
        fac['symbol'] = sym
        fac['fwd_ret_1d'] = sub['fwd_ret_1d']
        fac['fwd_ret_7d'] = sub['fwd_ret_7d']
        fac['fwd_ret_30d'] = sub['fwd_ret_30d']
        all_results.append(fac)

    panel_out = pd.concat(all_results)
    panel_out.to_csv(f'{BASE}/data/hypotheses.csv')

    # IC 分析
    ic_df = run_ic_analysis(panel_out)
    ic_df.to_csv(f'{BASE}/output/ic_results.csv', index=False)

    # 打印结果
    ic_valid = ic_df[ic_df['n_30d'] > 10].copy()
    ic_sorted = ic_valid.sort_values('ic_30d', ascending=False)

    print(f'\n=== Top 10 假设因子 by 30d IC ===')
    for _, r in ic_sorted.head(10).iterrows():
        print(f"  {r['factor'][:42]:42s} IC={r['ic_30d']:+.3f}  bias={r['bias']}  n={int(r['n_30d'])}  p={r['pval_30d']:.3f}")

    print(f'\n=== Bottom 10 假设因子 by 30d IC ===')
    for _, r in ic_sorted.tail(10).iterrows():
        print(f"  {r['factor'][:42]:42s} IC={r['ic_30d']:+.3f}  bias={r['bias']}  n={int(r['n_30d'])}  p={r['pval_30d']:.3f}")

    print(f'\n=== 统计摘要 ===')
    print(f'总假设因子: {len(ic_df)}')
    sig = ((ic_df['ic_30d'].abs() > 0.05) & (ic_df['n_30d'] > 10)).sum()
    print(f'|IC| > 0.05 且 n>10: {sig} 个')
    strong = ((ic_df['ic_30d'].abs() > 0.1) & (ic_df['n_30d'] > 10)).sum()
    print(f'|IC| > 0.1 且 n>10: {strong} 个')

    return ic_df


def generate_demo_panel():
    """生成演示用模拟数据（在没有真实链上数据时）"""
    dates = pd.date_range('2024-01-01', periods=600, freq='D')
    np.random.seed(42)

    prices = 40000 + np.cumsum(np.random.randn(600) * 500)
    btc = pd.DataFrame({
        'close': prices,
        'open': prices * (1 + np.random.randn(600) * 0.005),
        'high': prices * (1 + np.abs(np.random.randn(600)) * 0.01),
        'low': prices * (1 - np.abs(np.random.randn(600)) * 0.01),
    }, index=dates)

    c = btc['close']
    out = pd.DataFrame(index=dates)
    out['symbol'] = 'BTCUSDT'
    out['close'] = c
    out['open'] = btc['open']
    out['high'] = btc['high']
    out['low'] = btc['low']
    out['ma20'] = c.rolling(20, min_periods=1).mean()
    out['ma50'] = c.rolling(50, min_periods=1).mean()
    out['ma200'] = c.rolling(200, min_periods=1).mean()
    out['rsi14'] = 50 + np.random.randn(600) * 20
    out['bb_upper'] = out['ma20'] + 2 * c.rolling(20).std()
    out['bb_mid'] = out['ma20']
    out['bb_lower'] = out['ma20'] - 2 * c.rolling(20).std()
    out['price_above_ma200'] = (c > out['ma200']).astype(int)
    out['price_above_ma50'] = (c > out['ma50']).astype(int)
    out['ma50_above_ma200'] = (out['ma50'] > out['ma200']).astype(int)
    out['fwd_ret_1d'] = c.pct_change(1).shift(-1)
    out['fwd_ret_7d'] = c.pct_change(7).shift(-7)
    out['fwd_ret_30d'] = c.pct_change(30).shift(-30)

    # 链上特征（模拟）
    out['mvrv_zscore'] = np.random.randn(600) * 2
    out['mvrv_zscore_prev'] = out['mvrv_zscore'].shift(1)
    out['mvrv_ratio'] = 1 + np.random.randn(600) * 0.5
    out['mvrv_zscore_pctile'] = pd.Series(np.random.rand(600), index=dates)
    out['sopr'] = 1 + np.random.randn(600) * 0.15
    out['sopr_prev'] = out['sopr'].shift(1)
    out['exchange_balance_chg_7d'] = np.random.randn(600) * 0.05
    out['exchange_balance_chg_30d'] = np.random.randn(600) * 0.10

    # ETF 资金流（模拟 - 与价格变化相关联）
    # 假设：ETF净流入与未来收益正相关
    base_flow = np.random.randn(600) * 5e7  # 每日平均5000万
    # 加入与未来收益的微弱正相关（模拟机构买入→后续上涨）
    price_signal = c.pct_change(7).shift(-7).fillna(0) * 1e9  # 未来7日收益→信号
    flow = base_flow + price_signal * 0.3
    out['etf_flow_1d'] = flow
    out['etf_flow_3d_sum'] = pd.Series(flow).rolling(3).sum().values
    out['etf_flow_5d_ma'] = pd.Series(flow).rolling(5).mean().values
    # Z-Score（20日）
    flow_ma20 = pd.Series(flow).rolling(20).mean()
    flow_std20 = pd.Series(flow).rolling(20).std()
    out['etf_flow_zscore_20d'] = (flow - flow_ma20) / (flow_std20 + 1e-9)
    # 连续净流入天数
    consec = pd.Series(np.where(flow > 0, 1, -1))
    out['etf_flow_consecutive_days'] = consec.replace(-1, 0).rolling(30).apply(
        lambda x: len(x) - np.argmax(x[::-1]) if (x > 0).all() else 0, raw=True
    ).fillna(0).values
    out['etf_flow_domination'] = np.random.rand(600) * 0.6 + 0.3  # IBIT占比30-90%

    panel = pd.concat([out], keys=['BTCUSDT'], names=['symbol', 'date'])
    return panel


if __name__ == '__main__':
    ic_df = main()
