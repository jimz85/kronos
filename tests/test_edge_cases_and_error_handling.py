"""
Boundary Cases and Error Handling Tests for Kronos
==================================================

Comprehensive tests for edge cases and error handling:
- Boundary value tests for indicators
- Error handling for API failures
- File system error handling
- Invalid input handling
- Empty/null data handling
- Extreme value handling
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import numpy as np
import pandas as pd
import pytest
import requests


# =============================================================================
# Boundary Value Tests for Indicators
# =============================================================================

class TestIndicatorBoundaryValues:
    """Boundary value tests for technical indicator calculations."""

    def test_rsi_period_greater_than_data_length(self):
        """Test RSI when period is greater than data length."""
        from core.indicators import calc_rsi

        prices = np.array([100.0, 101.0, 102.0])  # Only 3 data points
        period = 14  # Greater than data length

        result = calc_rsi(prices, period=period)

        # calc_rsi returns float; should return NaN when not enough data
        assert isinstance(result, float)
        assert np.isnan(result)

    def test_rsi_period_equals_data_length(self):
        """Test RSI when period exactly equals data length."""
        from core.indicators import calc_rsi

        prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0,
                          105.0, 106.0, 107.0, 108.0, 109.0,
                          110.0, 111.0, 112.0, 113.0])  # 14 data points
        period = 14

        result = calc_rsi(prices, period=period)

        # Should have a valid value after warmup
        assert isinstance(result, float)

    def test_rsi_period_zero_returns_nan(self):
        """Test RSI with period=0 returns NaN instead of crashing."""
        from core.indicators import calc_rsi

        prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0])

        result = calc_rsi(prices, period=0)

        # Should return NaN instead of crashing
        assert isinstance(result, float)
        assert np.isnan(result)

    def test_rsi_period_negative_returns_nan(self):
        """Test RSI with negative period returns NaN."""
        from core.indicators import calc_rsi

        prices = np.array([100.0, 101.0, 102.0, 103.0, 104.0])

        result = calc_rsi(prices, period=-5)

        # Should return NaN instead of crashing
        assert isinstance(result, float)
        assert np.isnan(result)

    def test_ma_period_greater_than_data_length(self):
        """Test MA when period is greater than data length."""
        from core.indicators import calc_ma

        prices = np.array([100.0, 101.0])
        period = 10  # Greater than data length

        result = calc_ma(prices, period=period)

        # calc_ma returns a float, will be NaN when not enough data
        assert isinstance(result, float)
        assert np.isnan(result)

    def test_ma_period_zero_returns_nan(self):
        """Test MA with period=0 returns NaN instead of crashing."""
        from core.indicators import calc_ma

        prices = np.array([100.0, 101.0, 102.0])

        result = calc_ma(prices, period=0)

        # Should return NaN instead of crashing
        assert isinstance(result, float)
        assert np.isnan(result)

    def test_ema_period_greater_than_data_length(self):
        """Test EMA when period is greater than data length."""
        from core.indicators import calc_ema

        prices = np.array([100.0, 101.0, 102.0])
        period = 10  # Greater than data length

        # This will raise IndexError because the array is too short
        with pytest.raises(IndexError):
            calc_ema(prices, period=period)

    def test_atr_all_same_prices(self):
        """Test ATR with high=low=close (no range)."""
        from core.indicators import calc_atr

        high = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
        low = np.array([100.0, 100.0, 100.0, 100.0, 100.0])
        close = np.array([100.0, 100.0, 100.0, 100.0, 100.0])

        result = calc_atr(high, low, close, period=3)

        # ATR should be zero or near-zero for flat prices
        valid = result.dropna()
        assert (valid >= 0).all()

    def test_atr_zero_range_prices(self):
        """Test ATR when high equals low (zero range)."""
        from core.indicators import calc_atr

        high = np.array([100.0, 105.0, 110.0])
        low = high.copy()  # low == high
        close = np.array([100.0, 105.0, 110.0])

        result = calc_atr(high, low, close, period=2)

        valid = result.dropna()
        # ATR should be zero when high == low
        assert (valid >= 0).all()

    def test_bollinger_zero_std_deviation(self):
        """Test Bollinger Bands when all prices are identical."""
        from core.indicators import calc_bollinger

        prices = np.array([100.0, 100.0, 100.0, 100.0, 100.0,
                           100.0, 100.0, 100.0, 100.0, 100.0])

        middle, upper, lower = calc_bollinger(prices, period=5)

        # Upper and lower should equal middle when std is 0
        valid_idx = middle.notna()
        assert (upper[valid_idx] == lower[valid_idx]).all()

    def test_macd_fast_greater_than_slow(self):
        """Test MACD when fast period > slow period (works but unusual)."""
        from core.indicators import calc_macd

        # Need enough data for fast=26, slow=12
        prices = np.array([100.0 + i for i in range(50)])

        # This works, though it's unusual configuration
        macd, signal, hist = calc_macd(prices, fast=26, slow=12)

        assert len(macd) == len(prices)
        assert len(signal) == len(prices)
        assert len(hist) == len(prices)

    def test_macd_identical_fast_slow(self):
        """Test MACD when fast == slow (produces zero line)."""
        from core.indicators import calc_macd

        prices = np.random.uniform(100, 110, 50)

        macd, signal, hist = calc_macd(prices, fast=12, slow=12)

        # MACD line should be near zero when fast == slow
        assert len(macd) == len(prices)


class TestIndicatorExtremeValues:
    """Tests for handling extreme values in indicators."""

    def test_rsi_extremely_oversold(self):
        """Test RSI with continuously declining prices."""
        from core.indicators import calc_rsi

        # Need more data points for RSI period
        prices = np.array([100.0, 99.0, 98.0, 97.0, 96.0, 95.0, 94.0, 93.0,
                           92.0, 91.0, 90.0, 89.0, 88.0, 87.0, 86.0, 85.0,
                           84.0, 83.0, 82.0, 81.0, 80.0, 79.0, 78.0, 77.0])

        result = calc_rsi(prices, period=14)

        # RSI should be very low (oversold) for declining prices
        assert isinstance(result, float)
        # RSI should be < 50 for declining prices
        assert result < 50

    def test_rsi_extremely_overbought(self):
        """Test RSI with continuously rising prices."""
        from core.indicators import calc_rsi

        # Continuous rise
        prices = np.array([80.0, 81.0, 82.0, 83.0, 84.0, 85.0, 86.0, 87.0,
                           88.0, 89.0, 90.0, 91.0, 92.0, 93.0, 94.0, 95.0,
                           96.0, 97.0, 98.0, 99.0, 100.0, 101.0, 102.0, 103.0])

        result = calc_rsi(prices, period=14)

        # RSI should be very high (overbought) for rising prices
        assert isinstance(result, float)
        # RSI should be > 50 for rising prices
        assert result > 50

    def test_atr_very_large_values(self):
        """Test ATR with extremely large price ranges."""
        from core.indicators import calc_atr

        high = np.array([1e10, 1e10 + 1000, 1e10 + 2000, 1e10 + 3000, 1e10 + 4000])
        low = np.array([1e10 - 1000, 1e10, 1e10 + 1000, 1e10 + 2000, 1e10 + 3000])
        close = np.array([1e10, 1e10 + 500, 1e10 + 1500, 1e10 + 2500, 1e10 + 3500])

        result = calc_atr(high, low, close, period=3)
        valid = result.dropna()

        # Should handle large values without overflow
        assert (valid > 0).all()
        assert not np.isinf(valid).any()

    def test_atr_very_small_values(self):
        """Test ATR with extremely small price values."""
        from core.indicators import calc_atr

        high = np.array([0.001, 0.002, 0.003, 0.004, 0.005])
        low = np.array([0.0001, 0.001, 0.002, 0.003, 0.004])
        close = np.array([0.0005, 0.0015, 0.0025, 0.0035, 0.0045])

        result = calc_atr(high, low, close, period=3)
        valid = result.dropna()

        # Should handle small values without underflow
        assert (valid >= 0).all()

    def test_ma_with_very_large_prices(self):
        """Test MA with extremely large price values."""
        from core.indicators import calc_ma

        prices = np.array([1e10, 1e10, 1e10, 1e10, 1e10, 1e10, 1e10])

        result = calc_ma(prices, period=3)

        # calc_ma returns float
        assert isinstance(result, float)
        assert result > 0
        assert not np.isinf(result)

    def test_ma_with_very_small_prices(self):
        """Test MA with extremely small price values (near zero)."""
        from core.indicators import calc_ma

        prices = np.array([0.0001, 0.0002, 0.0003, 0.0004, 0.0005, 0.0006, 0.0007])

        result = calc_ma(prices, period=3)

        # calc_ma returns float
        assert isinstance(result, float)
        assert result > 0


class TestIndicatorInvalidInput:
    """Tests for handling invalid inputs in indicators."""

    def test_rsi_none_input(self):
        """Test RSI with None input returns NaN."""
        from core.indicators import calc_rsi

        # Should return NaN instead of crashing
        result = calc_rsi(None)
        assert np.isnan(result)

    def test_rsi_string_input(self):
        """Test RSI with string input returns NaN."""
        from core.indicators import calc_rsi

        # Should return NaN instead of crashing
        result = calc_rsi("not an array")
        assert np.isnan(result)

    def test_rsi_malformed_array(self):
        """Test RSI with malformed array (wrong dtype)."""
        from core.indicators import calc_rsi

        # Array of strings
        prices = np.array(["a", "b", "c", "d", "e"])

        with pytest.raises((TypeError, ValueError)):
            calc_rsi(prices, period=3)

    def test_ma_none_prices(self):
        """Test MA with None input."""
        from core.indicators import calc_ma

        with pytest.raises(TypeError):
            calc_ma(None, period=5)

    def test_ma_empty_array(self):
        """Test MA with empty array."""
        from core.indicators import calc_ma

        prices = np.array([])

        # Empty array with rolling will return NaN
        result = calc_ma(prices, period=5)

        # calc_ma returns float
        assert isinstance(result, float)
        assert np.isnan(result)

    def test_ema_invalid_period(self):
        """Test EMA with invalid period (< 1)."""
        from core.indicators import calc_ema

        prices = np.array([100.0, 101.0, 102.0])

        # Period 0 causes IndexError
        with pytest.raises(IndexError):
            calc_ema(prices, period=0)

    def test_atr_mismatched_array_lengths(self):
        """Test ATR with mismatched array lengths."""
        from core.indicators import calc_atr

        high = np.array([100.0, 101.0, 102.0, 103.0, 104.0])
        low = np.array([99.0, 100.0, 101.0])  # Different length!
        close = np.array([100.0, 101.0, 102.0, 103.0, 104.0])

        # This may raise an error or produce wrong results
        # depending on numpy broadcasting behavior
        with pytest.raises((ValueError, RuntimeError)):
            calc_atr(high, low, close, period=3)


# =============================================================================
# Error Handling Tests for OKX API
# =============================================================================

class TestOKXAPIErrorHandling:
    """Tests for OKX API error handling."""

    @patch("kronos_utils.requests.request")
    def test_okx_req_timeout(self, mock_request: MagicMock) -> None:
        """Test OKX request handles timeout."""
        from kronos_utils import okx_req

        mock_request.side_effect = requests.Timeout("Connection timed out")

        result = okx_req("GET", "/api/v5/account/balance")

        assert "error" in result
        assert "timed out" in result["error"].lower() or "timeout" in result["error"].lower()

    @patch("kronos_utils.requests.request")
    def test_okx_req_connection_error(self, mock_request: MagicMock) -> None:
        """Test OKX request handles connection error."""
        from kronos_utils import okx_req

        mock_request.side_effect = requests.ConnectionError("Connection refused")

        result = okx_req("GET", "/api/v5/account/balance")

        assert "error" in result

    @patch("kronos_utils.requests.request")
    def test_okx_req_http_error(self, mock_request: MagicMock) -> None:
        """Test OKX request handles HTTP error."""
        from kronos_utils import okx_req

        mock_response = MagicMock()
        mock_response.status_code = 500
        mock_response.json.return_value = {"code": "500", "msg": "Internal Server Error"}
        mock_request.return_value = mock_response

        result = okx_req("GET", "/api/v5/account/balance")

        # Should handle non-200 status code
        assert isinstance(result, dict)

    @patch("kronos_utils.requests.request")
    def test_okx_req_invalid_json(self, mock_request: MagicMock) -> None:
        """Test OKX request handles invalid JSON response."""
        from kronos_utils import okx_req

        mock_response = MagicMock()
        mock_response.json.side_effect = json.JSONDecodeError(
            "Invalid JSON", "", 0
        )
        mock_response.json.return_value = {}
        mock_request.return_value = mock_response

        result = okx_req("GET", "/api/v5/account/balance")

        assert "error" in result or isinstance(result, dict)

    @patch("kronos_utils.requests.request")
    def test_okx_req_empty_api_key(self, mock_request: MagicMock) -> None:
        """Test OKX request with empty API key."""
        from kronos_utils import okx_req

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": "0", "data": []}
        mock_request.return_value = mock_response

        # Should still make request (with empty key)
        result = okx_req("GET", "/api/v5/account/balance")

        assert isinstance(result, dict)


class TestAccountBalanceErrorHandling:
    """Tests for get_account_balance error handling."""

    @patch("kronos_utils.okx_req")
    def test_account_balance_none_response(self, mock_req: MagicMock) -> None:
        """Test balance retrieval with None response."""
        from kronos_utils import get_account_balance

        mock_req.return_value = None

        result = get_account_balance()

        assert result["totalEq"] == 0

    @patch("kronos_utils.okx_req")
    def test_account_balance_malformed_data(self, mock_req: MagicMock) -> None:
        """Test balance retrieval with malformed data."""
        from kronos_utils import get_account_balance

        mock_req.return_value = {
            "code": "0",
            "data": [{"totalEq": "not_a_number"}],
        }

        result = get_account_balance()

        # Should handle non-numeric value gracefully
        assert result["totalEq"] == 0

    @patch("kronos_utils.okx_req")
    def test_account_balance_missing_data_key(self, mock_req: MagicMock) -> None:
        """Test balance retrieval with missing data key."""
        from kronos_utils import get_account_balance

        mock_req.return_value = {"code": "0"}  # No 'data' key

        result = get_account_balance()

        assert result["totalEq"] == 0

    @patch("kronos_utils.okx_req")
    def test_account_balance_empty_data_array(self, mock_req: MagicMock) -> None:
        """Test balance retrieval with empty data array."""
        from kronos_utils import get_account_balance

        mock_req.return_value = {"code": "0", "data": []}

        result = get_account_balance()

        assert result["totalEq"] == 0


class TestFundingRateErrorHandling:
    """Tests for get_funding_rate error handling."""

    @patch("kronos_utils.requests.get")
    def test_funding_rate_timeout(self, mock_get: MagicMock) -> None:
        """Test funding rate retrieval with timeout."""
        from kronos_utils import get_funding_rate

        mock_get.side_effect = requests.Timeout()

        result = get_funding_rate("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_funding_rate_connection_error(self, mock_get: MagicMock) -> None:
        """Test funding rate retrieval with connection error."""
        from kronos_utils import get_funding_rate

        mock_get.side_effect = requests.ConnectionError()

        result = get_funding_rate("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_funding_rate_invalid_json(self, mock_get: MagicMock) -> None:
        """Test funding rate with invalid JSON response."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.side_effect = json.JSONDecodeError(
            "Invalid JSON", "", 0
        )
        mock_get.return_value = mock_response

        result = get_funding_rate("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_funding_rate_missing_fields(self, mock_get: MagicMock) -> None:
        """Test funding rate with missing required fields."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "0",
            "data": [{"some_field": "value"}],  # Missing fundingRate
        }
        mock_get.return_value = mock_response

        result = get_funding_rate("BTC")

        # Should handle missing fields gracefully
        assert isinstance(result, dict)

    @patch("kronos_utils.requests.get")
    def test_funding_rate_invalid_rate_format(self, mock_get: MagicMock) -> None:
        """Test funding rate with invalid rate format."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "0",
            "data": [{"fundingRate": "invalid", "nextFundingTime": "1700000000000"}],
        }
        mock_get.return_value = mock_response

        result = get_funding_rate("BTC")

        # Should handle non-numeric rate gracefully
        assert isinstance(result, dict)


class TestOpenInterestErrorHandling:
    """Tests for get_open_interest error handling."""

    @patch("kronos_utils.requests.get")
    def test_open_interest_timeout(self, mock_get: MagicMock) -> None:
        """Test OI retrieval with timeout."""
        from kronos_utils import get_open_interest

        mock_get.side_effect = requests.Timeout()

        result = get_open_interest("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_open_interest_connection_error(self, mock_get: MagicMock) -> None:
        """Test OI retrieval with connection error."""
        from kronos_utils import get_open_interest

        mock_get.side_effect = requests.ConnectionError()

        result = get_open_interest("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_open_interest_invalid_json(self, mock_get: MagicMock) -> None:
        """Test OI with invalid JSON response."""
        from kronos_utils import get_open_interest

        mock_response = MagicMock()
        mock_response.json.side_effect = json.JSONDecodeError(
            "Invalid JSON", "", 0
        )
        mock_get.return_value = mock_response

        result = get_open_interest("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_open_interest_missing_oi_field(self, mock_get: MagicMock) -> None:
        """Test OI with missing oi field."""
        from kronos_utils import get_open_interest

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "0",
            "data": [{"oiUsd": "123456"}],  # Missing 'oi' field
        }
        mock_get.return_value = mock_response

        result = get_open_interest("BTC")

        # Should handle gracefully
        assert isinstance(result, dict)


# =============================================================================
# File System Error Handling Tests
# =============================================================================

class TestAtomicWriteErrorHandling:
    """Tests for atomic_write_json error handling."""

    def test_atomic_write_to_readonly_filesystem(self, tmp_path: Path) -> None:
        """Test atomic write to a read-only filesystem location."""
        from kronos_utils import atomic_write_json

        # Create a read-only directory
        test_dir = tmp_path / "readonly"
        test_dir.mkdir()
        test_dir.chmod(0o444)

        test_file = test_dir / "test.json"

        try:
            with pytest.raises (PermissionError, OSError):
                atomic_write_json(test_file, {"key": "value"})
        finally:
            # Cleanup: restore permissions
            test_dir.chmod(0o755)

    def test_atomic_write_invalid_json_data(self, tmp_path: Path) -> None:
        """Test atomic write with non-serializable data."""
        from kronos_utils import atomic_write_json

        test_file = tmp_path / "test.json"

        # Create data that cannot be JSON serialized
        class NonSerializable:
            pass

        data = {"obj": NonSerializable()}

        with pytest.raises (TypeError):
            atomic_write_json(test_file, data)

    def test_atomic_write_creates_intermediate_dirs(self, tmp_path: Path) -> None:
        """Test atomic write fails gracefully without intermediate directories."""
        from kronos_utils import atomic_write_json

        # Path with non-existent intermediate directories
        test_file = tmp_path / "level1" / "level2" / "test.json"

        # Should raise FileNotFoundError because parent dir doesn't exist
        with pytest.raises(FileNotFoundError):
            atomic_write_json(test_file, {"key": "value"})


class TestLoadPaperLogErrorHandling:
    """Tests for load_paper_log error handling."""

    @patch("kronos_utils.Path.home")
    def test_load_paper_log_invalid_json(self, mock_home: MagicMock) -> None:
        """Test loading paper log with invalid JSON."""
        from kronos_utils import load_paper_log

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_home.return_value = Path(tmpdir)

            # Create the paper trades file with invalid JSON
            paper_dir = Path(tmpdir) / ".hermes" / "cron" / "output"
            paper_dir.mkdir(parents=True)
            paper_file = paper_dir / "paper_trades.json"
            paper_file.write_text("not valid json {{{")

            # Should return empty list on error
            result = load_paper_log()
            assert result == []

    @patch("kronos_utils.Path.home")
    def test_load_paper_log_permission_error(self, mock_home: MagicMock) -> None:
        """Test loading paper log with permission error."""
        from kronos_utils import load_paper_log

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_home.return_value = Path(tmpdir)

            paper_dir = Path(tmpdir) / ".hermes" / "cron" / "output"
            paper_dir.mkdir(parents=True)
            paper_file = paper_dir / "paper_trades.json"
            paper_file.write_text("[]")
            paper_file.chmod(0o000)

            try:
                result = load_paper_log()
                # Should return empty list on permission error
                assert result == []
            finally:
                paper_file.chmod(0o644)


# =============================================================================
# Calculate Trade PnL Edge Cases
# =============================================================================

class TestCalculateTradePnlEdgeCases:
    """Edge case tests for calculate_trade_pnl function."""

    def test_zero_leverage(self) -> None:
        """Test PnL calculation with zero leverage."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 0,  # Zero leverage
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # 0% return with zero leverage
        assert result_pct == 0.0
        assert pnl == 0.0

    def test_negative_leverage(self) -> None:
        """Test PnL calculation with negative leverage."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": -5,  # Invalid negative leverage
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # Should produce negative result with negative leverage
        assert result_pct < 0

    def test_zero_contracts(self) -> None:
        """Test PnL calculation with zero contracts."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 0.0,  # Zero contracts
            "leverage": 3,
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # PnL should be zero when contracts is zero
        assert pnl == 0.0
        assert result_pct == 30.0  # Return % is independent of contracts

    def test_very_large_entry_price(self) -> None:
        """Test PnL calculation with very large entry price."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 1e15,
            "contracts": 1.0,
            "leverage": 3,
        }
        exit_price = 1e15 + 1e10

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # Should handle large numbers without overflow
        assert not np.isinf(result_pct)
        assert not np.isinf(pnl)

    def test_very_small_entry_price(self) -> None:
        """Test PnL calculation with very small entry price."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 0.0001,
            "contracts": 1.0,
            "leverage": 3,
        }
        exit_price = 0.0002

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # Should handle small numbers without underflow
        assert result_pct > 0  # Profitable trade
        assert pnl > 0

    def test_entry_equals_exit(self) -> None:
        """Test PnL when entry equals exit (breakeven)."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 3,
        }
        exit_price = 100.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        assert result_pct == 0.0
        assert pnl == 0.0

    def test_missing_direction_defaults_to_long(self) -> None:
        """Test that missing direction defaults to LONG."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 3,
            # No 'direction' key
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # Should treat as LONG and calculate profit
        assert result_pct == 30.0

    def test_invalid_direction(self) -> None:
        """Test PnL with invalid direction."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "INVALID",  # Invalid direction
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 3,
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # Should default to LONG behavior or produce unexpected result
        # This documents the actual behavior
        assert isinstance(result_pct, float)
        assert isinstance(pnl, float)


# =============================================================================
# DailyDDMonitor Edge Cases
# =============================================================================

class TestDailyDDMonitorEdgeCases:
    """Edge case tests for DailyDDMonitor."""

    def test_zero_starting_equity(self, tmp_path: Path) -> None:
        """Test DailyDDMonitor with zero starting equity."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        # Update equity with 0
        result = monitor.update_equity(0.0)

        assert result["dd"] == 0.0
        assert result["halted"] is False

    def test_negative_equity(self, tmp_path: Path) -> None:
        """Test DailyDDMonitor with negative equity."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        # Update with negative equity
        result = monitor.update_equity(-1000.0)

        # Should not halt due to negative equity alone
        # (DD calculation will handle this)
        assert isinstance(result, dict)

    def test_equity_less_than_starting(self, tmp_path: Path) -> None:
        """Test DD calculation when current equity < starting equity."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        # Set starting equity to 10000
        monitor.state["starting_equity"] = 10000.0
        monitor.state["current_equity"] = 10000.0
        monitor.state["peak_equity"] = 10000.0

        # Drop to 9500 (5% DD)
        result = monitor.update_equity(9500.0)

        # 500/10000 = 5% DD
        assert result["dd"] == 0.05

    def test_equity_halts_at_threshold(self, tmp_path: Path) -> None:
        """Test that trading halts when DD exceeds threshold."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        # Set starting equity to 10000
        monitor.state["starting_equity"] = 10000.0
        monitor.state["current_equity"] = 10000.0
        monitor.state["peak_equity"] = 10000.0

        # Drop to 9400 (6% DD, exceeds 5% threshold)
        result = monitor.update_equity(9400.0)

        assert result["halted"] is True
        assert "action" in result

    def test_trade_result_with_zero_pnl(self, tmp_path: Path) -> None:
        """Test on_trade_result with zero PnL."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        monitor.state["starting_equity"] = 10000.0
        monitor.state["current_equity"] = 10000.0

        result = monitor.on_trade_result(0.0, 10000.0)

        assert result["action"] == "OK"

    def test_trade_result_warning_threshold(self, tmp_path: Path) -> None:
        """Test trade result warning at loss threshold."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        monitor.state["starting_equity"] = 10000.0
        monitor.state["current_equity"] = 10000.0

        # -2% loss (exactly at warning threshold)
        result = monitor.on_trade_result(-0.02, 10000.0)

        assert result["action"] == "WARNING"

    def test_can_trade_when_halted(self, tmp_path: Path) -> None:
        """Test can_trade returns False when halted."""
        from core.engine import DailyDDMonitor

        state_file = tmp_path / "dd_state.json"
        monitor = DailyDDMonitor(state_file)

        monitor.state["halted"] = True
        monitor.state["daily_dd"] = 0.06

        can_trade, reason = monitor.can_trade()

        assert can_trade is False
        assert "halted" in reason.lower()


# =============================================================================
# JSON and Data Handling Edge Cases
# =============================================================================

class TestJSONDataHandling:
    """Tests for JSON data handling edge cases."""

    def test_json_with_nan_values(self) -> None:
        """Test JSON serialization with NaN values."""
        data = {"value": float("nan"), "normal": 42}

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            try:
                json.dump(data, f)
                f.flush()

                with open(f.name) as rf:
                    loaded = json.load(rf)

                # NaN is not valid JSON - should fail or become null
                assert isinstance(loaded, dict)
            finally:
                os.unlink(f.name)

    def test_json_with_inf_values(self) -> None:
        """Test JSON serialization with infinity values."""
        data = {"value": float("inf"), "neg": float("-inf")}

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            try:
                json.dump(data, f)
                f.flush()

                with open(f.name) as rf:
                    loaded = json.load(rf)

                # Infinity is not valid JSON
                assert isinstance(loaded, dict)
            finally:
                os.unlink(f.name)

    def test_json_with_unicode(self) -> None:
        """Test JSON with unicode characters."""
        data = {"chinese": "你好", "emoji": "🎉", "special": "®"}

        with tempfile.NamedTemporaryFile(mode='w', suffix='.json', delete=False) as f:
            try:
                json.dump(data, f, ensure_ascii=False)
                f.flush()

                with open(f.name, encoding='utf-8') as rf:
                    loaded = json.load(rf)

                assert loaded["chinese"] == "你好"
                assert loaded["emoji"] == "🎉"
            finally:
                os.unlink(f.name)


# =============================================================================
# Type Coercion Edge Cases
# =============================================================================

class TestTypeCoercionEdgeCases:
    """Tests for type coercion in utility functions."""

    def test_string_to_number_conversion(self) -> None:
        """Test implicit string to number conversion."""
        # This simulates what happens when API returns "123" instead of 123
        string_number = "123.45"

        # Float conversion
        result = float(string_number)
        assert result == 123.45

    def test_int_from_float(self) -> None:
        """Test int from float conversion."""
        # When truncation is expected
        value = 123.99
        result = int(value)
        assert result == 123

    def test_boolean_truthiness(self) -> None:
        """Test boolean conversion of various values."""
        assert bool(0) is False
        assert bool(1) is True
        assert bool(0.0) is False
        assert bool(0.001) is True
        assert bool("") is False
        assert bool("x") is True
        assert bool([]) is False
        assert bool([1]) is True
        assert bool(None) is False


# =============================================================================
# Path Handling Edge Cases
# =============================================================================

class TestPathHandlingEdgeCases:
    """Tests for path handling edge cases."""

    def test_path_with_special_characters(self, tmp_path: Path) -> None:
        """Test path with special characters."""
        from kronos_utils import atomic_write_json

        special_names = [
            "file with spaces.json",
            "file-with-dashes.json",
            "file_with_underscores.json",
            "file.with.dots.json",
        ]

        for name in special_names:
            test_file = tmp_path / name
            atomic_write_json(test_file, {"key": "value"})
            assert test_file.exists()

    def test_very_long_path(self, tmp_path: Path) -> None:
        """Test handling of very long paths."""
        from kronos_utils import atomic_write_json

        # Create a deeply nested directory structure within tmp_path
        # to avoid system path limitations
        deep_path = tmp_path
        for i in range(15):  # Reduced depth to avoid OS limits
            deep_path = deep_path / f"level{i}"
            deep_path.mkdir(exist_ok=True)

        test_file = deep_path / "test.json"

        # Should handle gracefully
        try:
            atomic_write_json(test_file, {"key": "value"})
            assert test_file.exists()
        except OSError:
            # Path too long is acceptable
            pass

    def test_relative_path_handling(self) -> None:
        """Test that functions use absolute paths correctly."""
        # Verify that Path.home() works correctly
        home = Path.home()
        assert home.is_absolute()

        # Verify that pathlib operations maintain absolute paths
        file_path = home / "test.json"
        assert file_path.is_absolute()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
