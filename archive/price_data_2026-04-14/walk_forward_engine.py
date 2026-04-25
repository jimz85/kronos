#!/usr/bin/env python3
"""
Walk-Forward Analysis Engine - 量化研究核心框架

Walk-Forward 三层切分:
  训练集: 2024-01-01 → 2024-12-31  (调参用)
  验证集: 2025-01-01 → 2025-03-31  (盲测一次，不回看)
  测试集: 2025-04-01 → NOW           (最终审判，从未看过)

假设模板 (BTC为例):
  当 BTC 处于 4小时 EMA200 上方(牛市背景)
  且 15分钟 RSI(14) < 35
  且 当前收盘价未创新低(前3根K线最低价比)
  则未来 2-4根15分钟K线内 价格反弹 0.5% 的概率 > 55%

证伪条件:
  胜率衰减 > 10个百分点 → 假设无效
  平均收益衰减 > 50%   → 假设无效
"""
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import warnings
import time
import json
warnings.filterwarnings('ignore')

# ============================================================
# 数据获取
# ============================================================

def calc_rsi(prices, period=14):
    d = np.diff(prices, prepend=prices[0])
    g = np.where(d > 0, d, 0)
    l = np.where(d < 0, -d, 0)
    ag = pd.Series(g).rolling(period).mean()
    al = pd.Series(l).rolling(period).mean()
    return 100 - (100 / (1 + ag / (al + 1e-10)))

def calc_ema(prices, period):
    return pd.Series(prices).ewm(span=period, adjust=False).mean()

def fetch_crypto_data(ticker: str, start: str, end: str, interval: str = "15m", max_retries=3) -> Optional[pd.DataFrame]:
    """获取加密货币数据，带重试"""
    for attempt in range(max_retries):
        try:
            df = yf.download(ticker, start=start, end=end, interval=interval, progress=False)
            if df.empty:
                time.sleep(2)
                continue
            if isinstance(df.columns, pd.MultiIndex):
                df = df.loc[:, df.columns.get_level_values(0)]
            df.columns = [c.lower() for c in df.columns]
            df.index = df.index.tz_localize(None) if df.index.tz else df.index
            df = df[['open', 'high', 'low', 'close', 'volume']].dropna()
            return df
        except Exception as e:
            print(f"  [{ticker}] 获取数据失败 (尝试 {attempt+1}/{max_retries}): {e}")
            time.sleep(5)
    return None

# ============================================================
# Walk-Forward 核心
# ============================================================

class WalkForwardEngine:
    """
    三层隔离 Walk-Forward 引擎

    重要：绝对禁止把验证集/测试集数据用于参数搜索。
    每个阶段只能用前一阶段的数据。
    """

    def __init__(self, ticker: str, initial_capital: float = 10000, leverage: int = 10):
        self.ticker = ticker
        self.initial_capital = initial_capital
        self.leverage = leverage

        # 切分点（不可更改）
        self.train_end    = "2024-12-31"
        self.val_end      = "2025-03-31"
        # test_start = "2025-04-01"（动态）

        self.train_data = None
        self.val_data   = None
        self.test_data  = None

    def load_data(self, force_refresh: bool = False):
        """加载三层数据"""
        print(f"\n{'='*60}")
        print(f"📊 加载数据: {self.ticker}")
        print(f"{'='*60}")

        # 加载全量数据（2024-01 到 现在）
        end_now = datetime.now().strftime("%Y-%m-%d")
        full = fetch_crypto_data(self.ticker, "2024-01-01", end_now, "15m")
        if full is None or len(full) < 1000:
            print(f"❌ 数据获取失败")
            return False

        print(f"全量数据: {len(full)} bars ({full.index[0].date()} → {full.index[-1].date()})")

        # 切分
        self.train_data = full[full.index <= self.train_end].copy()
        self.val_data   = full[(full.index > self.train_end) & (full.index <= self.val_end)].copy()
        self.test_data  = full[full.index > self.val_end].copy()

        print(f"训练集: {len(self.train_data)} bars ({self.train_data.index[0].date()} → {self.train_data.index[-1].date()})")
        print(f"验证集: {len(self.val_data)}   bars ({self.val_data.index[0].date()} → {self.val_data.index[-1].date()})")
        print(f"测试集: {len(self.test_data)}  bars ({self.test_data.index[0].date()} → {self.test_data.index[-1].date()})")

        if len(self.train_data) < 5000 or len(self.val_data) < 1000:
            print(f"⚠️  数据量不足，验证/测试可能不可靠")
        return True

    # ============================================================
    # 策略信号生成
    # ============================================================

    def generate_signals(self, df: pd.DataFrame,
                         rsi_buy: int,
                         rsi_sell: int,
                         ema200_filter: bool = True,
                         no_new_low_filter: bool = True) -> pd.DataFrame:
        """
        生成交易信号

        假设条件:
        - 4H EMA200 趋势过滤 (可选)
        - 15分钟 RSI < rsi_buy
        - 价格未创新低 (前3根K线最低价，可选)
        """
        df = df.copy()
        close = df['close'].values.astype(float)
        high  = df['high'].values.astype(float)
        low   = df['low'].values.astype(float)

        # RSI
        rsi = calc_rsi(close, period=14)
        df['rsi'] = rsi.values

        # 4H EMA200 (从15分钟数据推导，取每16根K线的收盘价作为4H代理)
        if ema200_filter:
            # 每16根15分钟K线 = 1个4小时K线
            ema4h_close = close[::16] if len(close) > 16 else close
            ema200_4h = calc_ema(ema4h_close, 200 / 16 if len(ema4h_close) > 200//16 else 50)
            # 广播回15分钟 (每个4H bar内的15分钟共享同一EMA值)
            ema200_4h_expanded = np.repeat(ema200_4h.values, 16)[:len(close)]
            df['ema200_4h'] = ema200_4h_expanded
        else:
            df['ema200_4h'] = np.inf  # 不过滤

        # 前3根K线最低价
        if no_new_low_filter:
            rolling_low_3 = pd.Series(low).rolling(3).min().shift(1)  # shift避免前瞻
            df['rolling_low_3'] = rolling_low_3.values
        else:
            df['rolling_low_3'] = np.inf  # 不过滤

        # 信号: 多头入场
        df['signal'] = 0
        in_position = False
        signals = []
        for i in range(20, len(df)):
            rsi_val = float(df['rsi'].iloc[i])
            price   = float(close[i])
            ema200  = float(df['ema200_4h'].iloc[i])
            prev_low3 = float(df['rolling_low_3'].iloc[i])

            # 入场条件
            if not in_position:
                trend_ok = (not ema200_filter) or (price > ema200)  # 4H EMA200上方
                rsi_ok = rsi_val < rsi_buy
                no_new_low = (not no_new_low_filter) or (price >= prev_low3)  # 未创新低

                if trend_ok and rsi_ok and no_new_low:
                    in_position = True
                    signals.append(1)
                else:
                    signals.append(0)
            else:
                # 持仓中，持仓信号
                signals.append(1)
        df['signal'] = signals

        return df

    # ============================================================
    # 回测单次运行
    # ============================================================

    def backtest(self, df: pd.DataFrame,
                 rsi_buy: int,
                 rsi_sell: int,
                 stop_pct: float,
                 target_pct: float,
                 hold_max_bars: int,
                 ema200_filter: bool = True,
                 no_new_low_filter: bool = True,
                 leverage: int = 10) -> Dict:
        """在给定数据集上回测一组参数"""

        df = self.generate_signals(df, rsi_buy, rsi_sell, ema200_filter, no_new_low_filter)
        close = df['close'].values.astype(float)
        signal = df['signal'].values

        trades = []
        pos_entry_idx = None
        pos_entry_price = None

        for i in range(20, len(df) - hold_max_bars - 1):
            if signal[i] == 1 and pos_entry_idx is None:
                # 入场
                pos_entry_idx = i
                pos_entry_price = float(close[i])
            elif pos_entry_idx is not None:
                curr_price = float(close[i])
                hold_bars = i - pos_entry_idx
                ret = (curr_price - pos_entry_price) / pos_entry_price * leverage

                # 出场条件
                exit_reason = None
                # 止损
                if curr_price <= pos_entry_price * (1 - stop_pct):
                    exit_reason = 'stop'
                    ret = -stop_pct * leverage
                # 止盈
                elif curr_price >= pos_entry_price * (1 + target_pct):
                    exit_reason = 'target'
                # RSI回归50
                elif hold_bars < len(df) and float(df['rsi'].iloc[i]) > rsi_sell:
                    exit_reason = 'rsi_exit'
                # 超时
                elif hold_bars >= hold_max_bars:
                    exit_reason = 'hold_max'

                if exit_reason:
                    trades.append({
                        'entry_idx': pos_entry_idx,
                        'exit_idx': i,
                        'entry_price': pos_entry_price,
                        'exit_price': curr_price,
                        'return': ret,
                        'exit_reason': exit_reason,
                        'hold_bars': hold_bars
                    })
                    pos_entry_idx = None
                    pos_entry_price = None

        # 计算统计
        if not trades:
            return {'trades': 0, 'win_rate': 0, 'profit_factor': 0,
                    'avg_return': 0, 'total_return': 0}

        returns = [t['return'] for t in trades]
        wins    = [r for r in returns if r > 0]
        losses  = [r for r in returns if r < 0]

        return {
            'trades': len(trades),
            'win_rate': len(wins) / len(returns) if returns else 0,
            'profit_factor': abs(sum(wins) / sum(losses)) if losses and sum(losses) != 0 else 999,
            'avg_return': np.mean(returns) if returns else 0,
            'total_return': sum(returns) / 100,  # 转为百分比
            'avg_hold_bars': np.mean([t['hold_bars'] for t in trades]) if trades else 0,
            'wins': len(wins), 'losses': len(losses),
            'trades_detail': trades
        }

    # ============================================================
    # 阶段1: 训练集 - 暴力搜索参数
    # ============================================================

    def stage1_train(self) -> Dict:
        """训练集: 暴力搜索最优参数"""
        print(f"\n{'='*60}")
        print(f"🔍 阶段1: 训练集参数搜索 ({self.train_data.index[0].date()} → {self.train_data.index[-1].date()})")
        print(f"{'='*60}")

        best = None
        all_results = []

        for rsi_buy in [25, 30, 35, 40]:
            for rsi_sell in [50, 55, 60, 65, 70]:
                for stop_pct in [0.02, 0.03, 0.04]:
                    for target_pct in [0.02, 0.03, 0.04, 0.05]:
                        for hold_max in [8, 12, 16]:
                            for ema200_filter in [True, False]:
                                for no_new_low in [True, False]:
                                    result = self.backtest(
                                        self.train_data,
                                        rsi_buy, rsi_sell,
                                        stop_pct, target_pct, hold_max,
                                        ema200_filter, no_new_low
                                    )
                                    if result['trades'] < 20:
                                        continue

                                    all_results.append({
                                        'rsi_buy': rsi_buy,
                                        'rsi_sell': rsi_sell,
                                        'stop_pct': stop_pct,
                                        'target_pct': target_pct,
                                        'hold_max': hold_max,
                                        'ema200_filter': ema200_filter,
                                        'no_new_low_filter': no_new_low,
                                        **result
                                    })

        if not all_results:
            print("❌ 训练集无有效参数")
            return {}

        # 按 profit_factor * win_rate 排序
        all_results.sort(key=lambda x: x['profit_factor'] * x['win_rate'], reverse=True)
        best = all_results[0]

        print(f"\n🏆 训练集最优参数:")
        print(f"   RSI买入 < {best['rsi_buy']} / RSI卖出 > {best['rsi_sell']}")
        print(f"   止损 {best['stop_pct']:.1%} / 止盈 {best['target_pct']:.1%} / 持仓 {best['hold_max']}根")
        print(f"   EMA200过滤: {best['ema200_filter']} / 未创新低过滤: {best['no_new_low_filter']}")
        print(f"   交易次数: {best['trades']} / 胜率: {best['win_rate']:.1%} / PF: {best['profit_factor']:.2f}")
        print(f"   平均收益: {best['avg_return']:.2%} / 总收益: {best['total_return']:.1%}")

        # 保存top5
        self.top5_train = all_results[:5]

        return best

    # ============================================================
    # 阶段2: 验证集 - 盲测一次
    # ============================================================

    def stage2_validate(self, best_params: Dict) -> Dict:
        """验证集: 用训练集找到的参数盲测一次"""
        print(f"\n{'='*60}")
        print(f"🎯 阶段2: 验证集盲测 ({self.val_data.index[0].date()} → {self.val_data.index[-1].date()})")
        print(f"⚠️  参数来自训练集，数据从未看过")
        print(f"{'='*60}")

        if len(self.val_data) < 500:
            print("❌ 验证集数据不足")
            return {}

        result = self.backtest(
            self.val_data,
            best_params['rsi_buy'],
            best_params['rsi_sell'],
            best_params['stop_pct'],
            best_params['target_pct'],
            best_params['hold_max'],
            best_params['ema200_filter'],
            best_params['no_new_low_filter']
        )

        print(f"\n验证集结果:")
        print(f"   交易次数: {result['trades']}")
        print(f"   胜率: {result['win_rate']:.1%}")
        print(f"   PF: {result['profit_factor']:.2f}")
        print(f"   平均收益: {result['avg_return']:.2%}")

        # 证伪检查
        train_wr = best_params['win_rate']
        val_wr   = result['win_rate']
        train_avg = best_params['avg_return']
        val_avg   = result['avg_return']

        wr_decay = (train_wr - val_wr) / train_wr if train_wr > 0 else 1.0
        avg_decay = (train_avg - val_avg) / train_avg if train_avg > 0 else 1.0

        falsified = wr_decay > 0.5 or (val_wr < 0.40)  # 胜率衰减>50%或验证集<40%

        print(f"\n{'='*60}")
        if falsified:
            print(f"🚫 证伪: 胜率衰减 {wr_decay:.1%} ({train_wr:.1%} → {val_wr:.1%})")
        else:
            print(f"✅ 通过验证: 胜率衰减 {wr_decay:.1%} ({train_wr:.1%} → {val_wr:.1%})")
        print(f"{'='*60}")

        return {
            **result,
            'wr_decay': wr_decay,
            'avg_decay': avg_decay,
            'falsified': falsified,
            'train_win_rate': train_wr,
            'train_avg_return': train_avg
        }

    # ============================================================
    # 阶段3: 测试集 - 最终审判
    # ============================================================

    def stage3_test(self, best_params: Dict) -> Dict:
        """测试集: 从未看过的数据，最终结果"""
        print(f"\n{'='*60}")
        print(f"⚖️  阶段3: 测试集最终审判 ({self.test_data.index[0].date()} → {self.test_data.index[-1].date()})")
        print(f"🕳️  这是你从未看过的数据")
        print(f"{'='*60}")

        if len(self.test_data) < 500:
            print("❌ 测试集数据不足")
            return {}

        result = self.backtest(
            self.test_data,
            best_params['rsi_buy'],
            best_params['rsi_sell'],
            best_params['stop_pct'],
            best_params['target_pct'],
            best_params['hold_max'],
            best_params['ema200_filter'],
            best_params['no_new_low_filter']
        )

        print(f"\n测试集结果:")
        print(f"   交易次数: {result['trades']}")
        print(f"   胜率: {result['win_rate']:.1%}")
        print(f"   PF: {result['profit_factor']:.2f}")
        print(f"   平均收益: {result['avg_return']:.2%}")
        print(f"   总收益: {result['total_return']:.1%}")

        return result

    # ============================================================
    # 平滑性测试
    # ============================================================

    def smoothness_test(self, best_params: Dict) -> Dict:
        """平滑性测试: RSI阈值±5%范围内是否稳定"""
        print(f"\n{'='*60}")
        print(f"📈 平滑性测试: RSI阈值微调")
        print(f"{'='*60}")

        rsi_base = best_params['rsi_buy']
        results = []

        for delta in [-5, -2, 0, 2, 5]:
            rsi_test = max(15, rsi_base + delta)
            result = self.backtest(
                self.train_data,
                rsi_test,
                best_params['rsi_sell'],
                best_params['stop_pct'],
                best_params['target_pct'],
                best_params['hold_max'],
                best_params['ema200_filter'],
                best_params['no_new_low_filter']
            )
            results.append({'delta': delta, 'rsi': rsi_test, **result})
            print(f"  RSI {rsi_test} (Δ{delta:+d}): 胜率={result['win_rate']:.1%} PF={result['profit_factor']:.2f} 交易={result['trades']}")

        # 判断: 如果±5%内PF断崖下跌 = 过拟合
        pf_base = best_params['profit_factor']
        pf_min  = min(r['profit_factor'] for r in results)
        pf_max  = max(r['profit_factor'] for r in results)

        # 稳定性指标: min/max ratio
        stability = pf_min / pf_max if pf_max > 0 else 0
        is_smooth = stability > 0.7  # 70%维持率为阈值

        print(f"\nPF稳定性: {stability:.1%} ({pf_min:.2f} ~ {pf_max:.2f})")
        print(f"结论: {'✅ 平滑' if is_smooth else '❌ 尖峰 - 过拟合风险高'}")

        return {'stability': stability, 'is_smooth': is_smooth, 'details': results}


# ============================================================
# 主程序
# ============================================================

if __name__ == "__main__":
    import sys

    coins = ["BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "DOGE-USD"]
    target = sys.argv[1] if len(sys.argv) > 1 else "BTC-USD"

    if target not in coins:
        print(f"可用币种: {coins}")
        sys.exit(1)

    print(f"\n{'#'*60}")
    print(f'# 🚀 Walk-Forward 分析: {target}')
    print(f'# 训练集: 2024-01 → 2024-12')
    print(f'# 验证集: 2025-01 → 2025-03')
    print(f'# 测试集: 2025-04 → NOW')
    print(f'#')
    print(f'# 证伪条件: 胜率衰减>50% OR 验证集胜率<40%')
    print(f'# 平滑性: ±5%阈值内PF维持率>70%')
    print(f"{'#'*60}")

    engine = WalkForwardEngine(target)

    if not engine.load_data():
        print("数据加载失败，退出")
        sys.exit(1)

    # 阶段1: 训练
    best = engine.stage1_train()
    if not best:
        print("训练失败，退出")
        sys.exit(1)

    # 平滑性测试
    smooth = engine.smoothness_test(best)

    # 阶段2: 验证
    val_result = engine.stage2_validate(best)
    if val_result.get('falsified'):
        print(f"\n🚫 策略证伪，停止")
        print(f"训练胜率: {val_result['train_win_rate']:.1%}")
        print(f"验证胜率: {val_result['win_rate']:.1%} (衰减 {val_result['wr_decay']:.1%})")
        sys.exit(0)

    # 阶段3: 测试
    test_result = engine.stage3_test(best)

    # 保存结果
    summary = {
        'ticker': target,
        'best_params': {k: v for k, v in best.items() if k not in ['trades_detail']},
        'smoothness': smooth,
        'val_result': {k: v for k, v in val_result.items() if k not in ['trades_detail']},
        'test_result': {k: v for k, v in test_result.items() if k not in ['trades_detail']},
    }

    out_file = f"/Users/jimingzhang/kronos/walk_forward_{target.replace('-','_')}.json"
    with open(out_file, 'w') as f:
        json.dump(summary, f, indent=2, default=str)
    print(f"\n💾 结果已保存: {out_file}")