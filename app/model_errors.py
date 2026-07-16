"""把不同服务商的异常压缩为 Godot 可稳定处理的错误码。"""
from __future__ import annotations

from dataclasses import dataclass
import httpx


@dataclass(frozen=True)
class ModelError:
    code: str
    retryable: bool


def classify_model_error(exc: Exception) -> ModelError:
    """只重试网络/限流/服务端错误；配置和结构错误直接降级。"""
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError)):
        return ModelError("model_network_error", True)
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        if status in {401, 403}:
            return ModelError("model_auth_error", False)
        if status == 429:
            return ModelError("model_rate_limited", True)
        if 500 <= status <= 599:
            return ModelError("model_upstream_error", True)
        return ModelError("model_request_error", False)
    text = str(exc or "").lower()
    if "validation" in text or "output" in text:
        return ModelError("model_output_invalid", False)
    return ModelError("model_call_failed", False)
