"""Unified logging configuration for Kronos."""

import logging
from logging.handlers import RotatingFileHandler
from dataclasses import dataclass
from typing import Optional
import os


@dataclass
class LogConfig:
    """Logging configuration."""
    max_bytes: int = 10 * 1024 * 1024  # 10MB
    backup_count: int = 5
    level: int = logging.DEBUG
    format_str: str = '%(asctime)s [%(name)s] [%(levelname)s] %(message)s'


# Global log config instance
log_config = LogConfig()

# Cache for logger instances
_loggers = {}


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
    formatter = logging.Formatter(log_config.format_str)
    
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
