#!/usr/bin/env python3
"""
Kronos 垃圾数据自动清理脚本
================================

cron output: 保留最近24小时，删除更旧的
kronos_autoresearch/results: 保留最近48小时，删除更旧的实验文件
research_night: 保留最近7天
funding_rate_data: 压缩超过30天的旧研究文件

用法:
    python cleanup_stale_data.py              # 运行所有清理任务
    python cleanup_stale_data.py --cron        # 仅清理 cron output
    python cleanup_stale_data.py --autoresearch  # 仅清理 autoresearch
    python cleanup_stale_data.py --dry-run     # 预览要清理的内容
    python cleanup_stale_data.py --verbose     # 显示详细信息
"""

import argparse
import gzip
import os
import shutil
from datetime import datetime, timedelta
from pathlib import Path
from typing import NamedTuple


class CleanupResult(NamedTuple):
    """Result of a cleanup operation."""

    removed: int
    freed: int  # bytes


# ============================================================================
# 配置路径
# ============================================================================

CRON_OUTPUT = Path.home() / ".hermes" / "cron" / "output"
KRONOS_AUTORESEARCH = Path.home() / "kronos_autoresearch" / "results"
RESEARCH_NIGHT = Path.home() / "kronos" / "research_night"
FUNDING_DATA = Path.home() / "kronos" / "funding_rate_data"


# ============================================================================
# 工具函数
# ============================================================================


def human_size(n: int) -> str:
    """将字节数转换为人类可读格式."""
    for u in ["B", "K", "M", "G"]:
        if abs(n) < 1024:
            return f"{n:.1f}{u}"
        n /= 1024
    return f"{n:.1f}T"


# ============================================================================
# 清理函数
# ============================================================================


def cleanup_cron_output(dry_run: bool = False, verbose: bool = False) -> CleanupResult:
    """
    cron output: 删除24小时前的子目录（保留数据文件）

    Args:
        dry_run: 若为True，仅返回要清理的信息，不实际删除
        verbose: 若为True，显示详细信息

    Returns:
        CleanupResult: (删除数量, 释放空间)
    """
    now = datetime.now()
    removed = 0
    freed = 0

    if not CRON_OUTPUT.exists():
        return CleanupResult(0, 0)

    for d in CRON_OUTPUT.iterdir():
        if not d.is_dir():
            continue  # 跳过数据文件
        # 跳过活跃job的输出（通过目录修改时间判断，保留24h内更新的）
        age = now - datetime.fromtimestamp(d.stat().st_mtime)
        if age.total_seconds() > 86400:  # 24小时
            size = sum(f.stat().st_size for f in d.rglob("*") if f.is_file())
            if verbose:
                print(f"  {'[DRY-RUN] ' if dry_run else ''}删除旧目录: {d.name} ({human_size(size)})")
            if not dry_run:
                shutil.rmtree(d)
            removed += 1
            freed += size

    return CleanupResult(removed, freed)


def cleanup_autoresearch(
    dry_run: bool = False, verbose: bool = False
) -> CleanupResult:
    """
    autoresearch experiments: 删除48小时前的旧实验文件

    Args:
        dry_run: 若为True，仅返回要清理的信息，不实际删除
        verbose: 若为True，显示详细信息

    Returns:
        CleanupResult: (删除数量, 释放空间)
    """
    removed = 0
    freed = 0
    exp_dir = KRONOS_AUTORESEARCH / "experiments"

    if not exp_dir.exists():
        return CleanupResult(0, 0)

    cutoff = datetime.now() - timedelta(hours=48)
    for f in exp_dir.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
            size = f.stat().st_size
            if verbose:
                print(f"  {'[DRY-RUN] ' if dry_run else ''}删除旧实验: {f.name} ({human_size(size)})")
            if not dry_run:
                f.unlink()
            removed += 1
            freed += size

    return CleanupResult(removed, freed)


def cleanup_research_night(
    dry_run: bool = False, verbose: bool = False
) -> CleanupResult:
    """
    research_night: 删除7天前的日志文件

    Args:
        dry_run: 若为True，仅返回要清理的信息，不实际删除
        verbose: 若为True，显示详细信息

    Returns:
        CleanupResult: (删除数量, 释放空间)
    """
    removed = 0
    freed = 0

    if not RESEARCH_NIGHT.exists():
        return CleanupResult(0, 0)

    cutoff = datetime.now() - timedelta(days=7)
    for f in RESEARCH_NIGHT.iterdir():
        if f.is_file() and f.stat().st_mtime < cutoff.timestamp():
            size = f.stat().st_size
            if verbose:
                print(f"  {'[DRY-RUN] ' if dry_run else ''}删除旧日志: {f.name} ({human_size(size)})")
            if not dry_run:
                f.unlink()
            removed += 1
            freed += size

    return CleanupResult(removed, freed)


def cleanup_autoresearch_results(
    dry_run: bool = False, verbose: bool = False
) -> int:
    """
    autoresearch results.tsv: 只保留最近30天的 top N 记录

    Args:
        dry_run: 若为True，仅返回要清理的信息，不实际删除
        verbose: 若为True，显示详细信息

    Returns:
        int: 删除的记录数
    """
    results_file = KRONOS_AUTORESEARCH / "results.tsv"
    if not results_file.exists():
        return 0

    try:
        import pandas as pd
        import time as time_module

        # 读所有记录
        df = pd.read_csv(results_file, sep="\t")
        if len(df) == 0:
            return 0

        # 文件修改时间作为代理（无date列）
        cutoff = time_module.time() - 30 * 86400  # 30天前

        # 保留: 30天内 OR 排名top N（无论是否在30天内，保证最强实验不丢失）
        df_sorted = df.dropna(subset=["sharpe"]).sort_values("sharpe", ascending=False)
        top_n = 30000  # 保留 top 30000 条

        # 策略: (30天内的全部保留) + (不在30天内但排名top N的)
        # 由于无date列，用文件mtime作为所有记录的代理时间
        # 所有记录都用同一mtime → 无法区分新旧 → 改为只按top N保留
        df_top = df_sorted.head(top_n)

        original_len = len(df)
        removed = original_len - len(df_top)

        if verbose:
            print(f"  {'[DRY-RUN] ' if dry_run else ''}autoresearch results.tsv: {original_len}→{len(df_top)} (删{removed}条)")

        if not dry_run and removed > 0:
            df_top.to_csv(results_file, sep="\t", index=False)

        return removed
    except Exception as e:
        print(f"  autoresearch results.tsv 清理失败: {e}")
        return 0


def compress_old_funding_csvs(
    dry_run: bool = False, verbose: bool = False
) -> int:
    """
    压缩30天前的funding研究CSV

    Args:
        dry_run: 若为True，仅返回要清理的信息，不实际删除
        verbose: 若为True，显示详细信息

    Returns:
        int: 释放的空间(字节)
    """
    removed_size = 0

    if not FUNDING_DATA.exists():
        return 0

    cutoff = datetime.now() - timedelta(days=30)
    for f in FUNDING_DATA.iterdir():
        if f.is_file() and f.suffix == ".csv" and not str(f).endswith(".gz"):
            if f.stat().st_mtime < cutoff.timestamp():
                size = f.stat().st_size
                gz = Path(str(f) + ".gz")
                if verbose:
                    print(f"  {'[DRY-RUN] ' if dry_run else ''}压缩旧CSV: {f.name} → {gz.name}")
                if not dry_run:
                    with open(f, "rb") as fin, gzip.open(gz, "wb") as fout:
                        shutil.copyfileobj(fin, fout)
                    f.unlink()
                removed_size += size

    return removed_size


# ============================================================================
# 主函数
# ============================================================================


def run_cleanup(
    cron: bool = False,
    autoresearch: bool = False,
    research_night: bool = False,
    funding: bool = False,
    all_tasks: bool = True,
    dry_run: bool = False,
    verbose: bool = False,
) -> int:
    """
    运行清理任务

    Args:
        cron: 运行 cron output 清理
        autoresearch: 运行 autoresearch 清理
        research_night: 运行 research_night 清理
        funding: 运行 funding data 清理
        all_tasks: 运行所有清理任务
        dry_run: 预览模式，不实际删除
        verbose: 显示详细信息

    Returns:
        int: 0 表示成功
    """
    total_freed = 0
    mode_str = "[DRY-RUN] " if dry_run else ""
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] {mode_str}Kronos 数据清理开始")

    # 清理 cron output
    if all_tasks or cron:
        n, s = cleanup_cron_output(dry_run=dry_run, verbose=verbose)
        print(f"  cron output: 删除 {n} 个旧目录，释放 {human_size(s)}")
        total_freed += s

    # 清理 autoresearch
    if all_tasks or autoresearch:
        n, s = cleanup_autoresearch(dry_run=dry_run, verbose=verbose)
        print(f"  autoresearch experiments: 删除 {n} 个旧实验文件，释放 {human_size(s)}")
        total_freed += s

    # 清理 autoresearch results.tsv
    if all_tasks or autoresearch:
        removed = cleanup_autoresearch_results(dry_run=dry_run, verbose=verbose)
        if removed > 0:
            print(f"  autoresearch results.tsv: 删 {removed} 条旧记录")

    # 清理 research_night
    if all_tasks or research_night:
        n, s = cleanup_research_night(dry_run=dry_run, verbose=verbose)
        print(f"  research_night: 删除 {n} 个旧日志，释放 {human_size(s)}")
        total_freed += s

    # 压缩旧 funding CSVs
    if all_tasks or funding:
        s = compress_old_funding_csvs(dry_run=dry_run, verbose=verbose)
        if s > 0:
            print(f"  funding_data: 压缩旧CSV，节省 {human_size(s)}")
            total_freed += s

    print(f"  总计释放: {human_size(total_freed)}")
    print("清理完成 ✓")
    return 0


def main() -> int:
    """主入口点."""
    parser = argparse.ArgumentParser(
        prog="cleanup_stale_data.py",
        description="Kronos 数据清理工具 - 清理过期的临时文件和日志",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  %(prog)s                    # 运行所有清理任务
  %(prog)s --cron             # 仅清理 cron output
  %(prog)s --autoresearch     # 仅清理 autoresearch 实验
  %(prog)s --dry-run          # 预览要清理的内容
  %(prog)s --verbose          # 显示详细信息
  %(prog)s --cron --research-night  # 清理 cron 和 research_night

清理规则:
  - cron output: 保留最近 24 小时
  - autoresearch experiments: 保留最近 48 小时
  - research_night 日志: 保留最近 7 天
  - funding_rate_data CSV: 压缩超过 30 天的文件
  - autoresearch results.tsv: 只保留 top 30000 条
        """,
    )

    parser.add_argument(
        "--cron",
        action="store_true",
        help="仅清理 cron output (保留24小时内)",
    )
    parser.add_argument(
        "--autoresearch",
        action="store_true",
        help="仅清理 autoresearch 实验文件 (保留48小时内)",
    )
    parser.add_argument(
        "--research-night",
        action="store_true",
        dest="research_night",
        help="仅清理 research_night 日志 (保留7天内)",
    )
    parser.add_argument(
        "--funding",
        action="store_true",
        help="仅压缩旧 funding_rate_data CSV (30天+)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        dest="dry_run",
        help="预览要清理的内容，不实际删除",
    )
    parser.add_argument(
        "--verbose", "-v",
        action="store_true",
        help="显示详细信息",
    )
    parser.add_argument(
        "--version",
        action="version",
        version="%(prog)s 1.0.0",
    )

    args = parser.parse_args()

    # 如果没有指定任何具体任务，则运行所有
    all_tasks = not any([args.cron, args.autoresearch, args.research_night, args.funding])

    return run_cleanup(
        cron=args.cron,
        autoresearch=args.autoresearch,
        research_night=args.research_night,
        funding=args.funding,
        all_tasks=all_tasks,
        dry_run=args.dry_run,
        verbose=args.verbose,
    )


if __name__ == "__main__":
    import sys
    sys.exit(main())
