"""
LiteLLM custom callbacks.

1. LlamaCppIdleManager: mutual-exclusion start + idle-stop for llama.cpp.
2. OpenCodeGoSessionHeader: adds the mandatory x-opencode-session header and a
   stable identifying User-Agent for OpenCode Go models (the Go gateway
   rejects requests without the session id).
3. NemotronBoltThinking: maps the shared reasoning_effort Thinking control onto
   the Nemotron chat-template kwargs (enable_thinking / medium_effort).
4. VoxtralVoiceAlias: readable voice names + language-aware "auto" voice for
   Voxtral TTS (Mistral only accepts voice UUIDs / preset ids, not the names
   shown in Mistral Studio).

Only one llama.cpp container can hold the GPU at a time. Each user-facing
model is mapped to a container via the MODEL_CONTAINERS env var (JSON):

    MODEL_CONTAINERS={"qwen3.8-27b":{"container":"llama-server","health":"http://127.0.0.1:8084/health"},"qwen3.8-27b-abliterated":{"container":"llama-companion","health":"http://127.0.0.1:8086/health"}}

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
                      x-opencode-session header and a fixed User-Agent
                      for these

The Portainer start/stop/health helpers live in gpu_containers.py, shared with
the ComfyUI lifecycle proxy so both managers behave identically.
"""

import asyncio
import hashlib
import json
import os
import re
import time
import uuid

from litellm.integrations.custom_logger import CustomLogger
from fastapi import HTTPException

from gpu_containers import PortainerContainers


class LlamaCppIdleManager(CustomLogger):
    def __init__(self):
        super().__init__()
        # per-container last request time
        self.last_request_time = {}
        # per-container set of in-flight litellm_call_ids; membership is
        # request-scoped so releases are idempotent
        self.in_flight = {}
        self.start_lock = asyncio.Lock()
        self._watcher_started = False
        self._watcher_task = None

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
        call_id = data.get("litellm_call_id") or data.setdefault(
            "_idle_token", uuid.uuid4().hex
        )
        tracked = self.in_flight.setdefault(container, set())
        tracked.add(call_id)
        try:
            if not self._watcher_started:
                self._watcher_started = True
                self._watcher_task = asyncio.create_task(self._idle_loop())

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
        except Exception:
            tracked.discard(call_id)
            raise

    async def async_log_success_event(self, kwargs, response_obj, start_time, end_time):
        self._touch(kwargs)
        self._release_in_flight(kwargs)

    async def async_log_failure_event(self, kwargs, response_obj, start_time, end_time):
        self._touch(kwargs)
        self._release_in_flight(kwargs)

    def _release_in_flight(self, kwargs):
        cfg = self._resolve((kwargs or {}).get("model"))
        if not cfg:
            return
        call_id = (kwargs or {}).get("litellm_call_id") or (kwargs or {}).get(
            "_idle_token"
        )
        if call_id:
            self.in_flight.get(cfg["container"], set()).discard(call_id)

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
                    # A leaked id can only keep a container on, never stop a live one.
                    if self.in_flight.get(container):
                        print(
                            f"[idle-manager] {container} still serving an in-flight "
                            "request, delaying idle stop"
                        )
                        continue
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


class NemotronBoltThinking(CustomLogger):
    """Translate reasoning_effort into Nemotron chat-template kwargs.

    llama-bolt's chat template has no reasoning_effort variable (unlike the
    Qwen3.8 template): thinking is switched by the chat_template_kwargs
    enable_thinking / medium_effort (NVIDIA model card). Map the shared
    "Thinking" control onto them, three tiers matching the template's states:

        minimal | low | none | off  -> enable_thinking = False   (no thinking)
        medium                      -> enable_thinking = True, medium_effort = True
        high | xhigh                -> enable_thinking = True    (full effort)

    The kwargs are merged with any client-provided chat_template_kwargs, and
    explicit client kwargs win over the control mapping. litellm passes
    chat_template_kwargs through because it is allowlisted for this model in
    config.yaml.
    """

    MODELS = {"nemotron-bolt", "openai/nemotron-bolt"}

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        if data.get("model") not in self.MODELS:
            return data
        effort = data.get("reasoning_effort")
        if not isinstance(effort, str):
            return data
        if effort in ("minimal", "low", "none", "off"):
            mapped = {"enable_thinking": False}
        elif effort == "medium":
            mapped = {"enable_thinking": True, "medium_effort": True}
        elif effort in ("high", "xhigh"):
            mapped = {"enable_thinking": True, "medium_effort": False}
        else:
            return data
        existing = data.get("chat_template_kwargs")
        if not isinstance(existing, dict):
            existing = {}
        for key, value in mapped.items():
            existing.setdefault(key, value)
        data["chat_template_kwargs"] = existing
        return data


class VoxtralVoiceAlias(CustomLogger):
    """Readable voice names + language-aware "auto" voice for Voxtral TTS.

    Mistral's TTS API accepts only saved-voice UUIDs (GET /v1/audio/voices)
    and internal preset ids (gb_jane_sarcasm, ...) - the human-readable names
    shown in Mistral Studio ("Jo", "Jane") return 404 invalid_voice. The
    OpenWebUI TTS voice field is free text passed straight through, so this
    hook rewrites friendly names to the UUIDs before the request is routed.

    Accepted voice values on the voxtral-tts model (case-insensitive):

      - "jo" / "de": Jo, the only native German preset voice (male).
      - "jane" / "en": Jane - Neutral (en-GB, female).
      - "<person>-<mood>": any preset mood, e.g. "jane-sarcasm",
        "paul-cheerful", "oliver-curious", "marie-happy".
      - "auto": detect the language of the text being spoken - German
        -> Jo, anything else -> Jane - Neutral. OpenWebUI splits long
        replies on punctuation and synthesizes chunk by chunk, so
        detection runs per chunk: mixed-language answers use the right
        voice per sentence. Ambiguous short chunks ("1234") default to
        Jo (the instance's primary language is German).
      - UUIDs pass through untouched (custom/cloned voices from Mistral
        Studio keep working), as do litellm's OpenAI aliases (alloy, nova,
        ...), which litellm maps to English presets on its own.
    """

    TTS_MODEL = "voxtral-tts"

    # Saved-voice UUIDs from Mistral Studio (listed via /v1/audio/voices).
    JO = "01a1469d-0764-746e-80af-089a6ca3bddc"  # de, male - only German preset
    JANE_NEUTRAL = "82c99ee6-f932-423f-a4a3-d403c8914b8d"  # en-GB, female
    JANE_SARCASM = "a3e41ea8-020b-44c0-8d8b-f6cc03524e31"
    JANE_CONFUSED = "7d0a90a3-c211-4489-aaa0-61269299edc7"
    JANE_CURIOUS = "5de47977-6e47-4266-a938-3bc1d76b4676"
    JANE_SAD = "c7a8eb83-5247-4540-89f3-6650d349100d"
    JANE_FRUSTRATED = "60844938-221d-4d1e-8233-34203f787d9f"
    JANE_SHAMEFUL = "230ccacf-8800-4aa0-8ac2-8d004f1d9fb7"
    JANE_JEALOUSY = "e7168caa-f7ed-4e1c-98a1-434251f4f2b0"
    PAUL_NEUTRAL = "c69964a6-ab8b-4f8a-9465-ec0925096ec8"  # en-US, male
    PAUL_SAD = "530e2e20-58e2-45d8-b0a5-4594f4915944"
    PAUL_HAPPY = "1024d823-a11e-43ee-bf3d-d440dccc0577"
    PAUL_FRUSTRATED = "1f017bcb-02e5-460d-989b-db065c0c6122"
    PAUL_EXCITED = "5940190b-f58a-4c3e-8264-a40d63fd6883"
    PAUL_CONFIDENT = "98559b22-62b5-4a64-a7cd-fc78ca41faa8"
    PAUL_CHEERFUL = "01d985cd-5e0c-4457-bfd8-80ba31a5bc03"
    PAUL_ANGRY = "cb891218-482c-4392-9878-91e8d999d57a"
    OLIVER_NEUTRAL = "e3596645-b1af-469e-b857-f18ddedc7652"  # en-GB, male
    OLIVER_SAD = "d4101b8f-12c3-450d-a812-7d700b3a3245"
    OLIVER_EXCITED = "e8e5b1de-493c-4061-8414-e2170f9f4b6f"
    OLIVER_CURIOUS = "390c8a2b-60a6-4882-8437-c49a8bd33b63"
    OLIVER_CONFIDENT = "8169ab87-bc99-4669-a5ec-6855860ace24"
    OLIVER_CHEERFUL = "5ad5d44e-6b4e-4a57-a8a8-4cae088034ed"
    OLIVER_ANGRY = "862274a7-8333-48f7-b668-f19c932999e0"
    MARIE_NEUTRAL = "5a271406-039d-46fe-835b-fbbb00eaf08d"  # fr-FR, female
    MARIE_HAPPY = "49d024dd-981b-4462-bb17-74d381eb8fd7"
    MARIE_SAD = "4adeb2c6-25a3-44bc-8100-5234dfc1193b"
    MARIE_EXCITED = "2f62b1af-aea3-4079-9d10-7ca665ee7243"
    MARIE_CURIOUS = "e0580ce5-e63c-4cbe-88c8-a983b80c5f1f"
    MARIE_ANGRY = "a7c07cdc-1c35-4d87-a938-c610a654f600"

    ALIASES = {
        "jo": JO,
        "de": JO,
        "jane": JANE_NEUTRAL,
        "en": JANE_NEUTRAL,
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
            return self.JO
        words = re.findall(r"[a-z]+", t.lower())
        de = sum(w in self._DE_WORDS for w in words)
        en = sum(w in self._EN_WORDS for w in words)
        # Ties and no-signal chunks default to Jo (German is the primary
        # language of this instance).
        return self.JANE_NEUTRAL if en > de else self.JO


proxy_handler_instance = LlamaCppIdleManager()
opencode_go_session_header = OpenCodeGoSessionHeader()
nemotron_bolt_thinking = NemotronBoltThinking()
voxtral_voice_alias = VoxtralVoiceAlias()
