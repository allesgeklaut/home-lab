"""Unit tests for the custom_callbacks hook classes.

Covers OpenCodeGoSessionHeader (session/header injection) and
NemotronBoltThinking (reasoning_effort -> chat_template_kwargs mapping), plus
stream timing/throughput and cleanup in InferenceMetrics.

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


def _bolt_data(**extra):
    data = {
        "model": "nemotron-bolt",
        "messages": [{"role": "user", "content": "hi"}],
    }
    data.update(extra)
    return data


def test_bolt_low_disables_thinking():
    cb = cc.NemotronBoltThinking()
    for effort in ("minimal", "low", "none", "off"):
        out = asyncio.run(
            cb.async_pre_call_hook(
                None, None, _bolt_data(reasoning_effort=effort), "acompletion"
            )
        )
        assert out["chat_template_kwargs"] == {"enable_thinking": False}


def test_bolt_medium_and_high_mapping():
    cb = cc.NemotronBoltThinking()
    out = asyncio.run(
        cb.async_pre_call_hook(
            None, None, _bolt_data(reasoning_effort="medium"), "acompletion"
        )
    )
    assert out["chat_template_kwargs"] == {
        "enable_thinking": True,
        "medium_effort": True,
    }
    for effort in ("high", "xhigh"):
        out = asyncio.run(
            cb.async_pre_call_hook(
                None, None, _bolt_data(reasoning_effort=effort), "acompletion"
            )
        )
        assert out["chat_template_kwargs"] == {
            "enable_thinking": True,
            "medium_effort": False,
        }


def test_bolt_no_or_unknown_effort_untouched():
    cb = cc.NemotronBoltThinking()
    for extra in ({}, {"reasoning_effort": "turbo"}):
        out = asyncio.run(
            cb.async_pre_call_hook(None, None, _bolt_data(**extra), "acompletion")
        )
        assert "chat_template_kwargs" not in out


def test_bolt_client_kwargs_win():
    cb = cc.NemotronBoltThinking()
    out = asyncio.run(
        cb.async_pre_call_hook(
            None,
            None,
            _bolt_data(
                reasoning_effort="low",
                chat_template_kwargs={"enable_thinking": True, "custom": 1},
            ),
            "acompletion",
        )
    )
    # explicit client enable_thinking is kept; the low mapping adds nothing else
    assert out["chat_template_kwargs"] == {"enable_thinking": True, "custom": 1}


def test_bolt_other_models_untouched():
    cb = cc.NemotronBoltThinking()
    d = {
        "model": "qwen3.8-27b",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "low",
    }
    out = asyncio.run(cb.async_pre_call_hook(None, None, d, "acompletion"))
    assert out == d
    assert "chat_template_kwargs" not in out


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


class TestIdleInFlight(unittest.TestCase):
    def setUp(self):
        env = {
            "PORTAINER_URL": "https://fake.invalid:9443",
            "PORTAINER_API_KEY": "test-not-real",
            "PORTAINER_ENV_ID": "1",
            "PORTAINER_CA_FILE": "",
            "MODEL_CONTAINERS": (
                '{"test-model":{"container":"test-container",'
                '"health":"https://fake.invalid/health"}}'
            ),
        }
        previous = {key: os.environ.get(key) for key in env}
        os.environ.update(env)
        self.addCleanup(self._restore_env, previous)

        self.mgr = cc.LlamaCppIdleManager()
        self.mgr._watcher_started = True

    @staticmethod
    def _restore_env(previous):
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def _dispatch(self, call_id):
        data = {"model": "test-model", "litellm_call_id": call_id}
        return asyncio.run(
            self.mgr.async_pre_call_hook(None, None, data, "generic")
        )

    def test_dispatch_and_release(self):
        self._dispatch("c1")
        tracked = self.mgr.in_flight["test-container"]
        self.assertIn("c1", tracked)

        self.mgr._release_in_flight(
            {"model": "test-model", "litellm_call_id": "c1"}
        )
        self.assertEqual(tracked, set())
        self.mgr._release_in_flight(
            {"model": "test-model", "litellm_call_id": "c1"}
        )
        self.assertEqual(tracked, set())

    def test_two_requests_double_release(self):
        self._dispatch("c1")
        self._dispatch("c2")

        self.mgr._release_in_flight(
            {"model": "test-model", "litellm_call_id": "c1"}
        )
        self.mgr._release_in_flight(
            {"model": "test-model", "litellm_call_id": "c1"}
        )
        self.assertEqual(self.mgr.in_flight["test-container"], {"c2"})

        self.mgr._release_in_flight(
            {"model": "test-model", "litellm_call_id": "c2"}
        )
        self.assertEqual(self.mgr.in_flight["test-container"], set())

    def test_hook_exception_releases(self):
        original = self.mgr.portainer.is_running

        async def raising(container):
            raise RuntimeError("fake Portainer failure")

        self.mgr.portainer.is_running = raising
        self.addCleanup(
            setattr, self.mgr.portainer, "is_running", original
        )

        with self.assertRaises(RuntimeError):
            self._dispatch("c1")
        self.assertEqual(self.mgr.in_flight["test-container"], set())

    def test_idle_guard(self):
        tracked = self.mgr.in_flight.setdefault("test-container", set())
        tracked.add("c1")
        self.assertTrue(self.mgr.in_flight["test-container"])
        tracked.discard("c1")
        self.assertFalse(self.mgr.in_flight["test-container"])


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
        TestInferenceMetrics,
    ):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(test_case))
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"{len(tests) + suite.countTestCases()} passed")


if __name__ == "__main__":
    _main()
