#!/usr/bin/env python3
"""
Kronos Block Bootstrap 蒙特卡洛模拟器

替代GBM几何布朗运动模型，解决原L4验证中：
- GBM假设不适用于加密货币（肥尾、波动率聚集）
- 5%分位数出现-98%不现实回撤

方法：Block Bootstrap分块重采样
- 保留时间序列相关性
- 保留波动率聚集效应
- 保留收益率分布的肥尾特性

使用方法：
    from utils.block_bootstrap_mc import BlockBootstrapMC
    mc = BlockBootstrapMC(btc_returns)
    results = mc.run(n_simulations=1000, forecast_years=4)
"""

import numpy as np
import pandas as pd
import warnings
warnings.filterwarnings('ignore')


class BlockBootstrapMC:
    """
    Block Bootstrap 蒙特卡洛模拟器
    
    使用方法：
        mc = BlockBootstrapMC(returns_series)  # 传入收益率序列
        results = mc.run(n_simulations=1000, forecast_years=4)
    """
    
    def __init__(self, returns: pd.Series, block_length: int = None, random_seed: int = 42):
        """
        初始化模拟器
        
        Args:
            returns: 资产日收益率序列（pd.Series，索引为日期）
            block_length: 块长，None则自动计算
            random_seed: 随机种子（用于复现）
        """
        self.returns = returns.dropna()
        self.n_obs = len(self.returns)
        self.random_seed = random_seed
        np.random.seed(random_seed)
        
        # 自动计算最优块长（Politis & White 2004 简化版）
        if block_length is None:
            self.block_length = self._calc_optimal_block_length()
        else:
            self.block_length = block_length
        
        # 预计算收益率分布特征
        self.mean = self.returns.mean()
        self.std = self.returns.std()
        self.skew = self.returns.skew()
        self.kurtosis = self.returns.kurtosis()
        
    def _calc_optimal_block_length(self) -> int:
        """自动计算最优块长：基于自相关函数衰减"""
        rets = self.returns.values
        n = len(rets)
        mean = rets.mean()
        var = ((rets - mean) ** 2).sum() / n
        
        # 简化的自相关计算：滞后1期相关度
        if var == 0:
            return min(24, n // 10)
        
        autocorr = np.corrcoef(rets[:-1], rets[1:])[0, 1]
        if np.isnan(autocorr):
            autocorr = 0
        
        # 块长与自相关成正比：相关性越高，块长越长
        block_length = max(6, min(int(24 * (1 + autocorr)), n // 5))
        return block_length
    
    def _generate_blocks(self, n_blocks: int) -> np.ndarray:
        """生成分块重采样的收益率序列"""
        path = []
        for _ in range(n_blocks):
            # 随机选择块的起始位置
            start_idx = np.random.randint(0, self.n_obs - self.block_length + 1)
            # 提取块
            block = self.returns.iloc[start_idx:start_idx + self.block_length].values
            path.extend(block.tolist())
        return np.array(path)
    
    def run(self, n_simulations: int = 1000, forecast_days: int = 252,
            trades_per_year: int = None, fee: float = 0.004,
            initial_capital: float = 10000) -> dict:
        """
        运行蒙特卡洛模拟
        
        Args:
            n_simulations: 模拟次数
            forecast_days: 预测天数（默认252 = 1年）
            trades_per_year: 年均交易次数（None则从数据推断）
            fee: 单笔交易成本（默认0.4%）
            initial_capital: 初始资金
        
        Returns:
            dict: 包含所有风险收益指标
        """
        # 推断年均交易次数
        # 数据是hourly，但strategy是按Daily交易日（20日突破每年约20-30次）
        # 用 Daily 回报率来估计
        if trades_per_year is None:
            # 从数据中推断：先resample到日线
            if isinstance(self.returns.index, pd.DatetimeIndex) and len(self.returns) > 1000:
                daily_rets = self.returns.resample('1D').last().dropna()
                # 估计每年交易次数（每日突破约20-30次）
                trades_per_year = 22  # 20日突破策略的年化交易次数
            else:
                # 小时数据：直接除以forecast_days换算
                trades_per_year = max(4, int(self.n_obs / (forecast_days * 24) * 365))
        
        n_trades_total = int(trades_per_year * forecast_days / 365)
        n_blocks = int(np.ceil(n_trades_total / self.block_length)) + 1
        
        # 存储每次模拟的最终收益和最大回撤
        final_returns = []
        max_drawdowns = []
        yearly_returns = []
        
        for _ in range(n_simulations):
            # 生成重采样路径
            resampled_rets = self._generate_blocks(n_blocks)[:n_trades_total]
            
            # 应用交易成本（每笔交易扣fee）
            costs = np.full(n_trades_total, fee)
            net_rets = resampled_rets - costs
            
            # 计算累计收益曲线
            cum = np.cumprod(1 + net_rets)
            
            # 最终收益
            final_ret = (cum[-1] - 1) * 100 if len(cum) > 0 else 0
            final_returns.append(final_ret)
            
            # 最大回撤
            peak = np.maximum.accumulate(cum)
            dd = (peak - cum) / peak * 100
            max_dd = np.max(dd) if len(dd) > 0 else 0
            max_drawdowns.append(max_dd)
            
            # 年度收益
            trades_per_sim_year = trades_per_year
            n_years = n_trades_total / trades_per_sim_year if trades_per_sim_year > 0 else 1
            yearly = (cum[-1] ** (1 / n_years) - 1) * 100 if n_years > 0 and cum[-1] > 0 else 0
            yearly_returns.append(yearly)
        
        final_returns = np.array(final_returns)
        max_drawdowns = np.array(max_drawdowns)
        yearly_returns = np.array(yearly_returns)
        
        # 计算风险指标
        var_95 = np.percentile(final_returns, 5)
        var_99 = np.percentile(final_returns, 1)
        cvar_95 = final_returns[final_returns <= var_95].mean() if len(final_returns[final_returns <= var_95]) > 0 else var_95
        cvar_99 = final_returns[final_returns <= var_99].mean() if len(final_returns[final_returns <= var_99]) > 0 else var_99
        
        # 通过条件
        pct_5_positive = (final_returns > 0).mean() * 100
        pass_condition = var_95 > -50 and np.median(final_returns) > 0
        
        return {
            # 分布统计
            'n_simulations': n_simulations,
            'n_trades_per_year': trades_per_year,
            'block_length': self.block_length,
            
            # 收益率分布
            'mean_return': np.mean(final_returns),
            'median_return': np.median(final_returns),
            'std_return': np.std(final_returns),
            'min_return': np.min(final_returns),
            'max_return': np.max(final_returns),
            'pct_positive': pct_5_positive,
            
            # VaR / CVaR
            'var_95': var_95,
            'cvar_95': cvar_95,
            'var_99': var_99,
            'cvar_99': cvar_99,
            
            # 最大回撤分布
            'mean_max_dd': np.mean(max_drawdowns),
            'median_max_dd': np.median(max_drawdowns),
            'worst_max_dd': np.max(max_drawdowns),
            'pct_dd_over_30': (max_drawdowns > 30).mean() * 100,
            
            # 分布尾段
            'pct_5_return': np.percentile(final_returns, 5),
            'pct_25_return': np.percentile(final_returns, 25),
            'pct_75_return': np.percentile(final_returns, 75),
            'pct_95_return': np.percentile(final_returns, 95),
            
            # 通过条件
            'pass': pass_condition,
            'fail_reason': None if pass_condition else (
                'VaR_5%太高' if var_95 <= -50 else '中位数<=0'
            ),
            
            # 原始分布特征
            'source_mean': self.mean * 100,
            'source_std': self.std * 100,
            'source_skew': self.skew,
            'source_kurtosis': self.kurtosis,
        }
    
    def print_summary(self, results: dict) -> None:
        """打印模拟结果摘要"""
        print('\n' + '=' * 50)
        print('Block Bootstrap Monte Carlo 结果摘要')
        print('=' * 50)
        print(f'模拟次数: {results["n_simulations"]}')
        print(f'年均交易: {results["n_trades_per_year"]}笔')
        print(f'块长: {results["block_length"]}')
        print()
        print('--- 收益率分布 ---')
        print(f'均值:   {results["mean_return"]:+.1f}%')
        print(f'中位数: {results["median_return"]:+.1f}%')
        print(f'5%分位: {results["pct_5_return"]:+.1f}%  ← VaR 95%')
        print(f'25%分位:{results["pct_25_return"]:+.1f}%')
        print(f'75%分位:{results["pct_75_return"]:+.1f}%')
        print(f'95%分位:{results["pct_95_return"]:+.1f}%')
        print(f'最大:   {results["max_return"]:+.1f}%')
        print(f'最小:   {results["min_return"]:+.1f}%')
        print(f'正收益概率: {results["pct_positive"]:.1f}%')
        print()
        print('--- VaR / CVaR ---')
        print(f'VaR 95%:  {results["var_95"]:+.1f}%')
        print(f'CVaR 95%: {results["cvar_95"]:+.1f}%')
        print(f'VaR 99%:  {results["var_99"]:+.1f}%')
        print(f'CVaR 99%: {results["cvar_99"]:+.1f}%')
        print()
        print('--- 最大回撤 ---')
        print(f'均值:   -{results["mean_max_dd"]:.1f}%')
        print(f'中位数: -{results["median_max_dd"]:.1f}%')
        print(f'最差:   -{results["worst_max_dd"]:.1f}%')
        print(f'>30%概率:{results["pct_dd_over_30"]:.1f}%')
        print()
        print('--- 通过条件 ---')
        status = '✅ 通过' if results['pass'] else f'❌ 失败 ({results["fail_reason"]})'
        print(f'L4: {status}')
        print(f'条件: VaR_5%>-50% 且 中位数>0')
        print('=' * 50)


if __name__ == '__main__':
    # 测试：用BTC日收益率数据
    import os
    DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
    
    for coin in ['BTC', 'ETH']:
        path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
        if not os.path.exists(path):
            print(f'{coin}数据不存在，跳过')
            continue
        
        try:
            df = pd.read_csv(path)
            df.columns = [c.lstrip('\ufeff') for c in df.columns]
            ts_col = [c for c in df.columns if 'timestamp' in c.lower() or 'datetime' in c.lower()][0]
            df['ts'] = pd.to_datetime(df[ts_col], unit='ms', errors='coerce')
            if df['ts'].isna().all():
                df['ts'] = pd.to_datetime(df[ts_col], errors='coerce')
            df = df.set_index('ts')
            cols = [c for c in ['open','high','low','close','vol','volume'] if c in df.columns]
            df = df[cols].rename(columns={'volume':'vol'})
            ohlc = df.resample('1h').agg({'open':'first','high':'max','low':'min','close':'last','vol':'sum'}).dropna()
            
            # 用2020-2026数据计算日收益率
            close_daily = ohlc['close'].resample('1D').last().dropna()
            returns = close_daily.pct_change().dropna()
            
            print(f'\n{coin}: {returns.index[0].date()} ~ {returns.index[-1].date()} ({len(returns)}天)')
            
            mc = BlockBootstrapMC(returns)
            results = mc.run(n_simulations=1000, forecast_days=252, fee=0.004)
            mc.print_summary(results)
        except Exception as e:
            print(f'{coin}测试失败: {e}')
