#!/usr/bin/env python3
"""
strategy_validator.py - 策略验证器
所有策略必须通过此验证器才有资格进入实盘讨论

验证流程:
1. Walk-Forward 三层切分 (训练/验证/测试)
2. 参数正交扰动测试
3. 逻辑压力测试（失败案例分析）
4. 最终判决

用法:
    from strategy_validator import StrategyValidator
    validator = StrategyValidator(your_strategy_fn, df, params)
    report = validator.run_full_validation()
"""
import numpy as np
import pandas as pd
from typing import Callable, Dict, List, Tuple, Optional, Any
from dataclasses import dataclass, field
from datetime import datetime
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 指标库
# ============================================================

def calc_rsi(prices: np.ndarray, period: int = 14) -> np.ndarray:
    d = np.diff(prices, prepend=prices[0])
    g = np.where(d > 0, d, 0)
    l = np.where(d < 0, -d, 0)
    ag = pd.Series(g).rolling(period).mean()
    al = pd.Series(l).rolling(period).mean()
    return (100 - (100 / (1 + ag / (al + 1e-10)))).values

def calc_ema(series: pd.Series, span: int) -> np.ndarray:
    return series.ewm(span=span, adjust=False).mean().values

def calc_atr(high: np.ndarray, low: np.ndarray, close: np.ndarray, period: int = 14) -> np.ndarray:
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    return pd.Series(tr).rolling(period).mean().values

def calc_dmi(high, low, close, period=14):
    prev_close = np.roll(close, 1)
    prev_close[0] = close[0]
    up = high - np.roll(high, 1)
    dn = np.roll(low, 1) - low
    up[0] = 0; dn[0] = 0
    plus_dm = np.where((up > dn) & (up > 0), up, 0)
    minus_dm = np.where((dn > up) & (dn > 0), dn, 0)
    tr = np.maximum(high - low, np.maximum(np.abs(high - prev_close), np.abs(low - prev_close)))
    atr = pd.Series(tr).rolling(period).mean()
    plus_di = pd.Series(plus_dm).rolling(period).mean() / (atr + 1e-10) * 100
    minus_di = pd.Series(minus_dm).rolling(period).mean() / (atr + 1e-10) * 100
    dx = abs(plus_di - minus_di) / (plus_di + minus_di + 1e-10) * 100
    adx = dx.rolling(period).mean()
    return adx.values, plus_di.values, minus_di.values

# ============================================================
# 数据结构
# ============================================================

@dataclass
class ValidationResult:
    """单次回测结果"""
    trades: int
    win_rate: float
    avg_return: float
    profit_factor: float
    total_return: float
    # 扩展字段
    avg_hold_bars: float = 0
    max_win: float = 0
    max_loss: float = 0
    sr: float = 0  # Sharpe-like (avg/stdev of returns)

@dataclass
class SegmentResult:
    """Walk-Forward 阶段结果"""
    period: str
    start: str
    end: str
    bars: int
    result: Optional[ValidationResult]
    regime_distribution: Dict[str, float] = field(default_factory=dict)

@dataclass
class PerturbationResult:
    """参数扰动测试结果"""
    param_name: str
    base_value: Any
    perturbed_values: List[Any]
    base_metric: float
    perturbed_metrics: List[float]
    stability: float  # min/max ratio

# ============================================================
# 核心验证引擎
# ============================================================

class StrategyValidator:
    """
    策略验证器

    使用方法:
        def my_strategy(df, rsi_thresh=35):
            # 返回 dict: {'signal': 1/0, 'direction': 'long'/'short'/None}
            ...

        validator = StrategyValidator(
            strategy_fn=my_strategy,
            df=data,
            default_params={'rsi_thresh': 35, 'hold_bars': 2},
            metric='profit_factor'  # 评估指标
        )
        report = validator.run_full_validation()
    """

    def __init__(
        self,
        strategy_fn: Callable,
        df: pd.DataFrame,
        default_params: Dict[str, Any],
        metric: str = 'profit_factor',
        train_end: str = '2025-01-01',
        val_end: str = '2025-04-01',
    ):
        self.strategy_fn = strategy_fn
        self.df = df.copy()
        self.default_params = default_params
        self.metric = metric  # 'profit_factor' | 'win_rate' | 'avg_return'
        self.train_end = train_end
        self.val_end = val_end

        # 计算分割
        self.train = df[df.index < train_end].copy()
        self.val   = df[(df.index >= train_end) & (df.index < val_end)].copy()
        self.test  = df[df.index >= val_end].copy()

        self.results = {}
        self.verdict = "PENDING"

    # ============================================================
    # 回测引擎
    # ============================================================

    def backtest(self, data: pd.DataFrame, params: Dict[str, Any],
                 hold_max: int = 4) -> Optional[ValidationResult]:
        """在数据段上执行策略回测"""
        if len(data) < 50:
            return None

        # 生成信号
        try:
            signals = self.strategy_fn(data, **params)
        except Exception as e:
            return None

        if signals is None or 'signal' not in signals:
            return None

        sig = signals['signal']  # np.array of 0/1
        direction = signals.get('direction', 'long')

        close = data['close'].values.astype(float)
        close_arr = close

        trades = []
        pos = None
        entry_price = None

        for i in range(20, len(sig) - hold_max):
            if sig[i] == 1 and pos is None:
                pos = i
                entry_price = float(close_arr[i])
            elif pos is not None and sig[i] == 0:
                # 出场
                curr_price = float(close_arr[i])
                hold = i - pos
                if direction == 'long':
                    ret = (curr_price - entry_price) / entry_price
                else:  # short
                    ret = (entry_price - curr_price) / entry_price

                trades.append(ret)
                pos = None
                entry_price = None

        if not trades:
            return None

        returns = np.array(trades)
        wins = returns[returns > 0]
        losses = returns[returns < 0]

        if len(trades) < 5:
            return ValidationResult(
                trades=len(trades),
                win_rate=len(wins)/len(returns) if len(returns) > 0 else 0,
                avg_return=float(np.mean(returns)) if len(returns) > 0 else 0,
                profit_factor=float(abs(wins.sum()/losses.sum())) if len(losses) > 0 and losses.sum() != 0 else 999,
                total_return=float(returns.sum()),
            )

        return ValidationResult(
            trades=len(trades),
            win_rate=float(len(wins)/len(returns)),
            avg_return=float(np.mean(returns)),
            profit_factor=float(abs(wins.sum()/losses.sum())) if len(losses) > 0 and losses.sum() != 0 else 999,
            total_return=float(returns.sum()),
            avg_hold_bars=float(np.mean([i for i in range(len(trades))])),
            max_win=float(wins.max()) if len(wins) > 0 else 0,
            max_loss=float(losses.min()) if len(losses) > 0 else 0,
            sr=float(np.mean(returns)/np.std(returns)) if len(returns) > 1 and np.std(returns) > 0 else 0,
        )

    # ============================================================
    # Walk-Forward 验证
    # ============================================================

    def run_walkforward(self) -> Dict:
        """阶段1: Walk-Forward 三段验证"""
        print("\n" + "="*60)
        print("阶段1: Walk-Forward 三层切分")
        print("="*60)

        params = self.default_params.copy()
        results = {}

        for name, segment in [("训练集", self.train), ("验证集", self.val), ("测试集", self.test)]:
            if len(segment) < 50:
                results[name] = None
                continue
            r = self.backtest(segment, params)
            results[name] = r
            if r:
                metric_val = getattr(r, self.metric)
                print(f"  {name}: 交易={r.trades} 胜率={r.win_rate:.1%} "
                      f"均收益={r.avg_return:+.2%} PF={r.profit_factor:.2f}")
            else:
                print(f"  {name}: 无有效结果")

        # 证伪检查
        train_r = results.get("训练集")
        val_r = results.get("验证集")
        test_r = results.get("测试集")

        falsified = False
        decay_info = {}

        if train_r and val_r:
            train_metric = getattr(train_r, self.metric)
            val_metric = getattr(val_r, self.metric)
            if train_metric > 0:
                decay = (train_metric - val_metric) / train_metric
                decay_info['train_val'] = decay
                if self.metric == 'profit_factor':
                    if decay > 0.5 or val_metric < 1.0:
                        falsified = True
                elif self.metric == 'win_rate':
                    if decay > 0.3 or val_metric < 0.40:
                        falsified = True

        self.results['walkforward'] = {
            'segments': results,
            'falsified': falsified,
            'decay': decay_info,
        }
        return results

    # ============================================================
    # 参数正交扰动测试
    # ============================================================

    def run_perturbation(self, param_ranges: Dict[str, List]) -> PerturbationResult:
        """
        阶段2: 参数正交扰动测试
        param_ranges: {'param_name': [v1, v2, v3, ...]}
        """
        print("\n" + "="*60)
        print("阶段2: 参数正交扰动测试")
        print("="*60)

        results = []
        base_params = self.default_params.copy()

        # 对每个参数单独扰动
        for param_name, perturbed_values in param_ranges.items():
            perturbed_metrics = []
            for val in perturbed_values:
                test_params = base_params.copy()
                test_params[param_name] = val
                r = self.backtest(self.train, test_params)
                if r:
                    m = getattr(r, self.metric)
                    perturbed_metrics.append(m)
                else:
                    perturbed_metrics.append(0)

            base_value = base_params.get(param_name)
            base_r = self.backtest(self.train, base_params)
            base_metric = getattr(base_r, self.metric) if base_r else 0

            # 计算稳定性: min(perturbed) / max(perturbed)
            valid_metrics = [m for m in perturbed_metrics if m > 0]
            if len(valid_metrics) > 1:
                stability = min(valid_metrics) / max(valid_metrics) if max(valid_metrics) > 0 else 0
            else:
                stability = 0

            # 统计PF>1.5的比例
            good_count = sum(1 for m in valid_metrics if m >= 1.5)
            good_ratio = good_count / len(valid_metrics) if valid_metrics else 0

            print(f"\n  参数 {param_name} (基准={base_value}, 基准PF={base_metric:.2f}):")
            for val, m in zip(perturbed_values, perturbed_metrics):
                flag = " ← 基准" if val == base_value else ""
                print(f"    {param_name}={val}: {self.metric}={m:.2f}{flag}")
            print(f"    稳定性: {stability:.1%} | PF≥1.5占比: {good_ratio:.1%}")

            results.append(PerturbationResult(
                param_name=param_name,
                base_value=base_value,
                perturbed_values=perturbed_values,
                base_metric=base_metric,
                perturbed_metrics=perturbed_metrics,
                stability=stability,
            ))

        # 计算综合稳定性
        overall_stability = np.mean([r.stability for r in results]) if results else 0
        print(f"\n  综合稳定性: {overall_stability:.1%}")

        # 计算PF>1.5的占比（对所有参数组合）
        all_good = []
        for r in results:
            valid = [m for m in r.perturbed_metrics if m > 0]
            good = sum(1 for m in valid if m >= 1.5)
            all_good.append(good / len(valid) if valid else 0)
        pf_15_ratio = np.mean(all_good)

        self.results['perturbation'] = {
            'per_param': results,
            'overall_stability': overall_stability,
            'pf_15_ratio': pf_15_ratio,
        }
        return results

    # ============================================================
    # 失败案例分析
    # ============================================================

    def analyze_failures(self, data: pd.DataFrame, params: Dict[str, Any],
                        n_failures: int = 10) -> List[Dict]:
        """阶段3: 失败案例聚类分析"""
        print("\n" + "="*60)
        print("阶段3: 失败案例分析")
        print("="*60)

        try:
            signals = self.strategy_fn(data, **params)
        except:
            return []

        if signals is None or 'signal' not in signals:
            return []

        sig = signals['signal']
        close = data['close'].values.astype(float)
        direction = signals.get('direction', 'long')

        # 收集所有亏损交易
        losses = []
        pos = None
        entry_price = None

        for i in range(20, len(sig) - 4):
            if sig[i] == 1 and pos is None:
                pos = i
                entry_price = float(close[i])
            elif pos is not None and sig[i] == 0:
                curr_price = float(close[i])
                if direction == 'long':
                    ret = (curr_price - entry_price) / entry_price
                else:
                    ret = (entry_price - curr_price) / entry_price

                if ret < 0:
                    losses.append({
                        'entry_idx': pos,
                        'exit_idx': i,
                        'entry_price': entry_price,
                        'exit_price': curr_price,
                        'return': ret,
                        'entry_time': data.index[pos],
                    })
                pos = None
                entry_price = None

        if not losses:
            print("  无失败案例")
            return []

        losses.sort(key=lambda x: x['return'])

        print(f"  总亏损交易: {len(losses)}")
        print(f"  最差5笔: ")

        # 分析失败案例的特征
        for loss in losses[:min(n_failures, len(losses))]:
            idx = loss['entry_idx']
            # 提取失败时的市场特征
            atr_now = float(data['atr'].iloc[idx]) if 'atr' in data.columns else 0
            rsi_now = float(data['rsi'].iloc[idx]) if 'rsi' in data.columns else 50
            vol_now = float(data['volume'].iloc[idx]) if 'volume' in data.columns else 0
            vol_ma = float(data['volume_ma'].iloc[idx]) if 'volume_ma' in data.columns else 1
            vol_ratio = vol_now / vol_ma if vol_ma > 0 else 1

            print(f"    {loss['entry_time']} 亏损={loss['return']:+.2%} "
                  f"RSI={rsi_now:.0f} ATR={atr_now:.0f} Vol比={vol_ratio:.1f}x")

        # 统计失败案例的RSI分布
        rsi_vals = []
        for loss in losses:
            idx = loss['entry_idx']
            if 'rsi' in data.columns:
                rsi_vals.append(float(data['rsi'].iloc[idx]))

        if rsi_vals:
            print(f"\n  亏损案例RSI分布: 均值={np.mean(rsi_vals):.0f} 中位数={np.median(rsi_vals):.0f}")
            print(f"  RSI<30亏损占比: {sum(1 for r in rsi_vals if r < 30)/len(rsi_vals):.1%}")
            print(f"  RSI 30-50亏损占比: {sum(1 for r in rsi_vals if 30 <= r < 50)/len(rsi_vals):.1%}")
            print(f"  RSI>50亏损占比: {sum(1 for r in rsi_vals if r >= 50)/len(rsi_vals):.1%}")

        self.results['failures'] = {
            'total_losses': len(losses),
            'losses_sample': losses[:n_failures],
        }
        return losses

    # ============================================================
    # 综合报告
    # ============================================================

    def generate_report(self) -> str:
        """生成最终报告"""
        print("\n" + "="*60)
        print("最终判决")
        print("="*60)

        wf = self.results.get('walkforward', {})
        pert = self.results.get('perturbation', {})

        # 判决条件
        falsified = wf.get('falsified', False)
        overall_stability = pert.get('overall_stability', 0)
        pf_15_ratio = pert.get('pf_15_ratio', 0)

        # 三重判决
        verdict_1 = not falsified  # Walk-Forward 通过
        verdict_2 = overall_stability > 0.5  # 参数稳定
        verdict_3 = pf_15_ratio > 0.2  # 20%以上参数组合PF>1.5

        all_pass = verdict_1 and verdict_2 and verdict_3

        print(f"\n  判决维度:")
        print(f"    1. Walk-Forward验证: {'✅ 通过' if verdict_1 else '🚫 证伪'}")
        if 'decay' in wf:
            decay = wf['decay']
            for k, v in decay.items():
                print(f"       {k}衰减: {v:.1%}")

        print(f"    2. 参数稳定性: {'✅ 通过' if verdict_2 else '🚫 失败'} ({overall_stability:.1%})")
        print(f"    3. PF>1.5占比: {'✅ 通过' if verdict_3 else '🚫 失败'} ({pf_15_ratio:.1%})")

        print(f"\n{'='*60}")
        if all_pass:
            print(f"  最终判决: ✅ 进入实盘备选池")
            self.verdict = "PASS"
        else:
            print(f"  最终判决: 🚫 策略证伪，不建议实盘")
            self.verdict = "FAIL"
        print(f"{'='*60}")

        return self.verdict

    # ============================================================
    # 一键运行
    # ============================================================

    def run_full_validation(
        self,
        perturbation_ranges: Optional[Dict[str, List]] = None,
        analyze_failures: bool = True,
    ) -> str:
        """
        运行完整验证流程

        Args:
            perturbation_ranges: 参数扰动范围，如 {'rsi_thresh': [25,30,35,40,45]}
            analyze_failures: 是否分析失败案例
        """
        print(f"\n{'#'*60}")
        print(f"# 策略验证器运行中")
        print(f"# 指标: {self.metric}")
        print(f"# 训练: {self.train.index[0].date()} → {self.train.index[-1].date()} ({len(self.train)} bars)")
        print(f"# 验证: {self.val.index[0].date()} → {self.val.index[-1].date()} ({len(self.val)} bars)")
        print(f"# 测试: {self.test.index[0].date()} → {self.test.index[-1].date()} ({len(self.test)} bars)")
        print(f"{'#'*60}")

        # 阶段1: Walk-Forward
        self.run_walkforward()

        # 阶段2: 参数扰动
        if perturbation_ranges:
            self.run_perturbation(perturbation_ranges)

        # 阶段3: 失败案例
        if analyze_failures:
            self.analyze_failures(self.train, self.default_params)

        # 最终判决
        verdict = self.generate_report()

        return verdict


# ============================================================
# 辅助: 预置策略模板
# ============================================================

def make_rsi_strategy(direction='long'):
    """RSI均值回归策略工厂"""
    def strategy(df: pd.DataFrame, rsi_thresh: int = 35, **kwargs) -> Dict:
        if 'rsi' not in df.columns:
            return None
        sig = (df['rsi'].values < rsi_thresh).astype(int)
        return {'signal': sig, 'direction': direction}
    return strategy

def make_volatility_spring_strategy(direction='both'):
    """
    波动率弹簧策略
    当ATR收缩至20期均值的50%以下，且价格处于24小时区间下沿时触发
    """
    def strategy(df: pd.DataFrame,
                 atr_squeeze_thresh: float = 0.50,
                 lookback: int = 24,
                 **kwargs) -> Dict:

        if 'atr' not in df.columns or 'atr_ma' not in df.columns:
            return None

        close = df['close'].values
        atr = df['atr'].values
        atr_ma = df['atr_ma'].values

        # ATR收缩
        squeeze = atr < atr_ma * atr_squeeze_thresh

        # 24小时区间
        rolling_low = pd.Series(close).rolling(lookback).min().shift(1).values
        rolling_high = pd.Series(close).rolling(lookback).max().shift(1).values

        # 价格在区间下沿
        at_lower = close <= rolling_low * 1.01  # 1%容差

        # 价格在区间上沿
        at_upper = close >= rolling_high * 0.99

        if direction == 'long':
            sig = (squeeze & at_lower).astype(int)
        elif direction == 'short':
            sig = (squeeze & at_upper).astype(int)
        else:  # both - generate both signals
            sig_long = (squeeze & at_lower).astype(int)
            sig_short = (squeeze & at_upper).astype(int)
            # 合并: long和short信号合并
            sig = np.maximum(sig_long, sig_short)

        return {'signal': sig, 'direction': direction}
    return strategy

def make_volume_divergence_strategy(direction='long'):
    """
    量价背离策略
    下跌趋势中，价格创新低但成交量萎缩（<前一根的50%）
    """
    def strategy(df: pd.DataFrame,
                 vol_ratio_thresh: float = 0.50,
                 **kwargs) -> Dict:

        if 'volume' not in df.columns:
            return None

        close = df['close'].values
        volume = df['volume'].values

        # 价格创新低（过去3根K线）
        rolling_min = pd.Series(close).rolling(3).min().shift(1).values
        new_low = close < rolling_min

        # 成交量萎缩（<前一根的50%）
        prev_volume = np.roll(volume, 1)
        prev_volume[0] = volume[0]
        vol_shrinking = volume < prev_volume * vol_ratio_thresh

        sig = (new_low & vol_shrinking).astype(int)
        return {'signal': sig, 'direction': direction}
    return strategy


if __name__ == "__main__":
    print("strategy_validator.py 封装完成")
    print("\n使用示例:")
    print("""
    from kronos.strategy_validator import StrategyValidator, make_rsi_strategy

    # 加载数据
    df = pd.read_csv('/tmp/btc_1h_processed.csv', index_col=0, parse_dates=True)

    # 添加指标
    close = df['close'].astype(float)
    df['rsi'] = calc_rsi(close.values, 14)
    df['atr'] = calc_atr(df['high'].values, df['low'].values, close.values)
    df['atr_ma'] = pd.Series(df['atr'].values).rolling(20).mean().shift(1).values
    df['volume_ma'] = pd.Series(df['volume'].values).rolling(20).mean().shift(1).values

    # 创建验证器
    validator = StrategyValidator(
        strategy_fn=make_rsi_strategy('long'),
        df=df,
        default_params={'rsi_thresh': 35},
        metric='profit_factor',
    )

    # 运行验证
    verdict = validator.run_full_validation(
        perturbation_ranges={'rsi_thresh': [25, 30, 35, 40, 45]},
        analyze_failures=True,
    )
    """)
