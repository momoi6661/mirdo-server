"""TTS Provider 模块。

主后端通过 ``/tts`` 路由使用它；``run_tts.py`` 仍保留为不启动 Agent 的
独立联调入口。VOICEVOX 客户端、角色配置和缓存都集中在这里。
"""

from .models import TTSSynthesisRequest
from .service import TTSService

__all__ = ["TTSSynthesisRequest", "TTSService"]
