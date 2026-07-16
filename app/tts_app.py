"""可选的独立 VOICEVOX 测试入口。

主后端已经挂载同一组 ``/tts`` 路由；这个入口只用于不启动 Chat/Agent 时
单独测试音频。两边共用 Provider、角色配置和缓存规则。
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI

from .tts.config import get_tts_settings
from .tts.routes import router as tts_router
from .tts.service import TTSService


def create_tts_app() -> FastAPI:
    settings = get_tts_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        settings.ensure_dirs()
        app.state.tts_settings = settings
        app.state.tts_service = TTSService(settings) if settings.enabled else None
        try:
            yield
        finally:
            service = getattr(app.state, "tts_service", None)
            if service is not None:
                await service.close()

    app = FastAPI(title="Mirdo TTS Lab", version="0.2.0", lifespan=lifespan)
    app.include_router(tts_router)
    return app


app = create_tts_app()
