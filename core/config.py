"""Centralized configuration management for Kronos."""

import os
from dataclasses import dataclass
from typing import Optional
from dotenv import load_dotenv

# Load environment variables from ~/.hermes/.env
_env_path = os.path.expanduser('~/.hermes/.env')
load_dotenv(_env_path)


@dataclass
class OKXConfig:
    """OKX exchange configuration."""
    api_key: str = ''
    secret: str = ''
    passphrase: str = ''
    flag: str = '0'  # 0 = live, 1 = testnet


@dataclass
class TradingConfig:
    """Trading parameters configuration."""
    max_hold_hours: int = 72
    sl_danger_pct: float = 2.0
    sl_pct: float = 1.0
    tp_pct: float = 2.0
    max_position_pct: float = 10.0
    min_trade_amount: float = 10.0


@dataclass
class FeishuConfig:
    """Feishu (Lark) notification configuration."""
    app_id: str = ''
    app_secret: str = ''
    chat_id: str = ''


# Global config instances - loaded from environment
okx_config = OKXConfig(
    api_key=os.getenv('OKX_API_KEY', ''),
    secret=os.getenv('OKX_SECRET', ''),
    passphrase=os.getenv('OKX_PASSPHRASE', ''),
    flag=os.getenv('OKX_FLAG', '0')
)

trading_config = TradingConfig(
    max_hold_hours=int(os.getenv('MAX_HOLD_HOURS', '72')),
    sl_danger_pct=float(os.getenv('SL_DANGER_PCT', '2.0')),
    sl_pct=float(os.getenv('SL_PCT', '1.0')),
    tp_pct=float(os.getenv('TP_PCT', '2.0')),
    max_position_pct=float(os.getenv('MAX_POSITION_PCT', '10.0')),
    min_trade_amount=float(os.getenv('MIN_TRADE_AMOUNT', '10.0'))
)

feishu_config = FeishuConfig(
    app_id=os.getenv('FEISHU_APP_ID', ''),
    app_secret=os.getenv('FEISHU_APP_SECRET', ''),
    chat_id=os.getenv('FEISHU_CHAT_ID', '')
)


def reload_configs():
    """Reload all configurations from environment variables."""
    global okx_config, trading_config, feishu_config
    
    okx_config = OKXConfig(
        api_key=os.getenv('OKX_API_KEY', ''),
        secret=os.getenv('OKX_SECRET', ''),
        passphrase=os.getenv('OKX_PASSPHRASE', ''),
        flag=os.getenv('OKX_FLAG', '0')
    )
    
    trading_config = TradingConfig(
        max_hold_hours=int(os.getenv('MAX_HOLD_HOURS', '72')),
        sl_danger_pct=float(os.getenv('SL_DANGER_PCT', '2.0')),
        sl_pct=float(os.getenv('SL_PCT', '1.0')),
        tp_pct=float(os.getenv('TP_PCT', '2.0')),
        max_position_pct=float(os.getenv('MAX_POSITION_PCT', '10.0')),
        min_trade_amount=float(os.getenv('MIN_TRADE_AMOUNT', '10.0'))
    )
    
    feishu_config = FeishuConfig(
        app_id=os.getenv('FEISHU_APP_ID', ''),
        app_secret=os.getenv('FEISHU_APP_SECRET', ''),
        chat_id=os.getenv('FEISHU_CHAT_ID', '')
    )
