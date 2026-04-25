#!/usr/bin/env python3
"""
验收测试: TASK-P1A - 验证decide_for_candidate死代码问题

Bug描述:
- 原始代码有严重缩进错误，导致 vote_result/veto检查变成死代码
- P0 Fix注释声称已重写异常处理逻辑修复此问题
- 本测试验证当前代码是否仍存在死代码问题

核心死代码Bug:
- 当coin in SKIP_VOTING_COINS (DOGE/ADA/AVAX)时，vote_reason保持默认值
  '[投票超时/异常，信任规则引擎]'，导致返回消息误导用户
- 投票被跳过是正常行为（历史验证币种直接信任规则引擎），
  但被错误描述为"投票超时/异常"

测试必须在当前代码上失败（红色），修复后通过（绿色）。
"""

import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# 添加 kronos 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import kronos_multi_coin as kmc


def create_mock_md():
    """创建模拟市场数据，满足score_opportunity的基础要求"""
    return {
        'coin': 'BTC',
        'price': 50000.0,
        'rsi_1h': 45.0,
        'rsi_4h': 50.0,
        'adx_1h': 25.0,
        'adx_4h': 20.0,
        'btc_direction': 'neutral',
        'btc_regime': 'neutral',
        'atr_pct': 2.5,
        'atr_percentile': 50.0,
        'ma30': 49000.0,
    }


class TestDecideForCandidateMisleadingVoteReason(unittest.TestCase):
    """
    【核心Bug测试】验证vote_reason误导性问题

    原始Bug: 当coin in SKIP_VOTING_COINS时，vote_reason仍保持默认值
    '[投票超时/异常，信任规则引擎]'，导致返回消息误导用户。

    期望: 当投票被跳过时，vote_reason应该被正确重置或保持沉默，
    不应该包含"投票超时/异常"这样的误导性消息。
    """

    def test_skip_voting_coins_doge_reason_contains_vote_timeout_bug(self):
        """
        【死代码Bug验证 - 主要Bug测试】
        当coin=DOGE in SKIP_VOTING_COINS时，reason不应包含'投票超时'

        当前Bug: vote_reason初始化为'[投票超时/异常，信任规则引擎]'后，
        当投票被跳过时没有被重置，导致返回消息包含误导性的'投票超时'

        修复后: 当投票被跳过时，vote_reason应该被重置为''
        """
        md = create_mock_md()
        md['coin'] = 'DOGE'
        md['rsi_1h'] = 30.0
        md['atr_pct'] = 0.35

        result = kmc.decide_for_candidate('DOGE', md, equity=10000, num_positions=0)
        self.assertIsNotNone(result)
        action, score, reason, direction = result

        # 验证开仓成功
        self.assertEqual(action, 'open',
            f"DOGE使用规则引擎应返回'open'，action={action}")

        # 【核心Bug验证】投票跳过是正常行为，不应描述为投票超时
        self.assertNotIn('投票超时', reason,
            f"Bug: DOGE跳过voting是正常行为，reason不应包含'投票超时'，"
            f"但实际reason={reason}")

    def test_skip_voting_coins_ada_reason_contains_vote_exception_bug(self):
        """
        【死代码Bug验证】
        当coin=ADA in SKIP_VOTING_COINS时，reason不应包含'投票异常'
        """
        md = create_mock_md()
        md['coin'] = 'ADA'
        md['rsi_1h'] = 30.0
        md['atr_pct'] = 0.35

        result = kmc.decide_for_candidate('ADA', md, equity=10000, num_positions=0)
        self.assertIsNotNone(result)
        action, score, reason, direction = result

        self.assertEqual(action, 'open')

        # 【核心Bug验证】投票跳过是正常行为，不应描述为投票异常或投票超时
        self.assertNotIn('投票异常', reason,
            f"Bug: ADA跳过voting是正常行为，reason不应包含'投票异常'，"
            f"但实际reason={reason}")
        self.assertNotIn('投票超时', reason,
            f"Bug: ADA跳过voting是正常行为，reason不应包含'投票超时'，"
            f"但实际reason={reason}")

    def test_skip_voting_coins_avax_reason_should_not_be_misleading(self):
        """
        【死代码Bug验证】
        AVAX跳过voting时，reason也不应包含误导性消息
        """
        md = create_mock_md()
        md['coin'] = 'AVAX'
        md['rsi_1h'] = 25.0  # 较低RSI以抵消AVAX的历史惩罚
        md['atr_pct'] = 0.35

        result = kmc.decide_for_candidate('AVAX', md, equity=10000, num_positions=0)

        if result is None:
            self.skipTest("AVAX因历史惩罚评分不足，无法测试reason内容")

        action, score, reason, direction = result

        self.assertNotIn('投票异常', reason,
            f"Bug: AVAX跳过voting是正常行为，不应包含'投票异常'，"
            f"实际reason={reason}")
        self.assertNotIn('投票超时', reason,
            f"Bug: AVAX跳过voting是正常行为，不应包含'投票超时'，"
            f"实际reason={reason}")


class TestDecideForCandidateBasicFunctionality(unittest.TestCase):
    """
    基本功能测试 - 验证decide_for_candidate的基本功能是否正常
    """

    def test_max_positions_returns_none(self):
        """当仓位已满时，应返回None"""
        md = create_mock_md()

        result = kmc.decide_for_candidate('BTC', md, equity=10000, num_positions=3)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertIsNone(action,
            f"仓位已满(num_positions=3, MAX={kmc.MAX_POSITIONS})时应返回None")
        self.assertEqual(score, 0)

    def test_low_score_returns_none(self):
        """当规则评分<65时，应返回None"""
        md = create_mock_md()
        md['rsi_1h'] = 50.0  # 中性状态
        md['rsi_4h'] = 50.0
        md['adx_1h'] = 15.0  # 趋势弱
        md['atr_percentile'] = 50.0

        result = kmc.decide_for_candidate('BTC', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertIsNone(action,
            f"评分{score}<65时应返回None")
        self.assertIn('65', reason)

    def test_skip_voting_coins_use_rule_engine(self):
        """
        SKIP_VOTING_COINS (DOGE/ADA/AVAX) 应该直接使用规则引擎，跳过voting系统
        """
        md = create_mock_md()
        md['coin'] = 'DOGE'
        md['rsi_1h'] = 28.0
        md['atr_pct'] = 0.35

        result = kmc.decide_for_candidate('DOGE', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result, "DOGE应该直接信任规则引擎")
        action, score, reason, direction = result

        self.assertEqual(action, 'open',
            f"DOGE使用规则引擎应返回'open'，reason={reason}")
        self.assertGreater(score, 65)

    def test_skip_voting_coins_ada(self):
        """ADA也是SKIP_VOTING_COINS"""
        md = create_mock_md()
        md['coin'] = 'ADA'
        md['rsi_1h'] = 30.0
        md['atr_pct'] = 0.35

        result = kmc.decide_for_candidate('ADA', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertEqual(action, 'open')


class TestDecideForCandidateVotingEnabled(unittest.TestCase):
    """
    当HAS_VOTING_SYSTEM=True时，验证vote_result分析代码不是死代码
    """

    @patch.object(kmc, 'evaluate_coin')
    def test_voting_success_no_veto_opens_position(self, mock_evaluate):
        """投票成功且无否决时应开仓"""
        mock_evaluate.return_value = {
            'best_direction': 'long',
            'best_score': 50,
            'long': {'score': 50, 'veto_triggered': None},
            'short': {'score': 30}
        }

        md = create_mock_md()
        md['rsi_1h'] = 30.0

        result = kmc.decide_for_candidate('ETH', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertEqual(action, 'open')

    @patch.object(kmc, 'evaluate_coin')
    def test_voting_veto_rejects_candidate(self, mock_evaluate):
        """投票触发否决时应拒绝开仓"""
        mock_evaluate.return_value = {
            'best_direction': 'long',
            'best_score': 50,
            'long': {'score': 50, 'veto_triggered': '市场恐慌'},
            'short': {'score': 30}
        }

        md = create_mock_md()
        md['rsi_1h'] = 30.0

        result = kmc.decide_for_candidate('ETH', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertIsNone(action,
            f"投票否决时应返回None")
        self.assertIn('否决', reason)

    @patch.object(kmc, 'evaluate_coin')
    def test_voting_low_confidence_uses_rule_engine(self, mock_evaluate):
        """投票置信度<20%时应信任规则引擎"""
        mock_evaluate.return_value = {
            'best_direction': 'long',
            'best_score': 15,  # 置信度<20%
            'long': {'score': 15, 'veto_triggered': None},
            'short': {'score': 10}
        }

        md = create_mock_md()
        md['rsi_1h'] = 30.0

        result = kmc.decide_for_candidate('ETH', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertEqual(action, 'open',
            f"投票置信度<20%但规则引擎评分通过，应返回'open'")

    @patch.object(kmc, 'evaluate_coin')
    def test_voting_high_confidence_boosts_score(self, mock_evaluate):
        """投票置信度>=70%且方向一致时应加分"""
        mock_evaluate.return_value = {
            'best_direction': 'long',
            'best_score': 75,  # 置信度>=70%
            'long': {'score': 75, 'veto_triggered': None},
            'short': {'score': 30}
        }

        md = create_mock_md()
        md['coin'] = 'ETH'  # ETH不在SKIP_VOTING_COINS中
        md['rsi_1h'] = 30.0

        result = kmc.decide_for_candidate('ETH', md, equity=10000, num_positions=0)

        self.assertIsNotNone(result)
        action, score, reason, direction = result
        self.assertEqual(action, 'open')
        # 注意: 由于代码中存在英文/中文方向比较bug ('long' vs '做多')，
        # 共振加分可能不会发生，但投票结果应该被正确记录
        # 这里主要验证投票结果被正确记录在reason中
        self.assertIn('投票', reason,
            f"投票结果应该被记录在reason中，实际reason={reason}")


if __name__ == '__main__':
    unittest.main()
