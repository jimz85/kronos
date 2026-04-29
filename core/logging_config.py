"""
Unified logging configuration for Kronos.
==========================================

Supports both standard text logging and JSON structured logging.
JSON logging is ideal for log aggregation systems (ELK, Loki, etc.).

Usage:
    # Standard logging
    from core.logging_config import get_logger
    logger = get_logger('kronos.module')
    logger.info("Trading signal generated")

    # JSON structured logging
    from core.logging_config import get_json_logger
    logger = get_json_logger('kronos.trades')
    logger.info("Signal generated", extra={
        "coin": "BTC",
        "side": "long",
        "confidence": 0.85,
        "price": 50000.0
    })

Log Rotation:
    - Default: 10MB per file, 5 backup files
    - Configurable via LogConfig

Version: 5.1.0
"""

import logging
import json
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler
from dataclasses import dataclass, field
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path
import os
import traceback


@dataclass
class LogConfig:
    """Logging configuration."""
    # Rotation settings
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    when: str = 'midnight'  # Timed rotation at midnight
    interval: int = 1  # Daily rotation

    # Level settings
    level: int = logging.DEBUG

    # Format settings
    format_str: str = '%(asctime)s [%(name)s] [%(levelname)s] %(message)s'
    date_format: str = '%Y-%m-%d %H:%M:%S'

    # JSON logging settings
    json_enabled: bool = True
    json_log_dir: str = 'logs/json'
    json_app_name: str = 'kronos'

    # Structured fields to add to all JSON logs
    default_fields: Dict[str, Any] = field(default_factory=dict)


# Global log config instance
log_config = LogConfig()

# Cache for logger instances
_loggers: Dict[str, logging.Logger] = {}
_json_loggers: Dict[str, logging.Logger] = {}


class JSONFormatter(logging.Formatter):
    """
    Custom JSON formatter for structured logging.

    Adds consistent fields to every log entry:
    - timestamp: ISO8601 format
    - level: Log level (INFO, ERROR, etc.)
    - logger: Logger name
    - message: Log message
    - app: Application name (from config)
    - Any extra fields provided

    Example output:
    {
        "timestamp": "2026-04-27T20:30:00.000Z",
        "level": "INFO",
        "logger": "kronos.trades",
        "message": "Signal generated",
        "app": "kronos",
        "coin": "BTC",
        "side": "long",
        "confidence": 0.85
    }
    """

    def __init__(self, app_name: str = 'kronos', default_fields: Dict[str, Any] = None):
        super().__init__()
        self.app_name = app_name
        self.default_fields = default_fields or {}

    def format(self, record: logging.LogRecord) -> str:
        """Format log record as JSON."""
        log_data = {
            'timestamp': datetime.utcnow().isoformat() + 'Z',
            'level': record.levelname,
            'logger': record.name,
            'message': record.getMessage(),
            'app': self.app_name,
        }

        # Add default fields
        log_data.update(self.default_fields)

        # Add extra fields from record
        if hasattr(record, 'extra_fields'):
            log_data.update(record.extra_fields)

        # Add exception info if present
        if record.exc_info:
            log_data['exception'] = {
                'type': record.exc_info[0].__name__ if record.exc_info[0] else None,
                'message': str(record.exc_info[1]) if record.exc_info[1] else None,
                'traceback': traceback.format_exception(*record.exc_info)
            }

        # Add function and line info in debug mode
        if record.levelno <= logging.DEBUG:
            log_data['location'] = {
                'function': record.funcName,
                'file': record.filename,
                'line': record.lineno
            }

        return json.dumps(log_data, default=str)


class StructuredLogFilter(logging.Filter):
    """Filter to attach extra_fields to log records."""

    def __init__(self, extra_fields: Dict[str, Any] = None):
        super().__init__()
        self.extra_fields = extra_fields or {}

    def filter(self, record: logging.LogRecord) -> bool:
        record.extra_fields = self.extra_fields.copy()
        return True


def get_logger(name: str, log_file: Optional[str] = None) -> logging.Logger:
    """
    Get or create a logger with unified configuration.

    Args:
        name: Logger name (typically __name__ of the module)
        log_file: Optional custom log file path. If None, uses module name.
                  If provided as relative path, logs are stored in logs/ directory.

    Returns:
        Configured logger instance
    """
    if name in _loggers:
        return _loggers[name]

    logger = logging.getLogger(name)
    logger.setLevel(log_config.level)
    logger.propagate = False

    # Clear existing handlers
    logger.handlers.clear()

    # Determine log file path
    if log_file:
        if not os.path.isabs(log_file):
            log_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, log_file)

    # Create formatters
    formatter = logging.Formatter(log_config.format_str, datefmt=log_config.date_format)

    # File handler with rotation
    if log_file:
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=log_config.max_bytes,
            backupCount=log_config.backup_count
        )
        file_handler.setLevel(log_config.level)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)

    # Console handler
    console_handler = logging.StreamHandler()
    console_handler.setLevel(log_config.level)
    console_handler.setFormatter(formatter)
    logger.addHandler(console_handler)

    _loggers[name] = logger
    return logger


def get_json_logger(
    name: str,
    log_file: Optional[str] = None,
    when: str = None,
    interval: int = None,
    extra_fields: Dict[str, Any] = None
) -> logging.Logger:
    """
    Get or create a JSON structured logger with rotation.

    Args:
        name: Logger name (typically __name__ of the module)
        log_file: Optional custom log file path. If None, uses module name.
        when: Time-based rotation trigger (default: midnight)
        interval: Rotation interval (default: 1 day)
        extra_fields: Additional fields to include in every log entry

    Returns:
        Configured JSON logger instance
    """
    cache_key = f"json_{name}"
    if cache_key in _json_loggers:
        return _json_loggers[cache_key]

    logger = logging.getLogger(name)
    logger.setLevel(log_config.level)
    logger.propagate = False

    # Clear existing handlers
    logger.handlers.clear()

    # Build default fields
    default_fields = log_config.default_fields.copy()
    if extra_fields:
        default_fields.update(extra_fields)

    # JSON formatter
    json_formatter = JSONFormatter(
        app_name=log_config.json_app_name,
        default_fields=default_fields
    )

    # Add structured log filter
    structured_filter = StructuredLogFilter(default_fields)
    logger.addFilter(structured_filter)

    # Determine log directory
    if log_file:
        if not os.path.isabs(log_file):
            log_dir = os.path.join(
                os.path.dirname(os.path.dirname(__file__)),
                log_config.json_log_dir
            )
            os.makedirs(log_dir, exist_ok=True)
            log_file = os.path.join(log_dir, log_file)
    else:
        # Default: logs/json/{name}.json
        log_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            log_config.json_log_dir
        )
        os.makedirs(log_dir, exist_ok=True)
        log_file = os.path.join(log_dir, f"{name.replace('.', '_')}.json")

    # Time-based rotation for JSON logs (daily by default)
    rotation_when = when or log_config.when
    rotation_interval = interval or log_config.interval

    file_handler = TimedRotatingFileHandler(
        log_file,
        when=rotation_when,
        interval=rotation_interval,
        backupCount=log_config.backup_count,
        utc=True  # Use UTC for consistent timestamps
    )
    file_handler.setLevel(log_config.level)
    file_handler.setFormatter(json_formatter)
    file_handler.addFilter(structured_filter)
    logger.addHandler(file_handler)

    # Console handler with JSON in debug mode
    if log_config.level <= logging.DEBUG:
        console_handler = logging.StreamHandler()
        console_handler.setLevel(log_config.level)
        console_handler.setFormatter(json_formatter)
        console_handler.addFilter(structured_filter)
        logger.addHandler(console_handler)

    _json_loggers[cache_key] = logger
    return logger


def setup_root_logger(
    level: int = logging.INFO,
    json_log_file: Optional[str] = None
) -> None:
    """
    Setup the root logger for the application.

    Args:
        level: Logging level
        json_log_file: Optional path for JSON log file
    """
    log_config.level = level

    root_logger = logging.getLogger()
    root_logger.setLevel(level)
    root_logger.handlers.clear()

    # Standard formatter
    formatter = logging.Formatter(log_config.format_str, datefmt=log_config.date_format)

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(level)
    console.setFormatter(formatter)
    root_logger.addHandler(console)

    # JSON file handler if specified
    if json_log_file:
        json_logger = get_json_logger('root', log_file=json_log_file)
        root_logger.addHandler(json_logger)


def get_trade_logger(name: str = 'kronos.trades') -> logging.Logger:
    """
    Get a specialized logger for trade-related events.

    Trade logs include:
    - Signal generation
    - Order placement
    - Position updates
    - PnL updates

    Args:
        name: Logger name (default: 'kronos.trades')

    Returns:
        Configured trade logger with JSON output
    """
    return get_json_logger(
        name,
        extra_fields={
            'log_type': 'trade_event',
            'version': '5.1.0'
        }
    )


def get_audit_logger(name: str = 'kronos.audit') -> logging.Logger:
    """
    Get a specialized logger for audit events.

    Audit logs include:
    - Configuration changes
    - Risk limit breaches
    - System errors
    - Decision justifications

    Args:
        name: Logger name (default: 'kronos.audit')

    Returns:
        Configured audit logger with JSON output
    """
    return get_json_logger(
        name,
        extra_fields={
            'log_type': 'audit_event',
            'version': '5.1.0'
        }
    )


def close_all_loggers() -> None:
    """Close and remove all cached loggers. Useful for cleanup in tests."""
    all_loggers = {**_loggers, **_json_loggers}
    for logger in all_loggers.values():
        for handler in logger.handlers[:]:
            handler.close()
            logger.removeHandler(handler)
    _loggers.clear()
    _json_loggers.clear()
