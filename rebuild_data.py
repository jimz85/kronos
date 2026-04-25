"""
重建CSV结构 + 更新最新数据
"""
import requests
import pandas as pd
import numpy as np
from datetime import datetime, timezone

DATA_DIR = '/Users/jimingzhang/Desktop/crypto_data_Pre5m'
OKX_URL = 'https://www.okx.com'

def get_okx_kline(coin='BTC', bar='5m', after=None, limit=300):
    endpoint = '/api/v5/market/history-candles'
    params = {'instId': f'{coin}-USDT', 'bar': bar, 'limit': limit}
    if after:
        params['after'] = after
    try:
        r = requests.get(f'{OKX_URL}{endpoint}', params=params, timeout=10)
        data = r.json()
        if data.get('code') != '0':
            print(f"OKX错误: {data.get('msg')}")
            return None
        return data.get('data', [])
    except Exception as e:
        print(f'OKX API失败: {e}')
        return None

def rebuild_csv(coin):
    """重建干净的CSV"""
    csv_path = f'{DATA_DIR}/{coin}_USDT_5m_from_20180101.csv'
    
    # 读取现有数据
    df = pd.read_csv(csv_path)
    
    # 检查列名
    print(f"原始列名: {df.columns.tolist()}")
    
    # 如果有重复列，只保留第一个datetime_utc
    cols = df.columns.tolist()
    new_cols = []
    seen = set()
    for c in cols:
        cn = c.split('.')[0]  # 去掉 .1 后缀
        if cn not in seen:
            new_cols.append(c)
            seen.add(cn)
        else:
            print(f"  跳过重复列: {c}")
    df = df[new_cols]
    
    # 确保有正确列
    required = ['datetime_utc', 'open', 'high', 'low', 'close', 'volume']
    for col in required:
        if col not in df.columns:
            print(f"  缺少列 {col}")
            return False
    
    # 转换时间
    df['timestamp'] = pd.to_datetime(df['datetime_utc']).dt.tz_localize(None)
    df = df.set_index('timestamp').sort_index()
    
    # 过滤无效行
    df = df[df['close'] > 0]
    
    # 只保留需要的列
    df = df[['open', 'high', 'low', 'close', 'volume']]
    
    # 拉取最新数据补充
    last_ts = int(df.index[-1].timestamp() * 1000)
    print(f"{coin}: 本地最新 {df.index[-1]} ({last_ts})")
    
    all_candles = []
    fetch_count = 0
    current_after = None
    
    while fetch_count < 5:
        candles = get_okx_kline(coin, '5m', after=current_after)
        if not candles:
            break
        all_candles.extend(candles)
        current_after = candles[-1][0]
        fetch_count += 1
        if len(candles) < 300:
            break
    
    if all_candles:
        new_rows = []
        for c in all_candles:
            ts_ms = int(c[0])
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
            new_rows.append({
                'timestamp': dt,
                'open': float(c[1]),
                'high': float(c[2]),
                'low': float(c[3]),
                'close': float(c[4]),
                'volume': float(c[5]),
            })
        
        new_df = pd.DataFrame(new_rows).set_index('timestamp').sort_index()
        
        # 合并去重
        combined = pd.concat([df, new_df])
        combined = combined[~combined.index.duplicated(keep='last')]
        combined = combined.sort_index()
        
        print(f"{coin}: 合并后 {len(combined)} 行, 最新 {combined.index[-1]}")
    else:
        combined = df
        print(f"{coin}: 无新数据, 当前 {len(combined)} 行")
    
    # 重建CSV格式
    combined = combined.reset_index()
    combined = combined.rename(columns={'timestamp': 'datetime_utc'})
    combined.to_csv(csv_path, index=False)
    print(f"{coin}: 已保存, 列名: {combined.columns.tolist()}")
    
    # 验证
    verify = pd.read_csv(csv_path)
    print(f"{coin}: 验证读取, 列名: {verify.columns.tolist()}, 行数: {len(verify)}")
    return True

if __name__ == '__main__':
    import sys
    coin = sys.argv[1] if len(sys.argv) > 1 else 'BTC'
    rebuild_csv(coin)
    if coin == 'BTC':
        rebuild_csv('BCH')
