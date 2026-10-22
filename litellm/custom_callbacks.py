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


proxy_handler_instance = LlamaCppIdleManager()
opencode_go_session_header = OpenCodeGoSessionHeader()
nemotron_bolt_thinking = NemotronBoltThinking()
voxtral_voice_alias = VoxtralVoiceAlias()
voxtral_stt_context_bias = VoxtralSttContextBias()
