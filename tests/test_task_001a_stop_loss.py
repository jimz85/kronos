#!/usr/bin/env python3
"""
验收测试: test_task_001a_stop_loss
测试幂等跳过返回字符串的Bug

Bug描述:
- place_oco / place_sl / place_tp 幂等跳过时返回字符串消息（如 "⏭️ 已存在活跃订单(oco 12345678)，跳过"）
- 成功时也返回字符串（algoId）
- 这导致调用方无法区分"挂单成功"和"幂等跳过"

期望行为:
- 幂等跳过应该返回 None（或抛出异常），让调用方知道没做任何操作
- 成功时返回 str (algoId)

测试必须在当前代码上失败（红色），修复后通过（绿色）。
"""

import sys
import unittest
from unittest.mock import patch, MagicMock
from pathlib import Path

# 添加 kronos 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import kronos_multi_coin as kmc


def create_mock_req_with_pending(pending_response, post_response):
    """
    创建一个模拟 _req 函数的闭包
    pending_response: GET /orders-algo-pending 的返回值
    post_response: POST /order-algo 的返回值
    """
    call_count = [0]

    def mock_req(method, path, body=''):
        call_count[0] += 1
        if method == 'GET':
            return pending_response
        elif method == 'POST':
            return post_response
        return {}

    return mock_req


class TestIdempotentSkipReturnsStringBug(unittest.TestCase):
    """
    核心Bug测试：幂等跳过返回字符串，无法与成功区分

    当前代码行为:
    - 幂等跳过时返回: "⏭️ 已存在活跃订单(oco 12345678)，跳过"
    - 成功时返回: "12345678" (algoId)

    调用方无法区分这两种情况！
    """

    @patch.object(kmc, '_req')
    def test_place_oco_idempotent_skip_returns_string_is_bug(self, mock_req):
        """幂等跳过返回字符串是Bug - 调用方无法区分成功和跳过"""
        # 模拟已有pending订单（幂等检查会跳过）
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={
                'code': '0',
                'data': [{'algoId': '12345678', 'slTriggerPx': '50000', 'tpTriggerPx': '60000', 'sz': '10'}]
            },
            post_response={'code': '0', 'data': [{'algoId': '99999999'}]}
        )

        result = kmc.place_oco('BTC-USDT-SWAP', 'long', 10, 50000, 60000)

        # Bug: 幂等跳过时返回字符串，无法与成功区分
        # 期望: result 应该是 None（幂等跳过表示没做任何操作）
        self.assertIsNone(result,
            "BUG: 幂等跳过返回字符串 '⏭️ ...' 而非 None，"
            "调用方无法区分 '挂单成功(返回algoId)' 和 '幂等跳过(返回skip消息)'。"
            "应该返回 None 让调用方知道本次没有执行任何操作。")

    @patch.object(kmc, '_req')
    def test_place_sl_idempotent_skip_returns_string_is_bug(self, mock_req):
        """幂等跳过返回字符串是Bug"""
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={
                'code': '0',
                'data': [{'algoId': '87654321', 'slTriggerPx': '2000', 'sz': '5'}]
            },
            post_response={'code': '0', 'data': [{'algoId': '99999999'}]}
        )

        result = kmc.place_sl('ETH-USDT-SWAP', 'long', 5, 2000)

        self.assertIsNone(result,
            "BUG: 幂等跳过返回字符串 '⏭️ ...' 而非 None，"
            "调用方无法区分成功和跳过。")

    @patch.object(kmc, '_req')
    def test_place_tp_idempotent_skip_returns_string_is_bug(self, mock_req):
        """幂等跳过返回字符串是Bug"""
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={
                'code': '0',
                'data': [{'algoId': '11223344', 'tpTriggerPx': '150', 'sz': '20'}]
            },
            post_response={'code': '0', 'data': [{'algoId': '99999999'}]}
        )

        result = kmc.place_tp('SOL-USDT-SWAP', 'long', 20, 150)

        self.assertIsNone(result,
            "BUG: 幂等跳过返回字符串 '⏭️ ...' 而非 None，"
            "调用方无法区分成功和跳过。")


class TestSuccessReturnsAlgoId(unittest.TestCase):
    """成功时应该返回 algoId 字符串"""

    @patch.object(kmc, '_req')
    def test_place_oco_success_returns_algo_id(self, mock_req):
        """成功时应返回 algoId 字符串"""
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={'code': '0', 'data': []},  # 没有pending订单
            post_response={'code': '0', 'data': [{'algoId': '12345678'}]}
        )

        result = kmc.place_oco('BTC-USDT-SWAP', 'long', 10, 50000, 60000)

        self.assertIsInstance(result, str, "成功时应返回 str 类型")
        self.assertEqual(result, '12345678')

    @patch.object(kmc, '_req')
    def test_place_sl_success_returns_algo_id(self, mock_req):
        """成功时应返回 algoId 字符串"""
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={'code': '0', 'data': []},
            post_response={'code': '0', 'data': [{'algoId': '87654321'}]}
        )

        result = kmc.place_sl('ETH-USDT-SWAP', 'long', 5, 2000)

        self.assertIsInstance(result, str)
        self.assertEqual(result, '87654321')

    @patch.object(kmc, '_req')
    def test_place_tp_success_returns_algo_id(self, mock_req):
        """成功时应返回 algoId 字符串"""
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={'code': '0', 'data': []},
            post_response={'code': '0', 'data': [{'algoId': '11223344'}]}
        )

        result = kmc.place_tp('SOL-USDT-SWAP', 'long', 20, 150)

        self.assertIsInstance(result, str)
        self.assertEqual(result, '11223344')


class TestApiFailureReturnsNone(unittest.TestCase):
    """API失败时当前代码返回 None（这也是问题，但优先级低于幂等跳过Bug）"""

    @patch.object(kmc, '_req')
    def test_place_oco_api_failure_returns_none(self, mock_req):
        """API失败时返回 None - 无法让调用方知道失败了"""
        mock_req.side_effect = create_mock_req_with_pending(
            pending_response={'code': '0', 'data': []},
            post_response={'code': '50001', 'msg': 'Internal error', 'data': []}
        )

        result = kmc.place_oco('BTC-USDT-SWAP', 'long', 10, 50000, 60000)

        # 当前行为：API失败时返回 None
        # 这个测试记录当前行为，不应失败
        self.assertIsNone(result)


if __name__ == '__main__':
    unittest.main()
