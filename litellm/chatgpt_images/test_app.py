"""Offline regression tests: no subscription or paid API calls."""

import asyncio
import base64
import gzip
import io
import json
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from PIL import Image

import app

KEY = b"test-only-gateway-key"
HEADERS = {"Authorization": "Bearer " + KEY.decode()}

EDIT_FIELDS = {
    "model": app.MODEL, "prompt": "Recolor the gem magenta", "n": "1",
    "response_format": "b64_json", "output_format": "png", "quality": "high",
}


def png_bytes():
    data = io.BytesIO()
    Image.new("RGBA", (2, 2), (0, 0, 0, 0)).save(data, format="PNG")
    return data.getvalue()


def png():
    return base64.b64encode(png_bytes()).decode()


def jpeg_bytes():
    data = io.BytesIO()
    Image.new("RGB", (3, 3), (0, 200, 120)).save(data, format="JPEG")
    return data.getvalue()


def animated_webp_bytes():
    frames = [Image.new("RGBA", (2, 2), (255, 0, 0, 255)), Image.new("RGBA", (2, 2), (0, 0, 255, 255))]
    data = io.BytesIO()
    frames[0].save(data, format="WEBP", save_all=True, append_images=frames[1:], duration=100, loop=1)
    return data.getvalue()


def big_jpeg_bytes():
    import random

    rng = random.Random(7)
    image = Image.new("RGB", (1400, 1400))
    image.putdata([(rng.randrange(256), rng.randrange(256), rng.randrange(256))
                   for _ in range(1400 * 1400)])
    data = io.BytesIO()
    image.save(data, format="JPEG", quality=95)
    return data.getvalue()


def sse(*events):
    return b"".join(b"data: " + json.dumps(event).encode() + b"\r\n\r\n" for event in events)


def success():
    return sse(
        {"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": png()}},
        {"type": "response.completed", "response": {"status": "completed"}},
    )


class FakeUpstreamMixin:
    """Shared fake Responses upstream: records calls, never logs payloads."""

    async def start_fake_upstream(self):
        self.calls = []
        self.body = success()
        self.status = 200
        self.response_headers = {"Content-Type": "text/event-stream"}
        self.wait = False
        self.started = asyncio.Event()
        self.cancelled = asyncio.Event()

        async def upstream(request):
            self.calls.append({"path": request.path, "auth": request.headers.get("Authorization"),
                               "payload": await request.json()})
            self.started.set()
            if self.wait:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.cancelled.set()
                    raise
            return web.Response(body=self.body, status=self.status, headers=self.response_headers)

        fake = web.Application(client_max_size=32 * 1024 * 1024)
        fake.router.add_post("/v1/responses", upstream)
        self.upstream = TestServer(fake, handler_cancellation=True)
        await self.upstream.start_server()

    async def stop_fake_upstream(self):
        await self.upstream.close()


class AdapterTests(FakeUpstreamMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.start_fake_upstream()
        self.adapter = app.create_app(app.Settings(
            key=KEY, upstream=str(self.upstream.make_url("/v1/responses")), timeout=2,
        ))
        self.client = TestClient(TestServer(self.adapter, handler_cancellation=True, auto_decompress=False))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.stop_fake_upstream()

    async def post(self, payload=None, **kwargs):
        if payload is None:
            payload = {"model": app.MODEL, "prompt": "A cyan gemstone"}
        return await self.client.post("/v1/images/generations", json=payload, headers=HEADERS, **kwargs)

    async def test_health_and_authenticated_model_discovery(self):
        self.assertEqual((await self.client.get("/health")).status, 200)
        self.assertEqual((await self.client.get("/v1/models")).status, 401)
        response = await self.client.get("/v1/models", headers=HEADERS)
        self.assertEqual((await response.json())["data"][0]["id"], app.MODEL)
        self.assertFalse(self.calls)

    async def test_auth_before_body_parsing(self):
        response = await self.client.post("/v1/images/generations", data=b"not json")
        self.assertEqual(response.status, 401)
        response = await self.client.post("/v1/images/generations", json={},
                                          headers={"Authorization": "Bearer wrong"})
        self.assertEqual(response.status, 401)
        self.assertFalse(self.calls)

    async def test_success_preserves_png_and_pins_subscription_route(self):
        response = await self.post({"prompt": "Gem", "n": 1, "quality": "medium", "background": "transparent",
                                    "response_format": "b64_json", "size": "256x256", "user": "not-forwarded"})
        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        data = await response.json()
        self.assertEqual(data["data"], [{"b64_json": png()}])
        call = self.calls[0]
        self.assertEqual(call["path"], "/v1/responses")
        self.assertEqual(call["auth"], HEADERS["Authorization"])
        payload = call["payload"]
        self.assertEqual(payload["model"], "chatgpt-gpt-6.1-sol")
        self.assertEqual(payload["tool_choice"], {"type": "image_generation"})
        self.assertEqual(payload["tools"], [{"type": "image_generation", "model": "gpt-image-2.5-sunburst",
                                              "output_format": "png", "quality": "medium"}])
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        self.assertNotIn("user", payload)
        self.assertEqual(len(self.calls), 1)

    async def test_defaults_pin_quality_model_and_accept_string_count(self):
        await self.post({"prompt": "Gem"})
        tool = self.calls[0]["payload"]["tools"][0]
        self.assertEqual(tool["model"], "gpt-image-2.5-sunburst")
        self.assertEqual(tool["quality"], "high")
        await self.post({"prompt": "Gem", "n": "1"})
        self.assertEqual(len(self.calls), 2)

    async def test_invalid_parameters_never_reach_upstream(self):
        invalid = [
            {"model": "openai/gpt-image-1"}, {"n": 2}, {"n": True},
            {"prompt": ""}, {"prompt": "x" * (app.MAX_PROMPT + 1)}, {"prompt": None},
            {"prompt": []}, {"prompt": "\ud800"}, {"size": "1x1"}, {"size": None}, {"size": "9999x9999"},
            {"response_format": "url"}, {"output_format": "jpeg"}, {"quality": "invalid"},
            {"quality": "max"}, {"quality": []}, {"background": "bad"},
            {"api_base": "https://example.invalid"}, {"tools": []}, {"stream": True},
            {"api_key": "never-forward"},
        ]
        for index, override in enumerate(invalid):
            with self.subTest(case=index):
                response = await self.post({"prompt": "Gem", **override})
                self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)

    async def test_invalid_json_and_content_types(self):
        for body in [b"[1,2]", b"{bad json", b"null"]:
            response = await self.client.post("/v1/images/generations", data=body,
                                              headers={**HEADERS, "Content-Type": "application/json"})
            self.assertEqual(response.status, 400)
        response = await self.client.post("/v1/images/generations", data=b"{}", headers=HEADERS)
        self.assertEqual(response.status, 415)
        response = await self.client.post("/v1/images/generations", data=gzip.compress(b"{}"),
                                          headers={**HEADERS, "Content-Encoding": "gzip"})
        self.assertEqual(response.status, 415)
        self.assertFalse(self.calls)

    async def test_body_limit_known_length_and_chunked(self):
        response = await self.client.post("/v1/images/generations", data=b"x" * (app.MAX_BODY + 1),
                                          headers={**HEADERS, "Content-Type": "application/json"})
        self.assertEqual(response.status, 413)

        async def chunks():
            for _ in range(6):
                yield b"x" * 4096

        response = await self.client.post("/v1/images/generations", data=chunks(),
                                          headers={**HEADERS, "Content-Type": "application/json"})
        self.assertEqual(response.status, 413)
        self.assertFalse(self.calls)

    async def test_upstream_error_redaction_and_no_retry(self):
        for status, expected in [(401, 502), (403, 502), (500, 502), (429, 429)]:
            with self.subTest(status=status):
                self.status = status
                self.body = b"sensitive provider exception test-only-gateway-key"
                self.calls.clear()
                response = await self.post()
                self.assertEqual(response.status, expected)
                text = await response.text()
                self.assertNotIn("sensitive", text)
                self.assertNotIn(KEY.decode(), text)
                self.assertEqual(len(self.calls), 1)

    async def test_redirects_are_not_followed(self):
        self.status = 307
        self.response_headers["Location"] = "http://example.invalid/credentials"
        response = await self.post()
        self.assertEqual(response.status, 502)
        self.assertEqual(len(self.calls), 1)

    async def test_no_image_or_incomplete_stream_is_not_success(self):
        for body in [
            b"data: {}", b"data: invalid\n\n", b"data: []\n\n",
            sse({"type": "response.completed", "response": {"status": "completed"}}),
            sse({"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": png()}}),
            sse({"type": "response.failed", "response": {"error": {"message": "secret"}}}),
            sse({"type": "response.incomplete"}),
            sse({"type": "error", "error": {"message": "secret"}}),
            sse({"type": "response.completed", "response": {"status": "failed"}}),
        ]:
            self.body = body
            response = await self.post()
            self.assertEqual(response.status, 502, body[:60])
            self.assertNotIn("secret", await response.text())

    async def test_missing_invalid_url_and_non_png_images(self):
        for encoded in [None, "!bad-base64", "https://example.invalid/image.png", base64.b64encode(b"not png").decode()]:
            self.body = sse({"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": encoded}})
            response = await self.post()
            self.assertEqual(response.status, 502)

    async def test_multiple_images_rejected(self):
        image = {"type": "response.output_item.done", "item": {"type": "image_generation_call", "result": png()}}
        self.body = sse(image, image, {"type": "response.completed", "response": {"status": "completed"}})
        self.assertEqual((await self.post()).status, 502)

    async def test_limits_on_stream_event_image_and_pixels(self):
        for name, value in [("MAX_STREAM", 32), ("MAX_EVENT", 32), ("MAX_IMAGE", 8), ("MAX_PIXELS", 1)]:
            with self.subTest(limit=name), patch.object(app, name, value):
                response = await self.post()
                self.assertEqual(response.status, 502)

    async def test_timeout_closes_upstream(self):
        self.wait = True
        object.__setattr__(self.adapter[app.STATE].settings, "timeout", 0.1)
        response = await self.post()
        self.assertEqual(response.status, 504)
        await asyncio.wait_for(self.cancelled.wait(), 1)
        self.assertFalse(self.adapter[app.STATE].busy)

    async def test_concurrency_returns_429_without_queueing(self):
        self.wait = True
        pending = asyncio.create_task(self.post())
        await asyncio.wait_for(self.started.wait(), 1)
        response = await self.post()
        self.assertEqual(response.status, 429)
        self.assertEqual(len(self.calls), 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending

    async def test_client_disconnect_cancels_upstream_and_releases_slot(self):
        self.wait = True
        pending = asyncio.create_task(self.post())
        await asyncio.wait_for(self.started.wait(), 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(self.cancelled.wait(), 1)
        self.assertFalse(self.adapter[app.STATE].busy)

    async def test_variations_not_supported(self):
        response = await self.client.post("/v1/images/variations", json={}, headers=HEADERS)
        self.assertEqual(response.status, 404)
        self.assertFalse(self.calls)


class EditTests(FakeUpstreamMixin, unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        await self.start_fake_upstream()
        self.adapter = app.create_app(app.Settings(
            key=KEY, upstream=str(self.upstream.make_url("/v1/responses")), timeout=2,
        ))
        self.client = TestClient(TestServer(self.adapter, handler_cancellation=True))
        await self.client.start_server()

    async def asyncTearDown(self):
        await self.client.close()
        await self.stop_fake_upstream()

    def edit_form(self, fields, image=None, content_type="image/png", field="image"):
        form = aiohttp.FormData()
        for key, value in fields.items():
            form.add_field(key, value)
        if image is not None:
            form.add_field(field, image, filename="gem.png", content_type=content_type)
        return form

    async def edit(self, fields, image=None, content_type="image/png", field="image"):
        return await self.client.post("/v1/images/edits", data=self.edit_form(fields, image, content_type, field),
                                      headers=HEADERS)

    async def test_edit_success_and_pinned_payload(self):
        fields = {**EDIT_FIELDS, "size": "1024x1024", "background": "transparent"}
        response = await self.edit(fields, image=png_bytes())
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["data"], [{"b64_json": png()}])
        payload = self.calls[0]["payload"]
        self.assertEqual(payload["model"], "chatgpt-gpt-6.1-sol")
        self.assertEqual(payload["tools"], [{"type": "image_generation", "model": "gpt-image-2.5-sunburst",
                                              "output_format": "png", "quality": "high", "action": "edit"}])
        self.assertEqual(payload["tool_choice"], {"type": "image_generation"})
        self.assertTrue(payload["stream"])
        self.assertFalse(payload["store"])
        content = payload["input"][0]["content"]
        self.assertEqual(content[0]["type"], "input_text")
        self.assertIn("Recolor", content[0]["text"])
        self.assertIn("Requested canvas: 1024x1024", content[0]["text"])
        self.assertIn("alpha-zero", content[0]["text"])
        self.assertEqual(content[1]["type"], "input_image")
        self.assertTrue(content[1]["image_url"].startswith("data:image/png;base64,"))
        self.assertEqual(len(self.calls), 1)

    async def test_edit_accepts_jpeg_input(self):
        response = await self.edit(EDIT_FIELDS, image=jpeg_bytes(), content_type="image/jpeg")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["data"], [{"b64_json": png()}])
        payload = self.calls[0]["payload"]
        self.assertTrue(payload["input"][0]["content"][1]["image_url"].startswith("data:image/jpeg;base64,"))

    async def test_edit_rejects_animated_input(self):
        response = await self.edit(EDIT_FIELDS, image=animated_webp_bytes(), content_type="image/webp")
        self.assertEqual(response.status, 415)
        self.assertFalse(self.calls)

    async def test_edit_accepts_large_multi_mib_input(self):
        # >1 MiB forces aiohttp's multipart spool to disk; must still work.
        response = await self.edit(EDIT_FIELDS, image=big_jpeg_bytes(), content_type="image/jpeg")
        self.assertEqual(response.status, 200)
        data = await response.json()
        self.assertEqual(data["data"], [{"b64_json": png()}])

    async def test_edit_requires_auth(self):
        response = await self.client.post("/v1/images/edits", data=self.edit_form(EDIT_FIELDS, image=png_bytes()))
        self.assertEqual(response.status, 401)
        self.assertFalse(self.calls)

    async def test_edit_rejects_non_multipart(self):
        response = await self.client.post("/v1/images/edits", json=EDIT_FIELDS, headers=HEADERS)
        self.assertEqual(response.status, 415)
        self.assertFalse(self.calls)

    async def test_edit_file_rules(self):
        response = await self.edit(EDIT_FIELDS, image=None)
        self.assertEqual(response.status, 400)
        form = aiohttp.FormData()
        for key, value in EDIT_FIELDS.items():
            form.add_field(key, value)
        form.add_field("image", png_bytes(), filename="a.png", content_type="image/png")
        form.add_field("image[]", png_bytes(), filename="b.png", content_type="image/png")
        response = await self.client.post("/v1/images/edits", data=form, headers=HEADERS)
        self.assertEqual(response.status, 400)
        response = await self.edit(EDIT_FIELDS, image=png_bytes(), field="mask")
        self.assertEqual(response.status, 400)
        response = await self.edit(EDIT_FIELDS, image=png_bytes(), content_type="image/gif")
        self.assertEqual(response.status, 415)
        response = await self.edit(EDIT_FIELDS, image=b"not a png at all")
        self.assertEqual(response.status, 415)
        self.assertFalse(self.calls)

    async def test_edit_field_rules(self):
        cases = [
            ({**EDIT_FIELDS, "n": "2"}, None),
            ({**EDIT_FIELDS, "model": "gpt-image-1"}, None),
            ({**EDIT_FIELDS, "response_format": "url"}, None),
            ({**EDIT_FIELDS, "output_format": "jpeg"}, None),
            ({**EDIT_FIELDS, "quality": "max"}, None),
            ({**EDIT_FIELDS, "background": "bad"}, None),
            ({**EDIT_FIELDS, "size": "1x1"}, None),
            ({**EDIT_FIELDS, "prompt": ""}, None),
            ({**EDIT_FIELDS, "mask": "field"}, None),
            ({**EDIT_FIELDS, "api_key": "never-forward"}, None),
        ]
        for index, (fields, _) in enumerate(cases):
            with self.subTest(case=index):
                response = await self.edit(fields, image=png_bytes())
                self.assertEqual(response.status, 400)
        form = aiohttp.FormData()
        for key, value in EDIT_FIELDS.items():
            form.add_field(key, value)
        form.add_field("prompt", "duplicate prompt")
        form.add_field("image", png_bytes(), filename="gem.png", content_type="image/png")
        response = await self.client.post("/v1/images/edits", data=form, headers=HEADERS)
        self.assertEqual(response.status, 400)
        self.assertFalse(self.calls)

    async def test_edit_oversized_input_image(self):
        with patch.object(app, "MAX_IMAGE", 8):
            response = await self.edit(EDIT_FIELDS, image=png_bytes())
        self.assertEqual(response.status, 413)
        self.assertFalse(self.calls)

    async def test_edit_app_level_body_limit(self):
        small = app.create_app(app.Settings(
            key=KEY, upstream=str(self.upstream.make_url("/v1/responses")), timeout=2,
        ), client_max_size=512)
        client = TestClient(TestServer(small, handler_cancellation=True))
        await client.start_server()
        try:
            self.calls.clear()
            response = await client.post("/v1/images/edits", data=self.edit_form(EDIT_FIELDS, image=b"x" * 2048),
                                        headers=HEADERS)
            self.assertEqual(response.status, 413)
            self.assertFalse(self.calls)
        finally:
            await client.close()

    async def test_edit_upstream_error_redaction_and_no_retry(self):
        self.status = 500
        self.body = b"sensitive provider exception test-only-gateway-key"
        response = await self.edit(EDIT_FIELDS, image=png_bytes())
        self.assertEqual(response.status, 502)
        text = await response.text()
        self.assertNotIn("sensitive", text)
        self.assertNotIn(KEY.decode(), text)
        self.assertEqual(len(self.calls), 1)

    async def test_edit_disconnect_cancels_upstream_and_releases_slot(self):
        self.wait = True
        pending = asyncio.create_task(self.edit(EDIT_FIELDS, image=png_bytes()))
        await asyncio.wait_for(self.started.wait(), 1)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending
        await asyncio.wait_for(self.cancelled.wait(), 1)
        self.assertFalse(self.adapter[app.STATE].busy)

    async def test_edit_shares_single_inflight_slot_with_generation(self):
        self.wait = True
        pending = asyncio.create_task(self.client.post(
            "/v1/images/generations", json={"prompt": "Gem"}, headers=HEADERS))
        await asyncio.wait_for(self.started.wait(), 1)
        response = await self.edit(EDIT_FIELDS, image=png_bytes())
        self.assertEqual(response.status, 429)
        pending.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await pending


class StreamTests(unittest.IsolatedAsyncioTestCase):
    async def test_chunk_boundaries_crlf_and_multiline(self):
        class Content:
            async def iter_chunked(self, _size):
                for chunk in [b":comment\r\nevent: test\r\nda", b"ta: {\"x\":\r\n", b"data: 1}\r\n\r\n"]:
                    yield chunk

        values = [value async for value in app.events(Content())]
        self.assertEqual(json.loads(values[0]), {"x": 1})

    async def test_empty_credentials_fail_closed(self):
        with self.assertRaises(ValueError):
            app.create_app(app.Settings(key=b""))


class WebUIConfigTests(unittest.TestCase):
    def test_switch_and_scoped_rollback_without_printing_key(self):
        Path("/tmp/opencode").mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database, key = root / "webui.db", root / "key"
            key.write_bytes(KEY)
            with sqlite3.connect(database) as connection:
                connection.execute("CREATE TABLE config(key TEXT PRIMARY KEY,value TEXT,updated_at INTEGER)")
                connection.executemany("INSERT INTO config VALUES(?,?,?)", [
                    ("image_generation.engine", '"comfyui"', 123),
                    ("images.edit.engine", '"comfyui"', 126),
                    ("openai.api_configs", '{"unchanged":true}', 125),
                ])
                before = connection.execute("SELECT * FROM config ORDER BY key").fetchall()
            command = [sys.executable, str(Path(__file__).with_name("configure_webui.py")),
                       "--database", str(database), "--gateway-key", str(key)]
            output = subprocess.check_output(command, text=True)
            self.assertNotIn(KEY.decode(), output)
            backup = Path(output.split("Private rollback file: ")[1].strip())
            self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(backup.parent.stat().st_mode), 0o700)
            with sqlite3.connect(database) as connection:
                config = dict(connection.execute("SELECT key,value FROM config"))
            self.assertEqual(json.loads(config["image_generation.engine"]), "openai")
            self.assertEqual(json.loads(config["images.edit.engine"]), "openai")
            self.assertEqual(json.loads(config["image_generation.openai.api_key"]), KEY.decode())
            self.assertEqual(json.loads(config["images.edit.openai.api_key"]), KEY.decode())
            self.assertEqual(json.loads(config["image_generation.openai.params"])["quality"], "high")
            self.assertEqual(config["openai.api_configs"], '{"unchanged":true}')
            subprocess.check_output(command + ["--restore", str(backup)], text=True)
            with sqlite3.connect(database) as connection:
                self.assertEqual(connection.execute("SELECT * FROM config ORDER BY key").fetchall(), before)

    def test_missing_database_is_not_created(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing.db"
            result = subprocess.run([sys.executable, str(Path(__file__).with_name("configure_webui.py")),
                                     "--database", str(path)], capture_output=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(path.exists())


if __name__ == "__main__":
    unittest.main()
