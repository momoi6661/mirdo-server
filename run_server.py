from __future__ import annotations

import contextlib
import logging
import os
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

# PyInstaller 运行时没有可供 logfire.inspect 使用的源码文件；其 Pydantic
# 插件会在冻结程序启动阶段触发 OSError。后端并不依赖该可选观测插件，
# 禁用它也让源码运行与发布运行保持一致。
os.environ.setdefault("PYDANTIC_DISABLE_PLUGINS", "1")

import uvicorn

from app.config import get_settings
from app.main import app as application
from app.logging_setup import configure_file_logging

_LOGGER = logging.getLogger(__name__)
_LOCK_BYTES = 1
_STARTUP_WAIT_SECONDS = 20.0
_HEALTH_TIMEOUT_SECONDS = 0.8


class ServerInstanceLock:
    """跨进程单实例锁，避免 Godot 连续启动时拉起多个后端。"""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._file = None

    def __enter__(self) -> bool:
        """尝试占用锁文件；成功返回 True，失败说明已有后端正在启动或运行。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # 不用 a+b：Windows 下 append 模式会忽略 seek，容易锁住不同字节。
        self._file = self.path.open("r+b") if self.path.exists() else self.path.open("w+b")
        self._file.seek(0)
        try:
            self._lock_non_blocking()
            self._file.seek(0)
            self._file.truncate()
            self._file.write(str(os.getpid()).encode("ascii"))
            self._file.flush()
        except OSError:
            self._file.close()
            self._file = None
            return False
        return True

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        """进程退出时释放系统文件锁；锁会随进程死亡自动释放，不怕 stale pid。"""
        if self._file is None:
            return
        with contextlib.suppress(OSError):
            self._unlock()
        self._file.close()
        self._file = None

    def _lock_non_blocking(self) -> None:
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(self._file.fileno(), msvcrt.LK_NBLCK, _LOCK_BYTES)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)

    def _unlock(self) -> None:
        if os.name == "nt":
            import msvcrt

            self._file.seek(0)
            msvcrt.locking(self._file.fileno(), msvcrt.LK_UNLCK, _LOCK_BYTES)
        else:
            import fcntl

            fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)


def _health_url(host: str, port: int) -> str:
    """拼出本地健康检查地址，供启动前去重和等待已有服务使用。"""
    return f"http://{host}:{port}/health"


def _health_ok(url: str) -> bool:
    """轻量检查后端是否已经可用；只用于启动脚本，避免额外依赖 requests/httpx。"""
    try:
        with urlopen(url, timeout=_HEALTH_TIMEOUT_SECONDS) as response:
            return 200 <= response.status < 300 and b'"ok":true' in response.read(512).replace(b" ", b"")
    except (OSError, URLError, TimeoutError):
        return False


def _wait_for_existing_server(url: str, timeout_seconds: float = _STARTUP_WAIT_SECONDS) -> bool:
    """另一个进程正在启动时等待它就绪；成功后本进程直接退出，不再绑定端口。"""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if _health_ok(url):
            return True
        time.sleep(0.25)
    return False


def main() -> None:
    """启动 Mirdo 后端；同一端口同一时间只允许一个服务实例。"""
    # 源码运行时 __file__ 在项目根目录；PyInstaller onedir 运行时
    # __file__ 位于 _internal，资源和 .env 则位于 MirdoServer.exe 同级目录。
    bundle_dir = (
        Path(sys.executable).resolve().parent
        if getattr(sys, "frozen", False)
        else Path(__file__).resolve().parent
    )
    os.chdir(bundle_dir)
    settings = get_settings()
    settings.ensure_runtime_dirs()
    configure_file_logging(settings.runtime_dir)

    health_url = _health_url(settings.app_host, settings.app_port)
    if _health_ok(health_url):
        _LOGGER.info("backend_already_running url=%s", health_url)
        return

    lock_path = settings.runtime_dir / "server.lock"
    with ServerInstanceLock(lock_path) as acquired:
        if not acquired:
            _LOGGER.info("backend_start_waiting_for_existing url=%s", health_url)
            if _wait_for_existing_server(health_url):
                _LOGGER.info("backend_existing_became_ready url=%s", health_url)
                return
            _LOGGER.error("backend_start_lock_timeout url=%s", health_url)
            sys.exit(2)

        if _health_ok(health_url):
            _LOGGER.info("backend_already_running_after_lock url=%s", health_url)
            return

        _LOGGER.info("backend_starting host=%s port=%d", settings.app_host, settings.app_port)
        uvicorn.run(
            application,
            host=settings.app_host,
            port=settings.app_port,
            reload=settings.app_reload,
            log_config=None,
        )


if __name__ == "__main__":
    main()
