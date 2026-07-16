from __future__ import annotations

import uvicorn

from app.tts.config import get_tts_settings


def main() -> None:
    """启动独立 TTS 实验服务；接口和主后端一致，路径以 ``/tts`` 开头。"""
    settings = get_tts_settings()
    uvicorn.run(
        "app.tts_app:app",
        host=settings.host,
        port=settings.port,
        log_config=None,
    )


if __name__ == "__main__":
    main()
