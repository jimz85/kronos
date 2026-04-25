#!/usr/bin/env python3
"""BCH数据检查"""
import pandas as pd

path = '/Users/jimingzhang/Desktop/crypto_data_Pre5m/BCH_USDT_5m_from_20180101.csv'
df = pd.read_csv(path, nrows=10)
df.columns = [c.lstrip('\ufeff') for c in df.columns]
print(df.columns.tolist())
print(df.head())
