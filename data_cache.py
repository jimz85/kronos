#!/usr/bin/env python3
"""
Kronos Data Cache Layer
========================
本地K线数据缓存层：
- 多币种并行预获取
- 智能过期策略（K线周期越短过期越快）
- 自动增量更新（只拉新数据）
- 数据完整性验证

运行：
  python3 data_cache.py              # 刷新所有币种缓存
  python3 data_cache.py --coin AVAX  # 单币种
  python3 data_cache.py --watch      # 持续监控模式
"""

import os, json, time, math
import numpy as np
import pandas as pd
import requests
from pathlib import Path
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Literal, Optional

# ========== 配置 ==========
CACHE_DIR = Path.home() / '.hermes/cron/output'
CACHE_DIR.mkdir(parents=True, exist_ok=True)

# 币种列表
COINS = ['AVAX', 'ETH', 'BTC', 'SOL', 'DOGE', 'ADA', 'DOT', 'LINK', 'BNB', 'XRP']
# COINS = ['AVAX']  # 调试用

# 缓存过期策略（秒）
EXPIRY = {
    '1m':   60,        # 1分钟K线：1分钟后过期
    '5m':   300,       # 5分钟K线：5分钟后过期
    '15m':  900,       # 15分钟K线：15分钟后过期
    '1H':   1800,      # 1小时K线：30分钟后过期
    '4H':   7200,      # 4小时K线：2小时后过期
    '1D':   86400,     # 1天K线：1天后过期
}

# 数据保留策略（条数上限，0=不限）
MAX_CANDLES = {
    '1m':   10080,     # 7天
    '5m':   4032,      # 14天
    '15m':  2688,      # 28天
    '1H':   2160,      # 90天
    '4H':   1440,      # 240天
    '1D':   730,       # 2年
}

# ========== 数据类型定义 ==========

@dataclass
class CacheEntry:
    """缓存条目"""
    coin: str
    bar: str
    file_path: str
    rows: int = 0
    start_ts: int = 0
    end_ts: int = 0
    last_update: float = 0  # Unix timestamp
    is_valid: bool = False
    error: str = ''

@dataclass
class DataCache:
    """数据缓存管理器"""
    cache_dir: Path = field(default_factory=lambda: CACHE_DIR)
    coins: list = field(default_factory=list)
    bars: list = field(default_factory=lambda: list(EXPIRY.keys()))

    def __post_init__(self):
        self.entries: dict[str, CacheEntry] = {}  # "AVAX-1H" -> CacheEntry
        for coin in self.coins:
            for bar in self.bars:
                key = f'{coin}-{bar}'
                self.entries[key] = CacheEntry(
                    coin=coin, bar=bar,
                    file_path=str(self.cache_dir / f'klines_{coin}_{bar}.csv')
                )

    # ========== 缓存状态检查 ==========

    def get_cache_status(self, coin: str, bar: str) -> CacheEntry:
        """检查缓存状态"""
        entry = self.entries.get(f'{coin}-{bar}')
        if not entry:
            entry = CacheEntry(
                coin=coin, bar=bar,
                file_path=str(self.cache_dir / f'klines_{coin}_{bar}.csv')
            )
            self.entries[f'{coin}-{bar}'] = entry

        path = Path(entry.file_path)
        if not path.exists():
            entry.error = '文件不存在'
            entry.is_valid = False
            return entry

        try:
            df = pd.read_csv(path, nrows=1)
            mtime = path.stat().st_mtime
            age = time.time() - mtime
            expiry = EXPIRY.get(bar, 3600)

            # 读取元数据
            df_full = pd.read_csv(path)
            entry.rows = len(df_full)
            if len(df_full) > 0:
                entry.start_ts = int(df_full.iloc[0]['ts'])
                entry.end_ts = int(df_full.iloc[-1]['ts'])
            entry.last_update = mtime

            if age > expiry:
                entry.error = f'过期 (age={age:.0f}s > expiry={expiry}s)'
                entry.is_valid = False
            else:
                entry.error = ''
                entry.is_valid = True

        except Exception as e:
            entry.error = f'读取失败: {e}'
            entry.is_valid = False

        return entry

    def is_fresh(self, coin: str, bar: str) -> bool:
        """缓存是否新鲜"""
        return self.get_cache_status(coin, bar).is_valid

    def get_data(self, coin: str, bar: str, max_age: Optional[int] = None) -> Optional[pd.DataFrame]:
        """
        读取缓存数据（如果新鲜）
        max_age: 最大允许的缓存年龄（秒），None=按EXPIRY策略
        """
        entry = self.get_cache_status(coin, bar)
        if not entry.is_valid:
            return None

        if max_age is not None:
            age = time.time() - entry.last_update
            if age > max_age:
                return None

        try:
            df = pd.read_csv(entry.file_path, parse_dates=['date'], index_col='date')
            return df
        except:
            return None

    # ========== 数据获取 ==========

    def fetch_okx(self, coin: str, bar: str, after: int = None, limit: int = 300) -> list:
        """从OKX获取K线数据"""
        url = f'https://www.okx.com/api/v5/market/candles?instId={coin}-USDT-SWAP&bar={bar}&limit={limit}'
        if after:
            url += f'&after={after}'

        try:
            r = requests.get(url, timeout=10)
            data = r.json()
            if data.get('code') != '0' or not data.get('data'):
                return []
            return data['data']
        except Exception as e:
            print(f'  ❌ {coin}-{bar}: {e}')
            return []

    def fetch_all_pages(self, coin: str, bar: str, max_rows: int = None) -> list:
        """分页获取K线数据（从最新到最老）"""
        if max_rows is None:
            max_rows = MAX_CANDLES.get(bar, 10000)

        all_data = []
        after = None
        target_rows = 0

        while target_rows < max_rows:
            batch = self.fetch_okx(coin, bar, after=after, limit=300)
            if not batch:
                break

            all_data.extend(batch)
            target_rows += len(batch)

            if len(batch) < 300:
                break

            # 最老一条的时间戳，作为下一页的起点
            after = batch[-1][0]
            time.sleep(0.15)  # 避免限速

        return all_data

    def save_cache(self, coin: str, bar: str, raw_data: list) -> bool:
        """保存K线数据到缓存"""
        if not raw_data:
            return False

        try:
            # 转换格式（OKX格式 -> DataFrame）
            rows = []
            for d in reversed(raw_data):  # 反转：从旧到新
                rows.append({
                    'ts': int(d[0]),
                    'open': float(d[1]),
                    'high': float(d[2]),
                    'low': float(d[3]),
                    'close': float(d[4]),
                    'volume': float(d[5]),
                })

            df = pd.DataFrame(rows)
            df['date'] = pd.to_datetime(df['ts'], unit='ms')
            df = df.set_index('date')

            # 裁剪超限数据
            max_rows = MAX_CANDLES.get(bar, 0)
            if max_rows > 0 and len(df) > max_rows:
                df = df.iloc[-max_rows:]

            # 追加模式：如果已有缓存，合并新旧数据
            path = Path(self.cache_dir) / f'klines_{coin}_{bar}.csv'
            if path.exists():
                try:
                    existing = pd.read_csv(path, parse_dates=['date'], index_col='date')
                    # 合并：去除重复，保留最新的
                    combined = pd.concat([existing, df])
                    combined = combined[~combined.index.duplicated(keep='last')]
                    combined = combined.sort_index()
                    df = combined
                except:
                    pass

            # 保存
            df.to_csv(path)

            # 更新entry
            entry = self.entries.get(f'{coin}-{bar}')
            if entry:
                entry.rows = len(df)
                entry.start_ts = int(df.iloc[0]['ts'])
                entry.end_ts = int(df.iloc[-1]['ts'])
                entry.last_update = time.time()
                entry.is_valid = True
                entry.error = ''

            return True

        except Exception as e:
            entry = self.entries.get(f'{coin}-{bar}')
            if entry:
                entry.error = f'保存失败: {e}'
            return False

    def incremental_update(self, coin: str, bar: str) -> int:
        """增量更新：只拉取最新数据"""
        entry = self.get_cache_status(coin, bar)

        if entry.is_valid and entry.rows > 0:
            # 已有缓存，只拉取更新的数据
            after = str(entry.end_ts)
            new_data = self.fetch_okx(coin, bar, after=after, limit=300)
            if new_data:
                # 过滤掉已有的（end_ts）
                new_data = [d for d in new_data if int(d[0]) > entry.end_ts]
                if new_data:
                    self.save_cache(coin, bar, new_data)
                    return len(new_data)
            return 0
        else:
            # 无缓存，全量获取
            data = self.fetch_all_pages(coin, bar)
            self.save_cache(coin, bar, data)
            return len(data)

    # ========== 批量操作 ==========

    def refresh_all(self, incremental: bool = True) -> dict:
        """刷新所有币种缓存"""
        results = {}

        for coin in self.coins:
            for bar in ['1H', '4H']:  # 重点缓存1H和4H
                key = f'{coin}-{bar}'
                status = self.get_cache_status(coin, bar)

                if incremental and status.is_valid:
                    # 增量更新
                    new_rows = self.incremental_update(coin, bar)
                    results[key] = {
                        'status': 'incremental',
                        'new_rows': new_rows,
                        'total_rows': self.entries[key].rows,
                    }
                else:
                    # 全量获取
                    data = self.fetch_all_pages(coin, bar)
                    self.save_cache(coin, bar, data)
                    results[key] = {
                        'status': 'full',
                        'rows': len(data),
                        'total_rows': self.entries[key].rows,
                    }

                time.sleep(0.2)

        return results

    def parallel_refresh(self, coins: list = None, bars: list = None) -> dict:
        """并行刷新缓存"""
        coins = coins or self.coins
        bars = bars or ['1H', '4H']

        def fetch_one(coin, bar):
            entry = self.get_cache_status(coin, bar)
            if entry.is_valid and incremental:
                new_rows = self.incremental_update(coin, bar)
                return coin, bar, 'incremental', new_rows, self.entries[f'{coin}-{bar}'].rows
            else:
                data = self.fetch_all_pages(coin, bar)
                self.save_cache(coin, bar, data)
                return coin, bar, 'full', len(data), self.entries[f'{coin}-{bar}'].rows

        incremental = True  # 默认增量
        results = {}
        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = {executor.submit(fetch_one, c, b): (c, b)
                      for c in coins for b in bars}
            for future in as_completed(futures):
                coin, bar, status, new_rows, total = future.result()
                key = f'{coin}-{bar}'
                results[key] = {'status': status, 'new_rows': new_rows, 'total_rows': total}
                print(f'  {coin}-{bar}: {status} {new_rows}新行, 共{total}行')

        return results

    # ========== 缓存报告 ==========

    def print_status(self):
        """打印缓存状态报告"""
        print(f'\n{"="*60}')
        print(f'Kronos Data Cache 状态报告')
        print(f'{"="*60}')
        print(f'缓存目录: {self.cache_dir}')
        print(f'币种: {len(self.coins)}个 | 周期: {len(self.bars)}种')
        print()
        print(f'{"币种-周期":<15} {"状态":<12} {"行数":<8} {"最新时间":<22} {"年龄":<10} {"错误"}')
        print('-' * 90)

        stale_coins = set()
        valid_count = 0

        for coin in self.coins:
            for bar in ['1H', '4H']:
                entry = self.get_cache_status(coin, bar)
                key = f'{coin}-{bar}'

                if entry.is_valid:
                    status = '✅有效'
                    valid_count += 1
                    age = time.time() - entry.last_update
                    age_str = f'{age/3600:.1f}h'
                    latest = datetime.fromtimestamp(entry.end_ts / 1000).strftime('%Y-%m-%d %H:%M')
                else:
                    if entry.error == '文件不存在':
                        status = '❌缺失'
                        stale_coins.add(coin)
                    elif '过期' in entry.error:
                        status = '⏰过期'
                        stale_coins.add(coin)
                    else:
                        status = f'❌{entry.error[:10]}'
                    age = 0
                    age_str = '-'
                    latest = '-'

                print(f'{key:<15} {status:<12} {entry.rows:<8} {latest:<22} {age_str:<10} {entry.error}')

        print()
        print(f'有效缓存: {valid_count}/{len(self.coins)*2} ({valid_count/(len(self.coins)*2)*100:.0f}%)')
        if stale_coins:
            print(f'需要刷新: {", ".join(sorted(stale_coins))}')

        return stale_coins


# ========== 数据验证 ==========

def validate_cache(coin: str, bar: str) -> dict:
    """验证缓存数据完整性"""
    path = CACHE_DIR / f'klines_{coin}_{bar}.csv'
    if not path.exists():
        return {'valid': False, 'error': '文件不存在'}

    try:
        df = pd.read_csv(path, parse_dates=['date'], index_col='date')

        # 检查必要列
        required = ['open', 'high', 'low', 'close', 'volume', 'ts']
        missing = [c for c in required if c not in df.columns]
        if missing:
            return {'valid': False, 'error': f'缺少列: {missing}'}

        # 检查数据连续性（时间戳递增）
        if len(df) > 1:
            ts_diff = df['ts'].diff().dropna()
            # K线之间应该有一定间隔（4H=4*3600*1000ms）
            expected_gap_ms = {'1m': 60000, '5m': 300000, '15m': 900000,
                              '1H': 3600000, '4H': 14400000, '1D': 86400000}
            expected = expected_gap_ms.get(bar, 0)
            gaps = ts_diff[ts_diff > expected * 1.1]
            if len(gaps) > len(df) * 0.1:  # 超过10%有间隔
                return {
                    'valid': True,
                    'warning': f'有{len(gaps)}个时间间隙（>{expected/3600000:.0f}h）',
                    'rows': len(df), 'gaps': len(gaps)
                }

        # 检查OHLC逻辑：high应该是最高的，low应该是最低的
        # close和open都应该在[low, high]范围内
        valid_mask = (
            (df['high'] >= df['low']) &
            (df['high'] >= df['close']) &
            (df['high'] >= df['open']) &
            (df['low'] <= df['close']) &
            (df['low'] <= df['open'])
        )
        invalid = df[~valid_mask]
        if len(invalid) > 0:
            return {
                'valid': True,
                'warning': f'有{len(invalid)}条OHLC逻辑错误',
                'rows': len(df)
            }

        return {'valid': True, 'rows': len(df), 'start': df.index[0], 'end': df.index[-1]}

    except Exception as e:
        return {'valid': False, 'error': str(e)}


# ========== 主程序 ==========

if __name__ == '__main__':
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--coin', default=None, help='指定币种')
    parser.add_argument('--bar', default='4H', help='K线周期')
    parser.add_argument('--refresh', action='store_true', help='强制全量刷新')
    parser.add_argument('--parallel', action='store_true', help='并行刷新')
    parser.add_argument('--status', action='store_true', help='只显示状态')
    parser.add_argument('--validate', action='store_true', help='验证缓存')
    args = parser.parse_args()

    coins = [args.coin] if args.coin else COINS

    cache = DataCache(coins=coins)

    if args.status:
        cache.print_status()

    elif args.validate:
        print(f'\n{"="*60}')
        print(f'缓存验证报告')
        print(f'{"="*60}')
        for coin in coins:
            for bar in ['1H', '4H']:
                result = validate_cache(coin, bar)
                status = '✅' if result['valid'] else '❌'
                info = ', '.join(f'{k}={v}' for k, v in result.items() if k != 'valid')
                print(f'  {coin}-{bar}: {status} {info}')

    elif args.refresh or args.parallel:
        print(f'\n{"="*60}')
        print(f'刷新缓存: {coins}')
        print(f'{"="*60}')
        if args.parallel:
            results = cache.parallel_refresh(coins=coins, bars=['1H', '4H'])
        else:
            results = cache.refresh_all(incremental=not args.refresh)
        print()
        cache.print_status()

    else:
        # 默认：显示状态
        stale = cache.print_status()

        if stale:
            print(f'\n{"="*60}')
            print(f'开始刷新 {len(stale)} 个过期缓存...')
            results = cache.parallel_refresh(coins=list(stale))
            print()
            cache.print_status()
