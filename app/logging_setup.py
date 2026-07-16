"""后端统一日志配置。"""
from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)-14s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def configure_file_logging(runtime_dir: Path) -> Path:
    """同时写入日志文件和当前终端，方便调试启动阶段。"""
    runtime_dir.mkdir(parents=True, exist_ok=True)
    log_path = runtime_dir / "server.log"
    root = logging.getLogger()

    has_current_file_handler = False
    has_console_handler = False
    for handler in list(root.handlers):
        if getattr(handler, "_mirdo_log_file", None) == log_path:
            has_current_file_handler = True
        elif getattr(handler, "_mirdo_log_file", None):
            root.removeHandler(handler)
            handler.close()
        if getattr(handler, "_mirdo_console", False):
            has_console_handler = True

    formatter = logging.Formatter(_LOG_FORMAT, _DATE_FORMAT)
    if not has_current_file_handler:
        file_handler = RotatingFileHandler(log_path, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8")
        file_handler._mirdo_log_file = log_path  # type: ignore[attr-defined]
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)

    if not has_console_handler:
        # uvicorn 直启时也要在当前终端看到 startup complete、请求和错误。
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler._mirdo_console = True  # type: ignore[attr-defined]
        console_handler.setFormatter(formatter)
        root.addHandler(console_handler)

    root.setLevel(logging.INFO)
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        logger = logging.getLogger(name)
        logger.handlers.clear()
        logger.propagate = True
    return log_path
