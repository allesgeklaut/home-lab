"""Shared gateway-backed container lifecycle helpers.

Both GPU lifecycle managers use this module so the start/stop logic lives in
one place:

  * LlamaCppIdleManager in custom_callbacks.py (llama.cpp tenants)
  * the ComfyUI lifecycle proxy in comfyui_proxy/app.py (the comfyui tenant)

Only one tenant can own the 16 GB GPU at a time. Each manager stops the
tenants it does not own before starting its own, so a switch always looks like
"stop the others -> start mine -> wait for health".

Requires LIFECYCLE_URL, LIFECYCLE_API_KEY, LIFECYCLE_CA_FILE and LIFECYCLE_ENV_ID.
Direct Portainer access is not supported.
"""

import asyncio
import os
import ssl
import time
from urllib.parse import urlsplit

import aiohttp

LIFECYCLE_TIMEOUT = aiohttp.ClientTimeout(total=10)


class PortainerContainers:
    def __init__(self):
        gateway = os.environ.get("LIFECYCLE_URL", "").strip()
        parsed = urlsplit(gateway)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Lifecycle gateway requires an HTTPS origin")
        self.url = gateway.rstrip("/")
        self.key = os.environ["LIFECYCLE_API_KEY"]
        self.env_id = os.environ["LIFECYCLE_ENV_ID"]
        if not self.key or not self.env_id.isdecimal():
            raise ValueError("Missing lifecycle credentials or environment")
        self._headers = {"X-API-Key": self.key}
        ca = os.environ["LIFECYCLE_CA_FILE"].strip()
        if not ca:
            raise ValueError("Lifecycle gateway requires a trusted certificate")
        self._ssl = ssl.create_default_context(cafile=ca)

    def _api(self, container: str, path: str) -> str:
        return (
            f"{self.url}/api/endpoints/{self.env_id}"
            f"/docker/containers/{container}{path}"
        )

    async def is_running(self, container: str) -> bool:
        """Return True if the container is running.

        On any error assume running: never risk stopping something live.
        A 404 means the container does not exist yet -> not running.
        """
        try:
            async with aiohttp.ClientSession() as c:
                async with c.get(
                    self._api(container, "/json"),
                    headers=self._headers,
                    ssl=self._ssl,
                    timeout=LIFECYCLE_TIMEOUT,
                    allow_redirects=False,
                ) as r:
                    if r.status == 404:
                        return False
                    if r.status != 200:
                        return True
                    if r.content_length is not None and r.content_length > 1048576:
                        # Fail safe: an absurd response does not prove it stopped.
                        return True
                    data = await r.json()
                    return bool(data.get("State", {}).get("Running", False))
        except Exception:
            return True

    async def start(self, container: str) -> bool:
        try:
            async with aiohttp.ClientSession() as c:
                async with c.post(
                    self._api(container, "/start"),
                    headers=self._headers,
                    ssl=self._ssl,
                    timeout=LIFECYCLE_TIMEOUT,
                    allow_redirects=False,
                ) as r:
                    return r.status in (200, 204, 304)
        except Exception:
            return False

    async def stop(self, container: str) -> bool:
        try:
            async with aiohttp.ClientSession() as c:
                async with c.post(
                    self._api(container, "/stop"),
                    headers=self._headers,
                    ssl=self._ssl,
                    timeout=LIFECYCLE_TIMEOUT,
                    allow_redirects=False,
                ) as r:
                    return r.status in (200, 204, 304)
        except Exception:
            return False

    async def stop_and_wait(self, container: str, timeout: float = 120.0) -> bool:
        """Stop a container and wait until it has exited (VRAM freed)."""
        if not await self.is_running(container):
            return True
        print(f"[gpu] stopping {container} (model switch)")
        await self.stop(container)
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not await self.is_running(container):
                print(f"[gpu] {container} exited")
                return True
            await asyncio.sleep(1)
        print(f"[gpu] {container} did not exit within {timeout}s")
        return False

    async def wait_health(self, url: str, boot_timeout: int) -> bool:
        """Wait until *url* answers 200 twice in a row (server actually up)."""
        deadline = time.time() + boot_timeout
        consecutive_ok = 0
        timeout = aiohttp.ClientTimeout(total=5)
        async with aiohttp.ClientSession(timeout=timeout) as c:
            while time.time() < deadline:
                try:
                    async with c.get(url) as r:
                        if r.status == 200:
                            consecutive_ok += 1
                            # two consecutive 200s: avoids a transient "ok"
                            # while the model is still finalizing its load
                            if consecutive_ok >= 2:
                                return True
                        else:
                            consecutive_ok = 0
                except Exception:
                    consecutive_ok = 0
                await asyncio.sleep(1)
        return False
