#!/usr/bin/env python3
"""最小化OKX模拟交易验证脚本"""
import sys
sys.path.insert(0, '/Users/jimingzhang/kronos')

from okx_trading_engine import API_KEY, SECRET_KEY, PASSPHRASE, SIMULATED, api

print("=" * 50)
print("OKX 模拟交易验证")
print("=" * 50)
print(f"API_KEY:  {API_KEY[:10]}...")
print(f"SECRET:   {SECRET_KEY[:8]}...")
print(f"PASSPHRASE: {PASSPHRASE}")
print(f"SIMULATED: {SIMULATED} ({'模拟模式' if SIMULATED == '1' else '实盘模式'})")
print()

# 测试1: 获取账户信息
print("测试1: 获取账户信息...")
result = api('GET', '/api/v5/account/balance')
print(f"  code: {result.get('code')}")
print(f"  msg: {result.get('msg')}")
if result.get('code') == '0':
    print("  ✅ API连接成功")
else:
    print(f"  ❌ API错误: {result}")

# 测试2: 获取BTC当前价格
print("\n测试2: 获取BTC价格...")
ticker = api('GET', '/api/v5/market/ticker?instId=BTC-USDT')
if ticker.get('data'):
    price = ticker['data'][0]['last']
    print(f"  BTC-USDT 当前价格: ${price}")
    print("  ✅ 市场数据获取成功")
else:
    print(f"  ❌ 获取失败: {ticker}")

print("\n" + "=" * 50)
print("验证步骤:")
print("1. 登录 OKX官网 → 交易 → 模拟交易")
print("2. 查看是否有订单记录")
print("3. 确认实盘账户无任何订单")
print("=" * 50)
