#!/usr/bin/env python3
"""
feature_importance.py - Feature importance analyzer for Kronos
===============================================================

Feature importance analysis module for evaluating and weighting
predictive factors in the Kronos trading system.

Key Functions:
    - compute_ic_series(): Rolling IC calculation for factor evaluation
    - compute_factor_statistics(): Individual factor statistics
    - analyze_all_factors(): Batch analysis of multiple factors
    - get_tradeable_factors(): Filter factors by tradeability criteria
    - generate_weight_recommendation(): IC-based weight generation

Version: 1.0.0
"""

import logging
from typing import Dict, List, Optional, Tuple, Union

import numpy as np
import pandas as pd

logger = logging.getLogger('kronos.feature_importance')

# Minimum IC threshold for factor to be considered predictive
DEFAULT_IC_THRESHOLD = 0.02

# Minimum number of observations for statistical significance
DEFAULT_MIN_OBS = 30

# Default rolling window for IC calculation
DEFAULT_IC_WINDOW = 20


def compute_ic_series(
    factor: pd.Series,
    returns: pd.Series,
    window: int = DEFAULT_IC_WINDOW,
    method: str = 'spearman'
) -> pd.Series:
    """
    Compute rolling Information Coefficient (IC) between factor and returns.

    The IC measures how well the factor predicts future returns. Higher absolute
    IC values indicate stronger predictive power.

    Args:
        factor: Factor values (e.g., signal strength, technical indicator)
        returns: Future returns to predict
        window: Rolling window size for IC calculation (default: 20)
        method: Correlation method - 'spearman' (rank) or 'pearson' (linear)

    Returns:
        pd.Series: Rolling IC values indexed by date

    Example:
        >>> ic_series = compute_ic_series(rsi_signal, future_returns, window=20)
        >>> mean_ic = ic_series.mean()
        >>> print(f"Mean IC: {mean_ic:.4f}")
    """
    # Align indices
    combined = pd.DataFrame({'factor': factor, 'returns': returns}).dropna()

    if len(combined) < window:
        logger.warning(
            f"Insufficient data for IC calculation: {len(combined)} obs, "
            f"need at least {window}"
        )
        return pd.Series(dtype=float)

    if method == 'spearman':
        ic_series = combined['factor'].rolling(window).corr(
            combined['returns'], method='spearman'
        )
    elif method == 'pearson':
        ic_series = combined['factor'].rolling(window).corr(combined['returns'])
    else:
        raise ValueError(f"Unknown method: {method}. Use 'spearman' or 'pearson'")

    return ic_series


def compute_factor_statistics(
    factor: pd.Series,
    returns: pd.Series,
    ic_window: int = DEFAULT_IC_WINDOW,
    min_obs: int = DEFAULT_MIN_OBS
) -> Dict[str, Union[float, int]]:
    """
    Compute comprehensive statistics for a single factor.

    Args:
        factor: Factor values
        returns: Future returns
        ic_window: Window for rolling IC calculation
        min_obs: Minimum observations required

    Returns:
        dict: Factor statistics including:
            - mean_ic: Mean Information Coefficient
            - std_ic: Standard deviation of IC
            - ic_ir: Information Ratio (mean/std)
            - win_rate: Percentage of periods with positive IC
            - n_observations: Total observations used
            - decay_rate: IC autocorrelation (persistence)
    """
    combined = pd.DataFrame({'factor': factor, 'returns': returns}).dropna()
    n_obs = len(combined)

    if n_obs < min_obs:
        logger.warning(f"Insufficient observations: {n_obs} < {min_obs}")
        return {
            'mean_ic': np.nan,
            'std_ic': np.nan,
            'ic_ir': np.nan,
            'win_rate': np.nan,
            'n_observations': n_obs,
            'decay_rate': np.nan
        }

    # Compute rolling IC series
    ic_series = compute_ic_series(factor, returns, window=ic_window).dropna()

    if len(ic_series) == 0:
        return {
            'mean_ic': np.nan,
            'std_ic': np.nan,
            'ic_ir': np.nan,
            'win_rate': np.nan,
            'n_observations': n_obs,
            'decay_rate': np.nan
        }

    # Calculate statistics
    mean_ic = ic_series.mean()
    std_ic = ic_series.std()
    ic_ir = mean_ic / (std_ic + 1e-10)  # Information Ratio

    # Win rate: percentage of periods with positive IC
    win_rate = (ic_series > 0).sum() / len(ic_series)

    # Decay rate: autocorrelation of IC (measures persistence)
    if len(ic_series) > 1:
        decay_rate = ic_series.autocorr(lag=1)
    else:
        decay_rate = np.nan

    return {
        'mean_ic': mean_ic,
        'std_ic': std_ic,
        'ic_ir': ic_ir,
        'win_rate': win_rate,
        'n_observations': n_obs,
        'decay_rate': decay_rate
    }


def analyze_all_factors(
    factor_data: pd.DataFrame,
    returns: pd.Series,
    ic_window: int = DEFAULT_IC_WINDOW,
    min_obs: int = DEFAULT_MIN_OBS,
    ic_threshold: float = DEFAULT_IC_THRESHOLD
) -> pd.DataFrame:
    """
    Analyze all factors in a DataFrame and return statistics.

    Args:
        factor_data: DataFrame with factors as columns
        returns: Future returns series
        ic_window: Window for rolling IC calculation
        min_obs: Minimum observations required per factor
        ic_threshold: Minimum IC threshold for considering a factor valid

    Returns:
        pd.DataFrame: Factor statistics with factors as index
    """
    results = []

    for col in factor_data.columns:
        factor = factor_data[col]
        stats = compute_factor_statistics(
            factor, returns, ic_window=ic_window, min_obs=min_obs
        )
        stats['factor_name'] = col
        stats['meets_threshold'] = (
            abs(stats['mean_ic']) >= ic_threshold if not np.isnan(stats['mean_ic']) else False
        )
        results.append(stats)

    results_df = pd.DataFrame(results)
    results_df = results_df.set_index('factor_name')

    # Sort by absolute IC (predictive power)
    results_df = results_df.sort_values('mean_ic', key=abs, ascending=False)

    logger.info(
        f"Analyzed {len(results_df)} factors. "
        f"{results_df['meets_threshold'].sum()} meet IC threshold."
    )

    return results_df


def get_tradeable_factors(
    factor_stats: pd.DataFrame,
    min_ic: float = DEFAULT_IC_THRESHOLD,
    min_ir: float = 0.1,
    min_win_rate: float = 0.45,
    min_obs: int = DEFAULT_MIN_OBS
) -> pd.DataFrame:
    """
    Filter factors by tradeability criteria.

    A tradeable factor should have:
    1. Sufficient predictive power (IC >= min_ic)
    2. Good risk-adjusted returns (IR >= min_ir)
    3. Consistent performance (win_rate >= min_win_rate)
    4. Enough observations for statistical significance

    Args:
        factor_stats: DataFrame from analyze_all_factors()
        min_ic: Minimum mean IC threshold
        min_ir: Minimum Information Ratio
        min_win_rate: Minimum win rate (0.45 = 45%)
        min_obs: Minimum number of observations

    Returns:
        pd.DataFrame: Filtered factor statistics
    """
    filtered = factor_stats[
        (abs(factor_stats['mean_ic']) >= min_ic) &
        (factor_stats['ic_ir'] >= min_ir) &
        (factor_stats['win_rate'] >= min_win_rate) &
        (factor_stats['n_observations'] >= min_obs)
    ].copy()

    # Sort by IC * IR (combined score)
    filtered['composite_score'] = abs(filtered['mean_ic']) * filtered['ic_ir']
    filtered = filtered.sort_values('composite_score', ascending=False)

    logger.info(
        f"Filtered {len(filtered)} tradeable factors from {len(factor_stats)} "
        f"total factors (IC>={min_ic}, IR>={min_ir}, WR>={min_win_rate})"
    )

    return filtered


def generate_weight_recommendation(
    factor_stats: pd.DataFrame,
    method: str = 'ic_weighted',
    normalization: str = 'softmax',
    temperature: float = 1.0
) -> Dict[str, float]:
    """
    Generate weight recommendations based on IC performance.

    Args:
        factor_stats: DataFrame from analyze_all_factors() or filtered
        method: Weighting method:
            - 'ic_weighted': Weight proportional to absolute IC
            - 'ir_weighted': Weight proportional to Information Ratio
            - 'composite': Combined IC * IR score
        normalization: Normalization method:
            - 'softmax': Softmax normalization (temperature-controlled)
            - 'rank': Rank-based weights
            - 'zscore': Z-score normalization
        temperature: Temperature for softmax (higher = more uniform)

    Returns:
        dict: Factor weights (sums to 1.0)

    Example:
        >>> weights = generate_weight_recommendation(factor_stats, method='ic_weighted')
        >>> print(weights)
        {'factor1': 0.35, 'factor2': 0.45, 'factor3': 0.20}
    """
    if len(factor_stats) == 0:
        logger.warning("No factors provided for weight generation")
        return {}

    # Extract relevant columns
    ic_values = factor_stats['mean_ic'].values
    ir_values = factor_stats['ic_ir'].values
    factor_names = factor_stats.index.tolist()

    # Compute base scores based on method
    if method == 'ic_weighted':
        scores = np.abs(ic_values)
    elif method == 'ir_weighted':
        scores = ir_values
    elif method == 'composite':
        scores = np.abs(ic_values) * ir_values
    else:
        raise ValueError(f"Unknown method: {method}")

    # Handle NaN and negative values
    scores = np.nan_to_num(scores, nan=0.0)
    scores = np.maximum(scores, 0.0)  # Ensure non-negative

    if np.sum(scores) == 0:
        logger.warning("All scores are zero, using uniform weights")
        weights = np.ones(len(scores)) / len(scores)
    else:
        # Normalize based on specified method
        if normalization == 'softmax':
            # Softmax with temperature
            exp_scores = np.exp(scores / temperature)
            weights = exp_scores / exp_scores.sum()
        elif normalization == 'rank':
            # Rank-based: weight proportional to rank
            ranks = np.argsort(np.argsort(scores)) + 1
            weights = ranks / ranks.sum()
        elif normalization == 'zscore':
            # Z-score normalization
            z_scores = (scores - scores.mean()) / (scores.std() + 1e-10)
            z_scores = np.maximum(z_scores, 0)  # Floor negative values
            weights = z_scores / z_scores.sum()
        else:
            raise ValueError(f"Unknown normalization: {normalization}")

    # Build result dictionary
    weight_dict = {
        factor: float(weight)
        for factor, weight in zip(factor_names, weights)
    }

    # Verify weights sum to 1
    total = sum(weight_dict.values())
    if abs(total - 1.0) > 1e-6:
        logger.warning(f"Weights don't sum to 1.0: {total}, normalizing")
        weight_dict = {k: v / total for k, v in weight_dict.items()}

    return weight_dict


def compute_ic_decay(
    factor: pd.Series,
    returns: pd.Series,
    max_lag: int = 10
) -> Dict[int, float]:
    """
    Compute IC decay over different lag periods.

    Useful for understanding how quickly a factor's predictive
    power diminishes over time.

    Args:
        factor: Factor values
        returns: Future returns
        max_lag: Maximum lag periods to test

    Returns:
        dict: IC values by lag period
    """
    ic_by_lag = {}

    for lag in range(1, max_lag + 1):
        lagged_returns = returns.shift(-lag)
        combined = pd.DataFrame({
            'factor': factor,
            'returns': lagged_returns
        }).dropna()

        if len(combined) >= 10:
            ic = combined['factor'].corr(combined['returns'], method='spearman')
            ic_by_lag[lag] = ic
        else:
            ic_by_lag[lag] = np.nan

    return ic_by_lag


def compute_quantile_returns(
    factor: pd.Series,
    returns: pd.Series,
    n_quantiles: int = 5
) -> pd.DataFrame:
    """
    Compute returns by factor quantiles.

    Shows how returns are distributed across different levels
    of the factor, useful for understanding factor behavior.

    Args:
        factor: Factor values
        returns: Future returns
        n_quantiles: Number of quantile buckets

    Returns:
        pd.DataFrame: Mean returns and counts per quantile
    """
    combined = pd.DataFrame({'factor': factor, 'returns': returns}).dropna()

    if len(combined) < n_quantiles:
        logger.warning(f"Insufficient data for {n_quantiles} quantiles")
        return pd.DataFrame()

    combined['quantile'] = pd.qcut(
        combined['factor'], q=n_quantiles, labels=False, duplicates='drop'
    )

    stats = combined.groupby('quantile')['returns'].agg(['mean', 'std', 'count'])
    stats.columns = ['mean_return', 'std_return', 'count']

    return stats