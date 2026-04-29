#!/usr/bin/env python3
"""
exchange_usage_example.py - 交易所适配层使用示例
================================================

展示如何使用统一的交易所适配层在OKX和Binance之间切换。

注意: Binance testnet API 在某些地区可能不可用(返回451错误)

Version: 1.0.0
"""

import sys
from pathlib import Path

# 添加kronos根目录到路径
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from execution.exchange_adapter import (
    create_exchange_adapter,
    ExchangeType,
    get_default_okx_adapter,
    get_default_binance_adapter,
)


def example_basic_usage():
    """基本使用示例"""
    print("=" * 60)
    print("基本使用示例")
    print("=" * 60)

    # ── 方式1: 直接创建适配器 ─────────────────────────────────────
    okx = create_exchange_adapter(
        ExchangeType.OKX,
        api_key="your_api_key",
        secret_key="your_secret",
        passphrase="your_passphrase",
        testnet=True,  # 使用测试网
    )

    binance = create_exchange_adapter(
        ExchangeType.BINANCE,
        api_key="your_api_key",
        secret_key="your_secret",
        testnet=True,
        use_futures=False,  # 现货
    )

    print(f"OKX适配器: {okx.name}")
    print(f"Binance适配器: {binance.name}")

    # ── 方式2: 从环境变量创建 ─────────────────────────────────────
    # 需要在 ~/.hermes/.env 中设置:
    # OKX_API_KEY, OKX_SECRET_KEY, OKX_PASSPHRASE, OKX_FLAG
    # BINANCE_API_KEY, BINANCE_SECRET_KEY, BINANCE_TESTNET
    # okx = get_default_okx_adapter()
    # binance = get_default_binance_adapter()


def example_market_data(adapter, symbol: str):
    """行情数据获取示例"""
    print(f"\n{'='*60}")
    print(f"行情数据示例 - {adapter.name}")
    print("=" * 60)

    # 获取24小时行情
    ticker = adapter.get_ticker(symbol)
    if ticker:
        print(f"\n  {ticker.symbol}:")
        print(f"    最新价: ${ticker.last:,.2f}")
        print(f"    24h变化: {ticker.price_change_pct:+.2f}%")
        print(f"    24h高: ${ticker.high_24h:,.2f}")
        print(f"    24h低: ${ticker.low_24h:,.2f}")
        print(f"    24h成交量: {ticker.vol_24h:,.2f}")
    else:
        print(f"  行情获取失败")

    # 获取K线数据
    candles = adapter.get_candles(symbol, bar="1H", limit=10)
    if candles:
        print(f"\n  最近{len(candles)}根1小时K线:")
        for c in candles[-3:]:
            print(f"    {c.timestamp}: O={c.open:.2f} H={c.high:.2f} L={c.low:.2f} C={c.close:.2f}")
    else:
        print(f"  K线获取失败")

    # 获取订单簿
    ob = adapter.get_orderbook(symbol, limit=5)
    if ob:
        print(f"\n  订单簿 (Top 5):")
        print("    卖盘 (Asks):")
        for a in ob.asks[:3]:
            print(f"      ${a.price:.2f} x {a.quantity}")
        print("    买盘 (Bids):")
        for b in ob.bids[:3]:
            print(f"      ${b.price:.2f} x {b.quantity}")
    else:
        print("  订单簿获取失败")


def example_account_operations(adapter):
    """账户操作示例 (需要API密钥)"""
    print(f"\n{'='*60}")
    print(f"账户操作示例 - {adapter.name}")
    print("=" * 60)

    # 获取余额
    balance = adapter.get_balance()
    if balance:
        print(f"\n  账户余额:")
        print(f"    总权益: ${balance.total:,.2f}")
        print(f"    可用: ${balance.available:,.2f}")
        print(f"    锁定: ${balance.locked:,.2f}")
    else:
        print("  余额获取失败 (可能需要API密钥)")

    # 获取持仓
    positions = adapter.get_positions()
    print(f"\n  当前持仓 ({len(positions)}个):")
    for pos in positions:
        print(f"    {pos.symbol}: {pos.side} {pos.size} @ ${pos.avg_price:.4f}")
        print(f"      未实现盈亏: ${pos.unrealized_pnl:.2f}")
        if pos.liquidation_price:
            print(f"      强平价: ${pos.liquidation_price:.4f}")


def example_trading(adapter, symbol: str = "BTC-USDT"):
    """交易操作示例 (需要API密钥)"""
    print(f"\n{'='*60}")
    print(f"交易操作示例 - {adapter.name}")
    print("=" * 60)

    # 市价下单示例
    print(f"\n  市价下单示例 (不实际执行):")
    print(f"    adapter.place_order(")
    print(f"        symbol='{symbol}',")
    print(f"        side='buy',")
    print(f"        order_type='market',")
    print(f"        size=0.001")
    print(f"    )")

    # 限价下单示例
    print(f"\n  限价下单示例 (不实际执行):")
    print(f"    adapter.place_order(")
    print(f"        symbol='{symbol}',")
    print(f"        side='sell',")
    print(f"        order_type='limit',")
    print(f"        size=0.01,")
    print(f"        price=80000.0")
    print(f"    )")

    # 取消订单示例
    print(f"\n  取消订单示例:")
    print(f"    adapter.cancel_order('{symbol}', 'order_id_123')")


def example_switch_exchange():
    """交易所切换示例 - 同一套代码支持多交易所"""
    print("\n" + "=" * 60)
    print("交易所切换示例")
    print("=" * 60)

    # 模拟配置中指定交易所
    exchange_type = ExchangeType.OKX  # 或 ExchangeType.BINANCE

    # 创建适配器
    adapter = create_exchange_adapter(
        exchange_type,
        api_key="your_key",
        secret_key="your_secret",
        passphrase="your_passphrase" if exchange_type == ExchangeType.OKX else "",
        testnet=True,
    )

    print(f"\n  当前交易所: {adapter.name}")
    print(f"  交易所类型: {adapter.exchange_type.value}")

    # 统一调用 - 无需关心底层是哪个交易所
    ticker = adapter.get_ticker("BTC-USDT")
    if ticker:
        print(f"  BTC价格: ${ticker.last:.2f}")


def main():
    """主函数"""
    print("""
╔══════════════════════════════════════════════════════════════╗
║        OKX/Binance 双交易所适配层 - 使用示例                 ║
╚══════════════════════════════════════════════════════════════╝
""")

    # 基本信息
    example_basic_usage()

    # 创建OKX适配器测试
    print("\n" + "=" * 60)
    print("OKX 测试")
    print("=" * 60)

    okx = create_exchange_adapter(ExchangeType.OKX, testnet=True)

    # 行情数据
    example_market_data(okx, "BTC-USDT")

    # 账户操作
    example_account_operations(okx)

    # 交易操作
    example_trading(okx)

    # 交易所切换示例
    example_switch_exchange()

    print("\n" + "=" * 60)
    print("示例完成")
    print("=" * 60)
    print("""
使用提示:
  1. OKX测试网API通常可用
  2. Binance测试网API在某些地区可能被阻止(451错误)
  3. 需要设置正确的API密钥才能进行交易操作
  4. 公开行情数据不需要API密钥
""")


if __name__ == "__main__":
    main()