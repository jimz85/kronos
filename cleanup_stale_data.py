#!/usr/bin/env python3
"""
Kronos 垃圾数据自动清理脚本
- cron output: 保留最近24小时，删除更旧的
- kronos_autoresearch/results: 保留最近48小时，删除更旧的实验文件
- research_night: 保留最近7天
- funding_rate_data: 压缩超过30天的旧研究文件
"""

import os
import gzip
import shutil
from pathlib import Path
from datetime import datetime, timedelta

CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"
KRONOS_AUTORESEARCH = Path.home() / "kronos_autoresearch" / "results"
RESEARCH_NIGHT = Path.home() / "kronos" / "research_night"
FUNDING_DATA = Path.home() / "kronos" / "funding_rate_data"

def human_size(n):
    for u in ['B','K','M','G']:
        if abs(n) < 1024: return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"

def cleanup_cron_output():
    """cron output: 删除24小时前的子目录（保留数据文件）"""
    now = datetime.now()
    removed = 0
    freed = 0
    for d in CRON_OUTPUT.iterdir():
        if not d.is_dir():
            continue  # 跳过数据文件
        # 跳过活跃job的输出（通过目录修改时间判断，保留24h内更新的）
        age = now - datetime.fromtimestamp(d.stat().st_mtime)
        if age.total_seconds() > 86400:  # 24小时
            size = sum(f.stat().st_size for f in d.rglob('*') if f.is_file())
            shutil.rmtree(d)
            removed += 1
            freed += size
    return removed, freed

def cleanup_autoresearch():
    """autoresearch experiments: 删除48小时前的旧实验文件"""
    removed = 0
    freed = 0
    exp_dir = KRONOS_AUTORESEARCH / "experiments"
    if exp_dir.exists():
        cutoff = datetime.now() - timedelta(hours=48)
        for f in exp_dir.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
                size = f.stat().st_size
                f.unlink()
                removed += 1
                freed += size
    return removed, freed

def cleanup_research_night():
    """research_night: 删除7天前的日志文件"""
    removed = 0
    freed = 0
    if RESEARCH_NIGHT.exists():
        cutoff = datetime.now() - timedelta(days=7)
        for f in RESEARCH_NIGHT.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
                size = f.stat().st_size
                f.unlink()
                removed += 1
                freed += size
    return removed, freed

def cleanup_autoresearch_results():
    """autoresearch results.tsv: 只保留最近30天的 top N 记录"""
    results_file = KRONOS_AUTORESEARCH / "results.tsv"
    if not results_file.exists():
        return 0

    try:
        import pandas as pd
        import time as time_module

        # 读所有记录
        df = pd.read_csv(results_file, sep='\t')
        if len(df) == 0:
            return 0

        # 文件修改时间作为代理（无date列）
        file_mtime = results_file.stat().st_mtime
        cutoff = time_module.time() - 30 * 86400  # 30天前

        # 保留: 30天内 OR 排名top N（无论是否在30天内，保证最强实验不丢失）
        df_sorted = df.dropna(subset=['sharpe']).sort_values('sharpe', ascending=False)
        top_n = 30000  # 保留 top 30000 条

        # 策略: (30天内的全部保留) + (不在30天内但排名top N的)
        # 由于无date列，用文件mtime作为所有记录的代理时间
        # 所有记录都用同一mtime → 无法区分新旧 → 改为只按top N保留
        df_top = df_sorted.head(top_n)

        original_len = len(df)
        df_top.to_csv(results_file, sep='\t', index=False)
        removed = original_len - len(df_top)

        size = results_file.stat().st_size
        print(f"  autoresearch results.tsv: {original_len}→{len(df_top)} (删{removed}条, {human_size(size)})")
        return removed
    except Exception as e:
        print(f"  autoresearch results.tsv 清理失败: {e}")
        return 0


def compress_old_funding_csvs():
    """压缩30天前的funding研究CSV"""
    removed_size = 0
    if FUNDING_DATA.exists():
        cutoff = datetime.now() - timedelta(days=30)
        for f in FUNDING_DATA.iterdir():
            if f.is_file() and f.suffix == '.csv' and not str(f).endswith('.gz'):
                if f.stat().st_mtime < cutoff.timestamp():
                    gz = Path(str(f) + '.gz')
                    with open(f, 'rb') as fin, gzip.open(gz, 'wb') as fout:
                        shutil.copyfileobj(fin, fout)
                    removed_size += f.stat().st_size
                    f.unlink()
    return removed_size

if __name__ == "__main__":
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] Kronos 数据清理开始")
    total_freed = 0

    n, s = cleanup_cron_output()
    print(f"  cron output: 删除 {n} 个旧目录，释放 {human_size(s)}")
    total_freed += s

    n, s = cleanup_autoresearch()
    print(f"  autoresearch experiments: 删除 {n} 个旧实验文件，释放 {human_size(s)}")
    total_freed += s

    cleanup_autoresearch_results()

    n, s = cleanup_research_night()
    print(f"  research_night: 删除 {n} 个旧日志，释放 {human_size(s)}")
    total_freed += s

    s = compress_old_funding_csvs()
    if s:
        print(f"  funding_data: 压缩旧CSV，节省 {human_size(s)}")
        total_freed += s

    print(f"  总计释放: {human_size(total_freed)}")
    print("清理完成 ✓")
