#!/usr/bin/env python3
import os, hmac, hashlib, requests
from datetime import datetime

r = requests.get("https://www.okx.com/api/v5/public/time", timeout=5)
server_ts_ms = int(r.json()["data"][0]["ts"])

# OKX要求ISO 8601格式，精确到毫秒
ts_str = datetime.utcfromtimestamp(server_ts_ms / 1000).strftime("%Y-%m-%dT%H:%M:%S") + f".{server_ts_ms % 1000:03d}Z"
print(f"服务器时间: {ts_str}")

method = "GET"
path = "/api/v5/account/balance"

sign = hmac.new(
    os.getenv("OKX_SECRET","").encode(),
    (ts_str + method + path).encode(),
    hashlib.sha256
).hexdigest()

headers = {
    "OK-ACCESS-KEY": os.getenv("OKX_API_KEY",""),
    "OK-ACCESS-SIGN": sign,
    "OK-ACCESS-TIMESTAMP": ts_str,
    "OK-ACCESS-PASSPHRASE": os.getenv("OKX_PASSPHRASE",""),
    "Content-Type": "application/json",
}

resp = requests.get("https://www.okx.com" + path, headers=headers, timeout=10)
result = resp.json()
code = result.get("code", "N/A")
msg = result.get("msg", "N/A")
print(f"code: {code} msg: {msg}")
if code == "0":
    data = result.get("data", [{}])[0]
    total_eq = data.get("totalEq", "?")
    print(f"总资产(USDT): {total_eq}")
    for d in data.get("details", [])[:5]:
        ccy = d.get("ccy", "?")
        eq = d.get("eq", "?")
        avail = d.get("availEq", "?")
        print(f"  {ccy}: eq={eq} avail={avail}")
else:
    print(f"错误: {msg}")
