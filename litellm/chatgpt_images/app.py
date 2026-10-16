"""Unofficial, personal-use Images API shim for LiteLLM's chatgpt/ provider.

Bridges OpenAI Images API generation and editing requests to the existing
chatgpt/ Responses route, pinning the quality-focused GPT Image model
(gpt-image-2.5-sunburst). Only LiteLLM handles OAuth. This process has no
subscription tokens, paid API credentials, alternate providers, image URL
fetches, or automatic retries.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
from dataclasses import dataclass, field
import hmac
import io
import json
import logging
import os
from pathlib import Path
import re
import time
import warnings

import aiohttp
from aiohttp import web
from PIL import Image

log = logging.getLogger("chatgpt-images")
MODEL = "chatgpt-image"
UPSTREAM_MODEL = "chatgpt-gpt-6.1-sol"
IMAGE_TOOL_MODEL = "gpt-image-2.5-sunburst"
DEFAULT_QUALITY = "high"
MAX_BODY = 16 * 1024
MAX_PROMPT = 8 * 1024
MAX_EVENT = 24 * 1024 * 1024
MAX_STREAM = 48 * 1024 * 1024
MAX_IMAGE = 16 * 1024 * 1024
MAX_PIXELS = 16_777_216
EDIT_MAX_FORM = 17 * 1024 * 1024
ALLOWED_CONTENT_TYPES = ("image/png", "image/jpeg", "image/webp")
GENERATION_FIELDS = {"model", "prompt", "n", "size", "quality", "response_format", "output_format", "background", "user"}
EDIT_FIELDS = {"model", "prompt", "n", "size", "quality", "response_format", "output_format", "background"}
Image.MAX_IMAGE_PIXELS = MAX_PIXELS
warnings.simplefilter("error", Image.DecompressionBombWarning)


@dataclass(frozen=True)
class Settings:
    key: bytes = field(repr=False)
    upstream: str = "http://litellm:4000/v1/responses"
    timeout: float = 180


class ImageError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


@dataclass
class State:
    settings: Settings
    session: aiohttp.ClientSession | None = None
    busy: bool = False


STATE = web.AppKey("state", State)


def error(status: int, code: str, message: str) -> web.Response:
    return web.json_response(
        {"error": {"type": "image_generation_error", "code": code, "message": message}},
        status=status,
        headers={"Cache-Control": "no-store"},
    )


@web.middleware
async def authenticate(request: web.Request, handler):
    if request.path == "/health" and request.method == "GET":
        return await handler(request)
    expected = b"Bearer " + request.app[STATE].settings.key
    supplied = request.headers.get("Authorization", "").encode("utf-8")
    if not hmac.compare_digest(supplied, expected):
        return error(401, "unauthorized", "A valid gateway Bearer key is required.")
    if request.headers.get("Content-Encoding", "identity").lower() != "identity":
        return error(415, "unsupported_encoding", "Compressed request bodies are not supported.")
    try:
        return await handler(request)
    except ImageError as exc:
        return error(exc.status, exc.code, exc.message)
    except TimeoutError:
        return error(504, "upstream_timeout", "Image generation timed out; no retry was made.")
    except aiohttp.ClientError:
        return error(502, "upstream_unavailable", "The subscription gateway request failed.")
    except web.HTTPRequestEntityTooLarge:
        return error(413, "request_too_large", "Request body exceeds the adapter limit.")
    except web.HTTPException as exc:
        return error(exc.status, "http_error", "Unsupported image API request.")
    except Exception as exc:
        # Log only the exception class; messages can embed request data.
        log.error("Image request failed with an internal error: %s", type(exc).__name__)
        return error(500, "internal_error", "Image generation failed.")


async def read_payload(request: web.Request) -> dict:
    if request.content_type != "application/json":
        raise ImageError(415, "unsupported_content_type", "Use application/json.")
    if request.content_length is not None and request.content_length > MAX_BODY:
        raise ImageError(413, "request_too_large", "Request body exceeds 16 KiB.")
    body = bytearray()
    try:
        async with asyncio.timeout(10):
            async for chunk in request.content.iter_chunked(4096):
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    raise ImageError(413, "request_too_large", "Request body exceeds 16 KiB.")
    except TimeoutError:
        raise ImageError(408, "request_timeout", "Request body took too long.") from None
    try:
        payload = json.loads(body)
    except (ValueError, UnicodeError, RecursionError):
        raise ImageError(400, "invalid_json", "Invalid JSON request.") from None
    if not isinstance(payload, dict):
        raise ImageError(400, "invalid_request", "A JSON object is required.")
    return payload


def _common_request_params(data: dict, allowed: set) -> dict:
    """Validate shared generation/edit parameters and return prompt/quality."""
    if set(data) - allowed:
        raise ImageError(400, "unsupported_parameter", "The request contains unsupported image parameters.")
    if data.get("model", MODEL) != MODEL:
        raise ImageError(400, "unsupported_model", "Only chatgpt-image is supported.")
    count = data.get("n", 1)
    if isinstance(count, str) and count.strip() == "1":
        count = 1
    if type(count) is not int or count != 1:
        raise ImageError(400, "unsupported_count", "Exactly one image per request is supported.")
    prompt = data.get("prompt")
    try:
        valid_prompt = isinstance(prompt, str) and bool(prompt.strip()) and len(prompt.encode("utf-8")) <= MAX_PROMPT
    except UnicodeError:
        valid_prompt = False
    if not valid_prompt:
        raise ImageError(400, "invalid_prompt", "Supply a nonempty prompt of at most 8 KiB.")
    if data.get("response_format", "b64_json") != "b64_json" or data.get("output_format", "png") != "png":
        raise ImageError(400, "unsupported_format", "Only PNG with response_format=b64_json is supported.")
    quality = data.get("quality", DEFAULT_QUALITY)
    if quality not in ("low", "medium", "high"):
        raise ImageError(400, "invalid_quality", "Quality must be low, medium or high.")
    size = data.get("size", "auto")
    if size != "auto":
        match = re.fullmatch(r"(\d{3,4})x(\d{3,4})", size) if isinstance(size, str) else None
        if not match or not all(256 <= int(n) <= 4096 for n in match.groups()):
            raise ImageError(400, "invalid_size", "Use auto or dimensions between 256 and 4096.")
        # The subscription backend does not reliably honor exact dimensions.
        prompt += f"\nRequested canvas: {size} pixels, if supported."
    background = data.get("background", "auto")
    if background not in ("auto", "transparent", "opaque"):
        raise ImageError(400, "invalid_background", "Background must be auto, transparent or opaque.")
    if background == "transparent":
        prompt += "\nOutput a genuine RGBA PNG with alpha-zero background pixels, not a painted checkerboard."
    elif background == "opaque":
        prompt += "\nOutput an opaque background."
    return {"prompt": prompt, "quality": quality}


def build_request(data: dict) -> dict:
    params = _common_request_params(data, GENERATION_FIELDS)
    return {
        "model": UPSTREAM_MODEL,
        "instructions": "Generate exactly one image using the image_generation tool. Follow the user's image prompt.",
        "input": [{"role": "user", "content": [{"type": "input_text", "text": params["prompt"]}]}],
        "tools": [{"type": "image_generation", "model": IMAGE_TOOL_MODEL, "output_format": "png", "quality": params["quality"]}],
        "tool_choice": {"type": "image_generation"},
        "reasoning": {"effort": "low"},
        "stream": True,
        "store": False,
    }


def build_edit_request(fields: dict, data_url: str) -> dict:
    params = _common_request_params(fields, EDIT_FIELDS)
    return {
        "model": UPSTREAM_MODEL,
        "instructions": "Edit exactly the provided image using the image_generation tool. Follow the user's prompt.",
        "input": [{"role": "user", "content": [
            {"type": "input_text", "text": params["prompt"]},
            {"type": "input_image", "image_url": data_url},
        ]}],
        "tools": [{"type": "image_generation", "model": IMAGE_TOOL_MODEL, "output_format": "png", "quality": params["quality"], "action": "edit"}],
        "tool_choice": {"type": "image_generation"},
        "reasoning": {"effort": "low"},
        "stream": True,
        "store": False,
    }


async def events(content):
    """Bound memory even for chunked streams, huge lines, and multi-line SSE."""
    buffer = bytearray()
    parts = []
    event_size = total = 0
    async for chunk in content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > MAX_STREAM:
            raise ImageError(502, "stream_too_large", "The upstream image stream exceeded its limit.")
        buffer.extend(chunk)
        while (end := buffer.find(b"\n")) >= 0:
            line = bytes(buffer[:end]).rstrip(b"\r")
            del buffer[:end + 1]
            if not line:
                if parts:
                    yield b"\n".join(parts)
                parts, event_size = [], 0
            elif line.startswith(b"data:"):
                part = line[5:].lstrip(b" ")
                event_size += len(part)
                if event_size > MAX_EVENT:
                    raise ImageError(502, "event_too_large", "The upstream image event exceeded its limit.")
                parts.append(part)
        if len(buffer) + event_size > MAX_EVENT:
            raise ImageError(502, "event_too_large", "The upstream image event exceeded its limit.")
    # A successful request must include complete, delimited SSE events.
    if buffer or parts:
        raise ImageError(502, "truncated_stream", "The upstream image stream was truncated.")


def validate_png(encoded: str) -> str:
    if not isinstance(encoded, str) or len(encoded) > (MAX_IMAGE + 2) // 3 * 4:
        raise ImageError(502, "invalid_image", "The returned image is missing or too large.")
    try:
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) > MAX_IMAGE or not raw.startswith(b"\x89PNG\r\n\x1a\n"):
            raise ValueError("Not a bounded PNG")
        with Image.open(io.BytesIO(raw)) as image:
            if image.format != "PNG" or image.width * image.height > MAX_PIXELS or getattr(image, "n_frames", 1) != 1:
                raise ValueError("Unsupported image")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
    except (ValueError, OSError, EOFError, SyntaxError, AttributeError, binascii.Error, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageError(502, "invalid_image", "The returned image failed PNG validation.") from None
    return encoded


async def generate(state: State, payload: dict) -> str:
    assert state.session is not None
    async with state.session.post(
        state.settings.upstream, json=payload, allow_redirects=False,
        headers={"Authorization": "Bearer " + state.settings.key.decode("ascii")},
    ) as response:
        if response.status == 429:
            raise ImageError(429, "upstream_rate_limit", "The subscription is rate limited; no retry was made.")
        if response.status != 200:
            raise ImageError(502, "upstream_rejected", "The subscription gateway rejected the request; no fallback was used.")
        if response.content_type != "text/event-stream" or response.headers.get("Content-Encoding", "identity") != "identity":
            raise ImageError(502, "unexpected_response", "Expected an uncompressed subscription event stream.")
        image = None
        completed = False
        async for event in events(response.content):
            if event == b"[DONE]":
                continue
            try:
                data = json.loads(event)
            except (ValueError, RecursionError):
                raise ImageError(502, "invalid_event", "Invalid subscription event.") from None
            if not isinstance(data, dict):
                raise ImageError(502, "invalid_event", "Invalid subscription event.")
            kind = data.get("type")
            if kind in ("error", "response.failed", "response.incomplete") or data.get("error"):
                raise ImageError(502, "generation_failed", "Image generation failed or was refused upstream.")
            if kind == "response.output_item.done":
                item = data.get("item", {})
                if not isinstance(item, dict):
                    raise ImageError(502, "invalid_event", "Invalid subscription image event.")
                if item.get("type") == "image_generation_call":
                    if image is not None:
                        raise ImageError(502, "multiple_images", "Unexpected multiple image results.")
                    image = validate_png(item.get("result"))
            if kind == "response.completed":
                result = data.get("response", {})
                if not isinstance(result, dict):
                    raise ImageError(502, "invalid_event", "Invalid subscription completion event.")
                if result.get("error") or result.get("status") in ("failed", "incomplete", "cancelled"):
                    raise ImageError(502, "generation_failed", "Image generation did not complete successfully.")
                completed = True
        if not completed or image is None:
            raise ImageError(502, "missing_image", "No completed image was returned.")
        return image


async def _run(state: State, payload: dict) -> web.Response:
    if state.busy:
        raise ImageError(429, "busy", "An image request is already running; no queue or retry was created.")
    state.busy = True
    try:
        async with asyncio.timeout(state.settings.timeout):
            image = await generate(state, payload)
        return web.json_response({"created": int(time.time()), "data": [{"b64_json": image}]},
                                 headers={"Cache-Control": "no-store"})
    finally:
        state.busy = False


async def image_generation(request: web.Request):
    payload = build_request(await read_payload(request))
    return await _run(request.app[STATE], payload)


def _validate_input_image(raw: bytes, content_type: str) -> str:
    if not raw or len(raw) > MAX_IMAGE:
        raise ImageError(413, "image_too_large", "Input image exceeds 16 MiB.")
    try:
        with Image.open(io.BytesIO(raw)) as image:
            if image.format not in ("PNG", "JPEG", "WEBP") or getattr(image, "n_frames", 1) != 1 or image.width * image.height > MAX_PIXELS:
                raise ValueError("Unsupported input image")
            image.verify()
        with Image.open(io.BytesIO(raw)) as image:
            image.load()
    except (ValueError, OSError, EOFError, SyntaxError, Image.DecompressionBombWarning, Image.DecompressionBombError):
        raise ImageError(415, "unsupported_media_type", "The input image failed validation.") from None
    return f"data:{content_type};base64," + base64.b64encode(raw).decode()


async def edit_image(request: web.Request):
    # A urlencoded body is accepted only so it fails cleanly below: it cannot
    # carry a file part, so it always ends as "exactly one image required".
    if request.content_type not in ("multipart/form-data", "application/x-www-form-urlencoded"):
        raise ImageError(415, "unsupported_content_type", "Use multipart/form-data.")
    try:
        async with asyncio.timeout(15):
            form = await request.post()
    except TimeoutError:
        raise ImageError(408, "request_timeout", "Request body took too long.") from None
    fields: dict[str, str] = {}
    files = []
    for key, value in form.items():
        if isinstance(value, str):
            if key in fields:
                raise ImageError(400, "duplicate_field", "Duplicate form field.")
            fields[key] = value
        else:
            files.append(value)
    if any(f.name not in ("image", "image[]") for f in files):
        raise ImageError(400, "unsupported_parameter", "The request contains unsupported image parameters.")
    if len(files) != 1:
        raise ImageError(400, "invalid_image", "Exactly one image file is required.")
    file = files[0]
    content_type = (file.content_type or "").split(";")[0].strip().lower()
    if content_type not in ALLOWED_CONTENT_TYPES:
        raise ImageError(415, "unsupported_media_type", "Only PNG, JPEG or WebP input images are supported.")
    data_url = _validate_input_image(file.file.read(MAX_IMAGE + 1), content_type)
    payload = build_edit_request(fields, data_url)
    return await _run(request.app[STATE], payload)


async def models(_request):
    return web.json_response({"object": "list", "data": [{"id": MODEL, "object": "model", "owned_by": "unofficial-subscription-adapter"}]})


async def health(_request):
    return web.json_response({"status": "ok"})


def create_app(settings: Settings, client_max_size: int | None = None):
    if not settings.key or not settings.key.isascii() or any(c < 33 or c > 126 for c in settings.key):
        raise ValueError("A nonempty ASCII gateway key is required")
    app = web.Application(middlewares=[authenticate], client_max_size=client_max_size or EDIT_MAX_FORM)
    app[STATE] = State(settings)

    async def session_context(app):
        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=settings.timeout, connect=5, sock_read=90),
            connector=aiohttp.TCPConnector(limit=2), auto_decompress=False, trust_env=False,
        ) as session:
            app[STATE].session = session
            yield

    app.cleanup_ctx.append(session_context)
    app.router.add_get("/health", health)
    app.router.add_get("/v1/models", models)
    app.router.add_post("/v1/images/generations", image_generation)
    app.router.add_post("/v1/images/edits", edit_image)
    return app


if __name__ == "__main__":
    try:
        key = Path(os.environ.get("GATEWAY_KEY_FILE", "/run/secrets/litellm.key")).read_bytes().strip()
        app = create_app(Settings(key=key))
    except (OSError, ValueError):
        raise SystemExit("A readable, nonempty gateway key file is required") from None
    logging.basicConfig(level=logging.WARNING)
    web.run_app(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8180")),
                access_log=None, handler_cancellation=True, auto_decompress=False,
                max_line_size=4096, max_field_size=4096)
