#!/usr/bin/env python3
"""
资金费率实时监控
- 每小时检查OKX合约资金费率
- 连续3周期超过阈值自动预警
- 记录到本地CSV积累历史数据
"""

import ccxt
import time
import json
import os
from datetime import datetime

# ============ 配置 ============
OKX_API_KEY = os.getenv("OKX_API_KEY", "")
OKX_API_SECRET = os.getenv("OKX_API_SECRET", "")
OKX_PASSPHRASE = os.getenv("OKX_PASSPHRASE", "")

MONITOR_SYMBOLS = [
    'AVAX/USDT:USDT',
    'SOL/USDT:USDT', 
    'ETH/USDT:USDT',
    'BTC/USDT:USDT',
    'DOGE/USDT:USDT',
    'DOT/USDT:USDT',
    'BNB/USDT:USDT',
]

FUNDING_THRESHOLD = 0.0005   # 0.05%/周期（8小时）预警线
SERIOUS_THRESHOLD = 0.001     # 0.1%/周期 确认信号
CONSECUTIVE = 3               # 连续3周期触发

LOG_FILE = os.path.join(os.path.dirname(__file__), 'funding_rate_log.csv')
STATE_FILE = os.path.join(os.path.dirname(__file__), 'funding_state.json')

# ============ 初始化交易所 ============
if OKX_API_KEY and OKX_API_SECRET:
    exchange = ccxt.okx({
        'apiKey': OKX_API_KEY,
        'secret': OKX_API_SECRET,
        'password': OKX_PASSPHRASE,
        'enableRateLimit': True,
    })
else:
    exchange = ccxt.okx({'enableRateLimit': True})

# ============ 状态加载 ============
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r') as f:
            return json.load(f)
    return {s: [] for s in MONITOR_SYMBOLS}

def save_state(state):
    with open(STATE_FILE, 'w') as f:
        json.dump(state, f)

# ============ 邮件/飞书预警 ============
def send_alert(subject, message):
    print(f"[ALERT] {subject}")
    print(f"  {message}")
    # 尝试飞书通知
    try:
        from hermes_tools import terminal
        token = os.getenv("FEISHU_BOT_TOKEN", "")
        if token:
            import urllib.request
            url = f"https://open.feishu.cn/open-apis/bot/v2/hook/{token}"
            payload = {
                "msg_type": "text",
                "content": {"text": f"💰资金费率预警\n\n{message}"}
            }
            req = urllib.request.Request(url, 
                data=json.dumps(payload).encode(),
                headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req, timeout=5)
    except Exception as e:
        print(f"  飞书通知失败: {e}")

# ============ 主监控循环 ============
def monitor():
    state = load_state()
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    print(f"[{now_str}] 资金费率监控启动，监控 {len(MONITOR_SYMBOLS)} 个币种")

    for symbol in MONITOR_SYMBOLS:
        try:
            funding = exchange.fetch_funding_rate(symbol)
            current_rate = funding['fundingRate']
            next_funding = funding.get('nextFundingTime', 'N/A')
            mark_price = funding.get('mark', 'N/A')
            
            # 记录历史
            state[symbol].append(current_rate)
            if len(state[symbol]) > CONSECUTIVE:
                state[symbol] = state[symbol][-CONSECUTIVE:]
            save_state(state)
            
            # 写日志
            log_line = f"{datetime.now().isoformat()},{symbol},{current_rate:.6f},{mark_price}\n"
            if not os.path.exists(LOG_FILE):
                with open(LOG_FILE, 'w') as f:
                    f.write("time,symbol,rate,mark_price\n")
            with open(LOG_FILE, 'a') as f:
                f.write(log_line)
            
            # 预警判断
            if len(state[symbol]) >= CONSECUTIVE:
                avg = sum(state[symbol]) / len(state[symbol])
                all_above = all(r > FUNDING_THRESHOLD for r in state[symbol])
                
                if all_above and avg > SERIOUS_THRESHOLD:
                    direction = "做空" if avg > 0 else "做多"
                    send_alert(
                        f"⚠️ 资金费率确认信号: {symbol}",
                        f"币种: {symbol}\n"
                        f"当前费率: {current_rate:.4%}\n"
                        f"近{CONSECUTIVE}周期均值: {avg:.4%}\n"
                        f"建议: {direction} 持有24h\n"
                        f"下次资金费: {next_funding}"
                    )
                elif all_above and avg > FUNDING_THRESHOLD:
                    print(f"  [观察] {symbol}: 连续{CONSECUTIVE}周期平均 {avg:.4%}")
            
            print(f"  {symbol}: {current_rate:.4%} (均值: {sum(state[symbol])/max(len(state[symbol]),1):.4%})")

        except Exception as e:
            print(f"  ❌ {symbol}: {e}")
        
        time.sleep(0.5)

    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] 本轮检查完成")

if __name__ == "__main__":
    monitor()
