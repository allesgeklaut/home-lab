"""
LiteLLM custom callbacks.

1. LlamaCppIdleManager: mutual-exclusion start + idle-stop for llama.cpp.
2. OpenCodeGoSessionHeader: adds the mandatory x-opencode-session header for
   OpenCode Go models (the Go gateway rejects requests without it).

Only one llama.cpp container can hold the GPU at a time. Each user-facing
model is mapped to a container via the MODEL_CONTAINERS env var (JSON):

    MODEL_CONTAINERS={"qwen3.8-27b":{"container":"llama-server","health":"http://127.0.0.1:8084/health"},"qwen3.5-9b":{"container":"llama-companion","health":"http://127.0.0.1:8086/health"}}

  - async_pre_call_hook: looks up the requested model. If its container is
    stopped, every *other* group container is stopped first (and waited for),
    then the target container is started (via Portainer API) and health-waited
    before the request proceeds. Concurrent requests during a cold start all
    wait on one lock. On the very first call it also launches the background
    idle-watcher task on the same instance, so the hooks and the watcher share
    one per-container last_request_time map.
  - async_log_success_event / async_log_failure_event: update the per-container
    last_request_time so the background idle-loop stops containers
    independently after IDLE_TIMEOUT seconds of inactivity.

Env vars (provided via env_file in compose.yml):
  PORTAINER_API_KEY   Portainer API token
  PORTAINER_ENV_ID     Portainer environment ID (e.g. 3)
  PORTAINER_URL        Portainer base URL (https://host:9443)

Additional config (plain env, set in compose environment:):
  MODEL_CONTAINERS    JSON: model_name -> {container, health}
  IDLE_TIMEOUT        seconds idle before stop (default: 300)
  BOOT_TIMEOUT        seconds to wait for cold start (default: 90);
                      can be overridden per model via "boot_timeout" in the map
  EXTRA_GPU_CONTAINERS  comma-separated non-llama GPU tenants (e.g. comfyui)
                      that are stopped before cold-starting a managed model,
                      because only one container can hold the GPU at a time
  OPENCODE_GO_MODELS  comma-separated OpenCode Go model names; the
                      OpenCodeGoSessionHeader callback adds the mandatory
                      x-opencode-session header for these

The Portainer start/stop/health helpers live in gpu_containers.py, shared with
the ComfyUI lifecycle proxy so both managers behave identically.
"""

import asyncio
import hashlib
import json
import os
import time

from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException

from gpu_containers import PortainerContainers


class LlamaCppIdleManager(CustomLogger):
    def __init__(self):
        super().__init__()
        # per-container last request time
        self.last_request_time = {}
        self.start_lock = asyncio.Lock()
        self._watcher_started = False

        self.portainer = PortainerContainers()

        # Non-llama GPU tenants (e.g. comfyui) that must be stopped before
        # cold-starting one of ours: only one container can hold the GPU.
        self.extra = [
            c.strip()
            for c in os.environ.get("EXTRA_GPU_CONTAINERS", "").split(",")
            if c.strip()
        ]

        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "300"))
        self.boot_timeout = int(os.environ.get("BOOT_TIMEOUT", "90"))

        raw = os.environ.get(
            "MODEL_CONTAINERS",
            '{"qwen3.8-27b":{"container":"llama-server",'
            '"health":"http://127.0.0.1:8084/health"}}',
        )
        self.models = {}
        # lookup: user-facing model names (and upstream aliases) -> config
        self.lookup = {}
        for model, cfg in json.loads(raw).items():
            entry = {
                "container": cfg["container"],
                "health": cfg["health"],
                "boot_timeout": int(cfg.get("boot_timeout", self.boot_timeout)),
            }
            self.models[model] = entry
            self.lookup[model] = entry
            for alias in cfg.get("aliases", []):
                self.lookup[alias] = entry
        # container name -> model name (reverse map for the idle loop)
        self.containers = {cfg["container"]: m for m, cfg in self.models.items()}

    def _resolve(self, model) -> dict | None:
        """Map a litellm model string (user-facing or upstream) to its config."""
        if not model:
            return None
        if model in self.lookup:
            return self.lookup[model]
        # upstream names look like "openai/qwen3.8:27b"
        if "/" in model:
            return self.lookup.get(model.split("/", 1)[1])
        return None

    # ------------------------------------------------------------------
    # LiteLLM hooks
    # ------------------------------------------------------------------
    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        cfg = self._resolve(data.get("model"))
        if cfg is None:
            return data  # not a managed model, pass through untouched

        container = cfg["container"]
        self.last_request_time[container] = time.time()
        if not self._watcher_started:
            self._watcher_started = True
            asyncio.create_task(self._idle_loop())

        if not await self.portainer.is_running(container):
            async with self.start_lock:
                # re-check inside the lock (another coroutine may have started it)
                if not await self.portainer.is_running(container):
                    # make room: stop every other group container and every
                    # extra GPU tenant (e.g. comfyui) first. If one refuses to
                    # exit, do NOT start on top of it (they would fight over
                    # the 16 GB of VRAM).
                    switched = True
                    for other in self.containers:
                        if other != container:
                            if not await self.portainer.stop_and_wait(other):
                                switched = False
                    for other in self.extra:
                        if not await self.portainer.stop_and_wait(other):
                            switched = False
                    if not switched:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"Cannot start {container}: another GPU "
                                "container is still holding the GPU"
                            ),
                        )
                    print(f"[idle-manager] starting {container} (cold start)")
                    if not await self.portainer.start(container):
                        raise HTTPException(
                            status_code=503,
                            detail=f"Failed to start {container} via Portainer API",
                        )
                    if not await self.portainer.wait_health(cfg["health"], cfg["boot_timeout"]):
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"{container} did not become healthy within "
                                f"{cfg['boot_timeout']}s"
                            ),
                        )
                    print(f"[idle-manager] {container} is healthy")
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._touch(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._touch(kwargs)

    def _touch(self, kwargs):
        cfg = self._resolve((kwargs or {}).get("model"))
        if cfg:
            self.last_request_time[cfg["container"]] = time.time()

    # ------------------------------------------------------------------
    # Background idle watcher
    # ------------------------------------------------------------------
    async def _idle_loop(self):
        print(
            f"[idle-manager] watcher started "
            f"(idle_timeout={self.idle_timeout}s, containers={list(self.containers)})"
        )
        while True:
            await asyncio.sleep(30)
            now = time.time()
            for container in self.containers:
                last = self.last_request_time.get(container, 0)
                if last and now - last <= self.idle_timeout:
                    continue
                if await self.portainer.is_running(container):
                    print(
                        f"[idle-manager] idle for {self.idle_timeout}s, "
                        f"stopping {container}"
                    )
                    if await self.portainer.stop(container):
                        print(f"[idle-manager] {container} stopped")
                    self.last_request_time[container] = time.time()
                elif last:
                    # already stopped; keep the timestamp fresh so we don't poll spam
                    self.last_request_time[container] = time.time()


class OpenCodeGoSessionHeader(CustomLogger):
    """Inject x-opencode-session on requests to OpenCode Go models.

    The Go gateway rejects requests that lack a stable per-conversation
    session id ("MissingSessionID"). Resolve one from, in order:

      1. an incoming x-opencode-session header (clients that send one, e.g.
         OpenCode itself),
      2. litellm's session id (x-litellm-session-id / x-<vendor>-session-id
         headers, Anthropic metadata.user_id),
      3. a stable hash of the user and the first message, so stateless clients
         (e.g. OpenWebUI) still group each conversation under one session id.

    Model names to manage are provided via the OPENCODE_GO_MODELS env var
    (comma-separated, set in compose.yml).
    """

    def __init__(self):
        super().__init__()
        raw = os.environ.get("OPENCODE_GO_MODELS", "")
        self.models = {m.strip() for m in raw.split(",") if m.strip()}

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        if data.get("model") not in self.models:
            return data
        extra = data.get("extra_headers")
        if not isinstance(extra, dict):
            extra = {}
        if not any(k.lower() == "x-opencode-session" for k in extra):
            extra["x-opencode-session"] = self._resolve_session(data)
        data["extra_headers"] = extra
        return data

    def _resolve_session(self, data) -> str:
        headers = {}
        psr = data.get("proxy_server_request")
        if isinstance(psr, dict):
            headers = {
                str(k).lower(): v
                for k, v in (psr.get("headers") or {}).items()
                if isinstance(v, str)
            }
        if headers.get("x-opencode-session"):
            return headers["x-opencode-session"]
        if data.get("litellm_session_id"):
            return str(data["litellm_session_id"])
        return self._fingerprint_hash(data)

    def _fingerprint_hash(self, data) -> str:
        # OpenWebUI (and other stateless clients) resend the full history, so
        # the first user/assistant message stays constant for a conversation.
        # Hash it with the user id to get a stable per-conversation session
        # id. System messages are skipped: they are usually one constant
        # prompt shared by every conversation, which would collapse them all
        # into a single session.
        seed = [str(data.get("user") or "")]
        msgs = data.get("messages")
        if not isinstance(msgs, list) or not msgs:
            inp = data.get("input")
            msgs = inp if isinstance(inp, list) else []
        first = next(
            (
                m
                for m in msgs
                if not (isinstance(m, dict) and m.get("role") == "system")
            ),
            None,
        )
        if first is not None:
            seed.append(json.dumps(first, sort_keys=True, default=str))
        else:
            for key in ("system", "input"):
                value = data.get(key)
                if value is not None:
                    seed.append(json.dumps(value, sort_keys=True, default=str))
        return hashlib.sha256("|".join(seed).encode("utf-8", "replace")).hexdigest()[:32]


proxy_handler_instance = LlamaCppIdleManager()
opencode_go_session_header = OpenCodeGoSessionHeader()