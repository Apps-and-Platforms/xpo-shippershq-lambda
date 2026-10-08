"""Reusable application logger with console + rotating file output."""

import logging
import os
from logging.handlers import RotatingFileHandler
from pathlib import Path


class AppLogger:
    """Wraps `logging.Logger` with sane defaults (console + rotating file, one-time setup per name)."""

    _configured_names = set()

    def __init__(
        self,
        name: str,
        log_dir: str = None,
        log_file: str = "bigcommerce.log",
        level: str = None,
        max_bytes: int = 5_000_000,
        backup_count: int = 5,
    ):
        self.logger = logging.getLogger(name)
        log_dir = log_dir or os.getenv("LOG_DIR", "logs")
        level = (level or os.getenv("LOG_LEVEL", "INFO")).upper()
        self.logger.setLevel(getattr(logging, level, logging.INFO))

        # Guard against duplicate handlers if AppLogger(name) is instantiated more than once.
        if name not in AppLogger._configured_names:
            self.logger.propagate = False
            formatter = logging.Formatter(
                fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(filename)s:%(funcName)s:%(lineno)d | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )

            console_handler = logging.StreamHandler()
            console_handler.setFormatter(formatter)
            self.logger.addHandler(console_handler)

            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                Path(log_dir) / log_file, maxBytes=max_bytes, backupCount=backup_count
            )
            file_handler.setFormatter(formatter)
            self.logger.addHandler(file_handler)

            AppLogger._configured_names.add(name)
            self.logger.debug(
                f"Logger '{name}' configured: level={level}, file={Path(log_dir) / log_file}, "
                f"max_bytes={max_bytes}, backup_count={backup_count}"
            )

    # stacklevel=2 makes %(filename)s/%(funcName)s/%(lineno)d report the caller, not this wrapper.
    def debug(self, msg, *args, **kwargs):
        kwargs.setdefault("stacklevel", 2)
        self.logger.debug(msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        kwargs.setdefault("stacklevel", 2)
        self.logger.info(msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        kwargs.setdefault("stacklevel", 2)
        self.logger.warning(msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        kwargs.setdefault("stacklevel", 2)
        self.logger.error(msg, *args, **kwargs)

    def critical(self, msg, *args, **kwargs):
        kwargs.setdefault("stacklevel", 2)
        self.logger.critical(msg, *args, **kwargs)

    def exception(self, msg, *args, **kwargs):
        """Log at ERROR level and include the current exception's traceback."""
        kwargs.setdefault("stacklevel", 2)
        self.logger.exception(msg, *args, **kwargs)
