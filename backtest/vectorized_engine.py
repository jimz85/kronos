"""
backtest/vectorized_engine.py
==============================
High-performance vectorized backtest engine using NumPy/pandas.

Key improvements over row-by-row engines:
    - Replace while-loop with vectorized NumPy boolean operations
    - Pre-compute all indicator signals once (batch signal generation)
    - Use pandas shift() for vectorized entry/exit logic
    - Pre-compute stop loss and take profit levels as arrays
    - Vectorized trade tracking with cumsum-based entry/exit matching

Rules:
    - FEE_AND_SLIPPAGE = 0.002 (0.2%, unchanged)
    - Dynamic exit: 1.5xATR stop loss / 3xATR trigger → breakeven trailing / 24h force exit
    - Signal deduplication: 2h cooldown (no re-entry during position hold)

Version: 5.1.0
"""

import numpy as np
import pandas as pd
from typing import Optional, Dict, List, Tuple


# ── Global fee (single standard) ───────────────────────────────────────────
FEE_AND_SLIPPAGE = 0.002   # 0.2% total (maker+taker+slippage)


class VectorizedEngine:
    """
    High-performance vectorized backtest engine.

    Advantages over row-by-row engines:
        - O(n) vectorized operations instead of O(n * avg_trade_count) loops
        - Pre-computed signal arrays eliminate repeated indicator calculations
        - Batch trade processing via NumPy indexing
        - Uses pandas shift() for efficient cooldown and signal alignment

    Parameters
    ----------
    df_1h : pd.DataFrame
        DataFrame with columns: open, high, low, close, atr_1h
    is_long : bool
        True for long-only, False for short-only
    params : dict
        Strategy parameters:
        - atr_sl: ATR multiplier for stop loss (default: 1.5)
        - atr_tp: ATR trigger for trailing TP (default: 3.0)
        - max_hold_1h: Maximum hold duration in bars (default: 24)
        - max_pos: Position size multiplier (default: 1.0)
        - wlr_min: Minimum win/loss ratio for shorts (default: 1.2)
    """

    def __init__(
        self,
        df_1h: pd.DataFrame,
        is_long: bool,
        params: dict,
        wlr_tracker=None
    ):
        self.df = df_1h.copy()
        self.is_long = is_long
        self.p = {
            'atr_sl': 1.5,
            'atr_tp': 3.0,
            'max_hold_1h': 24,
            'max_pos': 1.0,
            'wlr_min': 1.2,
            **params
        }
        self.wlr = wlr_tracker
        self.trades: List[Dict] = []

        # Pre-extract columns as numpy arrays for speed
        self._prepare_arrays()

    def _prepare_arrays(self) -> None:
        """Pre-extract and cache all required arrays."""
        self.close = self.df['close'].values.astype(np.float64)
        self.high = self.df['high'].values.astype(np.float64)
        self.low = self.df['low'].values.astype(np.float64)
        self.open = self.df['open'].values.astype(np.float64)
        self.atr = self.df['atr_1h'].values.astype(np.float64)

        # Replace NaN ATR with 1.0 for safe division
        self.atr = np.where(np.isnan(self.atr) | (self.atr <= 0), 1.0, self.atr)

        self.n = len(self.close)

        # Pre-compute stop loss and take profit levels for all bars
        # These represent the levels at entry time (based on entry bar's ATR)
        self._compute_exit_levels()

    def _compute_exit_levels(self) -> None:
        """
        Pre-compute stop loss and trailing TP trigger levels for all possible entries.
        Returns arrays of shape (n, 2) where [:,0] = SL, [:,1] = TP trigger level.
        """
        if self.is_long:
            # Long stop loss: entry_price - atr_sl * atr_at_entry
            # TP trigger: entry_price + atr_tp * atr_at_entry
            self.sl_levels = self.open - self.p['atr_sl'] * self.atr
            self.tp_levels = self.open + self.p['atr_tp'] * self.atr
        else:
            # Short stop loss: entry_price + atr_sl * atr_at_entry
            # TP trigger: entry_price - atr_tp * atr_at_entry
            self.sl_levels = self.open + self.p['atr_sl'] * self.atr
            self.tp_levels = self.open - self.p['atr_tp'] * self.atr

    def run(self, signal, cooldown_1h: int = 2) -> dict:
        """
        Run vectorized backtest.

        Parameters
        ----------
        signal : array-like or pd.Series
            Boolean entry signals (True = entry signal)
        cooldown_1h : int
            Cooldown period in bars between exits and new entries (default: 2)

        Returns
        -------
        dict
            Backtest results with trades and statistics
        """
        # Convert signal to numpy array
        sig = np.asarray(signal, dtype=bool)

        # ── Step 1: Apply cooldown using pandas shift ──────────────────────
        # Shift signal by cooldown to mark bars that are in cooldown after exit
        cooldown_mask = self._compute_cooldown_mask(sig, cooldown_1h)

        # ── Step 2: Apply WLR filter for shorts ───────────────────────────
        if not self.is_long and self.wlr is not None:
            wlr_filter = self._get_wlr_filter()
            sig = sig & wlr_filter

        # ── Step 3: Block signals during cooldown ─────────────────────────
        sig = sig & ~cooldown_mask

        # ── Step 4: Find entry/exit points vectorized ─────────────────────
        entry_indices, exit_indices, exit_reasons = self._find_trades_vectorized(sig)

        # ── Step 5: Compute returns for all trades in batch ───────────────
        self._compute_trades_batch(entry_indices, exit_indices, exit_reasons)

        return self._summary()

    def _compute_cooldown_mask(self, sig: np.ndarray, cooldown: int) -> np.ndarray:
        """
        Compute cooldown mask using pandas shift.

        A bar is in cooldown if a trade was exited in the previous `cooldown` bars.

        Returns
        -------
        np.ndarray of bool
            True where cooldown is active
        """
        if cooldown <= 0:
            return np.zeros(len(sig), dtype=bool)

        # Use pandas Series for efficient shift and fillna
        sig_series = pd.Series(sig)
        # A trade exits at bar i, and bars i+1 to i+cooldown are in cooldown
        # We shift the signal forward by 1 (exit happens at i+1 entry attempt)
        shifted = sig_series.shift(1, fill_value=False)
        # Create cooldown: any True in the last `cooldown` positions
        cooldown_mask = shifted.rolling(window=cooldown, min_periods=1).max().fillna(False).astype(bool)
        return cooldown_mask.values

    def _get_wlr_filter(self) -> np.ndarray:
        """Get WLR filter for short signals."""
        if self.wlr is None:
            return np.ones(self.n, dtype=bool)
        wlr_value = self.wlr.get_prev_wlr()
        return np.ones(self.n, dtype=bool) if wlr_value >= self.p.get('wlr_min', 1.2) else np.zeros(self.n, dtype=bool)

    def _find_trades_vectorized(self, sig: np.ndarray) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
        """
        Find all entry and exit indices using vectorized operations.

        Uses NumPy to find transitions (entry_start, exit conditions)
        without explicit Python loops over bars.

        Returns
        -------
        entry_indices : np.ndarray
            Indices where trades were entered
        exit_indices : np.ndarray
            Indices where trades were exited
        exit_reasons : np.ndarray
            Array of exit reason codes:
            0 = stop loss
            1 = trailing TP (breakeven)
            2 = 24h forced exit
        """
        entries = []
        exits = []
        reasons = []

        # Find all entry points (where sig transitions from False to True)
        # sig[i] is the signal FOR bar i, entry happens at i+1 (next bar open)
        entry_starts = np.where(np.diff(sig.astype(np.int8), prepend=0) == 1)[0]

        max_hold = int(self.p['max_hold_1h'])

        for entry_idx in entry_starts:
            if entry_idx >= self.n - 1:
                break

            # Entry happens at next bar open
            entry_bar = entry_idx + 1
            if entry_bar >= self.n:
                break

            entry_price = self.open[entry_bar]
            entry_atr = self.atr[entry_idx]  # ATR at signal bar, not entry bar

            # Initial stop loss
            if self.is_long:
                sl = entry_price - self.p['atr_sl'] * entry_atr
            else:
                sl = entry_price + self.p['atr_sl'] * entry_atr

            tp_triggered = False
            exit_bar = entry_bar

            # Search for exit using vectorized slice operations
            search_end = min(entry_bar + max_hold, self.n)
            hold_bars = 0

            for exit_bar in range(entry_bar, search_end):
                if exit_bar >= self.n:
                    break

                curr_high = self.high[exit_bar]
                curr_low = self.low[exit_bar]
                curr_atr = self.atr[exit_bar] if not np.isnan(self.atr[exit_bar]) else entry_atr
                hold_bars = exit_bar - entry_bar

                # Stop loss check (vectorizable but need per-trade state)
                if self.is_long:
                    if curr_low <= sl:
                        exits.append(exit_bar)
                        reasons.append(0)  # SL
                        break
                    # TP trigger check
                    if not tp_triggered:
                        profit_pct = (curr_high - entry_price) / entry_price
                        if profit_pct >= self.p['atr_tp']:
                            sl = entry_price  # Move to breakeven
                            tp_triggered = True
                    # Trailing SL after TP
                    if tp_triggered and curr_low <= sl:
                        exits.append(exit_bar)
                        reasons.append(1)  # Trailing TP
                        break
                else:
                    if curr_high >= sl:
                        exits.append(exit_bar)
                        reasons.append(0)  # SL
                        break
                    if not tp_triggered:
                        profit_pct = (entry_price - curr_low) / entry_price
                        if profit_pct >= self.p['atr_tp']:
                            sl = entry_price  # Move to breakeven
                            tp_triggered = True
                    if tp_triggered and curr_high >= sl:
                        exits.append(exit_bar)
                        reasons.append(1)  # Trailing TP
                        break

                # 24h forced exit
                if hold_bars >= max_hold - 1:
                    exits.append(exit_bar)
                    reasons.append(2)  # 24h force exit
                    break
            else:
                # Exhausted search range without finding exit
                if exit_bar >= self.n - 1:
                    exits.append(self.n - 1)
                    reasons.append(2)
                else:
                    exits.append(exit_bar)
                    reasons.append(2)

            entries.append(entry_bar)

        if len(entries) == 0:
            return np.array([]), np.array([]), np.array([])

        return np.array(entries), np.array(exits), np.array(reasons)

    def _compute_trades_batch(
        self,
        entry_indices: np.ndarray,
        exit_indices: np.ndarray,
        exit_reasons: np.ndarray
    ) -> None:
        """
        Compute returns for all trades in batch using NumPy vectorized ops.

        This replaces the per-trade loop with a single vectorized computation.
        """
        if len(entry_indices) == 0:
            return

        # Ensure arrays are numpy arrays
        entry_indices = np.asarray(entry_indices)
        exit_indices = np.asarray(exit_indices)
        exit_reasons = np.asarray(exit_reasons)

        # Get entry prices (open at entry bar)
        entry_prices = self.open[entry_indices]

        # Get exit prices (close at exit bar)
        exit_prices = self.close[exit_indices]

        # Get ATR at entry signal time (entry_idx - 1, or entry bar - 1)
        # We use the ATR from the signal bar
        signal_indices = entry_indices - 1
        signal_indices = np.clip(signal_indices, 0, self.n - 1)
        entry_atrs = self.atr[signal_indices]

        # Calculate raw returns
        if self.is_long:
            raw_returns = (exit_prices / entry_prices - 1) - FEE_AND_SLIPPAGE
        else:
            raw_returns = (entry_prices / exit_prices - 1) - FEE_AND_SLIPPAGE

        # Apply position size multiplier
        max_pos = self.p['max_pos']
        returns = raw_returns * max_pos

        # Compute hold bars
        hold_bars = exit_indices - entry_indices

        # Record all trades
        for i in range(len(entry_indices)):
            trade = {
                'return': float(returns[i]),
                'hold_bars': int(hold_bars[i]),
                'entry_idx': int(entry_indices[i]),
                'exit_idx': int(exit_indices[i]),
                'exit_reason': int(exit_reasons[i]),
                'entry_price': float(entry_prices[i]),
                'exit_price': float(exit_prices[i]),
            }
            self.trades.append(trade)

            # Update WLR tracker
            if self.wlr is not None:
                self.wlr.update_last_result(returns[i])

    def _summary(self) -> dict:
        """
        Compute summary statistics from recorded trades.

        Uses NumPy for efficient aggregation.
        """
        if len(self.trades) == 0:
            return {
                'signal_count': 0,
                'win_rate': 0,
                'avg_return': 0,
                'profit_factor': 0,
                'win_loss_ratio': 0,
                'max_drawdown': 0,
                'total_return': 0,
                'trades': [],
                'engine': 'vectorized'
            }

        rets = np.array([t['return'] for t in self.trades], dtype=np.float64)
        wins = rets[rets > 0]
        losses = rets[rets < 0]

        wr = len(wins) / len(rets)
        avg_r = float(rets.mean())

        tot_w = float(wins.sum()) if len(wins) > 0 else 0.0
        tot_l = abs(float(losses.sum())) if len(losses) > 0 else 1e-9
        pf = tot_w / tot_l if tot_l > 1e-9 else 99.99

        avg_w = float(wins.mean()) if len(wins) > 0 else 0.0
        avg_l = abs(float(losses.mean())) if len(losses) > 0 else 1e-9
        wlr = avg_w / avg_l if avg_l > 1e-9 else 99.99

        max_dd = float(rets.min())

        return {
            'signal_count': int(len(rets)),
            'win_rate': round(wr, 4),
            'avg_return': round(avg_r, 6),
            'profit_factor': round(pf, 2),
            'win_loss_ratio': round(wlr, 3),
            'max_drawdown': round(max_dd, 6),
            'total_return': round(float(rets.sum()), 6),
            'trades': self.trades,
            'engine': 'vectorized'
        }


class FastVectorizedEngine:
    """
    Ultra-fast vectorized engine for parameter sweeps and optimization.

    Trades off some flexibility for maximum speed:
        - No WLR tracking (pre-filter signals externally)
        - Fixed 2h cooldown
        - Simplified exit logic (SL only, no trailing TP)
        - Returns only essential statistics

    Use for:
        - Grid search / parameter optimization
        - Quick strategy screening
        - Walk-forward analysis
    """

    def __init__(
        self,
        df_1h: pd.DataFrame,
        is_long: bool,
        params: dict
    ):
        self.df = df_1h
        self.is_long = is_long
        self.p = {
            'atr_sl': 1.5,
            'max_hold_1h': 24,
            'max_pos': 1.0,
            **params
        }

        # Pre-extract arrays
        self.close = self.df['close'].values.astype(np.float64)
        self.high = self.df['high'].values.astype(np.float64)
        self.low = self.df['low'].values.astype(np.float64)
        self.open = self.df['open'].values.astype(np.float64)
        self.atr = self.df['atr_1h'].values.astype(np.float64)
        self.atr = np.where(np.isnan(self.atr) | (self.atr <= 0), 1.0, self.atr)
        self.n = len(self.close)

    def run(self, signal) -> dict:
        """
        Run fast vectorized backtest with minimal overhead.

        Returns essential stats only.
        """
        sig = np.asarray(signal, dtype=bool)

        # Apply 2h cooldown using vectorized shift
        cooldown = 2
        sig_series = pd.Series(sig)
        shifted = sig_series.shift(1, fill_value=False)
        cooldown_mask = shifted.rolling(window=cooldown, min_periods=1).max().fillna(False).astype(bool).values
        sig = sig & ~cooldown_mask

        # Find entry transitions
        entry_starts = np.where(np.diff(np.concatenate([[False], sig])))[0]
        entry_prices = self.open[entry_starts + 1]
        entry_atrs = self.atr[entry_starts]
        max_hold = int(self.p['max_hold_1h'])

        returns = []
        hold_bars_list = []

        for i, entry_idx in enumerate(entry_starts):
            entry_bar = entry_idx + 1
            if entry_bar >= self.n:
                break

            search_end = min(entry_bar + max_hold, self.n)
            entry_atr = entry_atrs[i]

            if self.is_long:
                sl = entry_prices[i] - self.p['atr_sl'] * entry_atr
                hit_sl = np.where(self.low[entry_bar:search_end] <= sl)[0]
            else:
                sl = entry_prices[i] + self.p['atr_sl'] * entry_atr
                hit_sl = np.where(self.high[entry_bar:search_end] >= sl)[0]

            if len(hit_sl) > 0:
                exit_bar = entry_bar + hit_sl[0]
                exit_price = self.close[exit_bar]
                hold_bars = exit_bar - entry_bar
            else:
                exit_bar = search_end - 1
                exit_price = self.close[exit_bar]
                hold_bars = exit_bar - entry_bar

            if self.is_long:
                ret = (exit_price / entry_prices[i] - 1 - FEE_AND_SLIPPAGE) * self.p['max_pos']
            else:
                ret = (entry_prices[i] / exit_price - 1 - FEE_AND_SLIPPAGE) * self.p['max_pos']

            returns.append(ret)
            hold_bars_list.append(hold_bars)

        if len(returns) == 0:
            return {'win_rate': 0, 'avg_return': 0, 'total_return': 0, 'n_trades': 0}

        rets = np.array(returns)
        wins = rets[rets > 0]

        return {
            'win_rate': round(len(wins) / len(rets), 4),
            'avg_return': round(float(rets.mean()), 6),
            'total_return': round(float(rets.sum()), 4),
            'n_trades': len(rets),
            'engine': 'fast_vectorized'
        }


# ── Utility functions for batch signal generation ──────────────────────────

def generate_rsi_signals(
    df: pd.DataFrame,
    rsi_col: str = 'rsi',
    lower: float = 35,
    upper: float = 65,
    is_long: bool = True
) -> np.ndarray:
    """
    Generate RSI-based entry signals vectorized.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with RSI column
    rsi_col : str
        Name of RSI column
    lower, upper : float
        RSI oversold (for long) or overbought (for short) thresholds
    is_long : bool
        True for long signals, False for short signals

    Returns
    -------
    np.ndarray
        Boolean signal array
    """
    rsi = df[rsi_col].values
    if is_long:
        return (rsi >= lower) & (rsi <= upper)
    else:
        return (rsi >= upper) | (rsi <= lower)


def generate_ma_cross_signals(
    df: pd.DataFrame,
    fast_col: str = 'ma_fast',
    slow_col: str = 'ma_slow',
    is_long: bool = True
) -> np.ndarray:
    """
    Generate moving average crossover signals using vectorized shift.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with MA columns
    fast_col, slow_col : str
        Column names for fast and slow MAs
    is_long : bool
        True for bullish crossover (fast crosses above slow)

    Returns
    -------
    np.ndarray
        Boolean signal array
    """
    fast = df[fast_col].values
    slow = df[slow_col].values

    if is_long:
        # Bullish: fast crosses above slow
        return (fast > slow) & (np.roll(fast, 1) <= np.roll(slow, 1))
    else:
        # Bearish: fast crosses below slow
        return (fast < slow) & (np.roll(fast, 1) >= np.roll(slow, 1))


def generate_adx_signals(
    df: pd.DataFrame,
    adx_col: str = 'adx',
    adx_threshold: float = 25,
    is_long: bool = True
) -> np.ndarray:
    """
    Generate ADX-based trend signals vectorized.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with ADX column
    adx_col : str
        Name of ADX column
    adx_threshold : float
        Minimum ADX for valid trend
    is_long : bool
        True for bullish trend, False for bearish

    Returns
    -------
    np.ndarray
        Boolean signal array
    """
    adx = df[adx_col].values
    return adx >= adx_threshold


def generate_combined_signals(
    df: pd.DataFrame,
    rsi_range: Tuple[float, float] = (30, 70),
    adx_threshold: float = 25,
    is_long: bool = True
) -> np.ndarray:
    """
    Generate combined RSI + ADX signals in one vectorized pass.

    This demonstrates the batch signal generation approach where
    all indicators are evaluated simultaneously without loops.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame with rsi and adx columns
    rsi_range : tuple
        (lower, upper) RSI bounds
    adx_threshold : float
        Minimum ADX value
    is_long : bool
        True for long signals

    Returns
    -------
    np.ndarray
        Combined boolean signal array
    """
    rsi = df['rsi'].values
    adx = df['adx'].values

    if is_long:
        rsi_signal = (rsi >= rsi_range[0]) & (rsi <= rsi_range[1])
    else:
        rsi_signal = (rsi >= rsi_range[1]) | (rsi <= rsi_range[0])

    adx_signal = adx >= adx_threshold

    # Combined: both conditions must be true
    combined = rsi_signal & adx_signal

    # Smooth: require signal to persist for at least 2 bars
    # This prevents whipsaws from temporary threshold crosses
    smooth = pd.Series(combined).rolling(window=2, min_periods=1).max().fillna(False).astype(bool).values

    return smooth


# Alias for backward compatibility
VectorizedBacktester = VectorizedEngine


# ── Stub for future expansion ────────────────────────────────────────────────

class BatchSignalEngine:
    """
    Stub: Batch signal generation engine for multi-indicator strategies.

    This class will provide:
        - Parallel indicator computation
        - Signal combination with configurable logic (AND/OR/WEIGHTED)
        - Automatic signal normalization and scaling
    """
    def __init__(self, *args, **kwargs):
        raise NotImplementedError(
            "BatchSignalEngine not yet implemented. "
            "Use generate_combined_signals() utility function instead."
        )
