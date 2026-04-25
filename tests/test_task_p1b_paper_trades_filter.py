#!/usr/bin/env python3
"""
验收测试: TASK-P1B - 验证rank_coins过滤paper_trades CLOSED状态

Bug描述:
- rank_coins（第1765行）只过滤两类币种：
  1. EXCLUDED_COINS（来自coin_strategy_map.json）
  2. 已有持仓的币种（来自positions）
- 但rank_coins从未读取paper_trades.json，导致CLOSED状态的币种不会被过滤
- 影响：同一币种可被反复选中开仓，不受历史平仓状态约束

期望行为:
- 当paper_trades.json中存在某币种的CLOSED记录时，rank_coins应排除该币种
- 修复后：rank_coins应读取paper_trades.json，排除(coin, direction)为CLOSED的币种

测试必须在当前代码上失败（红色），修复后通过（绿色）。
"""

import sys
import unittest
import json
import tempfile
from pathlib import Path
from unittest.mock import patch, MagicMock

# 添加 kronos 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import kronos_multi_coin as kmc


def create_mock_md(coin):
    """创建模拟市场数据，满足score_opportunity的基础要求"""
    return {
        'coin': coin,
        'price': 50000.0,
        'rsi_1h': 30.0,
        'rsi_4h': 35.0,
        'adx_1h': 25.0,
        'adx_4h': 20.0,
        'btc_direction': 'neutral',
        'btc_regime': 'neutral',
        'atr_pct': 2.5,
        'atr_percentile': 50.0,
        'ma30': 49000.0,
    }


class TestRankCoinsPaperTradesFilter(unittest.TestCase):
    """
    核心Bug测试：rank_coins不过滤paper_trades CLOSED状态

    当前Bug: rank_coins从未读取paper_trades.json，导致CLOSED状态的币种
    不会被排除，可以被反复选中开仓。

    期望修复: rank_coins应读取paper_trades.json，排除(coin, direction)
    为CLOSED的币种。
    """

    def setUp(self):
        """保存原始的paper_trades路径以便恢复"""
        self.original_paper_trades_path = Path.home() / '.hermes/cron/output/paper_trades.json'

    @patch('kronos_multi_coin.score_opportunity')
    @patch('kronos_multi_coin.get_coin_strategy_map')
    def test_rank_coins_filters_closed_paper_trades_long(self, mock_strategy_map, mock_score):
        """
        【核心Bug测试 - 主要测试】验证rank_coins过滤paper_trades CLOSED状态（做多方向）

        当前Bug: 即使paper_trades.json中有DOGE的CLOSED做多记录，
        rank_coins仍然会包含DOGE在结果中。

        修复后: rank_coins应读取paper_trades.json，排除CLOSED状态的币种。
        """
        # 创建临时目录结构，模拟 ~/.hermes/cron/output/
        self.temp_home = tempfile.mkdtemp()
        self.paper_trades_dir = Path(self.temp_home) / '.hermes' / 'cron' / 'output'
        self.paper_trades_dir.mkdir(parents=True)
        self.temp_paper_trades_path = self.paper_trades_dir / 'paper_trades.json'
        
        closed_trades = [
            {
                "id": "kronos_DOGE_long_1745524800000",
                "coin": "DOGE",
                "direction": "做多",
                "status": "CLOSED",
                "close_reason": "止损触发",
                "pnl": -100.0,
                "result_pct": -5.0
            },
            {
                "id": "kronos_BTC_short_1745524800000",
                "coin": "BTC",
                "direction": "做空",
                "status": "CLOSED",
                "close_reason": "止盈触发",
                "pnl": 50.0,
                "result_pct": 2.5
            }
        ]
        with open(self.temp_paper_trades_path, 'w') as f:
            json.dump(closed_trades, f)

        # Mock策略地图（返回空，排除列表为空）
        mock_strategy_map.return_value = {}

        # Mock score_opportunity返回高分，让DOGE和BTC都能进入排名
        mock_score.return_value = (80, '做多', '测试评分')

        with patch.object(Path, 'home', return_value=Path(self.temp_home)):
            # 创建包含DOGE和BTC的市场数据
            doge_md = create_mock_md('DOGE')
            btc_md = create_mock_md('BTC')
            eth_md = create_mock_md('ETH')  # ETH没有CLOSED记录，应该在结果中

            coin_data_list = [doge_md, btc_md, eth_md]
            positions = {}  # 空仓位

            result = kmc.rank_coins(coin_data_list, positions)

            # 获取结果中的币种列表
            result_coins = [item['coin'] for item in result]

            print(f"\n[Bug验证] rank_coins结果: {result_coins}")
            print(f"  paper_trades.json中有DOGE(long=CLOSED)和BTC(short=CLOSED)")
            print(f"  DOGE做多方向有CLOSED记录，应该被过滤")
            print(f"  BTC做空方向有CLOSED记录，但DOGE是long方向所以不应该被过滤")

            # 核心断言: DOGE有做多CLOSED记录，应该被过滤掉
            self.assertNotIn('DOGE', result_coins,
                f"Bug: DOGE有做多CLOSED记录，应该被rank_coins过滤，但仍在结果中: {result_coins}")

            # BTC做多方向没有CLOSED记录，应该在结果中
            self.assertIn('BTC', result_coins,
                f"BTC做多方向没有CLOSED记录，应该在结果中: {result_coins}")

            # ETH没有CLOSED记录，应该在结果中
            self.assertIn('ETH', result_coins,
                f"ETH没有CLOSED记录，应该在结果中: {result_coins}")

        # 清理临时文件
        import shutil
        shutil.rmtree(self.temp_home, ignore_errors=True)

    @patch('kronos_multi_coin.score_opportunity')
    @patch('kronos_multi_coin.get_coin_strategy_map')
    def test_rank_coins_filters_closed_paper_trades_short(self, mock_strategy_map, mock_score):
        """
        【核心Bug测试】验证rank_coins过滤paper_trades CLOSED状态（做空方向）

        当paper_trades.json中有某币种的CLOSED做空记录时，
        rank_coins不应在相同方向上重复选中该币种。
        """
        # 创建临时目录结构，模拟 ~/.hermes/cron/output/
        self.temp_home = tempfile.mkdtemp()
        self.paper_trades_dir = Path(self.temp_home) / '.hermes' / 'cron' / 'output'
        self.paper_trades_dir.mkdir(parents=True)
        self.temp_paper_trades_path = self.paper_trades_dir / 'paper_trades.json'
        
        closed_trades = [
            {
                "id": "kronos_ETH_short_1745524800000",
                "coin": "ETH",
                "direction": "做空",
                "status": "CLOSED",
                "close_reason": "止损触发",
                "pnl": -50.0,
                "result_pct": -2.5
            }
        ]
        with open(self.temp_paper_trades_path, 'w') as f:
            json.dump(closed_trades, f)

        mock_strategy_map.return_value = {}
        mock_score.return_value = (75, '做空', '测试评分做空')

        with patch.object(Path, 'home', return_value=Path(self.temp_home)):
            eth_md = create_mock_md('ETH')
            btc_md = create_mock_md('BTC')

            coin_data_list = [eth_md, btc_md]
            positions = {}

            result = kmc.rank_coins(coin_data_list, positions)
            result_coins = [item['coin'] for item in result]

            print(f"\n[Bug验证] rank_coins结果: {result_coins}")
            print(f"  paper_trades.json中有ETH(short=CLOSED)")

            # ETH做空方向有CLOSED记录，应该被过滤
            # 但注意：当前Bug是rank_coins不读取paper_trades，所以ETH仍会在结果中
            self.assertNotIn('ETH', result_coins,
                f"Bug: ETH做空CLOSED记录应被过滤，但ETH仍在结果中: {result_coins}")

            # BTC没有CLOSED记录，应该在结果中
            self.assertIn('BTC', result_coins,
                f"BTC没有CLOSED记录，应该在结果中: {result_coins}")

        import shutil
        shutil.rmtree(self.temp_home, ignore_errors=True)

    @patch('kronos_multi_coin.score_opportunity')
    @patch('kronos_multi_coin.get_coin_strategy_map')
    def test_rank_coins_with_no_paper_trades_file(self, mock_strategy_map, mock_score):
        """
        当paper_trades.json不存在时，rank_coins应该正常返回所有币种

        边界情况：paper_trades.json不存在时，不应影响rank_coins的正常功能
        """
        mock_strategy_map.return_value = {}
        mock_score.return_value = (70, '做多', '正常评分')

        fake_home = Path('/fake/home')

        with patch.object(Path, 'home', return_value=fake_home):
            eth_md = create_mock_md('ETH')
            btc_md = create_mock_md('BTC')

            coin_data_list = [eth_md, btc_md]
            positions = {}

            # 不应该抛出异常
            try:
                result = kmc.rank_coins(coin_data_list, positions)
                result_coins = [item['coin'] for item in result]

                # 两个币种都应该在结果中
                self.assertIn('ETH', result_coins)
                self.assertIn('BTC', result_coins)
                print(f"\n[边界测试] paper_trades不存在时正常返回: {result_coins}")
            except Exception as e:
                self.fail(f"rank_coins在paper_trades不存在时不应抛出异常: {e}")


class TestRankCoinsDoesNotReadPaperTrades(unittest.TestCase):
    """
    直接验证Bug存在：rank_coins不读取paper_trades.json

    这个测试类用于直接验证Bug的存在性
    """

    @patch('kronos_multi_coin.score_opportunity')
    @patch('kronos_multi_coin.get_coin_strategy_map')
    def test_rank_coins_reads_paper_trades_bug_verification(self, mock_strategy_map, mock_score):
        """
        【Bug验证】验证rank_coins读取paper_trades

        这个测试验证rank_coins确实读取paper_trades.json文件。
        修复后，SOL应该被过滤（因为有CLOSED记录）。
        """
        # 创建临时目录结构，模拟 ~/.hermes/cron/output/
        self.temp_home = tempfile.mkdtemp()
        self.paper_trades_dir = Path(self.temp_home) / '.hermes' / 'cron' / 'output'
        self.paper_trades_dir.mkdir(parents=True)
        self.temp_paper_trades_path = self.paper_trades_dir / 'paper_trades.json'
        
        closed_trades = [
            {
                "id": "kronos_SOL_long_1745524800000",
                "coin": "SOL",
                "direction": "做多",
                "status": "CLOSED",
                "close_reason": "手动平仓",
                "pnl": 100.0,
                "result_pct": 5.0
            }
        ]
        with open(self.temp_paper_trades_path, 'w') as f:
            json.dump(closed_trades, f)

        mock_strategy_map.return_value = {}
        mock_score.return_value = (85, '做多', '高分测试')

        with patch.object(Path, 'home', return_value=Path(self.temp_home)):
            sol_md = create_mock_md('SOL')

            coin_data_list = [sol_md]
            positions = {}

            result = kmc.rank_coins(coin_data_list, positions)
            result_coins = [item['coin'] for item in result]

            # 修复后：SOL有做多CLOSED记录，应该被过滤
            self.assertNotIn('SOL', result_coins,
                f"SOL有做多CLOSED记录，应该被rank_coins过滤: {result_coins}")

        import shutil
        shutil.rmtree(self.temp_home, ignore_errors=True)


if __name__ == '__main__':
    unittest.main()
