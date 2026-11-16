"""
LiteLLM custom callbacks.

1. LlamaCppIdleManager: mutual-exclusion start + idle-stop for llama.cpp.
2. OpenCodeGoSessionHeader: adds the mandatory x-opencode-session header and a
   stable identifying User-Agent for OpenCode Go models (the Go gateway
   rejects requests without the session id).
3. VoxtralVoiceAlias: readable voice names + language-aware "auto" voice for
   Voxtral TTS (Mistral only accepts voice UUIDs / preset ids, not the names
   shown in Mistral Studio).
4. VoxtralSttContextBias: home-lab vocabulary bias for Voxtral transcription.
5. InferenceMetrics: proxy-observed timing and estimated throughput in streamed
   chat usage, displayed by Open WebUI's existing Info tooltip.

Only one llama.cpp container can hold the GPU at a time. Each user-facing
model is mapped to a container via the MODEL_CONTAINERS env var (JSON):

    MODEL_CONTAINERS={"qwen3.8-27b":{"container":"llama-server","health":"http://127.0.0.1:8084/health"},"qwen3.8-27b-abliterated":{"container":"llama-companion","health":"http://127.0.0.1:8086/health"}}

  - async_pre_call_hook: starts the background idle watcher on any request,
    then looks up the requested model. If its container is stopped, every
    *other* group container is stopped first (and waited for), then the target
    container is started (via the lifecycle gateway) and health-waited before
    the request proceeds. Concurrent requests during a cold start all wait on
    one lock. Each managed request gets a random opaque _idle_token in its
    metadata; the token is registered per container and released idempotently
    by the first terminal lifecycle signal.
  - async_post_call_success_hook / async_post_call_failure_hook: release the
    token for non-streaming proxy requests.
  - async_post_call_streaming_iterator_hook: pass chunks through and release
    the token in a finally block, covering normal end, upstream errors,
    cancellation and client disconnect/aclose.
  - async_log_success_event / async_log_failure_event: idempotent fallback
    release plus last_request_time update.
  - async_log_stream_event: heartbeat only; LiteLLM calls it per streamed
    chunk, so terminal release comes from the streaming iterator finally or
    proxy failure/success logging.
  - start_idle_watcher: optional LITELLM_WORKER_STARTUP_HOOKS entry point that
    starts the idle watcher before the first request and never raises.

The idle watcher probes llama.cpp's /metrics gauges
llamacpp:requests_processing and llamacpp:requests_deferred. A stale in-flight
token is cleared only after repeated authoritative idle probes (both gauges
present and zero). Missing/malformed metrics are treated as unknown and never
clear tokens; there is no age-based force release.

Lifecycle env vars (key via dedicated env_file, others in compose environment):
  LIFECYCLE_API_KEY   Restricted per-client gateway credential
  LIFECYCLE_ENV_ID    Approved local environment ID (3)
  LIFECYCLE_URL       Gateway HTTPS origin
  LIFECYCLE_CA_FILE   Trusted gateway certificate (hostname verified)

Additional config (plain env, set in compose environment:):
  MODEL_CONTAINERS    JSON: model_name -> {container, health[, metrics]}
                      (metrics defaults to /metrics on the health origin)
  IDLE_TIMEOUT        seconds idle before stop (default: 300)
  BOOT_TIMEOUT        seconds to wait for cold start (default: 90);
                      can be overridden per model via "boot_timeout" in the map
  EXTRA_GPU_CONTAINERS  comma-separated non-llama GPU tenants (e.g. comfyui)
                      that are stopped before cold-starting a managed model,
                      because only one container can hold the GPU at a time
  OPENCODE_GO_MODELS  comma-separated OpenCode Go model names; the
                      OpenCodeGoSessionHeader callback adds the mandatory
                      x-opencode-session header and a fixed User-Agent
                      for these

The Portainer start/stop/health helpers live in gpu_containers.py, shared with
the ComfyUI lifecycle proxy so both managers behave identically.
"""

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import sys
import threading
import time
import types
import uuid
from collections.abc import Mapping
from urllib.parse import urlsplit, urlunsplit

from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException

from gpu_containers import PortainerContainers


class LlamaCppIdleManager(CustomLogger):
    """Start/stop llama.cpp containers around requests and stop them when idle.

    In-flight tracking is request-scoped: every managed request gets a random
    opaque token in its metadata, and the token is released exactly once by
    whichever lifecycle signal fires first (proxy success/failure hook,
    streaming iterator close, or terminal logging callback). Releases are
    idempotent, so duplicate signals are harmless.
    """

    def __init__(self):
        super().__init__()
        # per-container last request time
        self.last_request_time = {}
        # per-container in-flight tokens: token -> registration timestamp.
        # A dict (not an aggregate counter) keeps releases idempotent and
        # makes duplicate/retried callbacks harmless.
        self.in_flight = {}
        self.start_lock = asyncio.Lock()
        self._watcher_started = False
        self._watcher_task = None
        # Containers whose health has been confirmed in this worker process.
        # A running container that is not in this set must pass wait_health
        # before admission. This covers a cold start that was cancelled or
        # timed out after the process started, and containers adopted after a
        # proxy restart. Cleared whenever the container is stopped.
        self._health_confirmed = set()
        # Idle watcher poll interval. Tests may shorten this; production
        # keeps the historical 30s cadence.
        self.poll_interval = 30
        # Stale-token recovery requires this many consecutive authoritative
        # backend-idle probes. This is deliberately not an age TTL: a long
        # generation keeps the backend busy and resets the streak.
        self.stale_idle_confirmations = 3
        self._backend_idle_streak = {}

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
                # Optional override; otherwise derive /metrics from /health.
                "metrics": cfg.get("metrics")
                or self._derive_metrics_url(cfg["health"]),
            }
            self.models[model] = entry
            self.lookup[model] = entry
            for alias in cfg.get("aliases", []):
                self.lookup[alias] = entry
        # container name -> model name (reverse map for the idle loop)
        self.containers = {cfg["container"]: m for m, cfg in self.models.items()}

    def _resolve(self, model) -> dict | None:
        """Map a litellm model string (user-facing or upstream) to its config."""
        if not isinstance(model, str) or not model:
            return None
        if model in self.lookup:
            return self.lookup[model]
        # upstream names look like "openai/qwen3.8:27b"
        if "/" in model:
            return self.lookup.get(model.split("/", 1)[1])
        return None

    # ------------------------------------------------------------------
    # Request-scoped lifecycle bookkeeping
    # ------------------------------------------------------------------
    @staticmethod
    def _log(message):
        # flush=True: the proxy's stdout is a pipe and block-buffered, which
        # previously delayed watchdog diagnostics by up to ~50 minutes.
        try:
            print(f"[idle-manager] {message}", flush=True)
        except Exception:
            # Diagnostics must never break request or watcher cleanup.
            pass

    @staticmethod
    def _short(token):
        return token[:8] if isinstance(token, str) and token else "-"

    def _ensure_token(self, data):
        metadata = data.get("metadata")
        if not isinstance(metadata, dict):
            metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
            data["metadata"] = metadata
        token = uuid.uuid4().hex
        # Overwrite any client-supplied value: the token is our lifecycle key.
        metadata["_idle_token"] = token
        return token

    @staticmethod
    def _token_from(data):
        if not isinstance(data, Mapping):
            return None
        candidates = []
        metadata = data.get("metadata")
        if isinstance(metadata, Mapping):
            candidates.append(metadata.get("_idle_token"))
        params = data.get("litellm_params")
        if isinstance(params, Mapping):
            metadata = params.get("metadata")
            if isinstance(metadata, Mapping):
                candidates.append(metadata.get("_idle_token"))
        details = data.get("model_call_details")
        if isinstance(details, Mapping):
            params = details.get("litellm_params")
            if isinstance(params, Mapping):
                metadata = params.get("metadata")
                if isinstance(metadata, Mapping):
                    candidates.append(metadata.get("_idle_token"))
        candidates.append(data.get("_idle_token"))
        for token in candidates:
            if isinstance(token, str) and token:
                return token
        return None

    def _release_token(self, container, token, reason):
        tracked = self.in_flight.get(container)
        if not tracked or token not in tracked:
            return False
        tracked.pop(token, None)
        self._log(
            f"released {container} token={self._short(token)} "
            f"active={len(tracked)} reason={reason}"
        )
        return True

    def _release_in_flight(self, data, reason):
        if not isinstance(data, Mapping):
            return False
        token = self._token_from(data)
        if not token:
            cfg = self._resolve(data.get("model"))
            # Only a container that still has tracked work makes a missing
            # token diagnostic; failures before registration have nothing to
            # release.
            if cfg and self.in_flight.get(cfg["container"]):
                self._log(
                    f"release skipped for {cfg['container']}: no _idle_token "
                    f"(reason={reason})"
                )
            return False
        # Fast path: the model still maps to the container that registered the
        # token. If a retry/fallback changed the model name, fall back to the
        # token itself, which is the stable request identity.
        cfg = self._resolve(data.get("model"))
        if cfg and self._release_token(cfg["container"], token, reason):
            return True
        for container in list(self.in_flight):
            if self._release_token(container, token, reason):
                return True
        return False

    def _touch(self, data):
        if not isinstance(data, Mapping):
            return
        cfg = self._resolve(data.get("model"))
        if cfg:
            self.last_request_time[cfg["container"]] = time.time()

    @staticmethod
    def _derive_metrics_url(health):
        if not isinstance(health, str) or not health:
            return None
        try:
            parsed = urlsplit(health)
        except ValueError:
            return None
        if parsed.scheme not in ("http", "https") or not parsed.netloc:
            return None
        return urlunsplit((parsed.scheme, parsed.netloc, "/metrics", "", ""))

    def _config_for_container(self, container):
        model = self.containers.get(container)
        return self.models.get(model) if model else None

    @staticmethod
    async def _read_bounded(content, limit=65536):
        """Read a complete body up to ``limit`` bytes; None if oversized.

        ``StreamReader.read(n)`` may return fewer than n bytes even when more
        data is buffered, so a single read could truncate the payload and hide
        a nonzero gauge. Keep reading until EOF and reject anything larger
        than the limit instead of accepting a truncated prefix.
        """
        chunks = []
        total = 0
        while total <= limit:
            chunk = await content.read(limit + 1 - total)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        if total > limit:
            return None
        return b"".join(chunks)

    async def _fetch_metrics_text(self, url):
        """Fetch a bounded, complete Prometheus snapshot, or None when unknown."""
        try:
            import aiohttp
        except ImportError:
            return None
        timeout = aiohttp.ClientTimeout(total=3)
        try:
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return None
                    data = await self._read_bounded(response.content)
                    if data is None:
                        return None
                    return data.decode("utf-8", "replace")
        except Exception:
            return None

    @staticmethod
    def _valid_label_block(block):
        """Validate ``label="value"[,label="value"]`` text without braces."""
        if not block.strip():
            return True
        i = 0
        n = len(block)
        while True:
            while i < n and block[i].isspace():
                i += 1
            if i >= n or not (block[i].isalpha() or block[i] == "_"):
                return False
            i += 1
            while i < n and (block[i].isalnum() or block[i] == "_"):
                i += 1
            while i < n and block[i].isspace():
                i += 1
            if i >= n or block[i] != "=":
                return False
            i += 1
            while i < n and block[i].isspace():
                i += 1
            if i >= n or block[i] != '"':
                return False
            i += 1
            while i < n:
                ch = block[i]
                if ch == "\\":
                    i += 2
                    continue
                if ch == '"':
                    i += 1
                    break
                i += 1
            else:
                return False
            while i < n and block[i].isspace():
                i += 1
            if i >= n:
                return True
            if block[i] != ",":
                return False
            i += 1

    @staticmethod
    def _parse_metrics_idle(text):
        """Return idle/busy/unknown from llama.cpp Prometheus gauges.

        Authoritative idle requires BOTH ``llamacpp:requests_processing`` and
        ``llamacpp:requests_deferred`` to be present and zero. Parsing is
        strict: the sample value must be the first token after the metric
        name (and optional label block) and must be a finite number. A
        malformed wanted-metric line is ``unknown``; a timestamp is never
        used as a fallback value. Missing gauges are ``unknown`` (fail safe:
        never stop on unknown).
        """
        if not isinstance(text, str):
            return "unknown"
        wanted = {
            "llamacpp:requests_processing",
            "llamacpp:requests_deferred",
        }
        samples = {name: [] for name in wanted}
        for raw_line in text.splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            # Metric name: up to whitespace or the optional label block.
            index = 0
            while (
                index < len(line)
                and not line[index].isspace()
                and line[index] != "{"
            ):
                index += 1
            name = line[:index]
            if name not in wanted:
                continue
            # Optional label block. Scan quoted values so a '}' inside a
            # label value does not terminate the block early.
            if index < len(line) and line[index] == "{":
                label_start = index + 1
                index = label_start
                in_quotes = False
                escaped = False
                while index < len(line):
                    char = line[index]
                    if in_quotes:
                        if escaped:
                            escaped = False
                        elif char == "\\":
                            escaped = True
                        elif char == '"':
                            in_quotes = False
                    elif char == '"':
                        in_quotes = True
                    elif char == "}":
                        break
                    index += 1
                if index >= len(line) or line[index] != "}":
                    return "unknown"
                if not LlamaCppIdleManager._valid_label_block(
                    line[label_start:index]
                ):
                    return "unknown"
                index += 1
            # The value must follow whitespace immediately after the name or
            # label block. Do not search later tokens for a number.
            if index >= len(line) or not line[index].isspace():
                return "unknown"
            fields = line[index:].split()
            if not fields or len(fields) > 2:
                return "unknown"
            if not re.fullmatch(
                r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
                fields[0],
            ):
                return "unknown"
            value = float(fields[0])
            if not math.isfinite(value):
                return "unknown"
            if len(fields) == 2:
                # Optional Prometheus timestamp: validate it, but never use
                # it as the sample value.
                if not re.fullmatch(r"[+-]?\d+", fields[1]):
                    return "unknown"
                if int(fields[1]) < 0:
                    return "unknown"
            samples[name].append(value)
        if any(not samples[name] for name in wanted):
            return "unknown"
        if any(value < 0 for values in samples.values() for value in values):
            return "unknown"
        if any(value > 0 for values in samples.values() for value in values):
            return "busy"
        return "idle"

    async def _probe_backend_idle(self, cfg):
        if not isinstance(cfg, Mapping):
            return "unknown"
        url = cfg.get("metrics")
        if not url:
            return "unknown"
        text = await self._fetch_metrics_text(url)
        if text is None:
            return "unknown"
        return self._parse_metrics_idle(text)

    def _ensure_watcher(self):
        if self._watcher_started:
            return
        if self._watcher_task is not None and not self._watcher_task.done():
            self._watcher_started = True
            return
        try:
            task = asyncio.create_task(self._idle_loop())
        except Exception:
            # No running loop (or task creation failed): leave the flag clear
            # so the next request or startup hook can retry.
            self._watcher_started = False
            self._watcher_task = None
            raise
        self._watcher_started = True
        self._watcher_task = task
        task.add_done_callback(self._on_watcher_done)

    def _on_watcher_done(self, task):
        if task is not self._watcher_task:
            return
        self._watcher_started = False
        self._watcher_task = None
        if task.cancelled():
            self._log("watcher cancelled; will restart on the next request")
            return
        exc = task.exception()
        if exc is not None:
            self._log(
                f"watcher crashed: {exc!r}; will restart on the next request"
            )

    @staticmethod
    async def _close_stream_quietly(response):
        close = getattr(response, "aclose", None)
        if close is None:
            close = getattr(response, "close", None)
        if close is None:
            return
        try:
            closing = close()
            if inspect.isawaitable(closing):
                await closing
        except asyncio.CancelledError:
            raise
        except Exception:
            # Cleanup must not replace a provider error or turn a completed
            # response into an error. No payloads are logged.
            logging.getLogger(__name__).warning(
                "LlamaCppIdleManager: stream cleanup failed"
            )

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
        # Start the watcher on any request, not only managed ones, so a
        # post-restart request for a cloud model still recovers idle stopping.
        self._ensure_watcher()

        cfg = self._resolve(data.get("model"))
        if cfg is None:
            return data  # not a managed model, pass through untouched

        container = cfg["container"]
        self.last_request_time[container] = time.time()
        token = self._ensure_token(data)
        tracked = self.in_flight.setdefault(container, {})
        tracked[token] = time.time()
        self._backend_idle_streak[container] = 0
        self._log(
            f"registered {container} token={self._short(token)} "
            f"active={len(tracked)}"
        )
        try:
            async with self.start_lock:
                # A concurrent GPU switch or idle stop may have cleared this
                # token while the request waited for the lock. Re-register it
                # under the same lock before admitting the request.
                tracked = self.in_flight.setdefault(container, {})
                if token not in tracked:
                    tracked[token] = time.time()
                    self._backend_idle_streak[container] = 0
                    self._log(
                        f"re-registered {container} token={self._short(token)} "
                        f"active={len(tracked)} reason=concurrent_switch"
                    )

                running = await self.portainer.is_running(container)
                if not running:
                    self._health_confirmed.discard(container)
                    # make room: stop every other group container and every
                    # extra GPU tenant (e.g. comfyui) first. If one refuses to
                    # exit, do NOT start on top of it (they would fight over
                    # the 16 GB of VRAM).
                    switched = True
                    stopped_others = []
                    for other in self.containers:
                        if other != container:
                            if await self.portainer.stop_and_wait(other):
                                stopped_others.append(other)
                            else:
                                switched = False
                    for other in self.extra:
                        if not await self.portainer.stop_and_wait(other):
                            switched = False

                    # A confirmed stop means no request can still be running
                    # in that container. Clear its tokens so a killed request
                    # cannot pin a future idle stop. Do not clear the target:
                    # concurrent waiters may have registered there.
                    for other in stopped_others:
                        self._health_confirmed.discard(other)
                        stale = self.in_flight.get(other)
                        if stale:
                            count = len(stale)
                            stale.clear()
                            self._log(
                                f"cleared {count} stale in-flight token(s) "
                                f"for {other} after GPU switch to {container}"
                            )

                    if not switched:
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"Cannot start {container}: another GPU "
                                "container is still holding the GPU"
                            ),
                        )
                    self._log(f"starting {container} (cold start)")
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
                    self._health_confirmed.add(container)
                    self._log(f"{container} is healthy")
                elif container not in self._health_confirmed:
                    # The container is running but this worker has not
                    # confirmed its health. This covers a cold start that was
                    # cancelled or timed out after the process started, and a
                    # container adopted after a proxy restart. Wait for health
                    # under the same lock instead of admitting into a loading
                    # server.
                    self._log(
                        f"{container} is running but health is unconfirmed; "
                        "waiting"
                    )
                    if not await self.portainer.wait_health(
                        cfg["health"], cfg["boot_timeout"]
                    ):
                        raise HTTPException(
                            status_code=503,
                            detail=(
                                f"{container} is running but did not become "
                                f"healthy within {cfg['boot_timeout']}s"
                            ),
                        )
                    self._health_confirmed.add(container)
                    self._log(f"{container} health confirmed")
            return data
        except BaseException:
            # Includes asyncio.CancelledError (a BaseException in Python 3.8+):
            # a client disconnect during a cold start must not leak the token.
            self._release_in_flight(data, "pre_call_error")
            raise

    async def async_post_call_success_hook(
        self, data, user_api_key_dict, response
    ):
        # The proxy calls this before a streaming response is consumed, so a
        # streaming request is released by the iterator hook instead.
        if not (isinstance(data, Mapping) and data.get("stream") is True):
            self._release_in_flight(data, "post_call_success")
        self._touch(data)
        return response

    async def async_post_call_failure_hook(
        self,
        request_data,
        original_exception,
        user_api_key_dict,
        traceback_str=None,
        **kwargs,
    ):
        self._release_in_flight(request_data, "post_call_failure")
        self._touch(request_data)
        return None

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        try:
            async for chunk in response:
                yield chunk
        finally:
            # Release before closing upstream: cleanup must not delay or
            # prevent the idle bookkeeping. The release is idempotent, so a
            # later success/failure log event is harmless.
            self._release_in_flight(request_data, "stream_closed")
            self._touch(request_data)
            await self._close_stream_quietly(response)

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._release_in_flight(kwargs, "log_success")
        self._touch(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._release_in_flight(kwargs, "log_failure")
        self._touch(kwargs)

    async def async_log_stream_event(self, kwargs, response_obj, start_time, end_time):
        # LiteLLM 1.106.x calls this for every streamed chunk, not only the
        # terminal one (streaming_handler.py runs success logging per chunk
        # and async_success_handler dispatches here when no complete response
        # was assembled). Treat it as a heartbeat only: touching
        # last_request_time keeps the idle watcher from stopping an active
        # stream. The token is released by the streaming iterator's finally
        # block, the proxy failure hook, or terminal success/failure logging;
        # releasing here would make active=0 after the first chunk.
        self._touch(kwargs)

    # ------------------------------------------------------------------
    # Background idle watcher
    # ------------------------------------------------------------------
    def _init_watcher_state(self, now=None):
        if now is None:
            now = time.time()
        self._backend_idle_streak.clear()
        recovered = []
        for container in self.containers:
            if container not in self.last_request_time:
                self.last_request_time[container] = now
                recovered.append(container)
        if recovered:
            self._log(
                f"watcher initialized idle clock for {recovered} "
                "(restart recovery)"
            )

    async def _idle_loop(self):
        self._init_watcher_state()
        self._log(
            f"watcher started "
            f"(idle_timeout={self.idle_timeout}s, containers={list(self.containers)})"
        )
        while True:
            await asyncio.sleep(self.poll_interval)
            try:
                await self._idle_tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(f"watcher tick failed: {exc!r}; continuing")

    async def _idle_tick(self, now=None):
        if now is None:
            now = time.time()
        for container in self.containers:
            try:
                await self._idle_container(container, now)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self._log(
                    f"watcher error for {container}: {exc!r}; continuing"
                )

    async def _idle_container(self, container, now):
        # Serialize idle decisions with pre-call admission and cold starts: a
        # request that registered while this tick was running must be seen
        # before the container is stopped.
        async with self.start_lock:
            last = self.last_request_time.get(container)
            if last is None:
                # First sighting: wait a full idle window instead of stopping a
                # container that may have just been started externally.
                self._backend_idle_streak[container] = 0
                self.last_request_time[container] = now
                return
            if now - last <= self.idle_timeout:
                return
            if not await self.portainer.is_running(container):
                # Already stopped; keep the timestamp fresh so we don't poll spam.
                self._health_confirmed.discard(container)
                self._backend_idle_streak[container] = 0
                self.last_request_time[container] = now
                return

            # A request may have completed while is_running() was awaited.
            # Preserve the full idle window instead of stopping immediately.
            if self.last_request_time.get(container) != last:
                self._backend_idle_streak[container] = 0
                return

            tracked = self.in_flight.get(container) or {}
            snapshot = dict(tracked)
            if tracked:
                oldest = min(tracked.values())
                self._log(
                    f"{container} has {len(tracked)} in-flight token(s), "
                    f"oldest {max(0.0, now - oldest):.0f}s; probing backend"
                )
            else:
                self._log(
                    f"{container} idle for {self.idle_timeout}s; probing backend"
                )

            # Probe before EVERY stop, including containers with no tracked
            # tokens. A direct local request, startup adoption, or an early
            # log_stream release can leave a busy generation with no token.
            status = await self._probe_backend_idle(
                self._config_for_container(container)
            )

            # Re-check after the awaited probe: pre-calls register their token
            # and refresh last_request_time before waiting on start_lock, so a
            # request admitted during the probe must prevent the stop.
            current = self.in_flight.get(container) or {}
            new_tokens = set(current) - set(snapshot)
            if new_tokens:
                self._backend_idle_streak[container] = 0
                self._log(
                    f"{container} admitted {len(new_tokens)} new request(s) "
                    "during the backend probe; not stopping"
                )
                return
            released_tokens = set(snapshot) - set(current)
            if (
                released_tokens
                or self.last_request_time.get(container) != last
            ):
                self._backend_idle_streak[container] = 0
                self._log(
                    f"{container} request activity during the backend probe; "
                    "not stopping"
                )
                return
            if status != "idle":
                # Unknown is fail-safe: retain the GPU rather than risk
                # killing a generation we cannot prove is finished.
                self._backend_idle_streak[container] = 0
                self._log(
                    f"{container} backend probe {status}; not stopping "
                    f"({len(current)} in-flight token(s))"
                )
                return

            if current:
                # Stale-token recovery: require repeated authoritative idle
                # probes before clearing tokens that no live request owns.
                streak = self._backend_idle_streak.get(container, 0) + 1
                if streak < self.stale_idle_confirmations:
                    self._backend_idle_streak[container] = streak
                    self._log(
                        f"{container} backend idle confirmation "
                        f"{streak}/{self.stale_idle_confirmations}; not "
                        f"clearing {len(current)} in-flight token(s) yet"
                    )
                    return
                # Repeated authoritative idle: these tokens have no matching
                # live request. Clear only the snapshot tokens; a token that
                # arrived after the snapshot was already handled above.
                stale_tokens = [
                    token for token in snapshot if token in current
                ]
                for token in stale_tokens:
                    current.pop(token, None)
                self._backend_idle_streak[container] = 0
                if stale_tokens:
                    self._log(
                        f"cleared {len(stale_tokens)} stale in-flight "
                        f"token(s) for {container} after {streak} confirmed "
                        "backend idle probes"
                    )
                if current:
                    self._log(
                        f"{container} has {len(current)} new in-flight "
                        "token(s) after stale-token clearance; not stopping"
                    )
                    return
            else:
                self._backend_idle_streak[container] = 0

            # Final guard immediately before stop: a request can register
            # outside start_lock while the probe/clear work above runs.
            if (
                self.in_flight.get(container)
                or self.last_request_time.get(container) != last
            ):
                self._log(
                    f"{container} activity appeared before idle stop; "
                    "keeping it running"
                )
                return

            self._log(f"idle for {self.idle_timeout}s, stopping {container}")
            if await self.portainer.stop(container):
                self._log(f"{container} stopped")
                self._health_confirmed.discard(container)
                self._backend_idle_streak[container] = 0
                self.last_request_time[container] = now
            else:
                # Do not refresh: the container is still running, so retry on
                # the next tick instead of waiting another full idle window.
                self._log(f"failed to stop {container}; will retry next tick")


class OpenCodeGoSessionHeader(CustomLogger):
    """Inject x-opencode-session and our User-Agent on OpenCode Go models.

    The Go gateway rejects requests that lack a stable per-conversation
    session id ("MissingSessionID"). Resolve one from, in order:

      1. an incoming x-opencode-session header (clients that send one, e.g.
         OpenCode itself; kept for clients that route through this proxy),
      2. OpenWebUI's X-OpenWebUI-Chat-Id header (exact per-conversation id,
         forwarded when ENABLE_FORWARD_USER_INFO_HEADERS=True on open-webui),
      3. litellm's session id (x-litellm-session-id / x-<vendor>-session-id
         headers, Anthropic metadata.user_id),
      4. a stable hash of the user and the first message, so stateless clients
         (e.g. OpenWebUI) still group each conversation under one session id.

    The outgoing User-Agent is always ours (johannes-litellm/1.0): the Go docs
    ask clients to identify with their own UA rather than a generic
    HTTP-library name, and OpenWebUI would otherwise forward aiohttp's
    "Python/3.x aiohttp/3.x". OpenCode CLI talks to Go directly, so no real
    client identity is lost.

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
        # Always identify with our own UA. The only client reaching this proxy
        # for Go models is OpenWebUI, which sends aiohttp's generic
        # "Python/3.x aiohttp/3.x" - exactly the HTTP-library User-Agent the Go
        # docs ask clients not to use. OpenCode CLI talks to Go directly, so no
        # real client identity is lost by overriding.
        for key in [k for k in extra if k.lower() == "user-agent"]:
            extra.pop(key)
        extra["User-Agent"] = "johannes-litellm/1.0"
        data["extra_headers"] = extra
        return data

    @staticmethod
    def _headers(data) -> dict:
        """Incoming client headers, lowercased, string values only."""
        psr = data.get("proxy_server_request")
        if not isinstance(psr, dict):
            return {}
        return {
            str(k).lower(): v
            for k, v in (psr.get("headers") or {}).items()
            if isinstance(v, str)
        }

    def _resolve_session(self, data) -> str:
        headers = self._headers(data)
        if headers.get("x-opencode-session"):
            return headers["x-opencode-session"]
        if headers.get("x-openwebui-chat-id"):
            # Exact per-conversation id, forwarded by OpenWebUI when
            # ENABLE_FORWARD_USER_INFO_HEADERS=True. Hash it with the user so
            # the value stays opaque and fixed-length (matching the
            # fingerprint fallback).
            seed = f"{self._user_seed(data)}|chat:{headers['x-openwebui-chat-id']}"
            return hashlib.sha256(seed.encode("utf-8", "replace")).hexdigest()[:32]
        if data.get("litellm_session_id"):
            return str(data["litellm_session_id"])
        return self._fingerprint_hash(data)

    def _incoming_header(self, data, name) -> str:
        return self._headers(data).get(name, "") or ""

    def _user_seed(self, data) -> str:
        # data["user"] is empty on this proxy (no user wired up), so fall back
        # to OpenWebUI's X-OpenWebUI-User-Id header (keys are lowercased).
        if data.get("user"):
            return str(data["user"])
        return self._incoming_header(data, "x-openwebui-user-id")

    def _fingerprint_hash(self, data) -> str:
        # OpenWebUI (and other stateless clients) resend the full history, so
        # the first user/assistant message stays constant for a conversation.
        # Hash it with the user id to get a stable per-conversation session
        # id. System messages are skipped: they are usually one constant
        # prompt shared by every conversation, which would collapse them all
        # into a single session.
        seed = [self._user_seed(data)]
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


class VoxtralVoiceAlias(CustomLogger):
    """Readable voice names + language-aware "auto" voice for Voxtral TTS.

    Mistral's TTS API accepts preset voice IDs (voice_481, gb_jane_sarcasm,
    ...) and saved-voice UUIDs - the human-readable names shown in Mistral
    Studio ("Heidi", "Jane") return 404 invalid_voice. The OpenWebUI TTS
    voice field is free text passed straight through, so this hook rewrites
    friendly names to the preset IDs before the request is routed.

    Accepted voice values on the voxtral-tts model (case-insensitive):

      - "heidi" / "de": Heidi, the native German Voxtral 3 preset (female).
        "jo" redirects to Heidi - Jo has no Voxtral 3 equivalent.
      - "mary" / "mary-uk" / "en": Mary, the default English (UK) Voxtral 3
        preset (female). "harriet" remains an explicit alias for Harriet for
        backward compatibility. "jane": Jane - Neutral (gb_jane_neutral, one
        of the Voxtral 2 emotion presets still accepted by TTS 3).
      - "<person>-<mood>": any Voxtral 2 emotion preset, e.g.
        "jane-sarcasm", "paul-cheerful", "oliver-curious", "marie-happy".
      - "auto": detect the language of the text being spoken - German
        -> Heidi, English -> Mary (UK); ties, ambiguous chunks, and other
        languages default to Heidi. OpenWebUI splits long replies on
        punctuation and synthesizes chunk by chunk, so
        detection runs per chunk: mixed-language answers use the right
        voice per sentence. Ambiguous short chunks ("1234") default to
        Heidi (the instance's primary language is German).
      - Preset IDs (voice_481, ...) and UUIDs pass through untouched
        (custom/cloned voices from Mistral Studio keep working), as do
        litellm's OpenAI aliases (alloy, nova, ...), which litellm maps
        to English presets on its own.
    """

    TTS_MODEL = "voxtral-tts"

    # Voxtral 3 preset voice IDs (docs: studio/audio/text-to-speech/voices).
    HEIDI = "voice_481"  # de, female - the Voxtral 3 German preset
    HARRIET = "voice_590"  # en-GB, female - retained for explicit alias
    MARY_UK = "voice_1787"  # en-GB, female - the default English voice
    # Voxtral 2 emotion presets are still accepted by voxtral-mini-tts-3-0.
    JANE_NEUTRAL = "gb_jane_neutral"
    JANE_SARCASM = "gb_jane_sarcasm"
    JANE_CONFUSED = "gb_jane_confused"
    JANE_CURIOUS = "gb_jane_curious"
    JANE_SAD = "gb_jane_sad"
    JANE_FRUSTRATED = "gb_jane_frustrated"
    JANE_SHAMEFUL = "gb_jane_shameful"
    JANE_JEALOUSY = "gb_jane_jealousy"
    PAUL_NEUTRAL = "en_paul_neutral"
    PAUL_SAD = "en_paul_sad"
    PAUL_HAPPY = "en_paul_happy"
    PAUL_FRUSTRATED = "en_paul_frustrated"
    PAUL_EXCITED = "en_paul_excited"
    PAUL_CONFIDENT = "en_paul_confident"
    PAUL_CHEERFUL = "en_paul_cheerful"
    PAUL_ANGRY = "en_paul_angry"
    OLIVER_NEUTRAL = "gb_oliver_neutral"
    OLIVER_SAD = "gb_oliver_sad"
    OLIVER_EXCITED = "gb_oliver_excited"
    OLIVER_CURIOUS = "gb_oliver_curious"
    OLIVER_CONFIDENT = "gb_oliver_confident"
    OLIVER_CHEERFUL = "gb_oliver_cheerful"
    OLIVER_ANGRY = "gb_oliver_angry"
    MARIE_NEUTRAL = "fr_marie_neutral"
    MARIE_HAPPY = "fr_marie_happy"
    MARIE_SAD = "fr_marie_sad"
    MARIE_EXCITED = "fr_marie_excited"
    MARIE_CURIOUS = "fr_marie_curious"
    MARIE_ANGRY = "fr_marie_angry"

    ALIASES = {
        "jo": HEIDI,  # Jo has no Voxtral 3 equivalent; redirect to Heidi
        "de": HEIDI,
        "heidi": HEIDI,
        "harriet": HARRIET,
        "mary": MARY_UK,
        "mary-uk": MARY_UK,
        "jane": JANE_NEUTRAL,
        "en": MARY_UK,
        "jane-sarcasm": JANE_SARCASM,
        "jane-confused": JANE_CONFUSED,
        "jane-curious": JANE_CURIOUS,
        "jane-sad": JANE_SAD,
        "jane-frustrated": JANE_FRUSTRATED,
        "jane-shameful": JANE_SHAMEFUL,
        "jane-jealousy": JANE_JEALOUSY,
        "paul": PAUL_NEUTRAL,
        "paul-sad": PAUL_SAD,
        "paul-happy": PAUL_HAPPY,
        "paul-frustrated": PAUL_FRUSTRATED,
        "paul-excited": PAUL_EXCITED,
        "paul-confident": PAUL_CONFIDENT,
        "paul-cheerful": PAUL_CHEERFUL,
        "paul-angry": PAUL_ANGRY,
        "oliver": OLIVER_NEUTRAL,
        "oliver-sad": OLIVER_SAD,
        "oliver-excited": OLIVER_EXCITED,
        "oliver-curious": OLIVER_CURIOUS,
        "oliver-confident": OLIVER_CONFIDENT,
        "oliver-cheerful": OLIVER_CHEERFUL,
        "oliver-angry": OLIVER_ANGRY,
        "marie": MARIE_NEUTRAL,
        "marie-happy": MARIE_HAPPY,
        "marie-sad": MARIE_SAD,
        "marie-excited": MARIE_EXCITED,
        "marie-curious": MARIE_CURIOUS,
        "marie-angry": MARIE_ANGRY,
    }

    _UMLAUT_RE = re.compile(r"[äöüßÄÖÜ]")

    # Frequent function words only - enough signal for the short chunks
    # OpenWebUI sends (it splits replies on punctuation before synthesis).
    _DE_WORDS = {
        "der", "die", "das", "und", "nicht", "ist", "ein", "eine", "mit",
        "für", "auf", "dem", "den", "von", "ich", "du", "wir", "sie", "aber",
        "oder", "auch", "schon", "noch", "nur", "wie", "was", "wann", "wo",
        "wer", "ja", "nein", "gut", "danke", "bitte", "hallo", "geht", "dir",
        "mich", "dich", "sich", "haben", "sein", "war", "wird", "kann",
        "muss", "soll", "hier", "dort", "über", "unter", "sehr", "wieder",
    }
    _EN_WORDS = {
        "the", "and", "is", "are", "was", "were", "of", "to", "in", "on",
        "for", "with", "that", "this", "it", "you", "i", "we", "they", "not",
        "no", "yes", "have", "has", "been", "be", "do", "does", "did", "will",
        "would", "can", "could", "should", "here", "there", "how", "what",
        "when", "where", "who", "hello", "thanks", "please", "good", "okay",
        "right", "just", "very", "again",
    }

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        if data.get("model") != self.TTS_MODEL:
            return data
        voice = data.get("voice")
        if not isinstance(voice, str) or not voice.strip():
            return data
        v = voice.strip()
        if v.lower() == "auto":
            data["voice"] = self._voice_for_text(data.get("input"))
            return data
        mapped = self.ALIASES.get(v.lower())
        if mapped is not None:
            data["voice"] = mapped
        # UUIDs (custom Studio voices) and litellm's OpenAI aliases
        # (alloy, nova, ...) pass through untouched.
        return data

    def _voice_for_text(self, text):
        t = text if isinstance(text, str) else ""
        if self._UMLAUT_RE.search(t):
            return self.HEIDI
        words = re.findall(r"[a-z]+", t.lower())
        de = sum(w in self._DE_WORDS for w in words)
        en = sum(w in self._EN_WORDS for w in words)
        # Ties and no-signal chunks default to Heidi (German is the primary
        # language of this instance); English selects Mary (UK).
        return self.MARY_UK if en > de else self.HEIDI


class VoxtralSttContextBias(CustomLogger):
    """Inject a home-lab vocabulary bias into every voxtral-stt request.

    Voxtral 3 batch transcription accepts a `context_bias` form field: names,
    terms, or a short description of the recording that steers the model
    toward the right spellings (docs: speech-to-text/bias-context-and-
    vocabulary). Open WebUI's STT integration only sends the audio file and
    the model name, so this hook injects the bias server-side. The user
    mostly dictates English, which is the optimized case for biasing.

    Mechanics (verified against litellm v1.105.1): an unknown non-OpenAI
    param rides the provider-specific path (the same one `diarize` uses)
    into the Mistral form fields and never hits the param-validation gate.
    The value must be a single string - litellm str()-ifies values, so a
    list would arrive as "['litellm', 'navidrome']" garbage. A
    client-supplied value is respected; `None`, an empty string, or an empty
    list is treated as opt-out and removed so no empty `context_bias` reaches
    the upstream form request. All other supplied values remain unchanged.
    """

    STT_MODEL = "voxtral-stt"

    # A comma-separated keyphrase list (one string - litellm str()-ifies
    # values, and through the proxy path only a single context_bias entry is
    # possible). This 17-term list names vocabulary worth biasing. One limited
    # synthetic experiment suggested a shorter list worked better for its
    # sample; that does not establish a universal ideal count or dilution rule.
    # Counts toward the model's context window (32k tokens for Small
    # Transcribe 3) - a dictation clip plus this string is nowhere near the
    # limit.
    CONTEXT_BIAS = (
        "litellm, voxtral, trilium, navidrome, searxng, mcpo, engram, "
        "open webui, heidi, harriet, cloudflared, calibre-web, freshrss, "
        "immich, jellyfin, audiobookshelf, GLM"
    )

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        if call_type != "transcription" or data.get("model") != self.STT_MODEL:
            return data
        if "context_bias" in data:
            # Empty opt-outs must be omitted, not sent as empty form values.
            context_bias = data["context_bias"]
            if (
                context_bias is None
                or (isinstance(context_bias, str) and not context_bias)
                or (isinstance(context_bias, list) and not context_bias)
            ):
                data.pop("context_bias")
            return data
        data["context_bias"] = self.CONTEXT_BIAS
        return data


class InferenceMetrics(CustomLogger):
    """Annotate existing chat usage without buffering or estimating token counts.

    Throughput is provider-reported completion tokens / full proxy request time,
    including prefill, network waits and any reasoning counted by the provider.
    It is deliberately NOT advertised as GPU decode speed. First-output timing
    includes reasoning and tool-call deltas, but ignores role-only chunks.
    All state is request-local; other API/response formats pass through unchanged.
    """

    START_KEY = "_proxy_inference_started_at"

    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        if call_type in {"completion", "acompletion"} and data.get("stream") is True:
            metadata = data.setdefault("metadata", {})
            if isinstance(metadata, dict):
                # LiteLLM's internal chat metadata, not an upstream body field.
                metadata[self.START_KEY] = time.perf_counter()
        return data

    @staticmethod
    def _get(value, key, default=None):
        if isinstance(value, dict):
            return value.get(key, default)
        return getattr(value, key, default)

    @classmethod
    def _has_output(cls, chunk):
        for choice in cls._get(chunk, "choices", None) or []:
            delta = cls._get(choice, "delta")
            if any(
                cls._get(delta, key)
                for key in (
                    "content", "reasoning_content", "reasoning", "thinking",
                    "refusal",
                )
            ):
                return True
            # Providers can emit an index/type-only tool placeholder before
            # generating its ID, name or arguments. That is not output yet.
            for tool in cls._get(delta, "tool_calls", None) or []:
                function = cls._get(tool, "function")
                if (
                    cls._get(tool, "id")
                    or cls._get(function, "name")
                    or cls._get(function, "arguments")
                ):
                    return True
            function = cls._get(delta, "function_call")
            if cls._get(function, "name") or cls._get(function, "arguments"):
                return True
        return False

    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict, response, request_data
    ):
        metadata = request_data.get("metadata") or {}
        started = self._get(metadata, self.START_KEY)
        enabled = (
            isinstance(started, (int, float))
            and not isinstance(started, bool)
            and math.isfinite(started)
        )
        first_output = None
        try:
            async for chunk in response:
                if enabled and self._get(chunk, "object") == "chat.completion.chunk":
                    now = time.perf_counter()
                    if first_output is None and self._has_output(chunk):
                        first_output = now
                    usage = self._get(chunk, "usage")
                    tokens = self._get(usage, "completion_tokens")
                    duration = now - started
                    if (
                        first_output is not None
                        and isinstance(tokens, (int, float))
                        and not isinstance(tokens, bool)
                        and math.isfinite(tokens)
                        and tokens > 0
                        and duration > 0
                    ):
                        metrics = {
                            "Speed [tok/s]": round(tokens / duration, 2),
                            "First output [s]": round(first_output - started, 3),
                            "Total time [s]": round(duration, 3),
                        }
                        try:
                            for key, value in metrics.items():
                                if isinstance(usage, dict):
                                    usage[key] = value
                                else:
                                    setattr(usage, key, value)
                        except (AttributeError, TypeError, ValueError):
                            # A future SDK may supply a strict/read-only usage
                            # object. Preserve the response rather than failing it.
                            logging.getLogger(__name__).warning(
                                "InferenceMetrics: usage does not accept metrics"
                            )
                yield chunk
        finally:
            # Close the upstream iterator on disconnect/error as well as success.
            close = getattr(response, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    # Optional telemetry must not replace a provider failure or
                    # turn a completed response into an error. No payloads logged.
                    logging.getLogger(__name__).warning(
                        "InferenceMetrics: stream cleanup failed"
                    )


# LiteLLM loads this module twice: the worker startup hook uses
# importlib.import_module, while config callback loading uses
# spec_from_file_location/exec_module (proxy/types_utils/utils.py
# get_instance_fn), which creates a second module object without inserting it
# into sys.modules. A plain module-level LlamaCppIdleManager() would therefore
# produce two managers, two watchers, and two activity clocks. Anchor the
# shared proxy handler in a private process-wide registry so both module
# objects resolve to one instance. Constructing LlamaCppIdleManager() directly
# still creates a fresh manager for tests.
_SHARED_HANDLER_KEY = "_litellm_custom_callbacks_process_singleton"


def _shared_handler_registry():
    registry = sys.modules.get(_SHARED_HANDLER_KEY)
    if registry is not None:
        return registry
    registry = types.ModuleType(_SHARED_HANDLER_KEY)
    registry.lock = threading.Lock()
    registry.proxy_handler_instance = None
    # dict.setdefault is atomic under the GIL, so concurrent first imports
    # converge on one registry (and therefore one lock) instead of creating
    # competing registries.
    return sys.modules.setdefault(_SHARED_HANDLER_KEY, registry)


def _shared_proxy_handler_instance():
    registry = _shared_handler_registry()
    with registry.lock:
        instance = registry.proxy_handler_instance
        if instance is None:
            instance = LlamaCppIdleManager()
            registry.proxy_handler_instance = instance
        return instance


proxy_handler_instance = _shared_proxy_handler_instance()
opencode_go_session_header = OpenCodeGoSessionHeader()
voxtral_voice_alias = VoxtralVoiceAlias()
voxtral_stt_context_bias = VoxtralSttContextBias()
inference_metrics = InferenceMetrics()


def start_idle_watcher():
    """LiteLLM worker startup hook: start the idle watcher without raising.

    Register with::

        LITELLM_WORKER_STARTUP_HOOKS=custom_callbacks:start_idle_watcher

    LiteLLM calls this before loading the config and re-raises hook failures,
    so every exception here is logged and swallowed. The watcher initializes
    its idle clock on start, which also recovers containers left running by a
    previous worker process.
    """
    try:
        proxy_handler_instance._ensure_watcher()
    except Exception as exc:
        proxy_handler_instance._log(f"startup hook failed: {exc!r}")
