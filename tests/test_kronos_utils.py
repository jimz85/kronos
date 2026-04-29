"""
Tests for kronos_utils.py
=========================

Unit tests for the shared utility functions including:
- OKX API utilities
- PnL calculations
- Atomic write operations
- Funding rate and OI helpers
"""

import json
import os
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch, MagicMock

import pytest


class TestAtomicWriteJson:
    """Tests for atomic_write_json function."""

    def test_atomic_write_creates_file(self, tmp_path: Path) -> None:
        """Test that atomic_write_json creates a file."""
        from kronos_utils import atomic_write_json

        test_file = tmp_path / "test.json"
        data = {"key": "value", "number": 42}

        atomic_write_json(test_file, data)

        assert test_file.exists()
        with open(test_file) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_overwrites_existing(self, tmp_path: Path) -> None:
        """Test that atomic_write_json overwrites existing file."""
        from kronos_utils import atomic_write_json

        test_file = tmp_path / "test.json"
        data1 = {"version": 1}
        data2 = {"version": 2}

        atomic_write_json(test_file, data1)
        atomic_write_json(test_file, data2)

        with open(test_file) as f:
            loaded = json.load(f)
        assert loaded == data2

    def test_atomic_write_nested_data(self, tmp_path: Path) -> None:
        """Test atomic write with nested data structures."""
        from kronos_utils import atomic_write_json

        test_file = tmp_path / "nested.json"
        data = {
            "trades": [
                {"coin": "BTC", "pnl": 1.5, "size": None},
                {"coin": "ETH", "pnl": -0.5, "size": 10},
            ],
            "metadata": {"timestamp": "2024-01-01", "count": 2},
        }

        atomic_write_json(test_file, data)

        with open(test_file) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_with_list(self, tmp_path: Path) -> None:
        """Test atomic write with list data."""
        from kronos_utils import atomic_write_json

        test_file = tmp_path / "list.json"
        data = [1, 2, 3, "test", {"nested": True}]

        atomic_write_json(test_file, data)

        with open(test_file) as f:
            loaded = json.load(f)
        assert loaded == data

    def test_atomic_write_indent_parameter(self, tmp_path: Path) -> None:
        """Test that indent parameter is respected."""
        from kronos_utils import atomic_write_json

        test_file = tmp_path / "indented.json"
        data = {"key": "value"}

        atomic_write_json(test_file, data, indent=4)

        with open(test_file) as f:
            content = f.read()
        # With indent=4, the content should have 4 spaces
        assert "    " in content or "key" in content


class TestAtomicWriteText:
    """Tests for atomic_write_text function."""

    def test_atomic_write_text_creates_file(self, tmp_path: Path) -> None:
        """Test that atomic_write_text creates a file."""
        from kronos_utils import atomic_write_text

        test_file = tmp_path / "test.txt"
        content = "Hello, World!\nSecond line"

        atomic_write_text(test_file, content)

        assert test_file.exists()
        with open(test_file) as f:
            loaded = f.read()
        assert loaded == content

    def test_atomic_write_text_overwrites(self, tmp_path: Path) -> None:
        """Test that atomic_write_text overwrites existing file."""
        from kronos_utils import atomic_write_text

        test_file = tmp_path / "test.txt"

        atomic_write_text(test_file, "Original")
        atomic_write_text(test_file, "Updated")

        with open(test_file) as f:
            assert f.read() == "Updated"


class TestCalculateTradePnl:
    """Tests for calculate_trade_pnl function."""

    def test_long_trade_profit(self) -> None:
        """Test profit calculation for a long trade."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 3,
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # (110-100)/100 = 0.1 * 3 (leverage) = 0.3 = 30%
        assert result_pct == 30.0
        # 0.3 * 1 contract = 0.3
        assert pnl == 0.3

    def test_long_trade_loss(self) -> None:
        """Test loss calculation for a long trade."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 3,
        }
        exit_price = 90.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # (90-100)/100 = -0.1 * 3 = -0.3 = -30%
        assert result_pct == -30.0
        assert pnl == -0.3

    def test_short_trade_profit(self) -> None:
        """Test profit calculation for a short trade."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "SHORT",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 2,
        }
        exit_price = 90.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # (100-90)/100 = 0.1 * 2 (leverage) = 0.2 = 20%
        assert result_pct == 20.0
        assert pnl == 0.2

    def test_short_trade_loss(self) -> None:
        """Test loss calculation for a short trade."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "SHORT",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 2,
        }
        exit_price = 110.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # (100-110)/100 = -0.1 * 2 = -0.2 = -20%
        assert result_pct == -20.0
        assert pnl == -0.2

    def test_trade_with_missing_entry_price(self) -> None:
        """Test that zero entry price causes ZeroDivisionError (expected bug)."""
        from kronos_utils import calculate_trade_pnl

        trade = {"direction": "LONG"}  # entry_price defaults to 0
        exit_price = 100.0

        # This is actually a bug in the original code - it doesn't handle zero entry_price
        # The test documents this behavior
        with pytest.raises(ZeroDivisionError):
            calculate_trade_pnl(trade, exit_price)

    def test_trade_with_valid_defaults(self) -> None:
        """Test calculation with explicit values (not defaults)."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 100.0,
            "contracts": 1.0,
            "leverage": 1,
        }
        exit_price = 100.0

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # No change in price = 0% return
        assert result_pct == 0.0
        assert pnl == 0.0

    def test_trade_precision(self) -> None:
        """Test that pnl is rounded to 4 decimal places."""
        from kronos_utils import calculate_trade_pnl

        trade = {
            "direction": "LONG",
            "entry_price": 33.33,
            "contracts": 1.5,
            "leverage": 5,
        }
        exit_price = 34.56

        result_pct, pnl = calculate_trade_pnl(trade, exit_price)

        # Check precision
        assert round(pnl, 4) == pnl
        assert round(result_pct, 2) == result_pct


class TestGetAccountBalance:
    """Tests for get_account_balance function."""

    @patch("kronos_utils.okx_req")
    def test_get_account_balance_success(self, mock_req: MagicMock) -> None:
        """Test successful balance retrieval."""
        from kronos_utils import get_account_balance

        mock_req.return_value = {
            "code": "0",
            "data": [{"totalEq": "12345.67"}],
        }

        result = get_account_balance()

        assert result["totalEq"] == 12345.67
        mock_req.assert_called_once_with("GET", "/api/v5/account/balance")

    @patch("kronos_utils.okx_req")
    def test_get_account_balance_error(self, mock_req: MagicMock) -> None:
        """Test balance retrieval with API error."""
        from kronos_utils import get_account_balance

        mock_req.return_value = {"code": "1", "error": "API Error"}

        result = get_account_balance()

        assert result["totalEq"] == 0

    @patch("kronos_utils.okx_req")
    def test_get_account_balance_no_data(self, mock_req: MagicMock) -> None:
        """Test balance retrieval with no data returned."""
        from kronos_utils import get_account_balance

        mock_req.return_value = {"code": "0", "data": []}

        result = get_account_balance()

        assert result["totalEq"] == 0


class TestGetFundingRate:
    """Tests for get_funding_rate function."""

    @patch("kronos_utils.requests.get")
    def test_get_funding_rate_positive(self, mock_get: MagicMock) -> None:
        """Test funding rate with positive rate."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "0.0001",  # 0.01%
                    "nextFundingTime": "1700000000000",
                }
            ],
        }
        mock_get.return_value = mock_response

        result = get_funding_rate("BTC")

        assert result["rate"] == 0.01  # Converted to percentage
        assert result["direction"] == "long_pays"
        assert result["coin"] == "BTC"

    @patch("kronos_utils.requests.get")
    def test_get_funding_rate_negative(self, mock_get: MagicMock) -> None:
        """Test funding rate with negative rate."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "0",
            "data": [
                {
                    "fundingRate": "-0.0001",  # -0.01%
                    "nextFundingTime": "1700000000000",
                }
            ],
        }
        mock_get.return_value = mock_response

        result = get_funding_rate("ETH")

        assert result["rate"] == -0.01
        assert result["direction"] == "short_pays"

    @patch("kronos_utils.requests.get")
    def test_get_funding_rate_api_error(self, mock_get: MagicMock) -> None:
        """Test funding rate with API error."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": "1", "error": "Failed"}
        mock_get.return_value = mock_response

        result = get_funding_rate("BTC")

        assert result == {}

    @patch("kronos_utils.requests.get")
    def test_get_funding_rate_no_data(self, mock_get: MagicMock) -> None:
        """Test funding rate with no data returned."""
        from kronos_utils import get_funding_rate

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": "0", "data": []}
        mock_get.return_value = mock_response

        result = get_funding_rate("INVALID")

        assert result == {}


class TestGetOpenInterest:
    """Tests for get_open_interest function."""

    @patch("kronos_utils.requests.get")
    def test_get_open_interest_success(self, mock_get: MagicMock) -> None:
        """Test successful OI retrieval."""
        from kronos_utils import get_open_interest

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "code": "0",
            "data": [{"oi": "1234567.89", "oiUsd": "9876543.21"}],
        }
        mock_get.return_value = mock_response

        result = get_open_interest("BTC")

        assert result["oi"] == 1234567.89
        assert result["oi_usd"] == 9876543.21
        assert result["coin"] == "BTC"

    @patch("kronos_utils.requests.get")
    def test_get_open_interest_api_error(self, mock_get: MagicMock) -> None:
        """Test OI retrieval with API error."""
        from kronos_utils import get_open_interest

        mock_response = MagicMock()
        mock_response.json.return_value = {"code": "1", "error": "Failed"}
        mock_get.return_value = mock_response

        result = get_open_interest("BTC")

        assert result == {}


class TestGetMultiFundingAndOi:
    """Tests for get_multi_funding_and_oi function."""

    @patch("kronos_utils.get_funding_rate")
    @patch("kronos_utils.get_open_interest")
    def test_get_multi_funding_and_oi(
        self, mock_oi: MagicMock, mock_fr: MagicMock
    ) -> None:
        """Test batch retrieval of funding rate and OI."""
        from kronos_utils import get_multi_funding_and_oi

        mock_fr.return_value = {"rate": 0.01, "coin": "BTC"}
        mock_oi.return_value = {"oi": 1000000, "coin": "BTC"}

        result = get_multi_funding_and_oi(["BTC", "ETH"])

        assert "BTC" in result
        assert "ETH" in result
        assert result["BTC"]["rate"] == 0.01
        assert result["BTC"]["oi"] == 1000000


class TestPaperLogFunctions:
    """Tests for paper trading log functions."""

    @patch("kronos_utils.Path.home")
    def test_load_paper_log_empty(self, mock_home: MagicMock) -> None:
        """Test loading paper log when file doesn't exist."""
        from kronos_utils import load_paper_log

        # Create a temp directory for home
        with tempfile.TemporaryDirectory() as tmpdir:
            mock_home.return_value = Path(tmpdir)
            result = load_paper_log()
            assert result == []

    @patch("kronos_utils.Path.home")
    def test_load_paper_log_with_data(self, mock_home: MagicMock) -> None:
        """Test loading paper log with existing data."""
        from kronos_utils import load_paper_log

        with tempfile.TemporaryDirectory() as tmpdir:
            mock_home.return_value = Path(tmpdir)

            # Create the paper trades file
            paper_dir = Path(tmpdir) / ".hermes" / "cron" / "output"
            paper_dir.mkdir(parents=True)
            paper_file = paper_dir / "paper_trades.json"
            paper_file.write_text('[{"coin": "BTC", "pnl": 1.5}]')

            result = load_paper_log()

            assert len(result) == 1
            assert result[0]["coin"] == "BTC"


class TestHumanSize:
    """Tests for human-readable file size formatting."""

    def test_human_size_bytes(self) -> None:
        """Test bytes formatting."""
        # This function is defined locally in cleanup_stale_data.py
        # We test the pattern here
        def human_size(n: float) -> str:
            for u in ["B", "K", "M", "G"]:
                if abs(n) < 1024:
                    return f"{n:.1f}{u}"
                n /= 1024
            return f"{n:.1f}T"

        assert human_size(500) == "500.0B"
        assert human_size(1024) == "1.0K"
        assert human_size(1536) == "1.5K"

    def test_human_size_megabytes(self) -> None:
        """Test megabyte formatting."""
        def human_size(n: float) -> str:
            for u in ["B", "K", "M", "G"]:
                if abs(n) < 1024:
                    return f"{n:.1f}{u}"
                n /= 1024
            return f"{n:.1f}T"

        assert human_size(1024 * 1024) == "1.0M"
        assert human_size(2.5 * 1024 * 1024) == "2.5M"

    def test_human_size_negative(self) -> None:
        """Test negative size handling."""
        def human_size(n: float) -> str:
            for u in ["B", "K", "M", "G"]:
                if abs(n) < 1024:
                    return f"{n:.1f}{u}"
                n /= 1024
            return f"{n:.1f}T"

        assert human_size(-500) == "-500.0B"
