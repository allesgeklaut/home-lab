"""
LiteLLM custom callback: idle-stop and on-demand start for llama.cpp.

  - async_pre_call_hook: starts the llama-server container (via Portainer API)
    if it is stopped, waits for its /health endpoint, then lets the request
    proceed. Concurrent requests during a cold start all wait on one lock.
    On the very first call it also launches the background idle-watcher task
    on the same instance, so the hooks and the watcher share one
    last_request_time.
  - async_log_success_event / async_log_failure_event: update last_request_time
    so the background idle-loop knows when to stop the container.

Env vars (provided via env_file in compose.yml):
  PORTAINER_API_KEY   Portainer API token
  PORTAINER_ENV_ID     Portainer environment ID (e.g. 3)
  PORTAINER_URL        Portainer base URL (https://host:9443)

Additional config (plain env, set in compose environment:):
  LLAMA_CONTAINER      container name to manage (default: llama-server)
  LLAMA_HEALTH_URL     health endpoint to poll (default: http://192.168.0.46:8084/health)
  IDLE_TIMEOUT         seconds idle before stop (default: 300)
  BOOT_TIMEOUT         seconds to wait for cold start (default: 90)
"""

import asyncio
import os
import time
import httpx
from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException


class LlamaCppIdleManager(CustomLogger):
    def __init__(self):
        super().__init__()
        self.last_request_time = time.time()
        self.start_lock = asyncio.Lock()
        self._watcher_started = False

        self.portainer_url = os.environ["PORTAINER_URL"].rstrip("/")
        self.portainer_key = os.environ["PORTAINER_API_KEY"]
        self.portainer_env_id = os.environ["PORTAINER_ENV_ID"]
        self._headers = {"X-API-Key": self.portainer_key}

        self.container = os.environ.get("LLAMA_CONTAINER", "llama-server")
        self.health_url = os.environ.get(
            "LLAMA_HEALTH_URL", "http://192.168.0.46:8084/health"
        )
        self.idle_timeout = int(os.environ.get("IDLE_TIMEOUT", "300"))
        self.boot_timeout = int(os.environ.get("BOOT_TIMEOUT", "90"))

    # ------------------------------------------------------------------
    # Portainer API helpers
    # ------------------------------------------------------------------
    def _api(self, path: str) -> str:
        return (
            f"{self.portainer_url}/api/endpoints/{self.portainer_env_id}"
            f"/docker/containers/{self.container}{path}"
        )

    async def _is_running(self) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=10) as c:
                r = await c.get(self._api("/json"), headers=self._headers)
            if r.status_code != 200:
                return True  # can't confirm stopped -> assume running (safe)
            return bool(r.json().get("State", {}).get("Running", False))
        except Exception:
            return True  # on error assume running (don't risk stopping a live one)

    async def _start_container(self) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=60) as c:
                r = await c.post(self._api("/start"), headers=self._headers)
            return r.status_code in (200, 204, 304)
        except Exception:
            return False

    async def _stop_container(self) -> bool:
        try:
            async with httpx.AsyncClient(verify=False, timeout=60) as c:
                r = await c.post(self._api("/stop"), headers=self._headers)
            return r.status_code in (200, 204, 304)
        except Exception:
            return False

    async def _wait_health(self) -> bool:
        deadline = time.time() + self.boot_timeout
        consecutive_ok = 0
        async with httpx.AsyncClient(timeout=5) as c:
            while time.time() < deadline:
                try:
                    r = await c.get(self.health_url)
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
        self.last_request_time = time.time()
        if not self._watcher_started:
            self._watcher_started = True
            asyncio.create_task(self._idle_loop())
        if not await self._is_running():
            async with self.start_lock:
                # re-check inside the lock (another coroutine may have started it)
                if not await self._is_running():
                    print(f"[idle-manager] starting {self.container} (cold start)")
                    if not await self._start_container():
                        raise HTTPException(
                            status_code=503,
                            detail=f"Failed to start {self.container} via Portainer API",
                        )
                    if not await self._wait_health():
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"{self.container} did not become healthy within "
                                f"{self.boot_timeout}s"
                            ),
                        )
                    print(f"[idle-manager] {self.container} is healthy")
        return data

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self.last_request_time = time.time()

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self.last_request_time = time.time()

    # ------------------------------------------------------------------
    # Background idle watcher
    # ------------------------------------------------------------------
    async def _idle_loop(self):
        print(
            f"[idle-manager] watcher started "
            f"(idle_timeout={self.idle_timeout}s, container={self.container})"
        )
        while True:
            await asyncio.sleep(30)
            if time.time() - self.last_request_time > self.idle_timeout:
                if await self._is_running():
                    print(
                        f"[idle-manager] idle for {self.idle_timeout}s, "
                        f"stopping {self.container}"
                    )
                    if await self._stop_container():
                        print(f"[idle-manager] {self.container} stopped")
                    # reset so we don't try to stop again immediately
                    self.last_request_time = time.time()
                else:
                    # already stopped; keep the timestamp fresh so we don't poll spam
                    self.last_request_time = time.time()


proxy_handler_instance = LlamaCppIdleManager()