"""Unit tests for custom_callbacks.OpenCodeGoSessionHeader.

Run directly (stdlib only) or under pytest:

    python3 test_custom_callbacks.py
    pytest test_custom_callbacks.py

The module under test imports litellm, fastapi and gpu_containers, none of
which are needed for the pure session/header logic. They are stubbed here so
the test is hermetic and runs without the litellm container.
"""

import asyncio
import os
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _install_stubs():
    if "litellm" not in sys.modules:
        litellm = types.ModuleType("litellm")
        integrations = types.ModuleType("litellm.integrations")
        custom_logger = types.ModuleType("litellm.integrations.custom_logger")

        class CustomLogger:
            pass

        custom_logger.CustomLogger = CustomLogger
        integrations.custom_logger = custom_logger
        litellm.integrations = integrations
        sys.modules.update(
            {
                "litellm": litellm,
                "litellm.integrations": integrations,
                "litellm.integrations.custom_logger": custom_logger,
            }
        )

    if "fastapi" not in sys.modules:
        fastapi = types.ModuleType("fastapi")

        class HTTPException(Exception):
            def __init__(self, status_code=None, detail=None):
                super().__init__(detail)
                self.status_code = status_code
                self.detail = detail

        fastapi.HTTPException = HTTPException
        sys.modules["fastapi"] = fastapi

    if "gpu_containers" not in sys.modules:
        gpu = types.ModuleType("gpu_containers")

        class PortainerContainers:
            def __init__(self):
                pass

        gpu.PortainerContainers = PortainerContainers
        sys.modules["gpu_containers"] = gpu


_install_stubs()
os.environ.setdefault(
    "OPENCODE_GO_MODELS", "deepseek-v4.1-flash,mimo-v2.6-flash"
)
sys.path.insert(0, str(ROOT))

import custom_callbacks as cc  # noqa: E402


def _cb():
    return cc.OpenCodeGoSessionHeader()


def _data(messages=None, headers=None, **extra):
    data = {
        "model": "deepseek-v4.1-flash",
        "messages": messages
        if messages is not None
        else [{"role": "user", "content": "hi"}],
    }
    if headers is not None:
        data["proxy_server_request"] = {"headers": headers}
    data.update(extra)
    return data


def _is_hex32(value):
    return len(value) == 32 and all(c in "0123456789abcdef" for c in value)


def test_resolve_session_precedence():
    cb = _cb()
    # 1. incoming x-opencode-session wins over everything else
    d = _data(
        headers={
            "X-Opencode-Session": "client-sess",
            "X-OpenWebUI-Chat-Id": "chat-1",
        }
    )
    assert cb._resolve_session(d) == "client-sess"

    # 2. OpenWebUI chat id beats the fingerprint, is stable and opaque
    d = _data(headers={"X-OpenWebUI-Chat-Id": "chat-1", "X-OpenWebUI-User-Id": "u1"})
    s1 = cb._resolve_session(d)
    assert s1 == cb._resolve_session(
        _data(headers={"X-OpenWebUI-Chat-Id": "chat-1", "X-OpenWebUI-User-Id": "u1"})
    )
    assert _is_hex32(s1)
    # different chat -> different session; same chat, different user -> different
    assert (
        cb._resolve_session(
            _data(headers={"X-OpenWebUI-Chat-Id": "chat-2", "X-OpenWebUI-User-Id": "u1"})
        )
        != s1
    )
    assert (
        cb._resolve_session(
            _data(headers={"X-OpenWebUI-Chat-Id": "chat-1", "X-OpenWebUI-User-Id": "u2"})
        )
        != s1
    )

    # 3. litellm session id when there is no chat id
    assert cb._resolve_session(_data(litellm_session_id="ls-1")) == "ls-1"

    # 4. fingerprint fallback: stable, and system messages are skipped
    f1 = cb._resolve_session(
        _data(
            messages=[
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "first"},
            ]
        )
    )
    assert f1 == cb._resolve_session(
        _data(
            messages=[
                {"role": "system", "content": "a different system prompt"},
                {"role": "user", "content": "first"},
            ]
        )
    )
    assert f1 != cb._resolve_session(
        _data(messages=[{"role": "user", "content": "other"}])
    )
    assert _is_hex32(f1)


def test_pre_call_hook_sets_session_and_ua():
    cb = _cb()
    d = _data(headers={"X-OpenWebUI-Chat-Id": "chat-1", "X-OpenWebUI-User-Id": "u1"})
    out = asyncio.run(cb.async_pre_call_hook(None, None, d, "acompletion"))
    extra = out["extra_headers"]
    assert extra["User-Agent"] == "johannes-litellm/1.0"
    assert extra["x-opencode-session"]


def test_pre_call_hook_overrides_forwarded_ua():
    cb = _cb()
    d = _data(headers={}, extra_headers={})
    d["extra_headers"] = {
        "user-agent": "Python/3.11 aiohttp/3.13.5",
        "X-Custom": "keep",
    }
    out = asyncio.run(cb.async_pre_call_hook(None, None, d, "acompletion"))
    extra = out["extra_headers"]
    assert extra["User-Agent"] == "johannes-litellm/1.0"
    assert "user-agent" not in extra
    assert extra["X-Custom"] == "keep"  # unrelated headers preserved


def test_non_managed_model_untouched():
    cb = _cb()
    d = {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": "hi"}],
        "extra_headers": {"User-Agent": "x"},
    }
    out = asyncio.run(cb.async_pre_call_hook(None, None, d, "acompletion"))
    assert out == d  # returned unchanged, nothing injected


def _main():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    print(f"{len(tests)} passed")


if __name__ == "__main__":
    _main()
