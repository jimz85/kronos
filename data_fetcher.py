"""
OKX K线数据补全工具
从OKX API拉取最新K线，追加到本地CSV
"""
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OKX_URL = 'https://www.okx.com'

def get_okx_kline(coin='BTC', bar='5m', after=None, limit=300):
    """从OKX获取K线数据"""
    endpoint = '/api/v5/market/history-candles'
    params = {'instId': f'{coin}-USDT', 'bar': bar, 'limit': limit}
    if after:
        params['after'] = after
    
    try:
        r = requests.get(f'{OKX_URL}{endpoint}', params=params, timeout=10)
        data = r.json()
        if data.get('code') != '0':
            return None
        return data.get('data', [])
    except Exception as e:
        print(f'OKX API失败: {e}')
        return None

def fetch_and_update(coin='BTC'):
    """获取最新数据并追加到CSV"""
    csv_path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    
    # 读取现有数据
    try:
        df = pd.read_csv(csv_path)
        df['timestamp'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
        df = df.set_index('timestamp').sort_index()
        last_ts = int(df.index[-1].timestamp() * 1000)
        print(f'{coin}: 本地最新 {df.index[-1]} ({last_ts})')
    except FileNotFoundError:
        df = None
        last_ts = int(datetime(2018, 1, 1, tzinfo=timezone.utc).timestamp() * 1000)
        print(f'{coin}: 无本地文件，从头开始')
    
    # 拉取最新数据
    all_candles = []
    fetch_count = 0
    current_after = None
    
    while fetch_count < 10:
        candles = get_okx_kline(coin, '5m', after=current_after)
        if not candles:
            break
        
        all_candles.extend(candles)
        current_after = candles[-1][0]
        fetch_count += 1
        
        if len(candles) < 300:
            break
        
        # 防止拉取太多
        if fetch_count >= 3:
            break
    
    if not all_candles:
        print(f'{coin}: 无新数据')
        return
    
    # 转换格式
    new_rows = []
    for c in all_candles:
        ts_ms = int(c[0])
        dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
        new_rows.append({
            'datetime_utc': dt.strftime('%Y-%m-%d %H:%M:%S'),
            'open': float(c[1]),
            'high': float(c[2]),
            'low': float(c[3]),
            'close': float(c[4]),
            'volume': float(c[5]),
        })
    
    new_df = pd.DataFrame(new_rows)
    new_df['timestamp'] = pd.to_datetime(new_df['datetime_utc']).dt.tz_localize(None)
    new_df = new_df.set_index('timestamp').sort_index()
    
    if df is not None:
        # 合并
        combined = pd.concat([df, new_df])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined = combined.sort_index()
        print(f'{coin}: 合并后 {len(combined)} 行, 最新 {combined.index[-1]}')
    else:
        combined = new_df
        print(f'{coin}: 新建 {len(combined)} 行')
    
    # 保存
    combined = combined.reset_index()
    combined = combined.rename(columns={'timestamp': 'datetime_utc'})[['datetime_utc','open','high','low','close','volume']]
    combined.to_csv(csv_path, index=False)
    print(f'{coin}: 已保存到 {csv_path}')

if __name__ == '__main__':
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else 'BTC'
    fetch_and_update(coin)
    if coin == 'BTC':
        fetch_and_update('BCH')
