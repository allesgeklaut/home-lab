#!/usr/bin/env python3
"""Engram hook dispatcher for the Vibe `engram` plugin.

Reads the Vibe hook payload JSON on stdin, dispatches on `hook_event_name`,
and talks to the local Engram HTTP server (default http://127.0.0.1:7437 —
the same store the `engram mcp` stdio server uses; cloud sync is engram's own
autosync and is not this script's concern).

Ported from the OpenCode adapter (~/.config/opencode/plugins/engram.ts):
  session_start -> start `engram serve` if the local API is down (mirrors the
                   OpenCode adapter), POST /sessions, `engram sync --import` for
                   .engram repos, inject memory protocol + /context/compaction as
                   additional_context
  session_end   -> POST /sessions/{id}/end (best effort, short timeout)
  pre_agent     -> inject protocol once per session, POST /prompts for the
                   user prompt, 15-minute save nudge
  pre_tool      -> bind the server-confirmed session (the "<id>:resume:N" child
                   after a resume) into engram write tools
  post_tool     -> mem_save: judge nudge via additional_context
                   agents_spawn: POST /observations/passive

Fail-open everywhere: any error exits 0 with no output (pass through), so a
broken hook never blocks the agent.
"""

import datetime
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

# An explicit ENGRAM_URL means someone else owns the server: never spawn one.
CONFIGURED_ENGRAM_URL = (os.environ.get("ENGRAM_URL") or "").strip()
ENGRAM_PORT = (os.environ.get("ENGRAM_PORT") or "").strip() or "7437"
ENGRAM_URL = (CONFIGURED_ENGRAM_URL or "http://127.0.0.1:%s" % ENGRAM_PORT).rstrip("/")
ENGRAM_BIN = (os.environ.get("ENGRAM_BIN") or "").strip() \
    or shutil.which("engram") or os.path.expanduser("~/.local/bin/engram")
PLUGIN_DATA = os.environ.get("PLUGIN_DATA") or "/tmp/engram-plugin-data"

# Per-event HTTP timeouts; session_end must finish inside Vibe's ~2 s quit
# budget for session_end hooks.
HTTP_TIMEOUT = {
    # 3 serial calls (project, register, compaction context) x 4 s = 12 s. The
    # ensure_server bootstrap adds up to ~5 s worst case (1.5 s instance-id,
    # 0.6 s health, 1.5 s poll, 0.6 s final probe), so a pathologically slow
    # local API can exceed the 15 s hook timeout — Vibe then kills the hook and
    # the event is skipped, which is the same fail-open mode as no daemon.
    "session_start": 4.0,
    "session_end": 2.0,
    "pre_agent": 3.0,
    "pre_tool": 3.0,
    "post_tool": 3.0,
}

NUDGE_COOLDOWN_SECS = int(os.environ.get("ENGRAM_NUDGE_COOLDOWN_SECS", "900"))
NUDGE_MIN_SESSION_AGE_SECS = 300
NUDGE_MIN_LAST_OBS_AGE_SECS = 900

# Tools that accept an authoritative session binding (mem_session_end takes
# `id` instead of `session_id`).
SESSION_ID_TOOLS = {
    "mem_save", "mem_save_prompt", "mem_session_summary", "mem_capture_passive",
    "mem_judge", "mem_judge_many", "mem_session_finalize",
}
SESSION_END_TOOL = "mem_session_end"

MEMORY_PROTOCOL = """# Engram Persistent Memory — Protocol (injected by the engram plugin)

You have Engram persistent memory via the `mcp_engram.*` tools. The plugin binds
your session_id automatically — never pass `session_id` yourself. Follow this
protocol without waiting to be asked.

## Proactive save (REQUIRED — call mcp_engram.mem_save immediately after any of these)
- Architecture/design decision, convention, workflow or tool/library choice with tradeoffs
- Bug fix completed (include root cause) or feature with a non-obvious approach
- Configuration/environment change, file structure change
- Non-obvious discovery, gotcha, edge case, pattern established
- User confirms/rejects a recommendation, expresses a preference, or picks a direction
- After EVERY task ask yourself: "did we just decide, learn, fix, or establish something?" — if yes, save NOW.

mem_save format: title = short searchable; type = bugfix|decision|architecture|discovery|pattern|config|preference;
scope = project (default)|personal|global; reuse topic_key for evolving topics (upsert).
content: **What** / **Why** / **Where** (files) / **Learned** (gotchas).

## Judging conflicts (REQUIRED)
When mem_save returns judgment_required with candidates[], call mcp_engram.mem_judge_many with
one judgments[] entry per candidate (relation: related|compatible|scoped|conflicts_with|supersedes|not_conflict).
Ask the user first when confidence < 0.7, or when relation is supersedes/conflicts_with on an
architecture/policy/decision memory. Otherwise resolve silently. Do not retry mem_save to dodge judging.

## Recalling memory
On "remember/recall/how did we..." or when starting work possibly done before:
mcp_engram.mem_context first, then mcp_engram.mem_search, then mcp_engram.mem_get_observation for full text.

## Session close (REQUIRED)
Before the session ends or when significant work concludes, call mcp_engram.mem_session_summary with:
## Goal / ## Instructions / ## Discoveries / ## Accomplished / ## Next Steps / ## Relevant Files.
The plugin closes the Engram session automatically at exit.

## After compaction / context reset
If you see a compaction notice, FIRST call mcp_engram.mem_session_summary with the compacted
summary content, then continue. Without this, pre-compaction work is lost from memory.

Memory work is internal bookkeeping: finish it before the final user-facing answer of the turn."""

MEMORY_REMINDER = (
    "\n\nMEMORY REMINDER: it's been a while since the last mem_save. If you've made decisions, "
    "discoveries, completed significant work, or found non-obvious things, call mcp_engram.mem_save now."
)

JUDGE_NUDGE = (
    "\n\nEngram: mem_save returned judgment_required with conflict candidates. "
    "Call mcp_engram.mem_judge_many NOW with one entry per candidate (judgment_id + relation). "
    "Ask the user first if confidence < 0.7 or the relation is supersedes/conflicts_with on an "
    "architecture/policy/decision memory."
)


def http(method, path, body=None, timeout=4.0):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        ENGRAM_URL + path, data=data, method=method,
        headers={"Content-Type": "application/json"} if data else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as res:
        text = res.read().decode()
    return json.loads(text) if text.strip() else {}


def local_instance_id():
    """Store identity, so another store's daemon is never mistaken for ours."""
    try:
        out = subprocess.run([ENGRAM_BIN, "instance-id"], capture_output=True,
                             text=True, timeout=1.5)
        value = (out.stdout or "").strip()
        return value if re.fullmatch(r"[a-f0-9]{32}", value) else ""
    except Exception:
        return ""


def server_responding(expected_id="", timeout=0.6):
    try:
        data = http("GET", "/health", timeout=timeout)
    except Exception:
        return False
    return not expected_id or data.get("instance_id") == expected_id


def ensure_server(deadline_secs=1.5):
    """Start `engram serve` when nothing is answering on the local API yet.

    Ported from the OpenCode adapter (~/.config/opencode/plugins/engram.ts):
    that daemon owns this HTTP API *and* cloud autosync, so a machine where no
    long-running agent ever converged — or a fresh boot — has neither. Spawned
    detached with ignored stdio so it outlives this one-shot hook, inheriting
    the hook env (ENGRAM_CLOUD_TOKEN for autosync). ENGRAM_CLOUD_AUTOSYNC is set
    to 1 when unset: the daemon is what runs autosync, and that is the role this
    hook starts it in.
    """
    if CONFIGURED_ENGRAM_URL:
        return True
    expected = local_instance_id()
    if server_responding(expected):
        return True
    env = dict(os.environ)
    env.setdefault("ENGRAM_CLOUD_AUTOSYNC", "1")
    try:
        subprocess.Popen(
            [ENGRAM_BIN, "serve"], cwd=os.path.expanduser("~"),
            stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, start_new_session=True, env=env,
        )
    except Exception:
        return False
    deadline = time.time() + deadline_secs
    while time.time() < deadline:
        time.sleep(0.15)
        if server_responding(expected):
            return True
    return server_responding(expected)


def strip_private(text):
    return re.sub(r"<private>[\s\S]*?</private>", "[REDACTED]", text or "", flags=re.I).strip()


def truncate(text, limit):
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "..."


def to_epoch_secs(ts):
    """SQLite datetime('now') is 'YYYY-MM-DD HH:MM:SS' UTC with no zone suffix."""
    if not ts:
        return None
    normalized = ts.replace(" ", "T")
    if not re.search(r"(?:Z|[+-]\d{2}:?\d{2})$", normalized, re.I):
        normalized += "Z"
    try:
        return int(datetime.datetime.fromisoformat(normalized).timestamp())
    except ValueError:
        return None


def state_path(session_id):
    return os.path.join(PLUGIN_DATA, "state-%s.json" % re.sub(r"[^A-Za-z0-9_.-]", "_", session_id))


def load_state(session_id):
    try:
        with open(state_path(session_id)) as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(session_id, state):
    try:
        os.makedirs(PLUGIN_DATA, exist_ok=True)
        with open(state_path(session_id), "w") as f:
            json.dump(state, f)
    except Exception:
        pass


def resolve_project(cwd, timeout):
    try:
        data = http("GET", "/project/current?cwd=" + urllib.parse.quote(cwd, safe=""), timeout=timeout)
        project = data.get("project")
        if isinstance(project, str) and project.strip() and project != "unknown" \
                and "/" not in project and "\\" not in project:
            return project.strip()
    except Exception:
        pass
    return None


def register_session(session_id, cwd, timeout):
    """POST /sessions (resume) and accept only an identity the server confirmed.

    Mirrors the official adapter: the acknowledgement must be status=created and
    the returned id must be this session or a resume child of it (`<id>:resume:N`).
    A server-chosen identity is never guessed.
    """
    project = resolve_project(cwd, timeout)
    if not project:
        return None, None
    try:
        result = http("POST", "/sessions", body={
            "id": session_id,
            "project": project,
            "directory": cwd,
            "resume": True,
        }, timeout=timeout)
        new_id = result.get("id")
        if result.get("status") == "created" and isinstance(new_id, str) \
                and (new_id == session_id or new_id.startswith(session_id + ":resume:")):
            return project, new_id
        return project, None
    except Exception:
        return project, None


def maybe_import_chunks(cwd):
    if os.path.isfile(os.path.join(cwd, ".engram", "manifest.json")):
        try:
            subprocess.Popen(
                [ENGRAM_BIN, "sync", "--import"], cwd=cwd,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
        except Exception:
            pass


def on_session_start(payload):
    session_id = payload.get("session_id") or ""
    cwd = payload.get("cwd") or os.getcwd()
    timeout = HTTP_TIMEOUT["session_start"]

    server_up = ensure_server()
    project, effective_id = register_session(session_id, cwd, timeout) if server_up else (None, None)
    state = load_state(session_id)
    if project:
        state["project"] = project
        state["registered"] = bool(effective_id)
        if effective_id and effective_id != session_id:
            state["effective_id"] = effective_id
    # session_start already injects MEMORY_PROTOCOL below; pre_agent must not
    # inject it again on turn 1.
    state["protocol_injected"] = True
    save_state(session_id, state)
    maybe_import_chunks(cwd)

    parts = [MEMORY_PROTOCOL]
    if project and state.get("registered"):
        try:
            data = http("GET", "/context/compaction?session_id="
                        + urllib.parse.quote(state.get("effective_id", session_id), safe=""), timeout=timeout)
            if data.get("context"):
                parts.append("# Engram context from previous sessions\n" + data["context"])
        except Exception:
            pass

    if state.get("registered"):
        message = "engram: session registered (project %s)" % project
    elif not server_up:
        message = ("engram: local server unavailable (engram serve not ready) — "
                   "letting Engram resolve the session itself")
    else:
        message = "engram: session NOT registered (%s) — memory writes may fail closed" % (project or "project unresolved")
    print(json.dumps({
        "hook_specific_output": {"additional_context": "\n\n".join(parts)},
        "system_message": message,
    }))


def on_session_end(payload):
    session_id = payload.get("session_id") or ""
    if not session_id:
        return
    state = load_state(session_id)
    effective_id = state.get("effective_id", session_id)
    try:
        http("POST", "/sessions/%s/end" % urllib.parse.quote(effective_id, safe=""),
             body={}, timeout=HTTP_TIMEOUT["session_end"])
    except Exception:
        pass  # best effort; resume re-registers


def on_pre_agent(payload):
    session_id = payload.get("session_id") or ""
    prompt = payload.get("prompt") or ""
    state = load_state(session_id)
    timeout = HTTP_TIMEOUT["pre_agent"]
    additions = []

    if not state.get("protocol_injected"):
        additions.append(MEMORY_PROTOCOL)
        state["protocol_injected"] = True

    # Capture the user prompt (mirrors the OpenCode chat.message hook).
    if state.get("registered") and len(prompt.strip()) > 10:
        try:
            http("POST", "/prompts", body={
                "session_id": state.get("effective_id", session_id),
                "content": truncate(strip_private(prompt), 2000),
                "project": state.get("project"),
            }, timeout=timeout)
        except Exception:
            pass

    # Save nudge: session older than 5 min, no observation in the last 15 min,
    # not nudged in the last 15 min.
    now = int(time.time())
    if state.get("registered") and now - state.get("last_nudge", 0) >= NUDGE_COOLDOWN_SECS:
        try:
            sess = http("GET", "/sessions/%s" % urllib.parse.quote(state.get("effective_id", session_id), safe=""), timeout=timeout)
            started = to_epoch_secs(sess.get("started_at") or "")
            if started and now - started >= NUDGE_MIN_SESSION_AGE_SECS:
                obs = http("GET", "/observations?project=%s&limit=1&sort=created_at:desc"
                           % urllib.parse.quote(state.get("project") or "", safe=""), timeout=timeout)
                last_obs = None
                if isinstance(obs, list) and obs and isinstance(obs[0], dict):
                    last_obs = to_epoch_secs(obs[0].get("created_at") or "")
                if last_obs is None or now - last_obs >= NUDGE_MIN_LAST_OBS_AGE_SECS:
                    additions.append(MEMORY_REMINDER)
                    state["last_nudge"] = now
        except Exception:
            pass

    save_state(session_id, state)
    if additions:
        print(json.dumps({"hook_specific_output": {"additional_context": "\n\n".join(additions)}}))


def tool_names(tool_name):
    """All spellings a tool can arrive under in a hook payload.

    Vibe sends the qualified name (e.g. 'mcp_engram.mem_save',
    'agents.spawn'); the matcher also accepts the bare and underscore forms,
    so accept all three.
    """
    tool_name = tool_name or ""
    short = tool_name.rsplit(".", 1)[-1] if "." in tool_name else tool_name
    return {tool_name, short, tool_name.replace(".", "_")}


def on_pre_tool(payload):
    names = tool_names(payload.get("tool_name"))
    if not (names & SESSION_ID_TOOLS) and SESSION_END_TOOL not in names:
        return
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return
    # Subagent tool calls carry parent_session_id: attribute to the main session.
    session_id = payload.get("parent_session_id") or payload.get("session_id") or ""
    if not session_id:
        return
    # Bind only sessions this hook actually registered. An id the store never
    # saw makes Engram reject the write outright (unknown_session); the official
    # adapter throws in that case, this hook fails open and binds nothing.
    state = load_state(session_id)
    if not state.get("registered"):
        return
    effective_id = state.get("effective_id") or session_id
    if SESSION_END_TOOL not in names:
        # Renew before attributed writes, like the official adapter's
        # ensureSession(id, renew=true): resuming again is idempotent and keeps
        # the id current after a daemon restart or a resume that closed the old
        # run. Not for the end tool: renewing after a child is ended would mint a
        # fresh empty one, and the official adapter renews only for its four
        # attributed write tools.
        project, renewed = register_session(
            session_id, payload.get("cwd") or os.getcwd(), HTTP_TIMEOUT["pre_tool"])
        if renewed:
            if project:
                state["project"] = project
            if renewed != session_id:
                state["effective_id"] = renewed
            else:
                state.pop("effective_id", None)
            save_state(session_id, state)
            effective_id = state.get("effective_id") or session_id
    new_input = dict(tool_input)
    if SESSION_END_TOOL in names:
        new_input["id"] = effective_id
    else:
        new_input["session_id"] = effective_id
    print(json.dumps({"hook_specific_output": {"tool_input": new_input}}))


def on_post_tool(payload):
    names = tool_names(payload.get("tool_name"))
    output_text = payload.get("tool_output_text") or ""
    timeout = HTTP_TIMEOUT["post_tool"]

    if "mem_save" in names:
        if "judgment_required" in output_text and "candidates" in output_text:
            print(json.dumps({"hook_specific_output": {"additional_context": JUDGE_NUDGE}}))
        return

    if "agents_spawn" in names:
        session_id = payload.get("parent_session_id") or payload.get("session_id") or ""
        cwd = payload.get("cwd") or os.getcwd()
        if not session_id or len(output_text) <= 50:
            return
        state = load_state(session_id)
        if not state.get("registered"):
            return
        try:
            http("POST", "/observations/passive", body={
                "session_id": state.get("effective_id", session_id),
                "content": strip_private(output_text),
                "project": state.get("project"),
                "source": "subagent-stop",
            }, timeout=timeout)
        except Exception:
            pass


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return  # fail open
    event = payload.get("hook_event_name")
    try:
        if event == "session_start":
            on_session_start(payload)
        elif event == "session_end":
            on_session_end(payload)
        elif event == "pre_agent":
            on_pre_agent(payload)
        elif event == "pre_tool":
            on_pre_tool(payload)
        elif event == "post_tool":
            on_post_tool(payload)
    except Exception:
        pass  # fail open: never block the agent because of a hook bug


if __name__ == "__main__":
    main()
