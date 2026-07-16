import httpx

from app.model_errors import classify_model_error


def test_classify_transient_network_error_as_retryable():
    error = classify_model_error(httpx.ConnectError("offline"))
    assert error.code == "model_network_error"
    assert error.retryable is True


def test_classify_auth_error_as_not_retryable():
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    error = classify_model_error(httpx.HTTPStatusError("unauthorized", request=request, response=httpx.Response(401, request=request)))
    assert error.code == "model_auth_error"
    assert error.retryable is False
