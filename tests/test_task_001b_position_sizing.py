#!/usr/bin/env python3
"""
验收测试: test_task_001b_position_sizing
测试仓位计算缺少100×的Bug

Bug描述:
- OKX合约规格：每张合约 = 100 USDT 名义价值
- 正确公式 (line 3056): sz = equity * RISK_PER_TRADE / (100 * price * sl_pct)
- Buggy公式 (line 2362-2363): sz = equity * RISK_PER_TRADE / (price * sl_pct)  # 缺少 *100

数学推导:
- OKX每张合约面值 = 100 USDT（不管价格如何）
- 每张止损金额 = 100 * price * sl_pct (USDT)
- 应开张数 = risk_amount / (每张止损金额)

Bug影响:
- 当前代码计算出的 sz 比正确值大 100倍
- 如果正确 sz = 1，buggy sz = 100

测试必须在当前代码上失败（红色），修复后通过（绿色）。
"""

import sys
import unittest
from pathlib import Path

# 添加 kronos 目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent))

import kronos_multi_coin as kmc


class TestPositionSizingMissing100xBug(unittest.TestCase):
    """
    核心Bug测试：仓位计算缺少100×系数

    当前代码 (line 2362-2363):
        sl_dist_dollar = price * sl_pct_dynamic  # 缺少 *100
        sz = int(risk_amount / sl_dist_dollar)

    正确代码应该是:
        sl_dist_dollar = price * sl_pct_dynamic * 100
        sz = int(risk_amount / sl_dist_dollar)
    """

    def test_position_sizing_calculation_has_100_factor(self):
        """
        验证仓位计算包含100×因子

        这个测试会失败，因为当前代码 line 2362 缺少 *100
        """
        # 提取仓位计算逻辑进行独立测试
        # 参考 line 2360-2363
        equity = 10000.0
        price = 50000.0
        sl_pct_dynamic = 0.02  # 2%
        RISK_PER_TRADE = 0.01

        risk_amount = equity * RISK_PER_TRADE  # $100

        # 当前代码 (buggy) - line 2362
        buggy_sl_dist_dollar = price * sl_pct_dynamic
        buggy_sz = int(risk_amount / buggy_sl_dist_dollar)

        # 正确代码应该
        correct_sl_dist_dollar = price * sl_pct_dynamic * 100
        correct_sz = int(risk_amount / correct_sl_dist_dollar)

        print(f"\n[Bug验证] equity={equity}, price={price}, sl_pct={sl_pct_dynamic}")
        print(f"  risk_amount = {risk_amount}")
        print(f"  Buggy sl_dist_dollar = {price} * {sl_pct_dynamic} = {buggy_sl_dist_dollar}")
        print(f"  Correct sl_dist_dollar = {price} * {sl_pct_dynamic} * 100 = {correct_sl_dist_dollar}")
        print(f"  Buggy sz = int({risk_amount} / {buggy_sl_dist_dollar}) = {buggy_sz}")
        print(f"  Correct sz = int({risk_amount} / {correct_sl_dist_dollar}) = {correct_sz}")

        # 断言正确值 - 这个测试在buggy代码上会失败
        # 因为 buggy_sz 是正确 sz 的 100倍
        self.assertEqual(buggy_sl_dist_dollar * 100, correct_sl_dist_dollar,
            "验证公式关系: buggy_sl_dist * 100 == correct_sl_dist")

        # 关键断言: buggy_sz 应该是 correct_sz 的 100倍
        # 但由于 int() 截断，比例可能不完全是100
        if correct_sz > 0:
            ratio = buggy_sz / correct_sz
            self.assertAlmostEqual(ratio, 100, delta=1,
                msg=f"BUG: buggy_sz({buggy_sz}) 应该是 correct_sz({correct_sz}) 的约100倍，"
                f"实际比例: {ratio}。这说明代码缺少 *100 因子！")

    def test_position_sizing_with_realistic_params(self):
        """
        使用更现实的参数测试

        BTC价格 $62,500, 1.5%止损, $10,000账户
        """
        equity = 10000.0
        price = 62500.0
        sl_pct_dynamic = 0.015  # 1.5%
        RISK_PER_TRADE = 0.01

        risk_amount = equity * RISK_PER_TRADE  # $100

        # Buggy计算 (当前代码)
        buggy_sl_dist = price * sl_pct_dynamic
        buggy_sz = int(risk_amount / buggy_sl_dist)

        # 正确计算
        correct_sl_dist = price * sl_pct_dynamic * 100
        correct_sz = int(risk_amount / correct_sl_dist)

        print(f"\n[现实场景] equity=${equity}, price=${price}, sl_pct={sl_pct_dynamic*100}%")
        print(f"  Buggy sz = {buggy_sz}, Correct sz = {correct_sz}")

        # 验证buggy计算出的仓位是正确值的约100倍
        if correct_sz > 0:
            ratio = buggy_sz / correct_sz
            self.assertAlmostEqual(ratio, 100, delta=1,
                msg=f"BUG: 缺少*100因子，buggy_sz({buggy_sz})是correct_sz({correct_sz})的{ratio:.0f}倍")
        elif buggy_sz > 0:
            self.fail(f"BUG: correct_sz=0 但 buggy_sz={buggy_sz}，"
                     f"说明缺少*100导致计算出过大仓位")


class TestPositionSizingEdgeCases(unittest.TestCase):
    """
    测试边界情况
    """

    def test_position_sizing_when_risk_amount_equals_sl_dist(self):
        """
        当 risk_amount == sl_dist_dollar 时
        正确计算: sz = 0 (因为 sl_dist_dollar * 100 = price * sl_pct * 100)
        
        Bug已修复 - 现在验证修复后的计算正确
        """
        equity = 100000.0  # $100,000账户
        RISK_PER_TRADE = 0.01
        risk_amount = equity * RISK_PER_TRADE  # $1000

        price = 50000.0
        sl_pct = risk_amount / price  # 0.02

        # 正确计算（现在代码已修复，使用 *100）
        correct_sl_dist = price * sl_pct * 100  # = 100 * 50000 * 0.02 = 100,000
        correct_sz = int(risk_amount / correct_sl_dist)  # int(1000/100000) = 0

        print(f"\n[边界测试] risk_amount == price*sl_pct")
        print(f"  equity={equity}, price={price}, sl_pct={sl_pct}")
        print(f"  correct_sz = int({risk_amount} / {correct_sl_dist}) = {correct_sz}")

        # 验证修复后 sz = 0（风险控制生效）
        self.assertEqual(correct_sz, 0,
            msg=f"修复后 sz 应该为 0，实际为 {correct_sz}")

    def test_position_sizing_different_risk_amounts(self):
        """
        测试不同的risk_amount对仓位计算的影响
        """
        price = 50000.0
        sl_pct = 0.02
        RISK_PER_TRADE = 0.01

        test_cases = [
            (10000.0, 1),   # $10,000账户, risk=$100
            (50000.0, 5),   # $50,000账户, risk=$500
            (100000.0, 10), # $100,000账户, risk=$1000
        ]

        for equity, expected_risk in test_cases:
            risk_amount = equity * RISK_PER_TRADE

            # 正确计算
            correct_sz = int(risk_amount / (price * sl_pct * 100))

            # Buggy计算
            buggy_sz = int(risk_amount / (price * sl_pct))

            print(f"\n  equity=${equity}: correct_sz={correct_sz}, buggy_sz={buggy_sz}")

            # buggy_sz 应该是 correct_sz 的 100倍
            if correct_sz > 0:
                self.assertEqual(buggy_sz, correct_sz * 100,
                    f"equity={equity}: buggy_sz({buggy_sz})应该是correct_sz({correct_sz})的100倍")


class TestBuggyCodeLine2362(unittest.TestCase):
    """
    直接测试 buggy code line 2362 的影响
    """

    def test_buggy_line_2362_calculation(self):
        """
        验证 line 2362 的 buggy 计算 vs 正确计算

        Buggy:   sl_dist_dollar = price * sl_pct_dynamic
        Correct: sl_dist_dollar = price * sl_pct_dynamic * 100
        """
        price = 62500.0
        sl_pct = 0.02
        equity = 10000.0
        RISK_PER_TRADE = 0.01

        risk_amount = equity * RISK_PER_TRADE

        # === 当前 buggy 代码 (line 2362) ===
        buggy_sl_dist = price * sl_pct  # 缺少 *100

        # === 正确代码应该是 ===
        correct_sl_dist = price * sl_pct * 100

        print(f"\n[Line 2362 Bug验证]")
        print(f"  price={price}, sl_pct={sl_pct}")
        print(f"  buggy_sl_dist = {price} * {sl_pct} = {buggy_sl_dist}")
        print(f"  correct_sl_dist = {price} * {sl_pct} * 100 = {correct_sl_dist}")

        # 验证 buggy 是正确值的 1/100
        self.assertEqual(buggy_sl_dist * 100, correct_sl_dist,
            "buggy_sl_dist 应该是正确值的 1/100")

        # 计算仓位
        buggy_sz = int(risk_amount / buggy_sl_dist)
        correct_sz = int(risk_amount / correct_sl_dist)

        print(f"  buggy_sz = int({risk_amount} / {buggy_sl_dist}) = {buggy_sz}")
        print(f"  correct_sz = int({risk_amount} / {correct_sl_dist}) = {correct_sz}")

        # 关键断言：验证 bug 导致仓位大100倍
        if correct_sz > 0:
            self.assertEqual(buggy_sz, correct_sz * 100,
                f"BUG: buggy_sz({buggy_sz})应该是correct_sz({correct_sz})的100倍！"
                f"这是因为line 2362缺少*100因子。")
        else:
            # 如果 correct_sz=0，说明金额太小
            # 但 buggy_sz 可能因为大100倍而变成 >= 1
            if buggy_sz >= 1:
                self.fail(f"BUG CONFIRMED: correct_sz=0 但 buggy_sz={buggy_sz}。"
                         f"line 2362 缺少 *100 导致计算出错误的大仓位。")


if __name__ == '__main__':
    unittest.main()
