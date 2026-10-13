import logging
import uuid

import logger as logger_module
from logger import AppLogger


def unique_name():
    return f"test-logger-{uuid.uuid4().hex}"


def test_writes_to_rotating_file_when_not_in_lambda(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "RUNNING_IN_LAMBDA", False)
    log = AppLogger(unique_name(), log_dir=str(tmp_path), log_file="app.log", level="debug")
    log.info("hello %s", "world")
    for handler in log.logger.handlers:
        handler.flush()

    content = (tmp_path / "app.log").read_text()
    assert "hello world" in content
    # stacklevel=2 makes the record point at the caller, not the wrapper.
    assert "test_logger.py:test_writes_to_rotating_file_when_not_in_lambda" in content
    assert "| - |" in content  # aws_request_id placeholder outside Lambda
    assert log.logger.level == logging.DEBUG


def test_no_file_handler_in_lambda(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "RUNNING_IN_LAMBDA", True)
    log = AppLogger(unique_name(), log_dir=str(tmp_path))
    assert [type(h) for h in log.logger.handlers] == [logging.StreamHandler]
    assert not any(tmp_path.iterdir())


def test_handlers_are_added_once_per_name(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "RUNNING_IN_LAMBDA", True)
    name = unique_name()
    AppLogger(name, log_dir=str(tmp_path))
    log = AppLogger(name, log_dir=str(tmp_path))
    assert len(log.logger.handlers) == 1
    assert log.logger.propagate is False


def test_level_from_env_and_invalid_level_defaults_to_info(tmp_path, monkeypatch):
    monkeypatch.setattr(logger_module, "RUNNING_IN_LAMBDA", True)
    monkeypatch.setenv("LOG_LEVEL", "warning")
    assert AppLogger(unique_name(), log_dir=str(tmp_path)).logger.level == logging.WARNING
    assert AppLogger(unique_name(), log_dir=str(tmp_path), level="nonsense").logger.level == logging.INFO


def test_exception_includes_traceback(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(logger_module, "RUNNING_IN_LAMBDA", True)
    log = AppLogger(unique_name(), log_dir=str(tmp_path))
    try:
        raise RuntimeError("kaboom")
    except RuntimeError:
        log.exception("failed")
    out = capsys.readouterr().out
    assert "failed" in out and "RuntimeError: kaboom" in out
