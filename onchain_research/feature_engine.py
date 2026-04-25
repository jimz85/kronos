#!/usr/bin/env python3
"""链上特征引擎：把原始链上数据变成可用特征面板

数据来源（待接入）：
- Glassnode API: MVRV, SOPR, 交易所余额
- 自建UTXO计算（未来）

输出: features.parquet — DataFrame indexed by (symbol, date)
"""
import json, os, sys
import numpy as np
import pandas as pd

BASE = os.path.dirname(__file__)


def load_onchain_data():
    """加载原始链上数据（待实现：接入 Glassnode）"""
    path = f'{BASE}/data/onchain_daily.json'
    if not os.path.exists(path):
        # 返回空字典，等待数据
        return {}
    return json.load(open(path))


def load_ohlc():
    """加载价格数据"""
    ohlc_path = os.path.expanduser('~/shared/materials/crypto_traders_distill/ohlc_daily.json')
    if not os.path.exists(ohlc_path):
        # fallback: 尝试本地缓存
        ohlc_path = f'{BASE}/data/ohlc_daily.json'
    if not os.path.exists(ohlc_path):
        return {}
    return json.load(open(ohlc_path))


def build_features_single(symbol, price_df, onchain_df=None):
    """为一个交易对构建特征面板"""
    df = price_df.copy()
    c, h, l, o = df['close'], df['high'], df['low'], df['open']

    out = pd.DataFrame(index=df.index)
    out['symbol'] = symbol
    out['close'] = c
    out['open'] = o
    out['high'] = h
    out['low'] = l

    # === 价格特征（复用之前验证过的指标）===
    out['ma20'] = c.rolling(20, min_periods=1).mean()
    out['ma50'] = c.rolling(50, min_periods=1).mean()
    out['ma200'] = c.rolling(200, min_periods=1).mean()
    out['ema20'] = c.ewm(span=20, adjust=False).mean()

    # RSI
    diff = c.diff()
    gain = diff.clip(lower=0)
    loss = (-diff).clip(lower=0)
    avg_gain = gain.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1/14, min_periods=14, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    out['rsi14'] = 100 - (100 / (1 + rs))

    # 布林带
    bb_mid = c.rolling(20, min_periods=1).mean()
    bb_std = c.rolling(20, min_periods=1).std(ddof=0)
    out['bb_upper'] = bb_mid + 2 * bb_std
    out['bb_mid'] = bb_mid
    out['bb_lower'] = bb_mid - 2 * bb_std

    # ATR
    tr = pd.concat([h - l, (h - c.shift(1)).abs(), (l - c.shift(1)).abs()], axis=1).max(axis=1)
    out['atr14'] = tr.ewm(alpha=1/14, min_periods=14, adjust=False).mean()

    # 均线关系
    out['price_above_ma200'] = (c > out['ma200']).astype(int)
    out['price_above_ma50'] = (c > out['ma50']).astype(int)
    out['ma50_above_ma200'] = (out['ma50'] > out['ma200']).astype(int)

    # 收益
    for n in [1, 7, 30]:
        out[f'ret_{n}d'] = c.pct_change(n)
    out['fwd_ret_1d'] = c.pct_change(1).shift(-1)
    out['fwd_ret_7d'] = c.pct_change(7).shift(-7)
    out['fwd_ret_30d'] = c.pct_change(30).shift(-30)

    # === 链上特征（框架占位，等待数据接入）===
    # 以下字段名需要 Glassnode API 接入后填充
    onchain_fields = [
        'mvrv_ratio', 'mvrv_zscore', 'mvrv_zscore_prev',
        'mvrv_zscore_pctile', 'sopr', 'sopr_prev',
        'exchange_balance', 'exchange_balance_chg_7d', 'exchange_balance_chg_30d',
    ]
    for field in onchain_fields:
        if field in df.columns:
            out[field] = df[field]
        else:
            out[field] = np.nan

    return out


def build_panel(symbols=['BTCUSDT']):
    """构建多币种特征面板"""
    ohlc = load_ohlc()
    onchain = load_onchain_data()
    frames = {}

    for sym in symbols:
        if sym not in ohlc:
            continue
        price_df = pd.DataFrame(ohlc[sym])
        price_df['date'] = pd.to_datetime(price_df['date'])
        price_df = price_df.set_index('date').sort_index()

        # 合并链上数据（如果有）
        combined = price_df.copy()
        if sym in onchain:
            onchain_df = pd.DataFrame(onchain[sym])
            onchain_df['date'] = pd.to_datetime(onchain_df['date'])
            onchain_df = onchain_df.set_index('date').sort_index()
            combined = combined.join(onchain_df, how='left')

        frames[sym] = build_features_single(sym, combined)

    if not frames:
        return pd.DataFrame()

    panel = pd.concat(frames.values(), keys=frames.keys(), names=['symbol', 'date'])
    return panel


if __name__ == '__main__':
    panel = build_panel(['BTCUSDT', 'ETHUSDT'])
    out_path = f'{BASE}/data/features.parquet'
    panel.to_parquet(out_path)
    print(f'saved {out_path}, shape: {panel.shape}')
    print(f'columns: {panel.columns.tolist()}')
