"""Unit tests for the custom_callbacks hook classes.

Covers OpenCodeGoSessionHeader (session/header injection), plus
LlamaCppIdleManager request-token lifecycle and idle-watcher behavior, and
stream timing/throughput and cleanup in InferenceMetrics.

Run directly (stdlib only) or under pytest:

    python3 test_custom_callbacks.py
    pytest test_custom_callbacks.py

The module under test imports litellm, fastapi and gpu_containers, none of
which are needed for the pure session/header logic. They are stubbed here so
the test is hermetic and runs without the litellm container.
"""

import asyncio
import importlib.util
import os
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

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

            async def is_running(self, container):
                return True

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


class TestVoxtralVoiceAlias(unittest.TestCase):
    def setUp(self):
        self.cb = cc.VoxtralVoiceAlias()

    def _data(self, **extra):
        data = {"model": "voxtral-tts", "voice": "jo", "input": "Hello there."}
        data.update(extra)
        return data

    def _hook(self, data):
        return asyncio.run(
            self.cb.async_pre_call_hook(None, None, data, "aspeech")
        )

    def test_alias_mapping(self):
        expected = {
            "jo": cc.VoxtralVoiceAlias.HEIDI,
            "de": cc.VoxtralVoiceAlias.HEIDI,
            "heidi": cc.VoxtralVoiceAlias.HEIDI,
            "harriet": cc.VoxtralVoiceAlias.HARRIET,
            "mary": cc.VoxtralVoiceAlias.MARY_UK,
            "mary-uk": cc.VoxtralVoiceAlias.MARY_UK,
            "jane": cc.VoxtralVoiceAlias.JANE_NEUTRAL,
            "en": cc.VoxtralVoiceAlias.MARY_UK,
            "jane-sarcasm": cc.VoxtralVoiceAlias.JANE_SARCASM,
            "paul-cheerful": cc.VoxtralVoiceAlias.PAUL_CHEERFUL,
            "oliver-curious": cc.VoxtralVoiceAlias.OLIVER_CURIOUS,
            "marie-happy": cc.VoxtralVoiceAlias.MARIE_HAPPY,
        }
        for voice, mapped in expected.items():
            with self.subTest(voice=voice):
                out = self._hook(self._data(voice=voice))
                self.assertEqual(out["voice"], mapped)

    def test_case_insensitive_and_whitespace(self):
        self.assertEqual(
            self._hook(self._data(voice="  JO "))["voice"],
            cc.VoxtralVoiceAlias.HEIDI,
        )
        self.assertEqual(
            self._hook(self._data(voice="Jane-Sarcasm"))["voice"],
            cc.VoxtralVoiceAlias.JANE_SARCASM,
        )

    def test_uuid_presets_and_unknown_voice_passthrough(self):
        for voice in (
            "123e4567-e89b-12d3-a456-426614174000",
            "voice_481",
            "voice_590",
            "voice_1787",
            "not-a-voice",
        ):
            with self.subTest(voice=voice):
                data = self._data(voice=voice)
                before = dict(data)
                self.assertEqual(self._hook(data), before)

    def test_non_string_and_empty_voices_unchanged(self):
        for voice in (None, 12, "", "   "):
            with self.subTest(voice=voice):
                data = self._data(voice=voice)
                before = dict(data)
                self.assertEqual(self._hook(data), before)

    def test_other_models_unchanged(self):
        for model in ("voxtral-stt", "gpt-4o"):
            with self.subTest(model=model):
                data = self._data(model=model, voice="auto")
                self.assertEqual(self._hook(data), data)

    def test_auto_voice_detection(self):
        cases = (
            ("Grüße aus Köln", cc.VoxtralVoiceAlias.HEIDI),
            (
                "the quick brown fox jumps over the lazy dog",
                cc.VoxtralVoiceAlias.MARY_UK,
            ),
            ("1234", cc.VoxtralVoiceAlias.HEIDI),
            ("Bonjour tout le monde", cc.VoxtralVoiceAlias.HEIDI),
            ("In Berlin.", cc.VoxtralVoiceAlias.MARY_UK),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(self.cb._voice_for_text(text), expected)

    def test_auto_hook_end_to_end(self):
        cases = (
            ("Grüße aus Köln", cc.VoxtralVoiceAlias.HEIDI),
            (
                "the quick brown fox jumps over the lazy dog",
                cc.VoxtralVoiceAlias.MARY_UK,
            ),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                out = self._hook(self._data(voice="auto", input=text))
                self.assertEqual(out["voice"], expected)


class TestVoxtralSttContextBias(unittest.TestCase):
    def setUp(self):
        self.cb = cc.VoxtralSttContextBias()

    def _hook(self, data, call_type="transcription"):
        return asyncio.run(
            self.cb.async_pre_call_hook(None, None, data, call_type)
        )

    def test_injects_vocabulary_string_for_voxtral_transcription(self):
        data = {"model": "voxtral-stt"}

        out = self._hook(data)

        self.assertIs(out, data)
        self.assertIsInstance(out["context_bias"], str)
        self.assertEqual(out["context_bias"], self.cb.CONTEXT_BIAS)
        self.assertIn("litellm", out["context_bias"])
        self.assertIn("trilium", out["context_bias"])

    def test_empty_context_bias_values_are_removed_without_injection(self):
        for bias in ("", [], None):
            with self.subTest(context_bias=bias):
                data = {"model": "voxtral-stt", "context_bias": bias}

                out = self._hook(data)

                self.assertIs(out, data)
                self.assertNotIn("context_bias", out)

    def test_nonempty_custom_context_bias_is_preserved(self):
        data = {"model": "voxtral-stt", "context_bias": "custom vocabulary"}

        out = self._hook(data)

        self.assertIs(out, data)
        self.assertEqual(out["context_bias"], "custom vocabulary")

    def test_unrelated_model_and_call_type_preserve_explicit_context_bias(self):
        cases = (
            ({"model": "voxtral-tts"}, "transcription"),
            ({"model": "other-model"}, "transcription"),
            ({"model": "voxtral-stt"}, "acompletion"),
        )
        for template, call_type in cases:
            for bias in ("custom vocabulary", "", [], None):
                with self.subTest(
                    model=template["model"], call_type=call_type, context_bias=bias
                ):
                    data = {**template, "context_bias": bias}
                    out = self._hook(data, call_type)

                    self.assertIs(out, data)
                    self.assertIn("context_bias", out)
                    self.assertEqual(out["context_bias"], bias)


class _IdleManagerTestCase(unittest.IsolatedAsyncioTestCase):
    """Shared fixture for the request-token and idle-watcher tests."""

    async def asyncSetUp(self):
        env = {
            "MODEL_CONTAINERS": (
                '{"test-model":{"container":"test-container",'
                '"health":"https://fake.invalid/health"}}'
            ),
            "EXTRA_GPU_CONTAINERS": "",
        }
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        self.addCleanup(self._restore_env, previous)

        self.mgr = cc.LlamaCppIdleManager()
        self.mgr.idle_timeout = 900
        self.mgr.poll_interval = 0.001
        self.mgr.extra = []
        # Most tests do not want a real background watcher. Tests that
        # exercise it set this to False and replace _idle_loop.
        self.mgr._watcher_started = True
        self.mgr._watcher_task = None

        self.stopped = []
        self.started = []

        async def running(container):
            return True

        async def stop(container):
            self.stopped.append(container)
            return True

        async def start(container):
            self.started.append(container)
            return True

        self.health_calls = []

        async def wait_health(url, timeout):
            self.health_calls.append(url)
            return True

        async def stop_and_wait(container):
            self.stopped.append(container)
            return True

        self.mgr.portainer.is_running = running
        self.mgr.portainer.stop = stop
        self.mgr.portainer.start = start
        self.mgr.portainer.wait_health = wait_health
        self.mgr.portainer.stop_and_wait = stop_and_wait

    @staticmethod
    def _restore_env(previous):
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    async def _pre_call(self, data):
        return await self.mgr.async_pre_call_hook(None, None, data, "acompletion")

    def _tracked(self, container="test-container"):
        return self.mgr.in_flight.get(container, {})

    @staticmethod
    def _token(data):
        return data["metadata"]["_idle_token"]


class TestIdleInFlight(_IdleManagerTestCase):
    async def test_dispatch_and_release(self):
        data = {"model": "test-model"}
        await self._pre_call(data)
        token = self._token(data)
        self.assertIn(token, self._tracked())

        self.assertTrue(self.mgr._release_in_flight(data, "test_release"))
        self.assertNotIn(token, self._tracked())
        self.assertFalse(self.mgr._release_in_flight(data, "test_release_again"))
        self.assertNotIn(token, self._tracked())

    async def test_two_requests_double_release(self):
        first = {"model": "test-model"}
        second = {"model": "test-model"}
        await self._pre_call(first)
        await self._pre_call(second)
        first_token = self._token(first)
        second_token = self._token(second)
        self.assertNotEqual(first_token, second_token)

        self.assertTrue(self.mgr._release_in_flight(first, "first"))
        self.assertFalse(self.mgr._release_in_flight(first, "first_again"))
        self.assertIn(second_token, self._tracked())

        self.assertTrue(self.mgr._release_in_flight(second, "second"))
        self.assertNotIn(second_token, self._tracked())

    async def test_hook_exception_releases(self):
        async def raising(container):
            raise RuntimeError("fake Portainer failure")

        self.mgr.portainer.is_running = raising
        data = {"model": "test-model"}
        with self.assertRaises(RuntimeError):
            await self._pre_call(data)
        self.assertNotIn(self._token(data), self._tracked())

    async def test_duplicate_call_ids_get_unique_tokens(self):
        first = {"model": "test-model", "litellm_call_id": "SAME-ID"}
        second = {"model": "test-model", "litellm_call_id": "SAME-ID"}
        await self._pre_call(first)
        await self._pre_call(second)
        self.assertNotEqual(self._token(first), self._token(second))

        self.assertTrue(self.mgr._release_in_flight(first, "first"))
        self.assertIn(self._token(second), self._tracked())

    async def test_client_supplied_token_is_overwritten(self):
        data = {
            "model": "test-model",
            "metadata": {"_idle_token": "client-value", "keep": "v"},
        }
        await self._pre_call(data)
        self.assertNotEqual(self._token(data), "client-value")
        self.assertEqual(data["metadata"]["keep"], "v")

    async def test_non_managed_model_untouched(self):
        data = {"model": "not-managed", "metadata": {"keep": "v"}}
        out = await self._pre_call(data)
        self.assertIs(out, data)
        self.assertEqual(data["metadata"], {"keep": "v"})
        self.assertEqual(self.mgr.in_flight, {})

    async def test_resolve_hardening(self):
        self.assertIsNone(self.mgr._resolve(None))
        self.assertIsNone(self.mgr._resolve(123))
        self.assertIsNone(self.mgr._resolve(""))
        self.assertEqual(
            self.mgr._resolve("test-model")["container"], "test-container"
        )
        self.assertEqual(
            self.mgr._resolve("openai/test-model")["container"], "test-container"
        )


class TestIdleLifecycle(_IdleManagerTestCase):
    async def test_pre_call_cancelled_during_cold_start_releases(self):
        started = asyncio.Event()

        async def hanging_running(container):
            started.set()
            await asyncio.Event().wait()

        self.mgr.portainer.is_running = hanging_running
        data = {"model": "test-model"}
        task = asyncio.create_task(self._pre_call(data))
        await asyncio.wait_for(started.wait(), 1)
        token = self._token(data)
        self.assertIn(token, self._tracked())

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn(token, self._tracked())

    async def test_nonstream_success_hook_releases(self):
        data = {"model": "test-model", "stream": False}
        await self._pre_call(data)
        token = self._token(data)
        response = object()
        out = await self.mgr.async_post_call_success_hook(data, None, response)
        self.assertIs(out, response)
        self.assertNotIn(token, self._tracked())

    async def test_streaming_success_hook_does_not_release(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        response = object()
        out = await self.mgr.async_post_call_success_hook(data, None, response)
        self.assertIs(out, response)
        self.assertIn(token, self._tracked())

        async def source():
            yield "chunk"

        chunks = [
            chunk
            async for chunk in self.mgr.async_post_call_streaming_iterator_hook(
                None, source(), data
            )
        ]
        self.assertEqual(chunks, ["chunk"])
        self.assertNotIn(token, self._tracked())

    async def test_nonstream_failure_hook_releases(self):
        data = {"model": "test-model"}
        await self._pre_call(data)
        token = self._token(data)
        out = await self.mgr.async_post_call_failure_hook(
            data, RuntimeError("boom"), None
        )
        self.assertIsNone(out)
        self.assertNotIn(token, self._tracked())

    async def test_retry_call_id_change_releases_by_token(self):
        data = {"model": "test-model", "litellm_call_id": "CID-1"}
        await self._pre_call(data)
        token = self._token(data)
        kwargs = {
            # A retry/fallback can change the model name and call id; the
            # token remains the stable request identity.
            "model": "openai/renamed-model",
            "litellm_call_id": "CID-CHANGED",
            "litellm_params": {"metadata": {"_idle_token": token}},
        }
        await self.mgr.async_log_success_event(kwargs, None, None, None)
        self.assertNotIn(token, self._tracked())

    async def test_log_success_event_releases(self):
        data = {"model": "test-model"}
        await self._pre_call(data)
        token = self._token(data)
        kwargs = {
            "model": "test-model",
            "litellm_params": {"metadata": {"_idle_token": token}},
        }
        await self.mgr.async_log_success_event(kwargs, None, None, None)
        self.assertNotIn(token, self._tracked())

    async def test_log_failure_event_releases(self):
        data = {"model": "test-model"}
        await self._pre_call(data)
        token = self._token(data)
        kwargs = {
            "model": "test-model",
            "litellm_params": {"metadata": {"_idle_token": token}},
        }
        await self.mgr.async_log_failure_event(kwargs, None, None, None)
        self.assertNotIn(token, self._tracked())

    async def test_log_stream_event_is_heartbeat_not_release(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        kwargs = {
            "model": "test-model",
            "litellm_params": {"metadata": {"_idle_token": token}},
        }
        self.mgr.last_request_time["test-container"] = 0
        await self.mgr.async_log_stream_event(kwargs, None, None, None)
        self.assertIn(token, self._tracked())
        self.assertGreater(self.mgr.last_request_time["test-container"], 0)

    async def test_stream_log_events_keep_token_until_finally(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        kwargs = {
            "model": "test-model",
            "litellm_params": {"metadata": {"_idle_token": token}},
        }
        # LiteLLM calls async_log_stream_event for every chunk. Intermediate
        # chunks must not release the token.
        for _ in range(3):
            await self.mgr.async_log_stream_event(kwargs, None, None, None)
            self.assertIn(token, self._tracked())

        async def source():
            yield "chunk"

        chunks = [
            chunk
            async for chunk in self.mgr.async_post_call_streaming_iterator_hook(
                None, source(), data
            )
        ]
        self.assertEqual(chunks, ["chunk"])
        self.assertNotIn(token, self._tracked())

    async def test_streaming_success_releases(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        closed = []

        async def source():
            try:
                yield "one"
                yield "two"
            finally:
                closed.append(True)

        chunks = [
            chunk
            async for chunk in self.mgr.async_post_call_streaming_iterator_hook(
                None, source(), data
            )
        ]
        self.assertEqual(chunks, ["one", "two"])
        self.assertEqual(closed, [True])
        self.assertNotIn(token, self._tracked())

    async def test_streaming_error_releases_and_propagates(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        closed = []

        async def source():
            try:
                yield "partial"
                raise RuntimeError("upstream failed")
            finally:
                closed.append(True)

        with self.assertRaisesRegex(RuntimeError, "upstream failed"):
            _ = [
                chunk
                async for chunk in self.mgr.async_post_call_streaming_iterator_hook(
                    None, source(), data
                )
            ]
        self.assertEqual(closed, [True])
        self.assertNotIn(token, self._tracked())

    async def test_streaming_aclose_after_first_chunk_releases(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        closed = []

        async def source():
            try:
                yield "first"
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        stream = self.mgr.async_post_call_streaming_iterator_hook(
            None, source(), data
        )
        self.assertEqual(await anext(stream), "first")
        await stream.aclose()
        self.assertEqual(closed, [True])
        self.assertNotIn(token, self._tracked())

    async def test_streaming_cancel_before_first_chunk_releases(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)
        started = asyncio.Event()
        closed = []

        async def source():
            try:
                started.set()
                await asyncio.Event().wait()
                yield "never"
            finally:
                closed.append(True)

        stream = self.mgr.async_post_call_streaming_iterator_hook(
            None, source(), data
        )
        task = asyncio.create_task(anext(stream))
        await asyncio.wait_for(started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(closed, [True])
        self.assertNotIn(token, self._tracked())

    async def test_streaming_close_failure_does_not_mask_error(self):
        data = {"model": "test-model", "stream": True}
        await self._pre_call(data)
        token = self._token(data)

        class BrokenStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("original provider error")

            async def aclose(self):
                raise ValueError("private cleanup details")

        with self.assertLogs(cc.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "original provider error"):
                _ = [
                    chunk
                    async for chunk in self.mgr.async_post_call_streaming_iterator_hook(
                        None, BrokenStream(), data
                    )
                ]
        self.assertNotIn("original provider error", str(logs.output))
        self.assertNotIn("private cleanup details", str(logs.output))
        self.assertNotIn(token, self._tracked())

    async def test_release_logs_once(self):
        data = {"model": "test-model"}
        await self._pre_call(data)
        token = self._token(data)
        with patch("builtins.print") as mock_print:
            self.assertTrue(self.mgr._release_in_flight(data, "first"))
            self.assertFalse(self.mgr._release_in_flight(data, "second"))
        lines = [str(call) for call in mock_print.call_args_list]
        released = [line for line in lines if "released" in line]
        self.assertEqual(len(released), 1)
        self.assertIn("reason=first", released[0])
        self.assertIn("active=0", released[0])
        self.assertIn(self.mgr._short(token), released[0])

    async def test_release_without_token_logs(self):
        data = {"model": "test-model"}
        await self._pre_call(data)
        # The container still has tracked work, so a callback without the
        # token is a real metadata-loss diagnostic.
        with patch("builtins.print") as mock_print:
            self.assertFalse(
                self.mgr._release_in_flight({"model": "test-model"}, "missing")
            )
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(any("release skipped" in line for line in lines))

    async def test_registration_log_has_container_and_count(self):
        data = {"model": "test-model"}
        with patch("builtins.print") as mock_print:
            await self._pre_call(data)
        lines = [str(call) for call in mock_print.call_args_list]
        registered = [line for line in lines if "registered" in line]
        self.assertEqual(len(registered), 1)
        self.assertIn("test-container", registered[0])
        self.assertIn("active=1", registered[0])

    async def test_watcher_start_on_any_request(self):
        self.mgr._watcher_started = False
        self.mgr._watcher_task = None
        waiting = asyncio.Event()

        async def fake_loop():
            await waiting.wait()

        self.mgr._idle_loop = fake_loop
        await self._pre_call({"model": "not-managed"})
        self.assertTrue(self.mgr._watcher_started)
        self.assertIsNotNone(self.mgr._watcher_task)

        task = self.mgr._watcher_task
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        self.assertFalse(self.mgr._watcher_started)
        self.assertIsNone(self.mgr._watcher_task)

    async def test_watcher_restarts_after_crash(self):
        self.mgr._watcher_started = False
        self.mgr._watcher_task = None

        async def crashing_loop():
            raise RuntimeError("watcher died")

        self.mgr._idle_loop = crashing_loop
        self.mgr._ensure_watcher()
        task = self.mgr._watcher_task
        with self.assertRaisesRegex(RuntimeError, "watcher died"):
            await task
        await asyncio.sleep(0)
        self.assertFalse(self.mgr._watcher_started)
        self.assertIsNone(self.mgr._watcher_task)

        waiting = asyncio.Event()

        async def alive_loop():
            await waiting.wait()

        self.mgr._idle_loop = alive_loop
        await self._pre_call({"model": "not-managed"})
        self.assertTrue(self.mgr._watcher_started)
        self.assertIsNotNone(self.mgr._watcher_task)

        self.mgr._watcher_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await self.mgr._watcher_task

    async def test_watcher_tick_survives_container_error(self):
        self.mgr.containers = {"bad": "bad-model", "good": "good-model"}
        self.mgr.last_request_time = {"bad": 0, "good": 0}

        async def is_running(container):
            if container == "bad":
                raise RuntimeError("probe failed")
            return True

        async def probe(cfg):
            return "idle"

        self.mgr.portainer.is_running = is_running
        self.mgr._probe_backend_idle = probe
        with patch("builtins.print") as mock_print:
            await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, ["good"])
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(any("watcher error for bad" in line for line in lines))
        self.assertTrue(any("good stopped" in line for line in lines))

    async def test_watcher_stop_failure_does_not_refresh(self):
        self.mgr.last_request_time = {"test-container": 0}
        calls = []

        async def stop(container):
            calls.append(container)
            return False

        async def probe(cfg):
            return "idle"

        self.mgr.portainer.stop = stop
        self.mgr._probe_backend_idle = probe
        with patch("builtins.print") as mock_print:
            await self.mgr._idle_tick(now=1000)
            self.assertEqual(self.mgr.last_request_time["test-container"], 0)
            await self.mgr._idle_tick(now=1001)
        self.assertEqual(calls, ["test-container", "test-container"])
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(
            any("failed to stop test-container" in line for line in lines)
        )

    async def test_watcher_inflight_blocks_stop_and_logs_age(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {"test-container": {"tok": 100}}

        async def probe(cfg):
            return "busy"

        self.mgr._probe_backend_idle = probe
        with patch("builtins.print") as mock_print:
            await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(any("1 in-flight token(s)" in line for line in lines))
        self.assertTrue(any("oldest 900s" in line for line in lines))
        self.assertTrue(any("backend probe busy" in line for line in lines))

    async def test_restart_recovery_stops_running_container(self):
        self.mgr.last_request_time.clear()
        self.mgr._init_watcher_state(now=1000)
        self.assertEqual(self.mgr.last_request_time, {"test-container": 1000})

        async def probe(cfg):
            return "idle"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000 + self.mgr.idle_timeout + 1)
        self.assertEqual(self.stopped, ["test-container"])

    async def test_no_tokens_busy_backend_does_not_stop(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {}
        calls = []

        async def probe(cfg):
            calls.append(cfg)
            return "busy"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])
        self.assertEqual(calls, [self.mgr.models["test-model"]])

    async def test_no_tokens_unknown_backend_does_not_stop(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {}

        async def probe(cfg):
            return "unknown"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])

    async def test_no_tokens_idle_backend_stops(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {}

        async def probe(cfg):
            return "idle"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, ["test-container"])

    async def test_restart_adoption_busy_backend_not_stopped(self):
        # Startup adoption: a direct local request is already generating and
        # has no proxy token, so only the backend probe can protect it.
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {}

        async def probe(cfg):
            return "busy"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])

    async def test_request_completed_during_probe_preserves_idle_window(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {}

        async def probe(cfg):
            # A request completes while the probe is in flight; its terminal
            # hook touches last_request_time.
            self.mgr.last_request_time["test-container"] = 999
            return "idle"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.mgr.last_request_time["test-container"], 999)
        self.assertEqual(self.mgr._backend_idle_streak["test-container"], 0)

    async def test_token_released_during_probe_preserves_idle_window(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {"test-container": {"done": 100}}

        async def probe(cfg):
            # A terminal callback releases the token during the probe but
            # does not touch last_request_time (e.g. a model rename).
            self.mgr.in_flight["test-container"].pop("done", None)
            return "idle"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.mgr._backend_idle_streak["test-container"], 0)

    async def test_new_token_during_probe_prevents_stop(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {}

        async def probe(cfg):
            self.mgr.in_flight["test-container"] = {"new": 101}
            return "idle"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, [])
        self.assertIn("new", self._tracked())

    async def test_running_unconfirmed_container_waits_health(self):
        await self._pre_call({"model": "test-model"})
        self.assertEqual(len(self.health_calls), 1)
        self.assertIn("test-container", self.mgr._health_confirmed)

    async def test_confirmed_container_skips_health(self):
        await self._pre_call({"model": "test-model"})
        self.assertEqual(len(self.health_calls), 1)
        await self._pre_call({"model": "test-model"})
        self.assertEqual(len(self.health_calls), 1)

    async def test_cancelled_coldstart_health_is_retried(self):
        health_started = asyncio.Event()

        async def not_running(container):
            return False

        async def start(container):
            self.started.append(container)
            return True

        async def hanging_health(url, timeout):
            health_started.set()
            await asyncio.Event().wait()

        self.mgr.portainer.is_running = not_running
        self.mgr.portainer.start = start
        self.mgr.portainer.wait_health = hanging_health
        task = asyncio.create_task(self._pre_call({"model": "test-model"}))
        await asyncio.wait_for(health_started.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertNotIn("test-container", self.mgr._health_confirmed)

        # The container is now running/loading. The next request must not
        # trust is_running alone; it must wait for health under the lock.
        async def running(container):
            return True

        calls = []

        async def healthy(url, timeout):
            calls.append(url)
            return True

        self.mgr.portainer.is_running = running
        self.mgr.portainer.wait_health = healthy
        await self._pre_call({"model": "test-model"})
        self.assertEqual(len(calls), 1)
        self.assertIn("test-container", self.mgr._health_confirmed)

    async def test_idle_stop_discards_health_confirmation(self):
        await self._pre_call({"model": "test-model"})
        self.assertIn("test-container", self.mgr._health_confirmed)
        self.mgr.in_flight = {}
        self.mgr.last_request_time = {"test-container": 0}

        async def probe(cfg):
            return "idle"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertEqual(self.stopped, ["test-container"])
        self.assertNotIn("test-container", self.mgr._health_confirmed)

    async def test_cold_start_clears_other_container_tokens(self):
        other_cfg = {
            "container": "other-container",
            "health": "https://fake.invalid/other-health",
            "boot_timeout": 90,
        }
        self.mgr.models["other-model"] = other_cfg
        self.mgr.lookup["other-model"] = other_cfg
        self.mgr.containers["other-container"] = "other-model"
        self.mgr.in_flight["other-container"] = {"stale-token": 1}

        async def is_running(container):
            return container != "test-container"

        self.mgr.portainer.is_running = is_running
        data = {"model": "test-model"}
        with patch("builtins.print") as mock_print:
            await self._pre_call(data)
        self.assertNotIn("stale-token", self.mgr.in_flight["other-container"])
        self.assertIn(self._token(data), self._tracked())
        self.assertEqual(self.started, ["test-container"])
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(
            any(
                "cleared 1 stale in-flight token(s) for other-container" in line
                for line in lines
            )
        )

    async def test_startup_hook_starts_watcher(self):
        with patch.object(
            cc.proxy_handler_instance, "_ensure_watcher"
        ) as ensure:
            cc.start_idle_watcher()
        ensure.assert_called_once()

    async def test_startup_hook_swallows_errors(self):
        with patch.object(
            cc.proxy_handler_instance,
            "_ensure_watcher",
            side_effect=RuntimeError("boom"),
        ):
            cc.start_idle_watcher()  # must not raise

    async def test_stale_token_cleared_after_repeated_backend_idle(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {"test-container": {"stale": 100}}
        self.mgr.stale_idle_confirmations = 2

        async def probe(cfg):
            return "idle"

        self.mgr._probe_backend_idle = probe
        with patch("builtins.print") as mock_print:
            await self.mgr._idle_tick(now=1000)
            self.assertIn("stale", self._tracked())
            self.assertEqual(self.stopped, [])
            await self.mgr._idle_tick(now=1001)
        self.assertNotIn("stale", self._tracked())
        self.assertEqual(self.stopped, ["test-container"])
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(
            any("backend idle confirmation 1/2" in line for line in lines)
        )
        self.assertTrue(
            any("cleared 1 stale in-flight token(s)" in line for line in lines)
        )

    async def test_stale_token_not_cleared_when_backend_busy(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {"test-container": {"stale": 100}}
        self.mgr.stale_idle_confirmations = 2

        async def probe(cfg):
            return "busy"

        self.mgr._probe_backend_idle = probe
        for now in (1000, 1001, 1002, 1003):
            await self.mgr._idle_tick(now=now)
        self.assertIn("stale", self._tracked())
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.mgr._backend_idle_streak["test-container"], 0)

    async def test_stale_token_not_cleared_when_probe_unknown(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {"test-container": {"stale": 100}}
        self.mgr.stale_idle_confirmations = 1

        async def probe(cfg):
            return "unknown"

        self.mgr._probe_backend_idle = probe
        await self.mgr._idle_tick(now=1000)
        self.assertIn("stale", self._tracked())
        self.assertEqual(self.stopped, [])

    async def test_new_admission_during_probe_prevents_clear(self):
        self.mgr.last_request_time = {"test-container": 0}
        self.mgr.in_flight = {"test-container": {"stale": 100}}
        self.mgr.stale_idle_confirmations = 1

        async def probe(cfg):
            self.mgr.in_flight["test-container"]["new"] = 101
            return "idle"

        self.mgr._probe_backend_idle = probe
        with patch("builtins.print") as mock_print:
            await self.mgr._idle_tick(now=1000)
        self.assertIn("stale", self._tracked())
        self.assertIn("new", self._tracked())
        self.assertEqual(self.stopped, [])
        self.assertEqual(self.mgr._backend_idle_streak["test-container"], 0)
        lines = [str(call) for call in mock_print.call_args_list]
        self.assertTrue(
            any(
                "admitted 1 new request(s) during the backend probe" in line
                for line in lines
            )
        )

    async def test_watcher_restart_resets_backend_streak(self):
        self.mgr._backend_idle_streak["test-container"] = 2
        self.mgr._init_watcher_state(now=1000)
        self.assertEqual(self.mgr._backend_idle_streak, {})


class TestIdleBackendProbe(_IdleManagerTestCase):
    def test_derive_metrics_url(self):
        self.assertEqual(
            self.mgr._derive_metrics_url("http://host:8084/health"),
            "http://host:8084/metrics",
        )
        self.assertEqual(
            self.mgr._derive_metrics_url("https://host/health?x=1"),
            "https://host/metrics",
        )
        self.assertIsNone(self.mgr._derive_metrics_url(None))
        self.assertIsNone(self.mgr._derive_metrics_url("not-a-url"))

    def test_metrics_url_derived_from_health(self):
        self.assertEqual(
            self.mgr.models["test-model"]["metrics"],
            "https://fake.invalid/metrics",
        )

    def test_parse_requires_both_gauges_zero(self):
        idle = (
            "# TYPE llamacpp:requests_processing gauge\n"
            "llamacpp:requests_processing 0\n"
            "llamacpp:requests_deferred 0\n"
        )
        self.assertEqual(self.mgr._parse_metrics_idle(idle), "idle")
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing 1\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "busy",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing 0\n"
                "llamacpp:requests_deferred 2\n"
            ),
            "busy",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle("llamacpp:requests_processing 0\n"),
            "unknown",
        )
        self.assertEqual(self.mgr._parse_metrics_idle(""), "unknown")
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing nan\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing -1\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )

    def test_parse_labels_and_multiple_samples(self):
        text = (
            'llamacpp:requests_processing{slot="0"} 0\n'
            'llamacpp:requests_processing{slot="1"} 0\n'
            'llamacpp:requests_deferred{slot="0"} 0\n'
            'llamacpp:requests_deferred{slot="1"} 0\n'
        )
        self.assertEqual(self.mgr._parse_metrics_idle(text), "idle")
        text = text.replace(
            'llamacpp:requests_deferred{slot="1"} 0',
            'llamacpp:requests_deferred{slot="1"} 1',
        )
        self.assertEqual(self.mgr._parse_metrics_idle(text), "busy")

    def test_parse_rejects_malformed_value_with_numeric_timestamp(self):
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing nonsense 0\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing 0 nonsense\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing 0 1 2\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )

    def test_parse_accepts_valid_timestamp(self):
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "llamacpp:requests_processing 0 1234567890\n"
                "llamacpp:requests_deferred 0\n"
            ),
            "idle",
        )

    def test_parse_rejects_malformed_labels(self):
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                'llamacpp:requests_processing{slot=0} 0\n'
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                'llamacpp:requests_processing{slot="0"}0\n'
                "llamacpp:requests_deferred 0\n"
            ),
            "unknown",
        )

    async def test_read_bounded_handles_partial_reads(self):
        class Content:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            async def read(self, n):
                if self.pos >= len(self.data):
                    return b""
                chunk = self.data[self.pos:self.pos + 1]
                self.pos += 1
                return chunk

        data = b"llamacpp:requests_processing 0\n"
        self.assertEqual(await self.mgr._read_bounded(Content(data)), data)

    async def test_read_bounded_accepts_exact_limit(self):
        class Content:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            async def read(self, n):
                chunk = self.data[self.pos:self.pos + n]
                self.pos += len(chunk)
                return chunk

        data = b"x" * 65536
        self.assertEqual(
            len(await self.mgr._read_bounded(Content(data))), 65536
        )

    async def test_read_bounded_rejects_oversize(self):
        class Content:
            def __init__(self, data):
                self.data = data
                self.pos = 0

            async def read(self, n):
                chunk = self.data[self.pos:self.pos + n]
                self.pos += len(chunk)
                return chunk

        data = b"x" * 65537
        self.assertIsNone(await self.mgr._read_bounded(Content(data)))

    @staticmethod
    def _fake_aiohttp(chunks):
        class FakeContent:
            def __init__(self):
                self.chunks = list(chunks)

            async def read(self, n):
                if not self.chunks:
                    return b""
                chunk = self.chunks.pop(0)
                if len(chunk) > n:
                    self.chunks.insert(0, chunk[n:])
                    return chunk[:n]
                return chunk

        class FakeResponse:
            status = 200

            def __init__(self):
                self.content = FakeContent()

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

        class FakeSession:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            def get(self, url, headers=None):
                return FakeResponse()

        class FakeAiohttp(types.ModuleType):
            ClientTimeout = lambda *a, **k: None
            ClientSession = FakeSession

        return FakeAiohttp("aiohttp")

    async def test_fetch_reads_split_payload_completely(self):
        fake = self._fake_aiohttp([
            b"llamacpp:requests_processing 0\nllamacpp:req",
            b"uests_deferred 0\n",
        ])
        with patch.dict(sys.modules, {"aiohttp": fake}):
            text = await self.mgr._fetch_metrics_text("http://host/metrics")
        self.assertEqual(self.mgr._parse_metrics_idle(text), "idle")

    async def test_fetch_rejects_oversized_payload(self):
        fake = self._fake_aiohttp([b"x" * 65536, b"y"])
        with patch.dict(sys.modules, {"aiohttp": fake}):
            text = await self.mgr._fetch_metrics_text("http://host/metrics")
        self.assertIsNone(text)

    async def test_probe_returns_unknown_without_metrics_url(self):
        self.assertEqual(await self.mgr._probe_backend_idle({}), "unknown")
        self.assertEqual(await self.mgr._probe_backend_idle(None), "unknown")

    async def test_probe_uses_fetch_and_parser(self):
        async def fetch(url, headers=None):
            self.assertEqual(url, "http://host:8084/metrics")
            self.assertIsNone(headers)
            return (
                "llamacpp:requests_processing 0\n"
                "llamacpp:requests_deferred 0\n"
            )

        self.mgr._fetch_metrics_text = fetch
        cfg = {"metrics": "http://host:8084/metrics"}
        self.assertEqual(await self.mgr._probe_backend_idle(cfg), "idle")

    async def test_probe_fetch_failure_is_unknown(self):
        async def fetch(url, headers=None):
            return None

        self.mgr._fetch_metrics_text = fetch
        cfg = {"metrics": "http://host:8084/metrics"}
        self.assertEqual(await self.mgr._probe_backend_idle(cfg), "unknown")

    # ------------------------------------------------------------------
    # vLLM-style metrics (Strata) and a keyed /metrics endpoint
    # ------------------------------------------------------------------
    def test_parse_accepts_vllm_gauge_family(self):
        idle = (
            "# HELP vllm:num_requests_running Requests reading their prompt.\n"
            "# TYPE vllm:num_requests_running gauge\n"
            "vllm:num_requests_running 0\n"
            "vllm:num_requests_waiting 0\n"
            "vllm:prompt_tokens_total 1234\n"
        )
        self.assertEqual(self.mgr._parse_metrics_idle(idle), "idle")
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "vllm:num_requests_running 1\n"
                "vllm:num_requests_waiting 0\n"
            ),
            "busy",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "vllm:num_requests_running 0\n"
                "vllm:num_requests_waiting 3\n"
            ),
            "busy",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle("vllm:num_requests_running 0\n"),
            "unknown",
        )
        self.assertEqual(
            self.mgr._parse_metrics_idle(
                "vllm:num_requests_running -1\n"
                "vllm:num_requests_waiting 0\n"
            ),
            "unknown",
        )

    def test_parse_llamacpp_family_wins_when_both_present(self):
        # A backend exposing both dialect families: llama.cpp's gauges are
        # consulted first, so loader behaviour stays byte-identical.
        text = (
            "llamacpp:requests_processing 0\n"
            "llamacpp:requests_deferred 0\n"
            "vllm:num_requests_running 4\n"
            "vllm:num_requests_waiting 0\n"
        )
        self.assertEqual(self.mgr._parse_metrics_idle(text), "idle")

    def test_metrics_headers_from_env(self):
        self.assertIsNone(self.mgr._metrics_headers({}))
        self.assertIsNone(self.mgr._metrics_headers(None))
        # Configured but unset: no header, so the probe fails safe.
        self.assertIsNone(
            self.mgr._metrics_headers({"metrics_key_env": "TEST_STRATA_KEY"})
        )
        with patch.dict(os.environ, {"TEST_STRATA_KEY": "  "}):
            self.assertIsNone(
                self.mgr._metrics_headers({"metrics_key_env": "TEST_STRATA_KEY"})
            )
        with patch.dict(os.environ, {"TEST_STRATA_KEY": "s3cret"}):
            self.assertEqual(
                self.mgr._metrics_headers({"metrics_key_env": "TEST_STRATA_KEY"}),
                {"Authorization": "Bearer s3cret"},
            )

    async def test_probe_sends_bearer_header_for_keyed_metrics(self):
        seen = {}

        async def fetch(url, headers=None):
            seen["url"] = url
            seen["headers"] = headers
            return "vllm:num_requests_running 0\nvllm:num_requests_waiting 0\n"

        self.mgr._fetch_metrics_text = fetch
        cfg = {
            "metrics": "http://host:8090/metrics?format=prometheus",
            "metrics_key_env": "TEST_STRATA_KEY",
        }
        with patch.dict(os.environ, {"TEST_STRATA_KEY": "s3cret"}):
            verdict = await self.mgr._probe_backend_idle(cfg)
        self.assertEqual(verdict, "idle")
        self.assertEqual(
            seen["headers"], {"Authorization": "Bearer s3cret"}
        )
        self.assertEqual(
            seen["url"], "http://host:8090/metrics?format=prometheus"
        )

    def test_model_entry_carries_metrics_key_env(self):
        # The per-model field reaches the probe unchanged; entries without it
        # stay anonymous (llama.cpp's /metrics needs no key).
        self.assertIsNone(self.mgr.models["test-model"]["metrics_key_env"])
        with patch.dict(
            os.environ,
            {
                "MODEL_CONTAINERS": (
                    '{"strata-model":{"container":"strata",'
                    '"health":"http://host:8090/health",'
                    '"metrics":"http://host:8090/metrics?format=prometheus",'
                    '"metrics_key_env":"TEST_STRATA_KEY"}}'
                )
            },
        ):
            mgr = cc.LlamaCppIdleManager()
        self.assertEqual(
            mgr.models["strata-model"]["metrics_key_env"], "TEST_STRATA_KEY"
        )
        self.assertEqual(
            mgr.models["strata-model"]["metrics"],
            "http://host:8090/metrics?format=prometheus",
        )


class TestProcessSingleton(unittest.IsolatedAsyncioTestCase):
    """LiteLLM imports custom_callbacks through two different loaders.

    The startup hook uses importlib.import_module; config callback loading
    uses spec_from_file_location/exec_module, which creates a second module
    object. Both must resolve to the same proxy handler, watcher, and clock.
    """

    @staticmethod
    def _load_dynamic(name):
        spec = importlib.util.spec_from_file_location(
            name, ROOT / "custom_callbacks.py"
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_duplicate_module_imports_share_manager_and_clock(self):
        startup = self._load_dynamic("_cc_dynamic_startup")
        config = self._load_dynamic("_cc_dynamic_config")
        manager = cc.proxy_handler_instance

        self.assertIsNotNone(manager)
        self.assertIs(startup.proxy_handler_instance, manager)
        self.assertIs(config.proxy_handler_instance, manager)
        # One activity clock: both module objects see the same dict.
        self.assertIs(
            startup.proxy_handler_instance.last_request_time,
            manager.last_request_time,
        )
        self.assertIs(
            config.proxy_handler_instance.last_request_time,
            manager.last_request_time,
        )
        # Direct construction stays independent for tests.
        self.assertIsNot(cc.LlamaCppIdleManager(), manager)

    async def test_startup_and_config_hooks_start_one_watcher(self):
        startup = self._load_dynamic("_cc_dynamic_startup_hook")
        config = self._load_dynamic("_cc_dynamic_config_hook")
        manager = cc.proxy_handler_instance
        manager._watcher_started = False
        manager._watcher_task = None

        startup.start_idle_watcher()
        task = manager._watcher_task
        self.assertIsNotNone(task)
        self.assertTrue(manager._watcher_started)

        config.start_idle_watcher()
        self.assertIs(manager._watcher_task, task)
        self.assertTrue(manager._watcher_started)

        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task


class TestInferenceMetrics(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.cb = cc.InferenceMetrics()
        self.now = 100.0
        self.clock = patch.object(cc.time, "perf_counter", lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    async def _request(self, **extra):
        data = {"stream": True, "metadata": {"keep": "value"}, **extra}
        return await self.cb.async_pre_call_hook(None, None, data, "acompletion")

    @staticmethod
    def _chunk(delta=None, usage=None):
        return {
            "object": "chat.completion.chunk",
            "choices": [{"delta": delta}] if delta is not None else [],
            "usage": usage,
        }

    async def _collect(self, data, timed_chunks):
        async def source():
            for timestamp, chunk in timed_chunks:
                self.now = timestamp
                yield chunk

        return [
            item async for item in self.cb.async_post_call_streaming_iterator_hook(
                None, source(), data
            )
        ]

    async def test_metrics_use_total_time_and_provider_tokens(self):
        data = await self._request()
        usage = {
            "prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150,
            "completion_tokens_details": {"reasoning_tokens": 40},
            "provider_field": "preserved",
        }
        chunks = [
            (101, self._chunk({"role": "assistant", "content": ""})),
            (102, self._chunk({"reasoning_content": "think"})),
            (103, self._chunk({"content": "answer"})),
            (105, self._chunk(usage=usage)),
        ]
        result = await self._collect(data, chunks)
        self.assertEqual(len(result), len(chunks))
        self.assertIs(result[0], chunks[0][1])
        self.assertEqual(data["metadata"]["keep"], "value")
        self.assertEqual(usage["Speed [tok/s]"], 20)
        self.assertEqual(usage["First output [s]"], 2)
        self.assertEqual(usage["Total time [s]"], 5)
        self.assertFalse(any(key.startswith("proxy_") for key in usage))
        self.assertEqual(usage["completion_tokens"], 100)  # no double-counted reasoning
        self.assertEqual(usage["provider_field"], "preserved")

    async def test_object_chunks_and_tool_call_first_output(self):
        data = await self._request()
        chunk = types.SimpleNamespace(
            object="chat.completion.chunk",
            choices=[types.SimpleNamespace(delta=types.SimpleNamespace(
                tool_calls=[{"id": "call-1"}]
            ))],
            usage=None,
        )
        usage = types.SimpleNamespace(completion_tokens=12)
        final = types.SimpleNamespace(
            object="chat.completion.chunk", choices=[], usage=usage
        )
        await self._collect(data, [(101, chunk), (104, final)])
        self.assertEqual(getattr(usage, "Speed [tok/s]"), 3)
        self.assertEqual(getattr(usage, "First output [s]"), 1)

    async def test_missing_usage_does_not_fabricate_counts_or_chunks(self):
        data = await self._request()
        chunk = self._chunk({"content": "text"})
        self.assertEqual(await self._collect(data, [(102, chunk)]), [chunk])
        self.assertIsNone(chunk["usage"])

    async def test_zero_invalid_or_missing_tokens_unchanged(self):
        for tokens in (None, 0, -1, True, "10", float("nan"), float("inf")):
            data = await self._request()
            usage = {"completion_tokens": tokens}
            await self._collect(data, [
                (101, self._chunk({"content": "text"})),
                (103, self._chunk(usage=usage)),
            ])
            self.assertEqual(set(usage), {"completion_tokens"})
            self.now = 100

    async def test_empty_stream_no_first_output_and_zero_duration(self):
        data = await self._request()
        self.assertEqual(await self._collect(data, []), [])
        usage = {"completion_tokens": 10}
        await self._collect(data, [(102, self._chunk(usage=usage))])
        self.assertEqual(usage, {"completion_tokens": 10})
        await self._collect(data, [(100, self._chunk({"content": "x"}, usage))])
        self.assertEqual(usage, {"completion_tokens": 10})

    async def test_nonstream_and_native_responses_untouched(self):
        for stream, call_type in ((False, "acompletion"), (True, "aresponses"),
                                  (True, "transcription")):
            data = {"stream": stream, "metadata": {"keep": "value"}}
            await self.cb.async_pre_call_hook(None, None, data, call_type)
            self.assertNotIn(self.cb.START_KEY, data["metadata"])
        data = await self._request()
        native = {"type": "response.completed", "usage": {"completion_tokens": 10}}
        await self._collect(data, [(102, native)])
        self.assertEqual(native["usage"], {"completion_tokens": 10})

    async def test_concurrent_requests_have_independent_timing(self):
        first = await self._request()
        self.now = 102
        second = await self._request()
        self.now = 105

        async def run(data):
            async def source():
                yield self._chunk({"content": "x"})
                await asyncio.sleep(0)
                yield self._chunk(usage={"completion_tokens": 30})
            return [item async for item in
                    self.cb.async_post_call_streaming_iterator_hook(None, source(), data)]

        a, b = await asyncio.gather(run(first), run(second))
        self.assertEqual(a[-1]["usage"]["Total time [s]"], 5)
        self.assertEqual(b[-1]["usage"]["Total time [s]"], 3)
        self.assertEqual(a[-1]["usage"]["Speed [tok/s]"], 6)
        self.assertEqual(b[-1]["usage"]["Speed [tok/s]"], 10)

    async def test_tool_placeholders_do_not_start_first_output(self):
        data = await self._request()
        usage = {"completion_tokens": 10}
        await self._collect(data, [
            (101, self._chunk({"tool_calls": [{"index": 0, "type": "function"}]})),
            (102, self._chunk({"tool_calls": [{"function": {"name": "", "arguments": ""}}]})),
            (103, self._chunk({"function_call": {"name": "", "arguments": ""}})),
            (104, self._chunk({"tool_calls": [{"function": {"arguments": "{"}}]})),
            (105, self._chunk(usage=usage)),
        ])
        self.assertEqual(usage["First output [s]"], 4)

    async def test_legacy_function_name_starts_first_output(self):
        data = await self._request()
        usage = {"completion_tokens": 10}
        await self._collect(data, [
            (101, self._chunk({"function_call": {"name": "lookup"}})),
            (103, self._chunk(usage=usage)),
        ])
        self.assertEqual(usage["First output [s]"], 1)

    async def test_cleanup_failure_does_not_mask_upstream_error(self):
        data = await self._request()

        class BrokenStream:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise RuntimeError("original provider error")

            async def aclose(self):
                raise ValueError("private cleanup details")

        with self.assertLogs(cc.__name__, level="WARNING") as logs:
            with self.assertRaisesRegex(RuntimeError, "original provider error"):
                _ = [item async for item in self.cb.async_post_call_streaming_iterator_hook(
                    None, BrokenStream(), data
                )]
        self.assertNotIn("original provider error", str(logs.output))
        self.assertNotIn("private cleanup details", str(logs.output))

    async def test_readonly_usage_does_not_break_response(self):
        data = await self._request()

        class ReadOnlyUsage:
            __slots__ = ()
            completion_tokens = 10

        usage = ReadOnlyUsage()
        final = self._chunk(usage=usage)
        with self.assertLogs(cc.__name__, level="WARNING"):
            chunks = await self._collect(data, [
                (101, self._chunk({"content": "answer"})),
                (105, final),
            ])
        self.assertIs(chunks[-1], final)
        self.assertEqual(usage.completion_tokens, 10)

    async def test_cleanup_failure_does_not_break_completed_stream(self):
        data = await self._request()

        class BrokenClose:
            def __aiter__(self):
                return self

            async def __anext__(self):
                raise StopAsyncIteration

            async def aclose(self):
                raise RuntimeError("cleanup failed")

        with self.assertLogs(cc.__name__, level="WARNING"):
            result = [item async for item in self.cb.async_post_call_streaming_iterator_hook(
                None, BrokenClose(), data
            )]
        self.assertEqual(result, [])

    async def test_failure_propagates_and_closes_upstream(self):
        data = await self._request()
        closed = []

        async def source():
            try:
                yield self._chunk({"content": "partial"})
                raise RuntimeError("upstream failed")
            finally:
                closed.append(True)

        stream = self.cb.async_post_call_streaming_iterator_hook(None, source(), data)
        with self.assertRaisesRegex(RuntimeError, "upstream failed"):
            _ = [item async for item in stream]
        self.assertEqual(closed, [True])

    async def test_cancelled_consumer_closes_upstream(self):
        data = await self._request()
        closed = []

        async def source():
            try:
                yield self._chunk({"content": "partial"})
                yield self._chunk({"content": "unused"})
            finally:
                closed.append(True)

        stream = self.cb.async_post_call_streaming_iterator_hook(None, source(), data)
        await anext(stream)
        await stream.aclose()
        self.assertEqual(closed, [True])

    async def test_task_cancellation_propagates_and_closes_upstream(self):
        data = await self._request()
        waiting = asyncio.Event()
        closed = []

        async def source():
            try:
                yield self._chunk({"content": "partial"})
                waiting.set()
                await asyncio.Event().wait()
            finally:
                closed.append(True)

        async def consume():
            async for _ in self.cb.async_post_call_streaming_iterator_hook(
                None, source(), data
            ):
                pass

        task = asyncio.create_task(consume())
        await waiting.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertEqual(closed, [True])


def _main():
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"ok  {test.__name__}")
    suite = unittest.TestSuite()
    for test_case in (
        TestVoxtralVoiceAlias,
        TestVoxtralSttContextBias,
        TestIdleInFlight,
        TestIdleLifecycle,
        TestIdleBackendProbe,
        TestProcessSingleton,
        TestInferenceMetrics,
    ):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(test_case))
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"{len(tests) + suite.countTestCases()} passed")


if __name__ == "__main__":
    _main()
