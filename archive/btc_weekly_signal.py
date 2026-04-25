#!/usr/bin/env python3
"""
BTC 周线 EMA30 实盘信号系统
每周日 23:59 UTC 检查信号，通过飞书发送
"""
import yfinance as yf
import numpy as np
import pandas as pd
import json
import subprocess
from datetime import datetime

def get_btc_signal():
    """获取BTC周线EMA30信号"""
    # 获取数据
    btc = yf.Ticker("BTC-USD").history(
        start='2015-01-01', 
        end='2026-12-31', 
        interval='1wk'
    )
    btc.sort_index(inplace=True)
    btc.dropna(inplace=True)
    
    # 计算EMA30
    btc['EMA30'] = btc['Close'].ewm(span=30).mean()
    
    # 计算ATR(20)
    btc['prev_close'] = btc['Close'].shift(1)
    tr1 = btc['High'] - btc['Low']
    tr2 = (btc['High'] - btc['prev_close']).abs()
    tr3 = (btc['Low'] - btc['prev_close']).abs()
    btc['TR'] = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    btc['ATR20'] = btc['TR'].rolling(20).mean()
    btc['ATR_pct'] = btc['ATR20'] / btc['Close'] * 100
    
    # 最新bar
    last = btc.iloc[-1]
    prev = btc.iloc[-2]
    
    # 交叉判断
    cross_up = prev['Close'] <= prev['EMA30'] and last['Close'] > last['EMA30']
    cross_down = prev['Close'] >= prev['EMA30'] and last['Close'] < last['EMA30']
    
    # 状态
    in_trend = last['Close'] > last['EMA30']
    ema_state = "🟢 持仓中" if in_trend else "🔴 空仓中"
    
    if cross_up:
        signal = "📈 **买入信号**（周线收盘上穿EMA30）"
        action = "买入"
    elif cross_down:
        signal = "📉 **卖出信号**（周线收盘下穿EMA30）"
        action = "卖出"
    else:
        signal = "⏸️ 无操作"
        action = "持有"
    
    # ATR仓位计算
    account = 10000.0
    risk_pct = 0.02
    risk_amount = account * risk_pct  # $200
    atr_pct = last['ATR_pct'] if pd.notna(last['ATR_pct']) else 3.0
    position_value = risk_amount / (atr_pct / 100)  # $200 / 10.6% = $1,887
    position_btc = position_value / last['Close']
    
    return {
        'date': last.name.strftime('%Y-%m-%d'),
        'close': last['Close'],
        'ema30': last['EMA30'],
        'atr': last['ATR20'],
        'atr_pct': atr_pct,
        'ema_state': ema_state,
        'signal': signal,
        'action': action,
        'cross_up': cross_up,
        'cross_down': cross_down,
        'position_value': position_value,
        'position_btc': position_btc,
        'position_pct': position_value / account * 100,
        'risk_amount': account * risk_pct
    }

def format_signal(s, risk_amount):
    """格式化飞书消息"""
    emoji = "🟢" if "持仓" in s['ema_state'] else "🔴"
    
    # 趋势强度
    if s['cross_up']:
        trend = "📈 刚刚上穿EMA30"
    elif s['cross_down']:
        trend = "📉 刚刚下穿EMA30"
    else:
        pct_above = (s['close'] / s['ema30'] - 1) * 100
        if pct_above > 5:
            trend = f"稳定在EMA30上方 {pct_above:.1f}%"
        elif pct_above < -5:
            trend = f"在EMA30下方 {abs(pct_above):.1f}%"
        else:
            trend = f"接近EMA30 ({pct_above:+.1f}%)"
    
    msg = f"""📊 **BTC 周线 EMA30 信号**
{'='*36}

📅 更新时间: {s['date']}

💰 **价格数据**
  当前收盘: ${s['close']:,.0f}
  EMA30: ${s['ema30']:,.0f}
  趋势: {trend}

📍 **当前状态**
  {s['ema_state']}
  信号: {s['signal']}

📐 **仓位管理** ($10,000示例账户)
  单笔风险: ${risk_amount:,.0f} (2%)
  ATR(20): ${s['atr']:,.0f} ({s['atr_pct']:.1f}%)
  仓位价值: ${s['position_value']:,.0f} ({s['position_pct']:.1f}%仓位)
  {'→ 可买入: ' + f'{s["position_btc"]:.4f} BTC' if s['cross_up'] or (s['close'] > s['ema30'] and not s['cross_down']) else ''}

{'⚠️ 操作建议: ' + s['action'] if s['cross_up'] or s['cross_down'] else ''}

{'='*36}
⏰ 下次检查: 下周日 23:59 UTC"""
    
    return msg

def send_feishu(message):
    """通过飞书发送消息"""
    try:
        # 使用飞书IM API发送消息
        import os
        app_id = os.environ.get('FEISHU_APP_ID', 'cli_a93c11b6bbf9dcc0')
        app_secret = os.environ.get('FEISHU_APP_SECRET', '')
        
        # 获取tenant_access_token
        token_url = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
        token_data = {"app_id": app_id, "app_secret": app_secret}
        token_r = requests.post(token_url, json=token_data, timeout=10)
        token_r.raise_for_status()
        token = token_r.json().get('tenant_access_token', '')
        
        if not token:
            print("Failed to get token")
            return False
        
        # 发送消息
        send_url = "https://open.feishu.cn/open-apis/im/v1/messages"
        headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
        
        # 发送到Home channel
        receive_id = "oc_bfd8a7cc1a606f190b53e3fd0167f5a0"
        
        payload = {
            "receive_id": receive_id,
            "msg_type": "text",
            "content": json.dumps({"text": message})
        }
        
        params = {"receive_id_type": "chat_id"}
        resp = requests.post(send_url, headers=headers, params=params, json=payload, timeout=10)
        
        if resp.status_code == 200:
            print("✅ 飞书消息发送成功")
            return True
        else:
            print(f"❌ 飞书发送失败: {resp.status_code} | {resp.text}")
            return False
    except Exception as e:
        print(f"❌ 发送失败: {e}")
        return False

if __name__ == "__main__":
    import requests
    
    print("🔍 获取BTC信号...")
    s = get_btc_signal()
    msg = format_signal(s, s['risk_amount'])
    
    print(msg)
    print("\n" + "="*50)
    
    # 总是发送（让用户知道系统在运行）
    # 只有信号变化时才建议操作
    send_feishu(msg)
