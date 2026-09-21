#!/usr/bin/env python3
"""ComfyUI lifecycle proxy.

Sits in front of the local ComfyUI so its container can be stopped when idle
and started on demand, mirroring the llama.cpp idle manager in
custom_callbacks.py. Open WebUI's ComfyUI engine talks to this proxy exactly
as it would to ComfyUI (websocket + /prompt + /history + /view + /object_info).

Request handling:
  /ws             Wake ComfyUI, connect our upstream websocket, and only THEN
                  upgrade the client. Open WebUI's client has no websocket
                  handshake timeout (its shared session uses
                  ClientTimeout(total=None)), and delaying the 101 guarantees
                  it cannot POST /prompt before the upstream ws is connected --
                  otherwise the completion "executing" event would be missed
                  and Open WebUI would hang.
  POST /*         Wake ComfyUI, then reverse-proxy (prompt, queue, interrupt,
                  /api/upload/image, ...).
  /object_info    Served from cache; never starts the GPU, so listing models
                  and Open WebUI's "Verify URL" check do not wake it. The cache
                  is filled on each successful proxy while ComfyUI is running,
                  with a static seed fallback for the very first call.
  /system_stats   Proxied when running, else a synthetic body (our own health
                  checks use this; Open WebUI does not).
  /health         This proxy's own health. Never starts the GPU.
  other GETs      Proxied when ComfyUI is running, else 503.

Lifecycle:
  * idle: stop ComfyUI after IDLE_TIMEOUT seconds with no in-flight request
    (an open websocket counts as in-flight, so a generation is never cut off).
  * switch: the other GPU tenants (GPU_CONTAINERS) are stopped before ComfyUI
    is started, since only one container can hold the 16 GB GPU at a time.
"""

import asyncio
import logging
import os
import time
from urllib.parse import quote

import aiohttp
from aiohttp import web

from gpu_containers import PortainerContainers

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("comfyui-proxy")

COMFYUI_CONTAINER = os.environ.get("COMFYUI_CONTAINER", "comfyui")
UPSTREAM = os.environ.get("COMFYUI_UPSTREAM", "http://127.0.0.1:8188").rstrip("/")
WS_UPSTREAM = UPSTREAM.replace("http://", "ws://", 1).replace("https://", "wss://", 1)
HEALTH_URL = os.environ.get("COMFYUI_HEALTH", f"{UPSTREAM}/system_stats")
GPU_CONTAINERS = [
    c.strip()
    for c in os.environ.get("GPU_CONTAINERS", "").split(",")
    if c.strip()
]
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "900"))
BOOT_TIMEOUT = int(os.environ.get("BOOT_TIMEOUT", "90"))
PORT = int(os.environ.get("PORT", "8189"))

ForwardTimeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=900)

# Seed for /object_info before the real response has ever been cached. Only the
# model node's "*_name" list is read by Open WebUI (routers/images.py), so this
# is all that is needed for the model dropdown and the 200 for Verify URL.
OBJECT_INFO_STUB = {
    "UNETLoader": {
        "input": {
            "required": {
                "unet_name": [["qwen_image_2.1_int8_convrot.safetensors"], {}]
            }
        }
    }
}


def _forward_headers(headers) -> dict:
    # host/content-length are per-connection; drop accept-encoding so the
    # upstream does not compress (aiohttp would then have to undecompress).
    return {
        k: v
        for k, v in headers.items()
        if k.lower() not in ("host", "content-length", "accept-encoding")
    }


def _response_headers(headers) -> dict:
    return {
        k: v
        for k, v in headers.items()
        if k.lower()
        not in ("content-length", "content-encoding", "transfer-encoding", "connection")
    }


async def _forward(request: web.Request, url: str) -> web.Response:
    body = await request.read()
    async with aiohttp.ClientSession(timeout=ForwardTimeout) as session:
        async with session.request(
            request.method, url, headers=_forward_headers(request.headers), data=body
        ) as r:
            data = await r.read()
            return web.Response(
                body=data, status=r.status, headers=_response_headers(r.headers)
            )


async def _ensure_running(app: web.Application) -> bool:
    """Start ComfyUI if stopped, stopping the other GPU tenants first."""
    pc: PortainerContainers = app["portainer"]
    if await pc.is_running(COMFYUI_CONTAINER):
        return True
    async with app["start_lock"]:
        if await pc.is_running(COMFYUI_CONTAINER):
            return True
        for other in GPU_CONTAINERS:
            if not await pc.stop_and_wait(other):
                log.error(
                    "cannot start %s: %s is still holding the GPU",
                    COMFYUI_CONTAINER,
                    other,
                )
                return False
        log.info("starting %s (cold start)", COMFYUI_CONTAINER)
        if not await pc.start(COMFYUI_CONTAINER):
            log.error("failed to start %s via Portainer API", COMFYUI_CONTAINER)
            return False
        if not await pc.wait_health(HEALTH_URL, BOOT_TIMEOUT):
            log.error(
                "%s did not become healthy within %ss", COMFYUI_CONTAINER, BOOT_TIMEOUT
            )
            return False
        log.info("%s is healthy", COMFYUI_CONTAINER)
    return True


# ---------------------------------------------------------------- middleware


@web.middleware
async def inflight_middleware(request: web.Request, handler):
    """Count in-flight requests and keep the idle clock fresh.

    The websocket handler holds its request open for the whole generation, so
    an active generation always shows as in-flight and is never idle-stopped.
    """
    app = request.app
    app["inflight"] += 1
    try:
        return await handler(request)
    finally:
        app["inflight"] -= 1
        app["last_request"] = time.time()


# ---------------------------------------------------------------- handlers


async def handle_ws(request: web.Request) -> web.StreamResponse:
    app = request.app
    client_id = request.rel_url.query.get("clientId", "unknown")

    if not await _ensure_running(app):
        return web.json_response({"error": "comfyui unavailable"}, status=503)

    # Connect upstream BEFORE upgrading the client (see module docstring).
    session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=None))
    try:
        upstream = await session.ws_connect(
            f"{WS_UPSTREAM}/ws?clientId={quote(client_id)}", heartbeat=30
        )
    except Exception as e:
        await session.close()
        log.error("upstream ws connect failed: %s", e)
        return web.json_response({"error": "comfyui ws unavailable"}, status=503)

    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(request)
    log.info("ws open (clientId=%s)", client_id)

    async def client_to_upstream():
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await upstream.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await upstream.send_bytes(msg.data)
            else:
                break

    async def upstream_to_client():
        async for msg in upstream:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await ws.send_str(msg.data)
            elif msg.type == aiohttp.WSMsgType.BINARY:
                await ws.send_bytes(msg.data)
            else:
                break

    # Relay until EITHER side closes, then tear both down. Waiting for both
    # (e.g. asyncio.gather) would hang forever: when Open WebUI closes its
    # socket, ComfyUI's stays open and the upstream->client loop would block,
    # leaking a task and a connection per generation.
    client_task = asyncio.create_task(client_to_upstream())
    upstream_task = asyncio.create_task(upstream_to_client())
    try:
        await asyncio.wait(
            {client_task, upstream_task}, return_when=asyncio.FIRST_COMPLETED
        )
    finally:
        for task in (client_task, upstream_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(client_task, upstream_task, return_exceptions=True)
        await upstream.close()
        await session.close()
        if not ws.closed:
            await ws.close()
        log.info("ws closed (clientId=%s)", client_id)
    return ws


async def handle_object_info(request: web.Request) -> web.Response:
    """Serve the object info without waking the GPU."""
    app = request.app
    if await app["portainer"].is_running(COMFYUI_CONTAINER):
        resp = await _forward(request, UPSTREAM + request.path_qs)
        if resp.status == 200:
            app["object_info"] = resp.body
        return resp
    if app["object_info"] is not None:
        return web.Response(body=app["object_info"], content_type="application/json")
    log.info("object_info served from static stub (%s is stopped)", COMFYUI_CONTAINER)
    return web.json_response(OBJECT_INFO_STUB)


async def handle_system_stats(request: web.Request) -> web.Response:
    if await request.app["portainer"].is_running(COMFYUI_CONTAINER):
        return await _forward(request, UPSTREAM + request.path_qs)
    return web.json_response({"proxy": "up", "comfyui": "down"})


async def handle_health(request: web.Request) -> web.Response:
    running = await request.app["portainer"].is_running(COMFYUI_CONTAINER)
    return web.json_response({"proxy": "up", "comfyui": "up" if running else "down"})


async def handle_catchall(request: web.Request) -> web.Response:
    app = request.app
    path = request.path

    if path == "/health":
        return await handle_health(request)
    if path == "/object_info":
        return await handle_object_info(request)
    if path == "/system_stats":
        return await handle_system_stats(request)

    if request.method != "GET":
        # mutating calls (prompt/queue/interrupt/upload) need the GPU
        if not await _ensure_running(app):
            return web.json_response({"error": "comfyui unavailable"}, status=503)
    elif not await app["portainer"].is_running(COMFYUI_CONTAINER):
        # Read-only calls (view/history/...) only make sense post-generation, so
        # a stopped backend returns 503 -- except /history, which clients use as
        # a liveness probe (comfy-cli's check_comfy_server_running) and which
        # legitimately has no entries while stopped. Answering it 200 keeps the
        # "status reads never start the GPU" rule intact: a mutation still wakes
        # the container.
        if path == "/history" or path.startswith("/history/"):
            return web.json_response({})
        return web.json_response({"error": "comfyui is stopped"}, status=503)

    return await _forward(request, UPSTREAM + request.path_qs)


# ---------------------------------------------------------------- idle watcher


async def _idle_loop(app: web.Application) -> None:
    log.info(
        "idle watcher started (timeout=%ss, container=%s, others=%s)",
        IDLE_TIMEOUT,
        COMFYUI_CONTAINER,
        GPU_CONTAINERS,
    )
    while True:
        await asyncio.sleep(30)
        if app["inflight"] > 0:
            continue
        if time.time() - app["last_request"] <= IDLE_TIMEOUT:
            continue
        if await app["portainer"].is_running(COMFYUI_CONTAINER):
            log.info("idle for %ss, stopping %s", IDLE_TIMEOUT, COMFYUI_CONTAINER)
            if await app["portainer"].stop(COMFYUI_CONTAINER):
                log.info("%s stopped", COMFYUI_CONTAINER)
            else:
                # Portainer unreachable / refused: the container keeps holding
                # the GPU. Log it (the next tick retries) rather than fail mute.
                log.warning(
                    "failed to stop %s (Portainer unreachable?) - will retry",
                    COMFYUI_CONTAINER,
                )
        app["last_request"] = time.time()


async def _on_startup(app: web.Application) -> None:
    app["idle_task"] = asyncio.create_task(_idle_loop(app))


async def _on_cleanup(app: web.Application) -> None:
    app["idle_task"].cancel()


def create_app() -> web.Application:
    app = web.Application(
        client_max_size=256 * 1024 * 1024,  # edit uploads can be several MB
        middlewares=[inflight_middleware],
    )
    app["portainer"] = PortainerContainers()
    app["object_info"] = None
    app["start_lock"] = asyncio.Lock()
    app["last_request"] = time.time()
    app["inflight"] = 0

    app.router.add_get("/ws", handle_ws)
    app.router.add_route("*", "/{tail:.*}", handle_catchall)

    app.on_startup.append(_on_startup)
    app.on_cleanup.append(_on_cleanup)
    return app


if __name__ == "__main__":
    log.info(
        "comfyui-proxy on :%s -> %s (container=%s, others=%s, idle=%ss)",
        PORT,
        UPSTREAM,
        COMFYUI_CONTAINER,
        GPU_CONTAINERS,
        IDLE_TIMEOUT,
    )
    web.run_app(create_app(), host="0.0.0.0", port=PORT, print=None)
