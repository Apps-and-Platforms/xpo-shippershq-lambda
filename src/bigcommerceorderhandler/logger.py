"""Reusable application logger: stdout (captured by CloudWatch on Lambda) + rotating file when run locally."""

import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

# Lambda forwards everything written to stdout/stderr to the function's CloudWatch log group.
RUNNING_IN_LAMBDA = bool(os.getenv("AWS_LAMBDA_FUNCTION_NAME"))


class _RequestIdFilter(logging.Filter):
    """Ensure every record has `aws_request_id` (set by the Lambda runtime; "-" when run locally)."""

    def filter(self, record):
        if not hasattr(record, "aws_request_id"):
            record.aws_request_id = "-"
        return True


class AppLogger:
    """Wraps `logging.Logger` with sane defaults (stdout + local rotating file, one-time setup per name)."""

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
                fmt="%(asctime)s | %(levelname)-8s | %(aws_request_id)s | %(name)s | "
                "%(filename)s:%(funcName)s:%(lineno)d | %(message)s",
                datefmt="%Y-%m-%d %H:%M:%S",
            )
            request_id_filter = _RequestIdFilter()

            # stdout is what CloudWatch picks up in Lambda; it's also the console when run locally.
            console_handler = logging.StreamHandler(sys.stdout)
            console_handler.setFormatter(formatter)
            console_handler.addFilter(request_id_filter)
            self.logger.addHandler(console_handler)

            # Lambda's filesystem is ephemeral (only /tmp is writable), so a log file there would never
            # be seen; skip it and rely on CloudWatch. Locally, keep the rotating file.
            log_path = None
            if not RUNNING_IN_LAMBDA:
                log_path = Path(log_dir) / log_file
                Path(log_dir).mkdir(parents=True, exist_ok=True)
                file_handler = RotatingFileHandler(log_path, maxBytes=max_bytes, backupCount=backup_count)
                file_handler.setFormatter(formatter)
                file_handler.addFilter(request_id_filter)
                self.logger.addHandler(file_handler)

            AppLogger._configured_names.add(name)
            self.logger.debug(
                f"Logger '{name}' configured: level={level}, file={log_path or 'disabled (CloudWatch only)'}, "
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
