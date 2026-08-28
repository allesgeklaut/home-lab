"""
LiteLLM custom callback: mutual-exclusion start + idle-stop for llama.cpp.

Only one llama.cpp container can hold the GPU at a time. Each user-facing
model is mapped to a container via the MODEL_CONTAINERS env var (JSON):

    MODEL_CONTAINERS={"qwen3.8-27b":{"container":"llama-server","health":"http://192.168.0.46:8084/health"},"ornith-1.5-9b":{"container":"llama-ornith","health":"http://192.168.0.46:8086/health"}}

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
"""

import asyncio
import json
import os
import time
import httpx
from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException


class LlamaCppIdleManager(CustomLogger):
    def __init__(self):
        super().__init__()
        # per-container last request time
        self.last_request_time = {}
        self.start_lock = asyncio.Lock()
        self._watcher_started = False

        self.portainer_url = os.environ["PORTAINER_URL"].rstrip("/")
        self.portainer_key = os.environ["PORTAINER_API_KEY"]
        self.portainer_env_id = os.environ["PORTAINER_ENV_ID"]
        self._headers = {"X-API-Key": self.portainer_key}

        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "300"))
        self.boot_timeout = int(os.environ.get("BOOT_TIMEOUT", "90"))

        raw = os.environ.get(
            "MODEL_CONTAINERS",
            '{"qwen3.8-27b":{"container":"llama-server",'
            '"health":"http://192.168.0.46:8084/health"}}',
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
    # Portainer API helpers
    # ------------------------------------------------------------------
    def _api(self, container: str, path: str) -> str:
        return (
            f"{self.portainer_url}/api/endpoints/{self.portainer_env_id}"
            f"/docker/containers/{container}{path}"
        )

    async def _is_running(self, container: str) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as c:
                r = await c.get(self._api(container, "/json"), headers=self._headers)
            if r.status_code == 404:
                return False  # container not created yet -> not running
            if r.status_code != 200:
                return True  # can't confirm stopped -> assume running (safe)
            return bool(r.json().get("State", {}).get("Running", False))
        except httpx.HTTPStatusError:
            return True
        except Exception:
            return True  # on error assume running (don't risk stopping a live one)

    async def _start_container(self, container: str) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=60) as c:
                r = await c.post(self._api(container, "/start"), headers=self._headers)
            return r.status_code in (200, 204, 304)
        except Exception:
            return False

    async def _stop_container(self, container: str) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=60) as c:
                r = await c.post(self._api(container, "/stop"), headers=self._headers)
            return r.status_code in (200, 204, 304)
        except Exception:
            return False

    async def _stop_and_wait(self, container: str, timeout: float = 120.0) -> bool:
        """Stop a container and wait until it has actually exited (VRAM free)."""
        if not await self._is_running(container):
            return True
        print(f"[idle-manager] stopping {container} (model switch)")
        await self._stop_container(container)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not await self._is_running(container):
                print(f"[idle-manager] {container} exited")
                return True
            await asyncio.sleep(1)
        print(f"[idle-manager] {container} did not exit within {timeout}s")
        return False

    async def _wait_health(self, url: str, boot_timeout: int) -> bool:
        deadline = time.time() + boot_timeout
        consecutive_ok = 0
        async with httpx.AsyncClient(timeout=5) as c:
            while time.time() < deadline:
                try:
                    r = await c.get(url)
                    if r.status_code == 200:
                        consecutive_ok += 1
                        # require two consecutive 200s to avoid a transient
                        # "ok" while the model is still finalizing its load
                        if consecutive_ok >= 2:
                            return True
                    else:
                        consecutive_ok = 0
                except Exception:
                    consecutive_ok = 0
                await asyncio.sleep(1)
        return False

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

        if not await self._is_running(container):
            async with self.start_lock:
                # re-check inside the lock (another coroutine may have started it)
                if not await self._is_running(container):
                    # make room: stop every other group container first.
                    # If one refuses to exit, do NOT start on top of it
                    # (two servers would fight over 16 GB of VRAM).
                    switched = True
                    for other in self.containers:
                        if other != container:
                            if not await self._stop_and_wait(other):
                                switched = False
                    if not switched:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"Cannot start {container}: another llama.cpp "
                                "container is still holding the GPU"
                            ),
                        )
                    print(f"[idle-manager] starting {container} (cold start)")
                    if not await self._start_container(container):
                        raise HTTPException(
                            status_code=503,
                            detail=f"Failed to start {container} via Portainer API",
                        )
                    if not await self._wait_health(cfg["health"], cfg["boot_timeout"]):
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
                if await self._is_running(container):
                    print(
                        f"[idle-manager] idle for {self.idle_timeout}s, "
                        f"stopping {container}"
                    )
                    if await self._stop_container(container):
                        print(f"[idle-manager] {container} stopped")
                    self.last_request_time[container] = time.time()
                elif last:
                    # already stopped; keep the timestamp fresh so we don't poll spam
                    self.last_request_time[container] = time.time()


proxy_handler_instance = LlamaCppIdleManager()