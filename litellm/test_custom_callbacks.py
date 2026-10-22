"""Unit tests for the custom_callbacks hook classes.

Covers OpenCodeGoSessionHeader (session/header injection) and
NemotronBoltThinking (reasoning_effort -> chat_template_kwargs mapping).

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
    ):
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(test_case))
    result = unittest.TextTestRunner(verbosity=0).run(suite)
    if not result.wasSuccessful():
        raise SystemExit(1)
    print(f"{len(tests) + suite.countTestCases()} passed")


if __name__ == "__main__":
    _main()
