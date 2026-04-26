#!/usr/bin/env python3
"""
run_backtest.py - Kronos Backtest CLI
======================================

Simple CLI script to run backtest for a given coin and date range.

Usage:
    python3 scripts/run_backtest.py --coin BTC --start 2024-01-01 --end 2026-01-01

Options:
    --coin     Trading pair symbol (e.g., BTC, ETH)
    --start    Start date (YYYY-MM-DD)
    --end      End date (YYYY-MM-DD)
    --long     Run long strategy (default: True)
    --short    Run short strategy (default: False)

Version: 5.0.0
"""

import argparse
import sys
from pathlib import Path

# Add kronos root to path
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT))

from kronos.backtest import BacktestEngine, UnifiedBacktester


def parse_args():
    parser = argparse.ArgumentParser(
        description="Kronos Backtest CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python3 scripts/run_backtest.py --coin BTC --start 2024-01-01 --end 2026-01-01
    python3 scripts/run_backtest.py --coin ETH --start 2025-01-01 --end 2025-06-01 --long
    python3 scripts/run_backtest.py --coin SOL --start 2024-06-01 --end 2025-01-01 --short
        """
    )
    parser.add_argument("--coin", required=True, help="Trading pair symbol (e.g., BTC)")
    parser.add_argument("--start", required=True, help="Start date (YYYY-MM-DD)")
    parser.add_argument("--end", required=True, help="End date (YYYY-MM-DD)")
    parser.add_argument("--long", action="store_true", default=True, help="Run long strategy")
    parser.add_argument("--short", action="store_true", default=False, help="Run short strategy")
    return parser.parse_args()


def main():
    args = parse_args()
    
    print(f"=" * 60)
    print(f"Kronos Backtest CLI")
    print(f"{'=' * 60}")
    print(f"Coin:   {args.coin}")
    print(f"Start:  {args.start}")
    print(f"End:    {args.end}")
    print(f"Long:   {args.long}")
    print(f"Short:  {args.short}")
    print(f"=" * 60)
    
    # Check if data directory exists
    data_dir = ROOT / "data"
    if not data_dir.exists():
        print(f"[WARNING] Data directory not found: {data_dir}")
    
    print(f"\nBacktest module loaded successfully.")
    print(f"Available classes: BacktestEngine, UnifiedBacktester")
    print(f"\nNote: Full backtest execution requires data loading infrastructure.")
    print(f"      This CLI provides the interface; connect data source for full results.")
    
    return 0


if __name__ == "__main__":
    sys.exit(main())
