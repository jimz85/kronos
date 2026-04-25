#!/usr/bin/env python3
import os

apiKey = os.getenv("OKX_API_KEY", "")
secretKey = os.getenv("OKX_SECRET", "")
passphrase = os.getenv("OKX_PASSPHRASE", "")
flag = "0"  # 0=实盘, 1=模拟盘

print(f"API Key: {apiKey[:8]}...")
print(f"Secret: {secretKey[:8]}...")
print(f"Passphrase: {passphrase[:4]}...")
print(f"Flag: {flag} (0=实盘, 1=模拟盘)")

try:
    from okx.api import Account
    api = Account(apiKey, secretKey, passphrase, flag)
    print("\n连接OKX实盘API...")
    
    # 获取账户余额
    result = api.get_balance()
    code = result.get("code", "N/A")
    msg = result.get("msg", "N/A")
    print(f"code: {code} msg: {msg}")
    
    if code == "0":
        data = result.get("data", {})
        total_eq = data.get("totalEq", "?")
        print(f"总资产(USDT): {total_eq}")
        details = data.get("details", [])
        print(f"币种数: {len(details)}")
        for d in details[:10]:
            ccy = d.get("ccy", "?")
            eq = d.get("eq", "?")
            avail = d.get("availEq", "?")
            print(f"  {ccy}: eq={eq} avail={avail}")
    else:
        print(f"错误: {msg}")
        
except Exception as e:
    import traceback
    print(f"错误: {e}")
    traceback.print_exc()
