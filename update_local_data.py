#!/usr/bin/env python3
"""
更新本地CSV数据文件
从OKX公开API拉取最新数据，追加到本地CSV

用法: python3 update_local_data.py [coin] [days]
  coin: 单个币种如DOGE，默认更新所有陈旧币种
  days: 拉取多少天数据，默认7天
"""
import requests
import pandas as pd
import os
from pathlib import Path
from datetime import datetime, timedelta
import time
import sys

DATA_DIR = Path('/Users/jimingzhang/Desktop/crypto_data_Pre5m')

# OKX公开k线接口（无需签名）
BASE_URL = 'https://www.okx.com/api/v5/market/history-candles'

def fetch_candles_since(coin, since_ts_ms, limit=300):
    """从OKX获取指定时间之后的数据（用于更新本地数据）"""
    instId = f'{coin}-USDT-SWAP'
    
    # 使用before参数获取since_ts之后的数据
    url = f'{BASE_URL}?instId={instId}&bar=5m&before={since_ts_ms}&limit={limit}'
    
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
        if data.get('code') != '0':
            print(f'  ❌ {coin}: API错误 {data.get("msg")}')
            return None
        
        candles = data.get('data', [])
        if not candles:
            print(f'  ⚠️ {coin}: 无新数据')
            return None
        
        # 过滤：只保留since_ts之后的数据
        candles = [c for c in candles if int(c[0]) > since_ts_ms]
        if not candles:
            print(f'  ⚠️ {coin}: 无超过指定时间的新数据')
            return None
        
        # data格式: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
        df = pd.DataFrame(candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm'])
        df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
        df['datetime_utc'] = df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
        df = df[['datetime_utc', 'open', 'high', 'low', 'close', 'volume']]
        
        for col in ['open', 'high', 'low', 'close', 'volume']:
            df[col] = pd.to_numeric(df[col])
        
        print(f'  ✅ {coin}: 获取{len(df)}条新数据 {df["datetime_utc"].min()} ~ {df["datetime_utc"].max()}')
        return df
        
    except Exception as e:
        print(f'  ❌ {coin}: 获取失败 {e}')
        return None

def fetch_candles(coin, days=7, limit=300):
    """从OKX获取K线数据，支持批量获取（默认获取最近N天）"""
    instId = f'{coin}-USDT-SWAP'
    after = int((datetime.now() - timedelta(days=days)).timestamp() * 1000)
    
    all_candles = []
    current_after = after
    
    for _ in range(20):  # 最多20批次 = 6000条 ≈ 20天
        url = f'{BASE_URL}?instId={instId}&bar=5m&after={current_after}&limit={limit}'
        
        try:
            resp = requests.get(url, timeout=15)
            data = resp.json()
            if data.get('code') != '0':
                print(f'  ❌ {coin}: API错误 {data.get("msg")}')
                return None
            
            candles = data.get('data', [])
            if not candles:
                break
            
            all_candles.extend(candles)
            
            # 如果返回数据少于limit，说明到头了
            if len(candles) < limit:
                break
            
            # 用最后一条的时间作为下一批的after（获取更早的数据）
            current_after = candles[-1][0]
            time.sleep(0.2)  # 避免请求过快
            
        except Exception as e:
            print(f'  ❌ {coin}: 获取失败 {e}')
            return None
    
    if not all_candles:
        print(f'  ⚠️ {coin}: 无数据')
        return None
    
    # data格式: [ts, open, high, low, close, vol, volCcy, volCcyQuote, confirm]
    df = pd.DataFrame(all_candles, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'volCcy', 'volCcyQuote', 'confirm'])
    df['timestamp'] = pd.to_datetime(df['timestamp'].astype(int), unit='ms')
    df['datetime_utc'] = df['timestamp'].dt.strftime('%Y-%m-%d %H:%M:%S')
    df = df[['datetime_utc', 'open', 'high', 'low', 'close', 'volume']]
    
    for col in ['open', 'high', 'low', 'close', 'volume']:
        df[col] = pd.to_numeric(df[col])
    
    print(f'  ✅ {coin}: 获取{len(df)}条数据 {df["datetime_utc"].min()} ~ {df["datetime_utc"].max()}')
    return df

def update_coin_data(coin, days=7):
    """更新单个币种的本地CSV"""
    csv_path = DATA_DIR / f'{coin}_USDT_5m_from_20180101.csv'
    
    if csv_path.exists():
        # 读取现有数据，找出最新一条的时间
        df_old = pd.read_csv(csv_path)
        
        if 'datetime_utc' in df_old.columns:
            latest_old_raw = df_old['datetime_utc'].max()
            latest_old = pd.to_datetime(latest_old_raw)
            if latest_old.tz:
                latest_old = latest_old.tz_localize(None)
            
            # 转换为毫秒时间戳
            latest_ts_ms = int(latest_old.timestamp() * 1000)
            
            # 用fetch_candles_since获取该时间之后的新数据
            df_new = fetch_candles_since(coin, latest_ts_ms)
            if df_new is None:
                return False
            
            # 只保留本地最新时间之后的数据
            df_new = df_new[df_new['datetime_utc'] > str(latest_old)]
            
            if len(df_new) == 0:
                print(f'  ⏭️ {coin}: 本地数据已最新，跳过')
                return True
            
            # 合并
            df_combined = pd.concat([df_old, df_new], ignore_index=True)
            df_combined.to_csv(csv_path, index=False)
            print(f'  ✅ {coin}: 追加{len(df_new)}条新数据，总计{len(df_combined)}行')
            
        elif 'timestamp' in df_old.columns:
            # 旧格式（毫秒时间戳），需要重建
            print(f'  ⚠️ {coin}: 旧文件格式(bug版本)，重新下载')
            df_new = fetch_candles(coin, days=7)  # 只获取7天
            if df_new is None:
                return False
            # 备份旧文件
            bak_path = csv_path.with_suffix('.csv.bak')
            os.rename(csv_path, bak_path)
            df_new.to_csv(csv_path, index=False)
            print(f'  ✅ {coin}: 重建完成，{len(df_new)}行')
        else:
            # 格式不同，备份并重新下载
            bak_path = csv_path.with_suffix('.csv.bak')
            os.rename(csv_path, bak_path)
            df_new = fetch_candles(coin, days=7)
            if df_new is None:
                return False
            df_new.to_csv(csv_path, index=False)
            print(f'  ⚠️ {coin}: 格式异常，备份到.bak，重新下载')
    else:
        # 文件不存在，直接下载
        df_new = fetch_candles(coin, days=days)
        if df_new is None:
            return False
        df_new.to_csv(csv_path, index=False)
        print(f'  🆕 {coin}: 创建新文件，{len(df_new)}行')
    
    return True

def check_data_age():
    """检查所有币种的数据年龄"""
    coins = ['BTC', 'ETH', 'AVAX', 'DOGE', 'SOL', 'DOT', 'ADA']
    stale = []
    
    for coin in coins:
        csv_path = DATA_DIR / f'{coin}_USDT_5m_from_20180101.csv'
        if not csv_path.exists():
            print(f'{coin}: 文件不存在')
            stale.append(coin)
            continue
        
        df = pd.read_csv(csv_path, nrows=5)
        if 'datetime_utc' in df.columns:
            # 新格式
            latest = pd.read_csv(csv_path, usecols=['datetime_utc'])['datetime_utc'].max()
        elif 'timestamp' in df.columns:
            # 可能是毫秒格式
            ts = pd.read_csv(csv_path, usecols=['timestamp'])['timestamp']
            if ts.max() > 1e12:
                latest = pd.to_datetime(ts.max(), unit='ms')
            else:
                continue
        else:
            continue
        
        latest_dt = pd.to_datetime(latest).tz_localize(None)
        age_days = (datetime.now() - latest_dt).days
        
        status = '✅' if age_days <= 3 else '⚠️'
        print(f'{status} {coin}: {age_days}天前 ({latest})')
        
        if age_days > 3:
            stale.append(coin)
    
    return stale

def main():
    coins = ['BTC', 'ETH', 'AVAX', 'DOGE', 'SOL', 'DOT', 'ADA']
    days = 7
    
    if len(sys.argv) > 1:
        if sys.argv[1] == '--check':
            print('=== 数据年龄检查 ===')
            check_data_age()
            return
        coins = [sys.argv[1]]
    if len(sys.argv) > 2:
        days = int(sys.argv[2])
    
    print(f'=== 更新数据 (最近{days}天) ===')
    stale = check_data_age()
    print()
    
    print('=== 开始更新 ===')
    for coin in stale:
        update_coin_data(coin, days)
        time.sleep(0.5)  # 避免请求过快
    
    print()
    print('=== 更新后检查 ===')
    check_data_age()

if __name__ == '__main__':
    main()
