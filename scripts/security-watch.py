#!/usr/bin/python3 -S
"""Read-only inventory -> advisory checks -> Mistral briefing -> local report/ntfy.

Standard library only. No model output is ever executed. --inventory collects
locally and over configured SSH connections without LLM/API calls;
--setup-agent creates/reuses only this dedicated agent; --notify is
explicit. See the Security watch section of the repository README.
"""

import argparse
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
from http.client import HTTPException
import ipaddress
import inspect
import json
import os
from pathlib import Path
import re
import shlex
import signal
import subprocess
import sys
import tempfile
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


ROOT = Path(__file__).resolve().parent
MISTRAL = "https://api.mistral.ai/v1"
OSV = "https://api.osv.dev/v1/querybatch"
KEV = "https://www.cisa.gov/sites/default/files/feeds/known_exploited_vulnerabilities.json"
AGENT_NAME = "Homelab Security Watch"
VERSION = re.compile(r"v?([0-9][A-Za-z0-9_.+~:!-]{0,100})\Z")
PYTHON_PACKAGES = (
    "open-webui", "mcpo", "open-terminal", "litellm", "cryptography",
    "fastapi", "starlette", "mcp", "fastmcp", "httpx", "aiohttp",
    "brotli", "brotlicffi",
)
BINARY_PROBES = {
    "paperless-ngx": ["python", "-S", "-c", "import runpy; print(runpy.run_path('/usr/src/paperless/src/paperless/version.py')['__full_version_str__'])"],
    "postgres": ["postgres", "--version"],
    "redis": ["redis-server", "--version"],
    "valkey": ["valkey-server", "--version"],
    "cloudflared": ["cloudflared", "--version"],
    "adguardhome": ["/opt/adguardhome/AdGuardHome", "--version"],
}
DEPENDABOT_REFSPEC = "+refs/heads/dependabot/*:refs/remotes/origin/dependabot/*"
REPOSITORY_ADVISORIES = {
    "paperless-webserver-1": {
        "repository": "paperless-ngx/paperless-ngx",
        "image": "ghcr.io/paperless-ngx/paperless-ngx",
        # The official c8f3 advisory misspells its own package name.
        "package_names": {"paperless-ngx", "papaerless-ngx"},
    },
}
# Exact incident-specific sources only, never generic security articles or titles.
INCIDENT_SOURCES = {
    "litellm": {
        "https://docs.litellm.ai/blog/security-incident-oct-21-2026": "INCIDENT-LITELLM-2026-10-21",
        "https://securitylabs.datadoghq.com/articles/litellm-compromised-pypi-docker/": "INCIDENT-LITELLM-2026-10-21",
    },
}
PROBE_PYTHON = """
import importlib.metadata as m, json, sys
result = {}
for name in sys.argv[1:]:
    try: result[name] = m.version(name)
    except m.PackageNotFoundError: pass
print(json.dumps(result))
"""
PROBE_NPM = """
import glob, json, re
result = set()
for path in glob.glob('/proc/[0-9]*/cmdline'):
    try:
        command = open(path, 'rb').read().replace(b'\\0', b' ').decode()
    except (OSError, UnicodeError): continue
    if command.startswith('npm exec '):
        result.update(re.findall(r'([@a-zA-Z0-9_/.-]+)@(\\d+\\.\\d+\\.\\d+(?:[-+][a-zA-Z0-9.-]+)?)', command))
print(json.dumps(sorted(result)))
"""
FINDING_STRINGS = (
    "component", "advisory", "title", "installed_version", "affected_versions",
    "fixed_version", "reason", "action",
)
ENUMS = {
    "priority": ["act_now", "update_soon", "monitor"],
    "status": ["confirmed", "possible", "unknown", "not_affected"],
    "exploited": ["yes", "no", "unknown"],
    "confidence": ["high", "medium", "low"],
}
FINDING_PROPERTIES = {key: {"type": "string"} for key in FINDING_STRINGS}
FINDING_PROPERTIES.update({key: {"type": "string", "enum": values} for key, values in ENUMS.items()})
FINDING_PROPERTIES["sources"] = {
    "type": "array", "items": {"type": "string"},
}
REPORT_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["summary", "checked_components", "limitations", "findings"],
    "properties": {
        "summary": {"type": "string"},
        "checked_components": {"type": "array", "items": {"type": "string"}},
        "limitations": {"type": "array", "items": {"type": "string"}},
        "findings": {
            "type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": FINDING_PROPERTIES,
                "required": list(FINDING_PROPERTIES),
            },
        },
    },
}
SOURCE_LIMIT = 8
DIAGNOSTIC_URL_LIMIT = 500
COVERAGE_ISSUE_LIMIT = 200
BATCH_ENDPOINT = "/v1/conversations"
BATCH_TIMEOUT_HOURS = 24
BATCH_MAX_AGE_HOURS = 48
BATCH_CONTROL_TIMEOUT = 60
BATCH_DOWNLOAD_TIMEOUT = 120
BATCH_OUTPUT_LIMIT = 4_000_000
BATCH_INPUT_BUDGET = 180_000
BATCH_SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,100}\Z")
BATCH_CUSTOM_ID = re.compile(r"[A-Za-z0-9_-]{1,64}\Z")
BATCH_PENDING_STATES = frozenset({"submitting", "submitted", "unknown", "finalizing", "invalid"})
BATCH_RESOLVED_STATES = frozenset({"rejected", "failed", "stale", "collected"})
BATCH_STATUSES = frozenset({
    "QUEUED", "RUNNING", "SUCCESS", "FAILED", "TIMEOUT_EXCEEDED",
    "CANCELLATION_REQUESTED", "CANCELLED",
})
BATCH_PENDING_STATUSES = frozenset({"QUEUED", "RUNNING", "CANCELLATION_REQUESTED"})
BATCH_FAILED_STATUSES = frozenset({"FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"})
# Provider-side cleanup is manual only. The documented operations are
# DELETE /v1/files/{file_id} and DELETE /v1/batch/jobs/{job_id}; this script
# never deletes provider data. Provider retention still applies to archived
# local references, so review cleanup explicitly after the trial.
GITHUB_ADVISORY_URL = re.compile(
    r"https://github\.com/advisories/(GHSA-[0-9A-Za-z]{4}-[0-9A-Za-z]{4}-[0-9A-Za-z]{4})\Z",
    re.IGNORECASE,
)
# KEV product matching is deliberately conservative: exact normalized names or
# significant-token subsets only. Generic packaging/role words never select a
# product by themselves. PostgreSQL/Postgres is the one common spelling alias.
KEV_TOKEN_ALIASES = {"postgresql": "postgres"}
KEV_GENERIC_TOKENS = frozenset({
    "aio", "all", "and", "api", "app", "application", "applications", "base",
    "bin", "built", "ce", "center", "cli", "client", "cloud", "code", "common",
    "community", "components", "core", "daemon", "data", "database", "datacenter",
    "db", "edition", "ee", "enterprise", "for", "gateway", "image", "in", "io",
    "js", "library", "linux", "manager", "management", "multiple", "node",
    "office", "on", "one", "online", "open", "os", "oss", "package", "platform",
    "plug", "plugin", "product", "products", "proxy", "runtime", "secure",
    "security", "server", "service", "services", "software", "source", "suite",
    "system", "systems", "the", "tool", "tools", "ui", "web", "webserver",
    "with",
})


class WatchError(Exception):
    pass


class BatchSubmitRejected(WatchError):
    pass


class BatchSubmitUnknown(WatchError):
    pass


class BatchOutputInvalid(WatchError):
    pass


class BatchDownloadError(WatchError):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward authentication to a redirect target.


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def now_precise():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def canonical_json(data):
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def sha256_text(text):
    return hashlib.sha256(text.encode()).hexdigest()


def version(value):
    match = VERSION.fullmatch(str(value or ""))
    return match.group(1) if match else None


def command(args, timeout=15):
    try:
        return subprocess.run(args, check=True, capture_output=True, text=True, timeout=timeout,
                              env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        raise WatchError(f"Local probe failed: {args[0]}") from None


def read_key(path, name=None):
    try:
        text = Path(path).expanduser().read_text()
    except OSError:
        raise WatchError(f"Cannot read credential file: {path}") from None
    if name is None:
        value = text.strip()
    else:
        value = ""
        for line in text.splitlines():
            key, sep, raw = line.strip().removeprefix("export ").partition("=")
            if sep and key.strip() == name:
                try:
                    words = shlex.split(raw, comments=True)
                except ValueError:
                    raise WatchError(f"Invalid credential entry: {name}") from None
                value = words[0] if len(words) == 1 else ""
    if not value or "\n" in value or "\r" in value:
        raise WatchError("Missing or invalid credential")
    return value


def request_json(url, payload=None, key=None, timeout=30, retry=False, method=None):
    headers = {"Accept": "application/json", "User-Agent": "homelab-security-watch/1"}
    if key:
        headers["Authorization"] = "Bearer " + key
    data = None if payload is None else json.dumps(payload).encode()
    if data is not None:
        headers["Content-Type"] = "application/json"
    for attempt in range(2 if retry else 1):
        try:
            with build_opener(NoRedirect()).open(Request(url, data=data, headers=headers, method=method), timeout=timeout) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise WatchError("API response exceeds size limit")
                return json.loads(raw)
        except HTTPError as error:
            if attempt == 0 and retry and error.code in (429, 500, 502, 503, 504):
                time.sleep(2)
                continue
            raise WatchError(f"{urlsplit(url).hostname}: HTTP {error.code}") from None
        except (URLError, TimeoutError, OSError, HTTPException):
            if attempt == 0 and retry:
                time.sleep(2)
                continue
            raise WatchError(f"{urlsplit(url).hostname}: network failure") from None
        except (ValueError, UnicodeError):
            raise WatchError(f"{urlsplit(url).hostname}: invalid JSON") from None


def atomic_text(path, text):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as output:
            temporary = output.name
            output.write(text)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def atomic_bytes(path, data):
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="wb", dir=path.parent, delete=False) as output:
            temporary = output.name
            output.write(data)
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def atomic_json(path, data):
    atomic_text(path, json.dumps(data, indent=2) + "\n")


def public_url(value):
    if not isinstance(value, str) or len(value) > 2000 or any(c.isspace() for c in value):
        return False
    try:
        parsed = urlsplit(value)
    except ValueError:
        return False
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        return False
    try:
        host = parsed.hostname.lower()
    except ValueError:
        return False
    try:
        ipaddress.ip_address(host)
        return False  # Reports should cite public named advisory sites, not IPs.
    except ValueError:
        return "." in host and not host.endswith((".local", ".internal", ".localhost"))


def public_source_urls(value, limit=SOURCE_LIMIT):
    """Bounded public URLs for rejection diagnostics; never an allowlist."""
    urls = []
    if not isinstance(value, list):
        return urls
    for item in value:
        if (isinstance(item, str) and len(item) <= DIAGNOSTIC_URL_LIMIT
                and public_url(item) and item not in urls):
            urls.append(item)
            if len(urls) >= limit:
                break
    return urls


def github_advisory_identity(url):
    """Exact global GitHub advisory URL only; repository security URLs differ."""
    if not isinstance(url, str):
        return None
    match = GITHUB_ADVISORY_URL.fullmatch(url)
    return match.group(1).upper() if match else None


def citation_alias_map(checks):
    """Scope exact GitHub advisory URLs to current OSV matches only.

    The GitHub page is never fetched or treated as retrieved evidence. The
    mapping merely recognizes the advisory identity already supplied by OSV.
    """
    aliases = {}
    for match in checks.get("osv_matches", []):
        if not isinstance(match, dict):
            continue
        advisory = str(match.get("advisory", ""))
        target = match.get("url")
        if not advisory.upper().startswith("GHSA-") or not public_url(target):
            continue
        key = (match.get("component"), advisory.upper(), match.get("version"), advisory.upper())
        aliases[key] = target
    return aliases


def product_normalized(value):
    text = re.sub(r"[^a-z0-9]+", "", str(value or "").lower())
    return KEV_TOKEN_ALIASES.get(text, text)


def product_tokens(value):
    tokens = set()
    for token in re.findall(r"[a-z0-9]+", str(value or "").lower()):
        token = KEV_TOKEN_ALIASES.get(token, token)
        if len(token) >= 2 and token not in KEV_GENERIC_TOKENS:
            tokens.add(token)
    return tokens


def product_keys(value):
    keys = product_tokens(value)
    normalized = product_normalized(value)
    if normalized and normalized not in KEV_GENERIC_TOKENS:
        keys.add(normalized)
    return keys


def inventory_product_keys(inventory):
    keys = set()

    def add(value):
        keys.update(product_keys(value))

    def add_normalized(value):
        normalized = product_normalized(value)
        if normalized and normalized not in KEV_GENERIC_TOKENS:
            keys.add(normalized)

    for component in inventory.get("components", []):
        image = str(component.get("image", ""))
        reference = image.split("@", 1)[0]
        parts = reference.split("/")
        # Drop a registry host, but keep the repository path as one exact key.
        if len(parts) > 1 and ("." in parts[0] or ":" in parts[0] or parts[0] == "localhost"):
            parts = parts[1:]
        leaf = parts[-1].split(":", 1)[0]
        add(leaf)
        # Keep the full repository path as one exact key. Do not add arbitrary
        # namespace tokens (e.g. "apache" for apache/tika): they over-match
        # unrelated KEV products such as Apache HTTP Server.
        add_normalized("/".join(parts).split(":", 1)[0])
        add(str(component.get("id", "")).split(":")[-1])
    for package in inventory.get("packages", []):
        add(package.get("name", ""))
    for package in inventory.get("host", {}).get("packages", []):
        add(str(package.get("name", "")).split(":")[0])
    for remote in inventory.get("remote_hosts", []):
        for package in remote.get("host", {}).get("packages", []):
            add(str(package.get("name", "")).split(":")[0])
    return keys


def host_has_kernel(inventory):
    hosts = [inventory.get("host", {})] + [remote.get("host", {}) for remote in inventory.get("remote_hosts", [])]
    if any(str(host.get("kernel", "")).strip() not in ("", "unknown") for host in hosts):
        return True
    return any(str(package.get("name", "")).split(":")[0].startswith("linux-image")
               for host in hosts for package in host.get("packages", []))


def host_has_docker_engine(inventory):
    hosts = [inventory.get("host", {})] + [remote.get("host", {}) for remote in inventory.get("remote_hosts", [])]
    for host in hosts:
        for package in host.get("packages", []):
            name = str(package.get("name", "")).split(":")[0]
            if name == "docker-ce":
                return True
    return False


def kev_relevant(entry, inventory_keys, kernel, docker_engine):
    vendor = product_normalized(entry.get("vendorProject"))
    product = product_normalized(entry.get("product"))
    if kernel and vendor == "linux" and product == "kernel":
        return True
    if docker_engine and vendor == "docker" and product == "dockerengine":
        return True
    if product and product in inventory_keys:
        return True
    tokens = product_tokens(entry.get("product"))
    return bool(tokens) and tokens <= inventory_keys


def relevant_kev_candidates(candidates, inventory):
    keys = inventory_product_keys(inventory)
    kernel = host_has_kernel(inventory)
    docker_engine = host_has_docker_engine(inventory)
    relevant = [entry for entry in candidates
                if isinstance(entry, dict) and kev_relevant(entry, keys, kernel, docker_engine)]
    return relevant, len(candidates) - len(relevant)


def dedupe_exact(items):
    """Remove only byte-identical duplicates; never merge differing evidence."""
    seen, result = set(), []
    for item in items:
        key = json.dumps(item, sort_keys=True, separators=(",", ":"))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


def image_reference(raw):
    # Public registry references only; do not disclose private registry domains.
    raw = str(raw)
    if not re.fullmatch(r"[A-Za-z0-9_./:@-]{1,240}", raw):
        return "local-image"
    first = raw.split("/")[0]
    public = {"ghcr.io", "docker.io", "quay.io", "lscr.io", "mcr.microsoft.com", "registry.gitlab.com"}
    if "/" in raw and ("." in first or ":" in first) and first not in public:
        return "private-image"
    if re.fullmatch(r"(?:sha256:)?[0-9a-f]{12,64}", raw):
        return "local-image"
    return raw


def label_matches_image(image, source):
    # Locally built images often inherit Ubuntu/uv's OCI version labels.
    # Only use a label as an application hint if its source matches the image.
    ignored = {"docker", "server", "image", "library", "linuxserver", "local"}
    words = lambda text: {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) >= 4} - ignored
    if not public_url(source or ""):
        return False
    return bool(words(image.split(":")[0]) & words(urlsplit(source).path))


def exposure(ports, network, override=None):
    if override:
        return override
    if network == "host":
        return "host networking; external reachability unknown"
    bindings = [binding for group in (ports or {}).values() if group for binding in group]
    if not bindings:
        return "unpublished; proxy/tunnel access unknown"
    if all(binding.get("HostIp") in ("127.0.0.1", "::1") for binding in bindings):
        return "loopback-published; proxy/tunnel access unknown"
    return "network-published; external reachability unknown"


def collect_local_inventory(config):
    os_info = {}
    for line in Path("/etc/os-release").read_text().splitlines():
        key, sep, value = line.partition("=")
        if sep:
            os_info[key] = value.strip('"')
    inventory = {
        "collected_at": now(),
        "host": {
            "alias": config["host_alias"], "distribution": os_info.get("ID", "unknown"),
            "release": os_info.get("VERSION_ID", "unknown"),
            "base_release": os_info.get("UBUNTU_CODENAME", "unknown"),
            "kernel": command(["uname", "-r"]).strip(), "packages": [],
        },
        "components": [], "packages": [], "package_probes": [],
        "coverage_gaps": [
            "Selected host/Python packages and MCP direct dependencies only; no full image/transitive scan.",
            "Custom application dependencies and host services outside Docker are not fully inventoried.",
            "Host package applicability requires distro advisory research; apt cache is not refreshed by this script.",
        ],
    }
    installed = command(["dpkg-query", "-W", "-f=${binary:Package}\t${Version}\t${db:Status-Status}\n"])
    for row in installed.splitlines():
        name, installed_version, status = row.split("\t")
        name = name.split(":")[0]
        if (name in config["host_packages"] or any(name.startswith(p) for p in config.get("host_package_prefixes", []))) and status == "installed":
            inventory["host"]["packages"].append({"name": row.split("\t")[0], "version": installed_version})
    # InRelease mtime is the upstream publication date, NOT last apt update.
    stamp = Path("/var/lib/apt/periodic/update-success-stamp")
    inventory["host"]["apt_last_successful_refresh"] = (
        datetime.fromtimestamp(stamp.stat().st_mtime, timezone.utc).isoformat() if stamp.exists() else "unknown"
    )
    try:
        updates = command(["apt", "list", "--upgradable"])
        inventory["host"]["pending_updates_from_local_cache"] = [
            {"name": match[1], "candidate": match[2], "installed": match[3]}
            for line in updates.splitlines()
            if (match := re.match(r"^([^/]+)/\S+\s+(\S+)\s+\S+\s+\[upgradable from: ([^\]]+)\]", line))
        ]
    except WatchError:
        inventory["coverage_gaps"].append("Host: pending package updates could not be read.")
    try:
        ids = command(["docker", "ps", "-aq"]).split()
    except WatchError:
        inventory["coverage_gaps"].append("Docker inventory unavailable (not installed or existing user lacks access); no sudo attempted.")
        return inventory
    fmt = ('{"id":{{json .Name}},"image":{{json .Config.Image}},"state":{{json .State.Status}},'
           '"image_id":{{json .Image}},"network":{{json .HostConfig.NetworkMode}},"ports":{{json .NetworkSettings.Ports}}}')
    rows = command(["docker", "inspect", "--format", fmt, *ids]).splitlines() if ids else []
    images = {}
    for row in rows:
        item = json.loads(row)
        name = item["id"].lstrip("/")
        if item["image_id"] not in images:
            try:
                label_fmt = ('{"version":{{json (index .Config.Labels "org.opencontainers.image.version")}},'
                             '"source":{{json (index .Config.Labels "org.opencontainers.image.source")}},'
                             '"revision":{{json (index .Config.Labels "org.opencontainers.image.revision")}}}')
                images[item["image_id"]] = json.loads(command(["docker", "image", "inspect", "--format", label_fmt, item["image_id"]]))
            except WatchError:
                images[item["image_id"]] = {}
                inventory["coverage_gaps"].append(f"{name}: deployed image metadata unavailable.")
        labels = images[item["image_id"]]
        label_version = version(labels.get("version")) if label_matches_image(item["image"], labels.get("source")) else None
        tag_version = version(item["image"].rsplit(":", 1)[-1]) if ":" in item["image"].split("/")[-1] else None
        component = {
            "id": name, "image": image_reference(item["image"]), "state": item["state"],
            "version": label_version or tag_version or "unknown",
            "version_source": "matching image source label (application unverified)" if label_version else "image tag (may not be application version)",
            "exposure": exposure(item["ports"], item["network"], config["exposure"].get(name)),
        }
        revision = labels.get("revision", "") or ""
        if re.fullmatch(r"[0-9a-f]{7,64}", revision):
            component["revision"] = revision
        inventory["components"].append(component)
        binary = BINARY_PROBES.get(item["image"].split("/")[-1].split(":")[0])
        if binary and item["state"] == "running":
            try:
                output = command(["docker", "exec", name, *binary])
                match = re.search(r"\b(\d+\.\d+(?:\.\d+)?(?:[-+][A-Za-z0-9.-]+)?)\b", output)
                if match:
                    component.update(version=match[1], version_source="runtime binary --version")
            except WatchError:
                inventory["coverage_gaps"].append(f"{name}: binary runtime version unavailable.")
        if name in config["python_containers"] and item["state"] == "running":
            try:
                packages = json.loads(command(["docker", "exec", name, "python", "-B", "-c", PROBE_PYTHON, *PYTHON_PACKAGES]))
                if (not isinstance(packages, dict) or any(p not in PYTHON_PACKAGES or not isinstance(v, str)
                                                          or not version(v) for p, v in packages.items())):
                    raise WatchError("Invalid Python metadata probe")
                for package, package_version in packages.items():
                    if version(package_version):
                        inventory["packages"].append({"component": name, "ecosystem": "PyPI", "name": package, "version": package_version, "source": "runtime Python metadata"})
                inventory["package_probes"].append({"component": name, "ecosystem": "PyPI",
                                                    "names": list(PYTHON_PACKAGES),
                                                    "missing": sorted(set(PYTHON_PACKAGES) - set(packages))})
                if name in packages:
                    component.update(version=packages[name], version_source="runtime Python metadata")
            except (WatchError, ValueError, TypeError):
                inventory["coverage_gaps"].append(f"{name}: Python runtime versions could not be read.")
    if any(c["id"] == "mcpo" and c["state"] == "running" for c in inventory["components"]):
        try:
            packages = json.loads(command(["docker", "exec", "mcpo", "python", "-B", "-c", PROBE_NPM]))
            for package, package_version in packages:
                inventory["packages"].append({"component": "mcpo", "ecosystem": "npm", "name": package, "version": package_version, "source": "running npm process"})
            if not packages:
                inventory["coverage_gaps"].append("mcpo: no running npm package versions detected.")
        except WatchError:
            inventory["coverage_gaps"].append("mcpo: npm runtime versions unavailable.")
        requirements = Path("/opt/stacks/webui/mcpo/requirements.txt")
        if requirements.is_file():
            for line in requirements.read_text().splitlines():
                match = re.fullmatch(r"([A-Za-z0-9_.-]+)==([0-9][A-Za-z0-9_.+-]*)", line)
                if match:
                    inventory["packages"].append({"component": "mcpo", "ecosystem": "PyPI", "name": match[1], "version": match[2], "source": "declared tool requirement; runtime environment not verified"})
    inventory["components"].sort(key=lambda c: c["id"])
    return inventory


def remote_collector_source(config):
    """Ship only our fixed, read-only collector; no agent/API/credential code."""
    source = "import json, os, re, subprocess, ipaddress\nfrom pathlib import Path\nfrom datetime import datetime, timezone\nfrom urllib.parse import urlsplit\n"
    for name in ("VERSION",):
        source += f"{name} = re.compile({globals()[name].pattern!r})\n"
    for name in ("PYTHON_PACKAGES", "BINARY_PROBES", "PROBE_PYTHON", "PROBE_NPM"):
        source += f"{name} = {globals()[name]!r}\n"
    source += "class WatchError(Exception): pass\n"
    for function in (now, version, command, public_url, image_reference, label_matches_image, exposure, collect_local_inventory):
        source += inspect.getsource(function) + "\n"
    return source + f"print(json.dumps(collect_local_inventory({config!r})))\n"


def collect_inventory(config):
    inventory = collect_local_inventory(config)
    inventory["remote_hosts"] = []
    inventory["collection_errors"] = []
    hosts_file = config.get("remote_hosts_file")
    if not hosts_file:
        return inventory
    try:
        hosts = json.loads(Path(hosts_file).expanduser().read_text())
        if not isinstance(hosts, list):
            raise ValueError()
        aliases = set()
        for host in hosts:
            alias, target = host["alias"], host["target"]
            if (not re.fullmatch(r"[a-z][a-z0-9-]{0,30}", alias) or alias in aliases
                    or alias in ("host", config["host_alias"])
                    or not re.fullmatch(r"(?:[A-Za-z0-9_][A-Za-z0-9_.-]*@)?[A-Za-z0-9][A-Za-z0-9_.-]*", target)):
                raise ValueError()
            aliases.add(alias)
    except (OSError, ValueError, TypeError, KeyError):
        inventory["collection_errors"].append("Remote host configuration unavailable or invalid; remote coverage missing.")
        inventory["coverage_gaps"] += inventory["collection_errors"]
        return inventory
    for host in hosts:
        alias = host["alias"]
        remote_config = {
            "host_alias": alias, "host_packages": config["host_packages"] + ["docker.io", "openssh-client"],
            "host_package_prefixes": ["openmediavault", "php", "linux-image"],
            "exposure": {}, "python_containers": [],
        }
        args = ["ssh", "-T", "-x", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
                "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=2",
                "-o", "ForwardAgent=no", "-o", "ClearAllForwardings=yes", "-o", "ControlMaster=no",
                "-o", "ControlPath=none", "--", host["target"], "python3 -S -B -"]
        try:
            result = subprocess.run(args, input=remote_collector_source(remote_config), check=True,
                                    capture_output=True, text=True, timeout=120,
                                    env={**os.environ, "LC_ALL": "C"})
            if len(result.stdout) > 500_000:
                raise ValueError()
            remote = json.loads(result.stdout)
            # Rebuild the envelope: never forward SSH targets/stderr or arbitrary
            # top-level remote data into a prompt or a failure notification.
            inventory["remote_hosts"].append({"alias": alias, "host": remote["host"], "collected_at": remote["collected_at"]})
            for component in remote["components"]:
                inventory["components"].append({**component, "id": alias + ":" + component["id"]})
            for package in remote["packages"]:
                inventory["packages"].append({**package, "component": alias + ":" + package["component"]})
            for probe in remote.get("package_probes", []):
                inventory["package_probes"].append({**probe, "component": alias + ":" + probe["component"]})
            inventory["coverage_gaps"] += [alias + ": " + gap for gap in remote["coverage_gaps"]]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            message = alias + ": SSH inventory failed (connection, authentication, host-key, timeout or collector error)."
            inventory["collection_errors"].append(message)
            inventory["coverage_gaps"].append(message)
    return inventory


def advisory_checks(inventory):
    result = {"checked_at": now(), "osv_matches": [], "osv_queries": [], "kev_candidates": [], "errors": []}
    packages = inventory["packages"]
    try:
        queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]}, "version": p["version"]} for p in packages]
        response = request_json(OSV, {"queries": queries}, retry=True) if queries else {"results": []}
        if len(response["results"]) != len(packages):
            raise WatchError("OSV returned incomplete results")
        successful_queries = []
        for package, matches in zip(packages, response["results"]):
            if not isinstance(matches, dict) or not isinstance(matches.get("vulns", []), list):
                raise WatchError("OSV returned malformed results")
            for match in matches.get("vulns", []):
                identifier = match["id"]
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", identifier):
                    raise WatchError("OSV returned an invalid advisory id")
                result["osv_matches"].append({**package, "advisory": identifier, "url": "https://osv.dev/vulnerability/" + identifier})
            successful_queries.append({**package, "advisories": [m["id"] for m in matches.get("vulns", [])]})
        # Publish receipts only after the entire response was validated.
        result["osv_queries"] = successful_queries
    except (WatchError, KeyError, TypeError) as error:
        result["errors"].append(str(error) if isinstance(error, WatchError) else "OSV returned malformed results")
    try:
        feed = request_json(KEV, retry=True)
        cutoff = (datetime.now(timezone.utc) - timedelta(days=30)).date().isoformat()
        products = [re.sub(r"[^a-z0-9]", "", c["image"].split("/")[-1].split(":")[0].lower()) for c in inventory["components"]]
        products += [p["name"].lower() for p in inventory["host"]["packages"]]
        products += [p["name"].lower() for remote in inventory.get("remote_hosts", []) for p in remote["host"]["packages"]]
        for entry in feed["vulnerabilities"]:
            product = re.sub(r"[^a-z0-9]", "", entry["product"].lower())
            if entry["dateAdded"] >= cutoff or (len(product) >= 4 and any(product in p or p in product for p in products if len(p) >= 4)):
                result["kev_candidates"].append({key: entry[key] for key in ("cveID", "vendorProject", "product", "dateAdded", "knownRansomwareCampaignUse")})
        result["kev_source"] = KEV
        result["kev_catalog_version"] = feed["catalogVersion"]
    except (WatchError, KeyError, TypeError) as error:
        result["errors"].append(str(error) if isinstance(error, WatchError) else "CISA returned malformed results")
    return result


def agent_definition(config):
    return {
        "name": AGENT_NAME, "model": config["model"],
        "description": "Read-only, cited security briefing for a sanitized homelab inventory.",
        "instructions": (ROOT / "security-watch-prompt.txt").read_text(),
        "tools": [{"type": "web_search"}],
        "completion_args": {
            "temperature": 0.1, "max_tokens": config["max_tokens"],
            "reasoning_effort": config.get("reasoning_effort", "high"),
            # Keep hosted-tool responses in text mode; the final JSON is
            # parsed and validated locally.
            "response_format": {"type": "text"},
        },
    }


def agent_matches(agent, definition):
    args = agent.get("completion_args") or {}
    actual_format = args.get("response_format") or {}
    return (
        all(agent.get(field) == definition[field] for field in ("name", "model", "instructions"))
        and [t.get("type") for t in agent.get("tools", [])] == ["web_search"]
        and not agent.get("handoffs")
        and all(args.get(field) == value for field, value in definition["completion_args"].items() if field != "response_format")
        and actual_format.get("type") == "text"
    )


def configured_agent(config, key):
    try:
        agent_id = json.loads(Path(config["agent_file"]).expanduser().read_text())["agent_id"]
    except (OSError, ValueError, KeyError):
        raise WatchError("Agent not configured; run --setup-agent first") from None
    if not isinstance(agent_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", agent_id):
        raise WatchError("Invalid configured agent id")
    return request_json(MISTRAL + "/agents/" + agent_id, key=key)


def setup_agent(config, key):
    path = Path(config["agent_file"]).expanduser()
    definition = agent_definition(config)
    if path.exists():
        agent = configured_agent(config, key)
        if agent.get("name") != AGENT_NAME:
            raise WatchError("Configured id belongs to another agent; refusing to change it")
    else:
        agents = request_json(MISTRAL + "/agents?page_size=100", key=key)
        agents = agents if isinstance(agents, list) else agents.get("data", [])
        matches = [a for a in agents if a.get("name") == AGENT_NAME]
        if len(matches) > 1 or (matches and matches[0].get("instructions") != definition["instructions"]):
            raise WatchError("Agent name already exists with a different definition; review in Studio")
        agent = matches[0] if matches else request_json(MISTRAL + "/agents", definition, key=key)
    if not agent_matches(agent, definition):
        agent = request_json(MISTRAL + "/agents/" + agent["id"], definition, key=key, method="PATCH")
    if not agent_matches(agent, definition):
        raise WatchError("Provider did not apply the requested agent definition")
    atomic_json(path, {"agent_id": agent["id"], "model": agent["model"]})
    print("Dedicated security agent configured; other agents left unchanged.")


def source_urls(value):
    urls = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "url" and public_url(item):
                urls.add(item)
            else:
                urls.update(source_urls(item))
    elif isinstance(value, list):
        for item in value:
            urls.update(source_urls(item))
    elif isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        # Hosted web_search returns info.result as a JSON-encoded string.
        try:
            urls.update(source_urls(json.loads(value)))
        except ValueError:
            pass
    return urls


def component_versions(inventory):
    result = {"host": {inventory["host"].get("kernel"), inventory["host"].get("release")}}
    for component in inventory["components"]:
        result[component["id"]] = {component.get("version")}
    for package in inventory["host"]["packages"]:
        result["host:" + package["name"]] = {package["version"]}
    for package in inventory["host"].get("pending_updates_from_local_cache", []):
        result.setdefault("host:" + package["name"], set()).add(package["installed"])
    for package in inventory["packages"]:
        result.setdefault(package["component"], set()).add(package["version"])
    for remote in inventory.get("remote_hosts", []):
        prefix = "host:" + remote["alias"]
        host = remote["host"]
        result[prefix] = {host.get("kernel"), host.get("release")}
        for package in host["packages"]:
            result[prefix + ":" + package["name"]] = {package["version"]}
        for package in host.get("pending_updates_from_local_cache", []):
            result.setdefault(prefix + ":" + package["name"], set()).add(package["installed"])
    return result


def version_order(installed, fixed, ecosystem):
    """Compare a conservative subset of public PyPI/SemVer versions.

    Unsupported syntax is unknown, never compared lexically or as Debian.
    """
    def key(text):
        if ecosystem == "PyPI":
            match = re.fullmatch(r"v?(?:(\d+)!)?(\d+(?:\.\d+)*)(?:(a|b|rc)(\d+))?(?:\.post(\d+))?(?:\.dev(\d+))?(?:\+[a-z0-9.]+)?", text.lower())
            if not match:
                return None
            epoch, release, pre, pre_number, post, dev = match.groups()
            numbers = tuple(int(n) for n in release.split("."))
            while numbers and numbers[-1] == 0:
                numbers = numbers[:-1]
            pre_key = ({"a": 0, "b": 1, "rc": 2}[pre], int(pre_number)) if pre else ((-1, 0) if dev and not post else (3, 0))
            return (int(epoch or 0), numbers, pre_key, int(post) if post else -1, int(dev) if dev else float("inf"))
        match = re.fullmatch(r"v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-([0-9A-Za-z.-]+))?(?:\+[0-9A-Za-z.-]+)?", text)
        if not match:
            return None
        major, minor, patch, pre = match.groups()
        identifiers = pre.split(".") if pre else []
        if any(not part or (part.isdigit() and len(part) > 1 and part.startswith("0")) for part in identifiers):
            return None
        return (int(major), int(minor), int(patch), (0, tuple((0, int(p)) if p.isdigit() else (1, p) for p in identifiers)) if pre else (1,))
    # A fixed local PyPI build cannot be ordered without its full local key.
    if ecosystem == "PyPI" and "+" in fixed:
        return None
    left, right = key(installed), key(fixed)
    return None if left is None or right is None else (left > right) - (left < right)


def finding_citation_aliases(finding, citation_aliases, allowed_urls=()):
    """Exact, finding-scoped GitHub advisory aliases; never a global allowlist.

    Defense in depth: an alias is usable only when its mapped target is still
    one of the actually supplied URLs. Callers normally rebuild allowed_urls
    from the current checks, but a public validator must fail closed if a
    stale or mismatched alias map is passed in.
    """
    used = {}
    allowed = set(allowed_urls)
    if (not citation_aliases or not isinstance(finding, dict)
            or not isinstance(finding.get("sources"), list)):
        return used
    component = finding.get("component")
    advisory = str(finding.get("advisory", "")).upper()
    installed = finding.get("installed_version")
    if not isinstance(component, str) or not isinstance(installed, str):
        return used
    for url in finding["sources"]:
        ghsa = github_advisory_identity(url)
        if not ghsa:
            continue
        target = citation_aliases.get((component, advisory, installed, ghsa))
        if target and target in allowed:
            used[url] = target
    return used


def validate_report(report, inventory, allowed_urls, citation_aliases=None):
    versions = component_versions(inventory)
    components = set(versions)
    if not isinstance(report, dict) or set(report) != set(REPORT_SCHEMA["required"]):
        raise WatchError("Agent returned an invalid report object")
    if not isinstance(report["summary"], str) or not report["summary"] or len(report["summary"]) > 8000:
        raise WatchError("Agent returned an invalid summary")
    for field in ("checked_components", "limitations"):
        if not isinstance(report[field], list) or not all(isinstance(x, str) and len(x) <= 4000 for x in report[field]):
            raise WatchError("Agent returned an invalid " + field)
    if not set(report["checked_components"]) <= components:
        raise WatchError("Agent claimed to check an unknown component")
    if not isinstance(report["findings"], list) or len(report["findings"]) > 25:
        raise WatchError("Agent returned too many/invalid findings")
    seen = set()
    for finding in report["findings"]:
        if not isinstance(finding, dict) or set(finding) != set(FINDING_PROPERTIES):
            raise WatchError("Agent returned an invalid finding")
        if not all(isinstance(finding[field], str) and 0 < len(finding[field]) <= 4000 for field in FINDING_STRINGS):
            raise WatchError("Agent returned an invalid finding field")
        if finding["component"] not in components or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{2,150}", finding["advisory"]):
            raise WatchError("Agent returned an unknown component/advisory")
        if any(finding[field] not in values for field, values in ENUMS.items()):
            raise WatchError("Agent returned an invalid classification")
        installed = finding["installed_version"]
        fixed = finding["fixed_version"]
        if installed != "unknown" and installed not in versions[finding["component"]]:
            raise WatchError("Agent used an unobserved installed version")
        if finding["status"] == "confirmed" and installed == "unknown":
            raise WatchError("Agent confirmed a vulnerability for an unknown installed version")
        if finding["status"] != "not_affected" and installed != "unknown":
            if version(installed) and version(installed) == version(fixed):
                raise WatchError("Agent flagged an installed version it also says is fixed")
            if finding["component"].startswith("host:") and version(fixed):
                comparison = subprocess.run(["dpkg", "--compare-versions", installed, "ge", fixed],
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5)
                if comparison.returncode == 0:
                    raise WatchError("Agent flagged a distro package already at/above its stated fixed version")
            elif version(fixed):
                ecosystems = {p["ecosystem"] for p in inventory["packages"]
                              if p["component"] == finding["component"] and p["version"] == installed}
                # Image/app release versions use only strict SemVer syntax;
                # PyPI packages use public PEP 440 ordering, including epochs.
                orders = [version_order(installed, fixed, e) for e in (ecosystems or {"npm"})]
                if orders and all(order is not None and order >= 0 for order in orders):
                    raise WatchError("Agent flagged a package already at/above its stated fixed version")
        sources = finding["sources"]
        aliases = finding_citation_aliases(finding, citation_aliases, allowed_urls)
        allowed_for_finding = set(allowed_urls) | set(aliases)
        if (not isinstance(sources, list) or not 1 <= len(sources) <= SOURCE_LIMIT
                or not all(isinstance(url, str) and url in allowed_for_finding for url in sources)):
            raise WatchError("Agent finding lacks a source from the actual search/advisory results")
        key = finding_key(finding)
        if key in seen:
            raise WatchError("Agent returned duplicate findings")
        seen.add(key)
    return report


def validated_findings(report, inventory, allowed_urls, citation_aliases=None):
    # Reject malformed reports, but do not throw away sound findings because
    # one assessment overclaims. Rejected claims cannot alert or resolve issues.
    if not isinstance(report, dict) or not isinstance(report.get("findings"), list) or len(report["findings"]) > 25:
        raise WatchError("Agent returned an invalid report")
    validate_report({**report, "findings": []}, inventory, allowed_urls)
    accepted, rejected, seen, mappings = [], [], set(), []
    mapping_keys = set()
    for finding in report["findings"]:
        try:
            validate_report({**report, "findings": [finding]}, inventory, allowed_urls, citation_aliases)
            key = finding_key(finding)
            if key in seen:
                raise WatchError("Duplicate assessment")
            seen.add(key)
            aliases = finding_citation_aliases(finding, citation_aliases, allowed_urls)
            sources = []
            for url in finding["sources"]:
                target = aliases.get(url)
                if target:
                    mapping = {
                        "from": url, "to": target, "component": finding["component"],
                        "advisory": finding["advisory"], "installed_version": finding["installed_version"],
                        "basis": "exact GitHub advisory URL for a currently supplied OSV match; GitHub page not fetched",
                    }
                    mapping_key = (mapping["from"], mapping["to"], mapping["component"],
                                   mapping["advisory"], mapping["installed_version"])
                    if mapping_key not in mapping_keys:
                        mapping_keys.add(mapping_key)
                        mappings.append(mapping)
                    url = target
                if url not in sources:
                    sources.append(url)
            accepted.append({**finding, "sources": sources})
        except WatchError as error:
            rejected_item = {
                "component": str(finding.get("component", "unknown"))[:100] if isinstance(finding, dict) else "unknown",
                "advisory": str(finding.get("advisory", "unknown"))[:150] if isinstance(finding, dict) else "unknown",
                "reason": str(error),
            }
            supplied = public_source_urls(finding.get("sources") if isinstance(finding, dict) else None)
            if supplied:
                rejected_item["supplied_sources"] = supplied
            recognized = (finding_citation_aliases(finding, citation_aliases, allowed_urls)
                          if isinstance(finding, dict) else {})
            unmatched = [url for url in supplied if url not in allowed_urls and url not in recognized]
            if unmatched:
                rejected_item["unmatched_sources"] = unmatched
            # Keep only actual, allowlisted incident references for identity.
            # Dropping them would make a same-incident rejection's veto use a
            # raw name while accepted dismissals use the canonical identity.
            if isinstance(finding, dict) and isinstance(finding.get("sources"), list):
                sources = [url for url in finding["sources"] if isinstance(url, str)
                           and url in allowed_urls
                           and url in INCIDENT_SOURCES.get(rejected_item["component"], {})]
                if sources:
                    rejected_item["sources"] = sources
            rejected.append(rejected_item)
    if rejected and not accepted:
        raise WatchError("All agent assessments failed local validation")
    report["findings"] = accepted
    report["rejected_assessments"] = rejected
    report["citation_mappings"] = mappings
    if rejected:
        # Do not display a model summary that may repeat the rejected claims.
        report["summary"] = f"Research returned {len(accepted)} accepted assessments; {len(rejected)} claims were excluded by local validation. Coverage is partial."
        report["limitations"] += [f"Excluded {item['component']} / {item['advisory']}: {item['reason']}" for item in rejected]
    return report


def previous_finding_briefs(findings):
    """Compact historical context; never a substitute for fresh citations."""
    briefs = []
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        brief = {key: finding[key] for key in (
            "component", "advisory", "status", "priority", "confidence",
            "installed_version", "fixed_version", "sources",
        ) if key in finding}
        brief["not_rechecked"] = bool(finding.get("not_rechecked"))
        if str(finding.get("advisory", "")).upper().startswith("INCIDENT-"):
            # Historical exposure context is needed to avoid inventing a new
            # incident identity or losing the affected-artifact scope.
            for key in ("affected_versions", "reason"):
                if key in finding:
                    brief[key] = finding[key]
        briefs.append(brief)
    return briefs


def research_payload(inventory, checks, previous):
    relevant, omitted = relevant_kev_candidates(checks.get("kev_candidates", []), inventory)
    compact_checks = {key: checks[key] for key in (
        "checked_at", "osv_queries", "kev_source", "kev_catalog_version", "errors",
    ) if key in checks}
    compact_checks["osv_matches"] = dedupe_exact(checks.get("osv_matches", []))
    compact_checks["kev_candidates"] = relevant
    compact_checks["kev_candidates_omitted"] = omitted
    compact_checks["kev_selection_note"] = (
        "KEV candidates are filtered to inventory-related products by a conservative heuristic. "
        "Omitted rows are not evidence of absence; the full candidate list remains in the local JSON archive."
    )
    return {
        "as_of_utc": now(), "inventory": inventory, "advisory_checks": compact_checks,
        "valid_component_ids": sorted(component_versions(inventory)),
        "response_schema": REPORT_SCHEMA,
        "previous_findings": previous_finding_briefs(previous.get("findings", [])),
        "previous_report_at": previous.get("checked_at", "first run"),
        "previous_findings_note": (
            "Briefs are historical context only; their sources are not fresh citations. "
            "Omission or absence here is not evidence of resolution. Return new, changed, or "
            "currently re-verified assessments; do not recopy unchanged history."
        ),
        "incident_identity_sources": INCIDENT_SOURCES,
        "incident_review_guidance": (
            "For incidents, cite the incident-specific primary advisory and identify affected versions/tags/digests. "
            "Reuse the canonical incident ID when its listed source actually applies. A newly published recap is not a new incident. "
            "Compare current deployment evidence with affected artifacts; do not claim exposure from pull date alone. "
            "Unknown historical exposure stays unknown; missing evidence is not a not-affected assessment."),
    }


def coverage_summary(inventory, checks, report):
    """Deterministic coverage facts, separate from model findings and alerts."""
    versions = component_versions(inventory)
    queries = [query for query in checks.get("osv_queries", []) if isinstance(query, dict)]
    matches = [match for match in checks.get("osv_matches", []) if isinstance(match, dict)]
    assessments = [finding for finding in report.get("assessments", []) if isinstance(finding, dict)]
    # The model finding contract has no package name, so the strongest safe
    # identity is component + advisory + observed installed version. An
    # unknown installed version cannot identify which OSV receipt was reviewed.
    assessed = set()
    for finding in assessments:
        installed = finding.get("installed_version")
        if not isinstance(installed, str) or not installed or installed == "unknown":
            continue
        assessed.add((finding.get("component"), str(finding.get("advisory", "")).upper(), installed))
    groups = {}
    for match in matches:
        key = (match.get("component"), match.get("ecosystem"), match.get("name"), match.get("version"))
        groups.setdefault(key, []).append({"advisory": match.get("advisory"), "url": match.get("url")})
    grouped = []
    for key in sorted(groups, key=lambda item: tuple(str(part) for part in item)):
        component, ecosystem, name, version = key
        grouped.append({
            "component": component, "ecosystem": ecosystem, "package": name, "version": version,
            "advisories": dedupe_exact(sorted(
                groups[key], key=lambda item: (str(item.get("advisory")), str(item.get("url"))))),
        })
    historical = [finding for finding in report.get("findings", [])
                  if isinstance(finding, dict) and finding.get("not_rechecked")]
    unreviewed = [match for match in matches
                  if (match.get("component"), str(match.get("advisory", "")).upper(),
                      match.get("version")) not in assessed]
    return {
        "inventory_ids": len(versions),
        "osv_queried_package_versions": len(queries),
        "osv_query_components": len({query.get("component") for query in queries}),
        "osv_match_receipts": len(matches),
        "osv_match_advisories": len({str(match.get("advisory", "")).upper() for match in matches}),
        "osv_match_components": len({match.get("component") for match in matches}),
        "osv_matches_without_accepted_assessment": len(unreviewed),
        "model_checked_components": len({component for component in report.get("checked_components", [])
                                         if isinstance(component, str)}),
        "historical_not_rechecked": len(historical),
        "source_errors": len(checks.get("errors", [])),
        "groups": grouped,
        "note": (
            "OSV queries are automated package-version lookups, not a full component security review. "
            "Counts distinguish queried package-version pairs from inventory IDs. The assessment-matched "
            "metric is scoped by component, advisory and installed version because package name is not part "
            "of the model finding contract. Grouped OSV matches are upstream package matches with "
            "applicability unverified; they are not confirmed findings and do not trigger alerts. "
            "Historical findings, when present, were not rechecked this run."
        ),
    }


def coverage_issues(errors, rejected):
    """Stable fingerprints for new/changed coverage issues, not findings."""
    issues = {}
    for error in errors:
        key = hashlib.sha256(json.dumps(["error", str(error)]).encode()).hexdigest()
        issues[key] = "error: " + str(error)
    for item in rejected:
        if isinstance(item, dict):
            component = str(item.get("component", "unknown"))
            advisory = str(item.get("advisory", "unknown"))
            reason = str(item.get("reason", ""))
            # Advisory IDs are case-insensitive; normalize for the fingerprint
            # so case drift cannot re-notify the same issue. The display label
            # keeps the original spelling.
            key = hashlib.sha256(json.dumps(
                ["rejected", component, advisory.upper(), reason]).encode()).hexdigest()
            issues[key] = f"{component} / {advisory}"
        else:
            key = hashlib.sha256(json.dumps(["rejected", str(item)]).encode()).hexdigest()
            issues[key] = str(item)
    return issues


def notification_label(value, limit=120):
    return re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))[:limit]


def markdown_value(value, limit=300):
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", str(value))
    return text.replace("`", "'").replace("<", "&lt;").replace(">", "&gt;")[:limit]


def markdown_url(value):
    if public_url(value) and not any(character in value for character in "<>"):
        return f"<{value}>"
    return ""


def research_response(response, inventory, checks):
    """Strictly validate one conversation response.

    Shared by synchronous research and delayed batch collection so both paths
    require the same web-search evidence, citations, versions and schema.
    """
    if not isinstance(response, dict):
        raise WatchError("Agent returned an invalid response object")
    outputs = response.get("outputs", [])
    if not isinstance(outputs, list) or not all(isinstance(entry, dict) for entry in outputs):
        raise WatchError("Agent returned invalid outputs")
    searches = [entry for entry in outputs if entry.get("type") == "tool.execution" and entry.get("name") in ("web_search", "web_search_premium")]
    if not searches:
        raise WatchError("Agent did not perform a web search; report rejected")
    messages = [entry for entry in outputs if entry.get("type") == "message.output"]
    content = messages[-1].get("content", []) if messages else []
    if not isinstance(content, (str, list)):
        raise WatchError("Agent returned invalid report content")
    text = content if isinstance(content, str) else "".join(
        chunk.get("text", "") for chunk in content if isinstance(chunk, dict) and chunk.get("type") == "text"
    )
    try:
        report = json.loads(text)
    except ValueError:
        raise WatchError("Agent returned invalid/truncated report JSON") from None
    # Only actual tool references and supplied advisory links count as citations.
    references = source_urls([e for e in outputs if e.get("type") == "tool.execution"])
    for message in messages:
        chunks = message.get("content", [])
        if isinstance(chunks, list):
            references.update(source_urls([c for c in chunks if isinstance(c, dict) and c.get("type") == "tool_reference"]))
    for match in checks.get("osv_matches", []):
        if isinstance(match, dict) and isinstance(match.get("url"), str):
            references.add(match["url"])
    if checks.get("kev_source"):
        references.add(checks["kev_source"])
    report = validated_findings(report, inventory, references, citation_alias_map(checks))
    # Inventory proves package presence/version, not use of a vulnerable server
    # transport, parser, build type or code path. Never present that as confirmed
    # application applicability, even when the model does.
    downgraded = 0
    for finding in report["findings"]:
        if finding["status"] == "confirmed" and not finding["component"].startswith("host:"):
            finding["status"] = "possible"
            finding["reason"] = "Deployment applicability unverified (package/version evidence only). " + finding["reason"]
            downgraded += 1
    if downgraded:
        report["summary"] = f"Research returned {len(report['findings'])} accepted assessments; {downgraded} application matches require deployment-applicability review; {len(report['rejected_assessments'])} claims excluded. See coverage limitations."
    report["limitations"].append("Application package matches are possible issues: vulnerable feature/transport/build use is not verified. Review preconditions in the original advisory before remediation.")
    report.update(usage=response.get("usage", {}), searches=len(searches), search_sources=sorted(references))
    return report


def research(config, key, inventory, checks, previous):
    agent = configured_agent(config, key)
    if not agent_matches(agent, agent_definition(config)):
        raise WatchError("Agent settings differ from tracked settings; run --setup-agent to apply them")
    payload = {
        # Agent conversations reject completion_args overrides. Define and
        # verify the budget/output mode/tools on the agent before every run.
        "agent_id": agent["id"], "store": False,
        "inputs": [{"role": "user", "content": json.dumps(research_payload(inventory, checks, previous))}],
    }
    if len(json.dumps(payload)) > 180_000:
        raise WatchError("Research input exceeds budget; reduce inventory scope rather than silently truncate")
    # No automatic retry: a timed-out research POST may still be running/billed.
    response = request_json(MISTRAL + "/conversations", payload, key, config["timeout_seconds"])
    return research_response(response, inventory, checks)


def collect_dependabot():
    """Fetch and list origin's Dependabot update branches.

    The scoped refspec fetches and prunes only refs/remotes/origin/dependabot,
    leaving the working tree and unrelated remote refs untouched. A fetch
    failure returns an explicit gap instead of a list: the remote state is
    unknown, which must never read as an all-clear or produce a false
    new-branch storm on the next successful fetch.
    """
    repo = ROOT.parent
    try:
        command(["git", "-C", str(repo), "fetch", "--quiet", "--prune", "--no-tags",
                 "origin", DEPENDABOT_REFSPEC], timeout=90)
        listing = command(["git", "-C", str(repo), "for-each-ref",
                           "refs/remotes/origin/dependabot",
                           "--format=%(refname:short)%09%(committerdate:short)%09%(objectname:short)"])
    except WatchError:
        return [], "Dependabot branches could not be fetched; pending update PRs are unknown this run."
    entries = []
    for row in listing.splitlines():
        if not row.strip():
            continue
        fields = [field.strip() for field in row.split("\t", 2)]
        if len(fields) != 3:
            # Never let one malformed line fail the whole watch.
            continue
        ref, opened, tip = fields
        entries.append({"branch": ref.removeprefix("origin/"), "opened": opened, "tip": tip})
    return entries, None


def short_branch(branch):
    # dependabot/<ecosystem>/<repo path> -> <repo path>; the ecosystem segment
    # spelling varies (docker-compose / docker_compose), so match any single one.
    return re.sub(r"^dependabot/[^/]+/", "", branch)


def dependabot_lines(dependabot):
    """Bounded ntfy lines for pending Dependabot updates; no line without open branches."""
    if not dependabot or not dependabot.get("open"):
        return []
    open_branches, new_branches = dependabot["open"], dependabot.get("new", [])
    if not new_branches:
        return [f"Dependabot update PRs: {len(open_branches)} open, none new."]
    names = [short_branch(entry["branch"]) for entry in new_branches]
    overflow = f" (+{len(names) - 5} more in the local report)" if len(names) > 5 else ""
    return [f"Dependabot update PRs: {len(names)} new of {len(open_branches)} open — "
            + ", ".join(names[:5]) + overflow]


def finding_key(finding):
    advisory = finding["advisory"].upper()
    if advisory.startswith("INCIDENT-"):
        identities = {INCIDENT_SOURCES.get(finding["component"], {})[url]
                      for url in finding.get("sources", [])
                      if url in INCIDENT_SOURCES.get(finding["component"], {})}
        if len(identities) == 1:
            advisory = next(iter(identities))
    return finding["component"] + ":" + advisory


def merge_findings(previous, current, complete, checked_components=None, blocked_keys=None):
    """Merge per component/advisory key; a blocked key vetoes only its own dismissal.

    When blocked_keys is supplied, only those keys veto model-derived
    dismissals; complete is then irrelevant. Without it, complete=False blocks
    all dismissals. Omitted previous findings are always retained as not
    rechecked. A rejected claim never dismisses or resolves an unrelated finding.
    """
    blocked = set(blocked_keys or ())
    for finding in previous + current:
        raw = finding["component"] + ":" + finding["advisory"].upper()
        if raw in blocked:
            blocked.add(finding_key(finding))
    merged = {finding_key(f): {**f, "not_rechecked": True} for f in previous}
    for finding in current:
        key = finding_key(finding)
        if finding["status"] == "not_affected":
            # Partial checks must never dismiss an existing finding.
            if ((complete or blocked_keys is not None) and key not in blocked
                    and finding["confidence"] == "high"
                    and (checked_components is None or finding["component"] in checked_components)):
                merged.pop(key, None)
            continue
        merged[key] = finding
    return list(merged.values())


def collect_resolution_advisories(findings):
    """Fetch authoritative package/alias identities, never infer them from versions."""
    records, errors = {}, []
    identifiers = sorted({f["advisory"].upper() for f in findings
                          if f.get("not_rechecked") and f["advisory"].upper().startswith("GHSA-")})
    for identifier in identifiers:
        try:
            canonical = "GHSA-" + identifier[5:].lower()
            data = request_json("https://api.osv.dev/v1/vulns/" + canonical, timeout=10)
            if (not isinstance(data, dict) or not isinstance(data.get("id"), str)
                    or data["id"].upper() != identifier
                    or not isinstance(data.get("affected"), list)
                    or not data["affected"]
                    or not all(isinstance(a, dict) and isinstance(a.get("package"), dict)
                               and isinstance(a["package"].get("name"), str)
                               and isinstance(a["package"].get("ecosystem"), str)
                               for a in data["affected"])
                    or not isinstance(data.get("aliases", []), list)
                    or not all(isinstance(alias, str) for alias in data.get("aliases", []))):
                raise WatchError("Invalid OSV advisory metadata")
            records[identifier] = data
        except (WatchError, ValueError, KeyError, TypeError):
            # Some vendor GHSAs are published only in the repository advisory
            # API, not the global GitHub/OSV indexes. Never derive a repository
            # or URL from model prose: only explicitly configured owners.
            repositories = {REPOSITORY_ADVISORIES[f["component"]]["repository"]
                            for f in findings if f["advisory"].upper() == identifier
                            and f["component"] in REPOSITORY_ADVISORIES}
            recovered = False
            for repository in sorted(repositories):
                try:
                    source = "https://github.com/" + repository + "/security/advisories/" + canonical
                    vendor = request_json("https://api.github.com/repos/" + repository
                                          + "/security-advisories/" + canonical, timeout=10)
                    if (not isinstance(vendor, dict) or not isinstance(vendor.get("ghsa_id"), str)
                            or vendor["ghsa_id"].upper() != identifier
                            or vendor.get("html_url") != source or vendor.get("state") != "published"
                            or vendor.get("withdrawn_at")
                            or not isinstance(vendor.get("vulnerabilities"), list)
                            or not vendor["vulnerabilities"]):
                        raise WatchError("Invalid repository advisory metadata")
                    records[identifier] = {"repository": repository, "source_url": source,
                                           "repository_advisory": vendor}
                    recovered = True
                    break
                except (WatchError, ValueError, KeyError, TypeError):
                    continue
            if not recovered:
                errors.append("Could not verify package/alias identity for " + identifier + "; retained historical finding.")
    return records, errors


def osv_fixed_boundary(entries, current, ecosystem):
    """Prove current is beyond every affected range; unsupported ranges stay open."""
    if ecosystem not in ("PyPI", "npm"):
        return False
    fixes = []
    for entry in entries:
        versions = entry.get("versions", [])
        if (not isinstance(versions, list) or any(not isinstance(v, str) for v in versions)
                or any(version_order(current, v, ecosystem) in (None, 0) for v in versions)):
            return False
        ranges = entry.get("ranges", [])
        if not isinstance(ranges, list) or not ranges:
            return False
        for affected_range in ranges:
            if not isinstance(affected_range, dict) or affected_range.get("type") != "ECOSYSTEM":
                return False
            events = affected_range.get("events", [])
            if not isinstance(events, list) or not events or len(events) % 2:
                return False
            previous_fix = None
            for start, end in zip(events[::2], events[1::2]):
                if (not isinstance(start, dict) or not isinstance(end, dict)
                        or set(start) != {"introduced"} or set(end) != {"fixed"}):
                    return False
                introduced, fixed = start["introduced"], end["fixed"]
                if not isinstance(introduced, str) or not isinstance(fixed, str):
                    return False
                if introduced != "0" and version_order(fixed, introduced, ecosystem) != 1:
                    return False
                if previous_fix is not None and (introduced == "0" or
                        version_order(introduced, previous_fix, ecosystem) not in (0, 1)):
                    return False
                if version_order(current, fixed, ecosystem) not in (0, 1):
                    return False
                fixes.append(fixed)
                previous_fix = fixed
    return bool(fixes)


def osv_resolution(finding, inventory, checks, record):
    """Require evidence for every advisory package, including explicit absence."""
    component = finding["component"]
    groups = {}
    for entry in record["affected"]:
        package = entry["package"]
        groups.setdefault((package["name"], package["ecosystem"]), []).append(entry)
    identities = {record["id"].upper(), *(alias.upper() for alias in record.get("aliases", []))}
    observed_evidence = []
    for (name, ecosystem), entries in groups.items():
        observed = [p for p in inventory["packages"] if p["component"] == component
                    and (p["name"], p["ecosystem"]) == (name, ecosystem)]
        probes = [p for p in inventory.get("package_probes", [])
                  if p["component"] == component and p["ecosystem"] == ecosystem
                  and name in p["names"]]
        if len(probes) > 1 or (observed and probes and name in probes[0]["missing"]):
            return None
        if not observed:
            if len(probes) != 1 or name not in probes[0]["missing"]:
                return None
            continue
        if len(observed) != 1:
            return None
        current = observed[0]["version"]
        receipts = [q for q in checks.get("osv_queries", [])
                    if (q["component"], q["name"], q["ecosystem"], q["version"])
                    == (component, name, ecosystem, current)]
        if len(receipts) != 1 or not isinstance(receipts[0].get("advisories"), list):
            return None
        matches = receipts[0]["advisories"]
        if any(not isinstance(m, str) for m in matches) or identities.intersection(m.upper() for m in matches):
            return None
        boundary = osv_fixed_boundary(entries, current, ecosystem)
        # Preserve the older single-package zero-match path when upstream range
        # metadata is unavailable. Multi-package or nonzero receipts need ranges.
        fallback = (len(groups) == 1 and not matches
                    and all(e.get("ranges", []) == [] and isinstance(e.get("versions", []), list)
                            and all(isinstance(v, str) and version_order(current, v, ecosystem) not in (None, 0)
                                    for v in e.get("versions", [])) for e in entries)
                    and current != finding["installed_version"] and version(finding["fixed_version"])
                    and version_order(current, finding["fixed_version"], ecosystem) in (0, 1))
        if not boundary and not fallback:
            return None
        observed_evidence.append(f"{ecosystem} {name} {current}")
    if not observed_evidence:
        return None
    return ("Current " + ", ".join(observed_evidence)
            + "; authoritative OSV package identities, fixed ranges or recorded fix, and successful current queries confirm this advisory is fixed. "
            "All advisory packages were checked; uninstalled alternatives require explicit probe evidence. Other package advisories may remain.")


def reconcile_findings(findings, inventory, checks):
    """Resolve only exact package identities with fresh, comparable evidence.

    Never guess a package from prose or treat missing inventory as removal.
    OSV resolution requires authoritative package identity and a successful
    current query receipt. Nonzero receipts require authoritative fixed ranges;
    matches to the advisory or its aliases always block resolution.
    Distro fixes require a previously confirmed, high-confidence fix.
    This evidence is measured directly (dpkg, OSV, runtime probes), so it is
    independent of rejected research claims; the same-key veto governs only
    model-derived not-affected dismissals in merge_findings.
    """
    remaining, resolved = [], []
    hosts = {"host:" + p["name"]: p["version"] for p in inventory["host"]["packages"]}
    for remote in inventory.get("remote_hosts", []):
        hosts.update({"host:" + remote["alias"] + ":" + p["name"]: p["version"]
                      for p in remote["host"]["packages"]})
    for finding in findings:
        evidence = None
        if finding.get("not_rechecked"):
            component, advisory = finding["component"], finding["advisory"]
            installed, fixed = hosts.get(component), finding["fixed_version"]
            if (installed and version(installed) and version(fixed)
                    and finding["status"] == "confirmed" and finding["confidence"] == "high"
                    and finding.get("sources")):
                result = subprocess.run(["dpkg", "--compare-versions", installed, "ge", fixed],
                                        capture_output=True, timeout=5)
                if result.returncode == 0:
                    evidence = f"Current distro package {installed} meets recorded fixed revision {fixed}."
            record = checks.get("resolution_advisories", {}).get(advisory.upper())
            if record and "repository_advisory" in record:
                policy = REPOSITORY_ADVISORIES.get(component)
                applications = [c for c in inventory["components"] if c["id"] == component]
                if policy and record["repository"] == policy["repository"] and len(applications) == 1:
                    app = applications[0]
                    image = app.get("image", "").split("@", 1)[0].split(":", 1)[0]
                    current = app.get("version", "unknown")
                    entries = record["repository_advisory"]["vulnerabilities"]
                    fixes = []
                    for entry in entries:
                        if (not isinstance(entry, dict) or not isinstance(entry.get("package"), dict)
                                or not isinstance(entry["package"].get("name"), str)
                                or entry["package"]["name"].lower() not in policy["package_names"]
                                or not isinstance(entry.get("patched_versions"), str)):
                            break
                        patched = entry["patched_versions"].strip()
                        order = version_order(current, patched, "npm")
                        # Strictly newer avoids ambiguous inclusive boundaries
                        # in vendor metadata (e.g. affected through the fix).
                        if order is None or order <= 0:
                            break
                        fixes.append(patched)
                    if (image == policy["image"] and app.get("state") == "running"
                            and app.get("version_source") == "runtime binary --version"
                            and entries and len(fixes) == len(entries)):
                        evidence = (f"Runtime {component} {current} is newer than vendor-patched versions "
                                    + ", ".join(sorted(set(fixes))) + "; official repository advisory: " + record["source_url"])
            elif record and not record.get("withdrawn"):
                evidence = osv_resolution(finding, inventory, checks, record)
        if evidence:
            resolved.append({**finding, "resolution": evidence, "resolved_at": now()})
        else:
            remaining.append(finding)
    return remaining, resolved


def fingerprint(finding):
    # Exclude prose, ordering and timestamps: wording drift must not spam ntfy.
    fields = ("priority", "status", "installed_version", "fixed_version", "exploited")
    return hashlib.sha256(json.dumps([finding[field] for field in fields]).encode()).hexdigest()


def changed_alerts(findings, notified):
    return [f for f in findings if not f.get("not_rechecked") and f["priority"] in ("act_now", "update_soon")
             and notified.get(finding_key(f)) != fingerprint(f)]


def canonical_notifications(notified, findings):
    """Migrate delivered keys only using exact incident-source evidence."""
    result = dict(notified)
    for finding in findings:
        old = finding["component"] + ":" + finding["advisory"].upper()
        canonical = finding_key(finding)
        if old != canonical and old in result:
            result.setdefault(canonical, result.pop(old))
    return result


def notification_body(alerts, errors, rejected, dependabot=None, new_issue_labels=None, unchanged_issue_count=0):
    lines = [f"{len(alerts)} new/changed actionable findings. Full report: ~/.local/state/security-watch/latest.md"]
    ordered = sorted(alerts, key=lambda f: (ENUMS["priority"].index(f["priority"]), finding_key(f)))
    notices = []
    if errors:
        notices.append(f"Incomplete advisory checks: {len(errors)} errors; inspect the local report.")
    if rejected:
        notices.append(f"Partial report: {len(rejected)} unverified claims excluded; inspect limitations.")
    if new_issue_labels:
        labels = [notification_label(label, 80) for label in new_issue_labels[:3]]
        more = len(new_issue_labels) - len(labels)
        notices.append(
            f"New coverage issues: {len(new_issue_labels)} ({'; '.join(labels)}"
            + (f"; +{more} more" if more else "") + "); inspect the local report.")
    if unchanged_issue_count:
        notices.append(f"Coverage remains partial: {unchanged_issue_count} unchanged issue(s) in the local report.")
    updates = dependabot_lines(dependabot)
    shown = 0
    for finding in ordered[:8]:
        line = f"{finding['priority']}: {finding['component']} / {finding['advisory']} ({finding['status']}); fixed: {finding['fixed_version'][:60]}"
        # Reserve space for overflow, update branches and notices even when IDs are long/Unicode.
        reserve = [f"+ {len(ordered)} additional findings in the local report."] + updates
        if len("\n".join(lines + [line] + reserve + notices).encode()) > 3500:
            break
        lines.append(line)
        shown += 1
    if shown < len(ordered):
        lines.append(f"+ {len(ordered) - shown} additional findings in the local report.")
    return "\n".join(lines + updates + notices)


def notify(config, body, urgent=False):
    if len(body.encode()) > 3500:
        raise WatchError("Notification exceeds byte budget; refusing to truncate")
    key = read_key(config["ntfy_token_file"])
    request = Request(config["ntfy_url"], data=body.encode(), headers={
        "Authorization": "Bearer " + key, "Title": "Security watch: main",
        "Priority": "high" if urgent else "default", "Tags": "shield",
        "Content-Type": "text/plain; charset=utf-8",
    })
    try:
        with build_opener(NoRedirect()).open(request, timeout=15) as response:
            response.read(4096)
    except (HTTPError, URLError, TimeoutError, OSError):
        raise WatchError("ntfy delivery failed") from None


def markdown(report):
    ordered = [f for f in report["findings"] if not f.get("not_rechecked")]
    historical = [f for f in report["findings"] if f.get("not_rechecked")]
    lines = ["# Security watch", "", f"Checked: {report['checked_at']}", ""]
    batch = report.get("batch")
    if isinstance(batch, dict):
        lines += [
            f"Batch snapshot: submitted {batch.get('submitted_at', 'unknown')}; "
            f"age {batch.get('snapshot_age_hours', 'unknown')} hours at collection; "
            "delayed asynchronous research against that snapshot, not a live collection.",
            "",
        ]
    lines += [
        f"Current findings: {len(ordered)}; historical review backlog: {len(historical)}; "
        f"resolved by current evidence: {len(report.get('resolved_findings', []))}.", "",
        "Research summary (before historical reconciliation): " + report["summary"], "",
        "Advisory synthesis, not a full vulnerability scan or an all-clear certificate.", "", "## Findings",
    ]
    if not ordered:
        lines.append("No current actionable findings identified; this is not an all-clear.")
    for finding in ordered:
        lines += ["", f"### {finding['priority'].upper()}: {finding['component']} / {finding['advisory']}",
                  finding["title"], f"Status: {finding['status']}; confidence: {finding['confidence']}; exploited: {finding['exploited']}",
                  f"Installed: {finding['installed_version']}; affected: {finding['affected_versions']}; fixed: {finding['fixed_version']}",
                  finding["reason"], "Action: " + finding["action"], *["- " + url for url in finding["sources"]]]
    if historical:
        lines += ["", "## Historical findings — not reverified",
                  "Old evidence below is not a statement of the currently installed version or current vulnerability.",
                  "Retained for review, not current action instructions. Missing evidence never means resolved."]
    for finding in historical:
        lines += ["", f"### REVIEW NEEDED: {finding['component']} / {finding['advisory']}",
                  "Historical title: " + finding["title"],
                  f"Previously assessed priority: {finding['priority']}; status: {finding['status']}; confidence: {finding['confidence']}",
                  f"Previously observed version: {finding['installed_version']}; recorded affected: {finding['affected_versions']}; recorded fix: {finding['fixed_version']}",
                  "Historical rationale: " + finding["reason"],
                  "Historical advice (not revalidated): " + finding["action"],
                  *["- " + url for url in finding["sources"]]]
    if not report["findings"]:
        lines.append("No actionable findings identified within the checked sources; see coverage limits below.")
    if report.get("resolved_findings"):
        lines += ["", "## Resolved by current package evidence"]
        for finding in report["resolved_findings"]:
            lines += ["", f"### {finding['component']} / {finding['advisory']}", finding["resolution"]]
    excluded = [f for f in report.get("assessments", []) if f["status"] == "not_affected"]
    if excluded:
        lines += ["", "## Assessed as not affected (review evidence)"]
        for finding in excluded:
            lines += ["", f"### {finding['component']} / {finding['advisory']}", finding["reason"],
                      "Advice: " + finding["action"], *["- " + url for url in finding["sources"]]]
    pending = report.get("dependabot", {}).get("open", [])
    if pending:
        fresh = {entry["branch"] for entry in report["dependabot"].get("new", [])}
        lines += ["", "## Dependabot update branches",
                  "Pending image-update branches; pinned tags deploy nothing until a PR is merged."]
        lines += [f"- {'NEW ' if entry['branch'] in fresh else ''}{short_branch(entry['branch'])}"
                  f" — opened {entry['opened']}, tip {entry['tip']}" for entry in pending]
    mappings = report.get("citation_mappings", [])
    if mappings:
        lines += ["", "## Citation alias notes",
                  "Exact GitHub advisory URLs below were mapped to a currently supplied OSV URL for the same "
                  "component, advisory and observed version; the GitHub page was not fetched."]
        for mapping in mappings:
            lines.append(
                f"- {markdown_value(mapping.get('component'), 150)} / {markdown_value(mapping.get('advisory'), 150)}: "
                f"{markdown_value(mapping.get('from'), 300)} -> {markdown_value(mapping.get('to'), 300)}")
    coverage = report.get("coverage")
    if coverage:
        lines += ["", "## OSV package-match coverage (not confirmed findings)"]
        lines.append(
            f"Inventory IDs: {coverage.get('inventory_ids', 0)}; OSV queried package-version pairs: "
            f"{coverage.get('osv_queried_package_versions', 0)} across {coverage.get('osv_query_components', 0)} components; "
            f"model-reported checked components: {coverage.get('model_checked_components', 0)}; "
            f"historical findings not rechecked: {coverage.get('historical_not_rechecked', 0)}.")
        lines.append(
            f"OSV match receipts: {coverage.get('osv_match_receipts', 0)} "
            f"({coverage.get('osv_match_advisories', 0)} advisory IDs, {coverage.get('osv_match_components', 0)} components); "
            f"without an accepted current assessment (component/advisory/version-scoped): "
            f"{coverage.get('osv_matches_without_accepted_assessment', 0)}.")
        lines.append("These are upstream package matches with applicability unverified; they are not new confirmed findings and do not trigger alerts.")
        if coverage.get("source_errors"):
            lines.append(f"Source errors: {coverage['source_errors']}; coverage is partial.")
        if not coverage.get("groups"):
            lines.append("No OSV package-version matches were returned; this is not an all-clear.")
        for group in coverage.get("groups", []):
            lines.append(
                f"- {markdown_value(group.get('component'), 150)} / {markdown_value(group.get('package'), 150)} "
                f"{markdown_value(group.get('version'), 100)} ({markdown_value(group.get('ecosystem'), 40)}):")
            for advisory in group.get("advisories", []):
                url = markdown_url(advisory.get("url"))
                lines.append(f"  - {markdown_value(advisory.get('advisory'), 150)}" + (f" {url}" if url else ""))
        if coverage.get("note"):
            lines.append(coverage["note"])
    lines += ["", "## Coverage and limitations", *["- " + item for item in report["limitations"]], "",
              f"Web search executions: {report['searches']}; usage: {json.dumps(report['usage'])}"]
    return "\n".join(lines) + "\n"


def prepare_report(report, inventory, checks, dependabot, new_branches, previous,
                   checked_at, dependabot_gap=None):
    """Merge validated research with history and measured resolution evidence."""
    unchecked = set(component_versions(inventory)) - set(report["checked_components"])
    report["limitations"] += inventory["coverage_gaps"] + checks["errors"]
    if dependabot_gap:
        report["limitations"].append(dependabot_gap)
    if unchecked:
        report["limitations"].append("Not researched this run: " + ", ".join(sorted(unchecked)))
    rejected = report.get("rejected_assessments", [])
    # Validated resolutions are scoped to their own component/advisory;
    # unrelated source failures or rejected claims must not veto them.
    complete = not checks["errors"] and not rejected
    blocked = {finding_key(item) for item in rejected if "advisory" in item}
    # A rejected claim vetoes only a model-derived dismissal with the same
    # component/advisory key; measured package reconciliation remains
    # independent. Each accepted assessment already passed local validation.
    report["assessments"] = report["findings"]  # Preserve explicit not-affected evidence in the archive.
    report["findings"] = merge_findings(previous.get("findings", []), report["findings"], complete,
                                        set(report["checked_components"]), blocked)
    records, resolution_errors = collect_resolution_advisories(report["findings"])
    checks["resolution_advisories"] = records
    report["limitations"] += resolution_errors
    checks["resolution_errors"] = resolution_errors
    report["findings"], report["resolved_findings"] = reconcile_findings(
        report["findings"], inventory, checks)
    report.update(checked_at=checked_at, inventory=inventory, advisory_checks=checks,
                  coverage=coverage_summary(inventory, checks, report),
                  source_checks_complete=not checks["errors"] and not resolution_errors,
                  dependabot={"open": dependabot, "new": new_branches})
    return report


def publish_report(config, state, report, previous, send_notifications, report_id=None):
    """Write the archive/latest report and apply the existing notification rules."""
    stamp = report_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    reports = state / "reports"
    atomic_json(reports / (stamp + ".json"), report)
    # JSON is authoritative; Markdown is a convenience copy of that report.
    rendered = markdown(report)
    atomic_text(reports / (stamp + ".md"), rendered)
    atomic_json(state / "latest.json", report)
    atomic_text(state / "latest.md", rendered)
    notified_path = state / "notified.json"
    stored_notified = json.loads(notified_path.read_text()) if notified_path.exists() else {}
    notified = canonical_notifications(stored_notified, previous.get("findings", []) + report["findings"])
    active = {finding_key(f) for f in report["findings"]}
    retained = {key: value for key, value in notified.items() if key in active}
    if retained != stored_notified:
        # A later recurrence must alert again, even at the same version.
        atomic_json(notified_path, retained)
    notified = retained
    alerts = changed_alerts(report["findings"], notified)
    notification_errors = (report["advisory_checks"].get("errors", [])
                           + report["advisory_checks"].get("resolution_errors", []))
    rejected = report.get("rejected_assessments", [])
    issues = coverage_issues(notification_errors, rejected)
    coverage_path = state / "coverage_notified.json"
    stored_issues = set()
    if send_notifications and coverage_path.exists():
        try:
            stored = json.loads(coverage_path.read_text())
            stored_issues = {item for item in stored if isinstance(item, str)} if isinstance(stored, list) else set()
        except (OSError, ValueError):
            stored_issues = set()
    new_issue_keys = sorted(set(issues) - stored_issues)
    new_issue_labels = [issues[key] for key in new_issue_keys]
    unchanged_issues = len(issues) - len(new_issue_keys)
    if send_notifications and (alerts or new_issue_keys or report["dependabot"]["new"]):
        body = notification_body(alerts, notification_errors, rejected, report["dependabot"],
                                 new_issue_labels=new_issue_labels,
                                 unchanged_issue_count=unchanged_issues)
        notify(config, body, urgent=any(f["priority"] == "act_now" for f in alerts))
        # Record delivery only after success. A manual run consumes no alerts.
        notified.update({finding_key(f): fingerprint(f) for f in alerts})
        atomic_json(notified_path, notified)
        # Branch and coverage-issue delivery is also recorded only after success.
        atomic_json(state / "dependabot.json",
                    [entry["branch"] for entry in report["dependabot"]["open"]])
        # Bounded dedup state; the report still lists every issue.
        atomic_json(coverage_path, sorted(issues)[:COVERAGE_ISSUE_LIMIT])
    elif send_notifications:
        # Forget issues that no longer occur; never consume an undelivered one.
        retained_issues = sorted(set(issues) & stored_issues)
        if retained_issues != sorted(stored_issues):
            atomic_json(coverage_path, retained_issues)
    cutoff = time.time() - config["retention_days"] * 86400
    for path in reports.iterdir():
        if re.fullmatch(r"\d{8}T\d{12}Z\.(?:json|md)", path.name) and path.stat().st_mtime < cutoff:
            path.unlink()
    print(f"Report: {reports / (stamp + '.md')}")
    print(f"Findings: {len(report['findings'])}; new/changed actionable: {len(alerts)}; source errors: {len(notification_errors)}; excluded claims: {len(rejected)}; dependabot new: {len(report['dependabot']['new'])}")
    return 2 if notification_errors or rejected else 0


def run(config, send_notifications):
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WatchError("Another security-watch run is already active") from None
        previous_path = state / "latest.json"
        previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
        inventory = collect_inventory(config)
        dependabot, dependabot_gap = collect_dependabot()
        # A branch is 'new' until a notification listing it was delivered;
        # manual runs and failed deliveries must not consume it.
        delivered_path = state / "dependabot.json"
        delivered = json.loads(delivered_path.read_text()) if delivered_path.exists() else []
        new_branches = [entry for entry in dependabot if entry["branch"] not in delivered]
        checks = advisory_checks(inventory)
        checks["errors"] += inventory.get("collection_errors", [])
        report = research(config, read_key(config["mistral_key_file"], "MISTRAL_API_KEY"), inventory, checks, previous)
        report = prepare_report(report, inventory, checks, dependabot, new_branches,
                                previous, now(), dependabot_gap)
        return publish_report(config, state, report, previous, send_notifications)


def batch_timeout_hours(config):
    value = config.get("batch_timeout_hours", BATCH_TIMEOUT_HOURS)
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= 168:
        raise WatchError("Invalid batch timeout_hours setting")
    return value


def batch_max_age_hours(config):
    value = config.get("batch_max_age_hours", BATCH_MAX_AGE_HOURS)
    if not isinstance(value, (int, float)) or isinstance(value, bool) or not 1 <= value <= 168:
        raise WatchError("Invalid batch max age setting")
    return float(value)


def batch_metadata(custom_id, snapshot_hash, submitted_at):
    metadata = {
        "tool": "security-watch",
        "custom_id": custom_id,
        "snapshot_hash": snapshot_hash,
        "submitted_at": submitted_at,
    }
    if len(metadata) > 32 or any(
            not isinstance(key, str) or not isinstance(value, str)
            or len(key) > 32 or len(value) > 512 for key, value in metadata.items()):
        raise WatchError("Batch metadata exceeds provider limits")
    return metadata


def batch_request_payload(agent_id, custom_id, content, metadata, timeout_hours):
    # Official CreateBatchJobRequest: endpoint + legacy agent_id are required;
    # requests[].body is the original /v1/conversations request body.
    return {
        "endpoint": BATCH_ENDPOINT,
        "agent_id": agent_id,
        "requests": [{
            "custom_id": custom_id,
            "body": {
                "agent_id": agent_id,
                "store": False,
                "inputs": [{"role": "user", "content": content}],
            },
        }],
        "metadata": metadata,
        "timeout_hours": timeout_hours,
    }


def batch_core(snapshot):
    keys = ("version", "submitted_at", "inventory", "checks", "dependabot",
            "dependabot_gap", "new_branches", "previous", "agent",
            "research_input", "timeout_hours")
    return {key: snapshot[key] for key in keys}


def batch_custom_id(core):
    return "sw-" + hashlib.sha256(canonical_json(core).encode()).hexdigest()[:32]


def batch_snapshot_hash(core, custom_id):
    return hashlib.sha256(canonical_json({**core, "custom_id": custom_id}).encode()).hexdigest()


def batch_snapshot(snapshot):
    core = batch_core(snapshot)
    custom_id = batch_custom_id(core)
    snapshot_hash = batch_snapshot_hash(core, custom_id)
    metadata = batch_metadata(custom_id, snapshot_hash, snapshot["submitted_at"])
    request = batch_request_payload(snapshot["agent"]["id"], custom_id,
                                    json.dumps(snapshot["research_input"]), metadata,
                                    snapshot["timeout_hours"])
    return {**core, "custom_id": custom_id, "snapshot_hash": snapshot_hash,
            "metadata": metadata, "request": request}


def verify_batch_snapshot(snapshot):
    if not isinstance(snapshot, dict):
        raise WatchError("Batch snapshot is not an object")
    required = {"version", "submitted_at", "inventory", "checks", "dependabot",
                "dependabot_gap", "new_branches", "previous", "agent",
                "research_input", "timeout_hours", "custom_id", "snapshot_hash",
                "metadata", "request"}
    if not required <= set(snapshot):
        raise WatchError("Batch snapshot fields are incomplete")
    core = batch_core(snapshot)
    custom_id = batch_custom_id(core)
    if snapshot["version"] != 1:
        raise WatchError("Batch snapshot version is invalid")
    if snapshot["custom_id"] != custom_id or not BATCH_CUSTOM_ID.fullmatch(custom_id):
        raise WatchError("Batch snapshot custom_id does not match its content")
    if snapshot["snapshot_hash"] != batch_snapshot_hash(core, custom_id):
        raise WatchError("Batch snapshot hash does not match its content")
    if not isinstance(snapshot["submitted_at"], str):
        raise WatchError("Batch snapshot timestamp is invalid")
    if not isinstance(snapshot["inventory"], dict) or not isinstance(snapshot["checks"], dict):
        raise WatchError("Batch snapshot inventory/checks are invalid")
    if not isinstance(snapshot["dependabot"], list) or not isinstance(snapshot["new_branches"], list):
        raise WatchError("Batch snapshot dependabot context is invalid")
    if not isinstance(snapshot["previous"], dict) or not isinstance(snapshot["research_input"], dict):
        raise WatchError("Batch snapshot previous context is invalid")
    if snapshot["dependabot_gap"] is not None and not isinstance(snapshot["dependabot_gap"], str):
        raise WatchError("Batch snapshot dependabot gap is invalid")
    if not isinstance(snapshot["timeout_hours"], int) or isinstance(snapshot["timeout_hours"], bool):
        raise WatchError("Batch snapshot timeout is invalid")
    agent = snapshot["agent"]
    if not isinstance(agent, dict) or not isinstance(agent.get("id"), str):
        raise WatchError("Batch snapshot agent settings are invalid")
    definition = agent.get("definition")
    if not isinstance(definition, dict) or not isinstance(definition.get("instructions"), str):
        raise WatchError("Batch snapshot agent definition is invalid")
    if agent.get("settings_hash") != sha256_text(canonical_json(definition)):
        raise WatchError("Batch snapshot agent settings hash is invalid")
    if agent.get("prompt_sha256") != sha256_text(definition["instructions"]):
        raise WatchError("Batch snapshot prompt hash is invalid")
    actual = agent.get("actual")
    if not isinstance(actual, dict) or actual.get("instructions_sha256") != agent["prompt_sha256"]:
        raise WatchError("Batch snapshot hosted agent prompt does not match")
    metadata = snapshot["metadata"]
    if (not isinstance(metadata, dict) or len(metadata) > 32
            or any(not isinstance(key, str) or not isinstance(value, str)
                   or len(key) > 32 or len(value) > 512 for key, value in metadata.items())):
        raise WatchError("Batch snapshot metadata is invalid")
    if (metadata.get("tool") != "security-watch"
            or metadata.get("custom_id") != custom_id
            or metadata.get("snapshot_hash") != snapshot["snapshot_hash"]
            or metadata.get("submitted_at") != snapshot["submitted_at"]):
        raise WatchError("Batch snapshot metadata does not match its content")
    request = snapshot["request"]
    if not isinstance(request, dict) or request.get("endpoint") != BATCH_ENDPOINT:
        raise WatchError("Batch snapshot request endpoint is invalid")
    if request.get("agent_id") != agent["id"]:
        raise WatchError("Batch snapshot request agent id is invalid")
    if request.get("timeout_hours") != snapshot["timeout_hours"]:
        raise WatchError("Batch snapshot request timeout is invalid")
    if request.get("metadata") != metadata:
        raise WatchError("Batch snapshot request metadata is invalid")
    requests = request.get("requests")
    if not isinstance(requests, list) or len(requests) != 1:
        raise WatchError("Batch snapshot must contain exactly one request")
    item = requests[0]
    if not isinstance(item, dict) or item.get("custom_id") != custom_id:
        raise WatchError("Batch snapshot request custom_id is invalid")
    body = item.get("body")
    if (not isinstance(body, dict) or body.get("agent_id") != agent["id"]
            or body.get("store") is not False):
        raise WatchError("Batch snapshot request body is invalid")
    if body.get("inputs") != [{"role": "user", "content": json.dumps(snapshot["research_input"])}]:
        raise WatchError("Batch snapshot request input does not match its content")
    return snapshot


def batch_paths(state, snapshot_hash):
    base = Path(state).expanduser() / "batch"
    return {
        "base": base,
        "active": base / "active.json",
        "snapshot": base / "snapshots" / (snapshot_hash + ".json"),
        "raw": base / "results" / (snapshot_hash + ".jsonl"),
        "inline": base / "results" / (snapshot_hash + ".outputs.json"),
        "report": base / "results" / (snapshot_hash + ".report.json"),
    }


def load_batch_active(state):
    path = Path(state).expanduser() / "batch" / "active.json"
    if not path.exists():
        return None
    try:
        active = json.loads(path.read_text())
    except (OSError, ValueError):
        raise WatchError("Batch state is unreadable; manual review required") from None
    if not isinstance(active, dict) or active.get("version") != 1:
        raise WatchError("Batch state is invalid; manual review required")
    if active.get("state") not in BATCH_PENDING_STATES | BATCH_RESOLVED_STATES:
        raise WatchError("Batch state is invalid; manual review required")
    for field in ("custom_id", "snapshot_hash", "snapshot_file", "submitted_at"):
        if not isinstance(active.get(field), str):
            raise WatchError("Batch state is incomplete; manual review required")
    if not BATCH_CUSTOM_ID.fullmatch(active["custom_id"]):
        raise WatchError("Batch state custom_id is invalid; manual review required")
    if not re.fullmatch(r"[0-9a-f]{64}", active["snapshot_hash"]):
        raise WatchError("Batch state snapshot hash is invalid; manual review required")
    if active["snapshot_file"] != f"snapshots/{active['snapshot_hash']}.json":
        raise WatchError("Batch state snapshot path is invalid; manual review required")
    if active["state"] in ("submitted", "finalizing") and not isinstance(active.get("job_id"), str):
        raise WatchError("Batch state job id is missing; manual review required")
    return active


def write_batch_active(state, active):
    active["updated_at"] = now()
    atomic_json(Path(state).expanduser() / "batch" / "active.json", active)


def load_batch_snapshot(state, active):
    path = batch_paths(state, active["snapshot_hash"])["snapshot"]
    if not path.exists():
        raise WatchError("Batch snapshot is missing; manual review required")
    try:
        snapshot = json.loads(path.read_text())
    except (OSError, ValueError):
        raise WatchError("Batch snapshot is unreadable; manual review required") from None
    verify_batch_snapshot(snapshot)
    if (snapshot["custom_id"] != active["custom_id"]
            or snapshot["snapshot_hash"] != active["snapshot_hash"]):
        raise WatchError("Batch snapshot does not match the active submission")
    return snapshot


def batch_age_hours(snapshot, at=None):
    try:
        submitted = datetime.fromisoformat(snapshot["submitted_at"])
    except (TypeError, ValueError):
        raise WatchError("Batch snapshot timestamp is invalid") from None
    if submitted.tzinfo is None:
        submitted = submitted.replace(tzinfo=timezone.utc)
    at = at or datetime.now(timezone.utc)
    if at.tzinfo is None:
        at = at.replace(tzinfo=timezone.utc)
    return max(0.0, (at - submitted).total_seconds() / 3600)


def batch_latest_allowed(state, snapshot, active):
    path = Path(state).expanduser() / "latest.json"
    previous = snapshot.get("previous")
    previous_checked = previous.get("checked_at") if isinstance(previous, dict) else None
    if not isinstance(previous_checked, str):
        previous_checked = None
    finalized = active.get("finalized_at")
    if not isinstance(finalized, str):
        finalized = None
    if not path.exists():
        return previous_checked is None
    try:
        latest = json.loads(path.read_text())
    except (OSError, ValueError):
        return False
    if not isinstance(latest, dict):
        return False
    current = latest.get("checked_at")
    if not isinstance(current, str):
        return False
    if previous_checked is not None and current == previous_checked:
        return True
    if finalized is not None and current == finalized:
        batch = latest.get("batch")
        return isinstance(batch, dict) and batch.get("report_id") == active.get("report_id")
    return False


def batch_report_id(value):
    try:
        moment = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise WatchError("Batch finalization timestamp is invalid") from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def batch_report_metadata(snapshot, active, finalized_at, age):
    return {
        "custom_id": snapshot["custom_id"],
        "snapshot_hash": snapshot["snapshot_hash"],
        "submitted_at": snapshot["submitted_at"],
        "collected_at": finalized_at,
        "snapshot_age_hours": round(age, 1),
        "job_id": active.get("job_id"),
        "timeout_hours": snapshot["timeout_hours"],
        "report_id": active.get("report_id"),
        "note": "Delayed asynchronous research against the submitted snapshot; not a live collection.",
    }


def batch_limitation(snapshot, age):
    return (f"Batch research is delayed: it used the inventory/advisory snapshot submitted at "
            f"{snapshot['submitted_at']} ({age:.1f} hours old at collection), not a live collection. "
            "Current state may differ; verify before acting.")


def batch_reconcile_hint(state, active):
    path = Path(state).expanduser() / "batch" / "active.json"
    return (f"Manual reconciliation required: list provider jobs with GET /v1/batch/jobs "
            f"(filters: agent_id, metadata, status[], created_after, created_by_me, "
            f"order_by=-created) and match custom_id={active.get('custom_id')} / "
            f"snapshot_hash={active.get('snapshot_hash')}. Then update {path} to state "
            "'submitted' with the job id to retry collection, or state 'failed' to abandon it. "
            "Do not resubmit blindly. Provider cleanup is manual: DELETE "
            "/v1/files/{file_id} and DELETE /v1/batch/jobs/{job_id}.")


def batch_invalid(state, active, message):
    active.update(state="invalid", error=message, invalid_at=now())
    write_batch_active(state, active)
    print("Batch result rejected; no report/latest/notification. " + message, file=sys.stderr)
    print(batch_reconcile_hint(state, active), file=sys.stderr)
    return 2


def batch_stale(state, active, snapshot, reason):
    active.update(state="stale", stale_reason=reason, stale_at=now())
    write_batch_active(state, active)
    # A crash after publication but before the 'collected' commit can leave an
    # already-published report under reports/; only latest.json is protected.
    print("Batch result is stale: " + reason + ". No report/latest/notification was written "
          "by this attempt (a previously published batch report may already exist under "
          "reports/). Any downloaded raw provider output remains under batch/results for "
          "audit; the provider job itself is not deleted. A new explicit --batch-submit "
          "requires manual review and archival of batch/active.json.", file=sys.stderr)
    return 2


def batch_job_counts_ok(job):
    for field, expected in (("total_requests", 1), ("succeeded_requests", 1), ("failed_requests", 0)):
        value = job.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value != expected:
            raise WatchError("Batch job did not report exactly one successful request")


def batch_job_status(job):
    status = job.get("status")
    if not isinstance(status, str) or status not in BATCH_STATUSES:
        raise WatchError("Batch status is unrecognized")
    return status


def batch_job_id(job):
    value = job.get("id")
    if not isinstance(value, str) or not BATCH_SAFE_ID.fullmatch(value):
        raise BatchSubmitUnknown("Batch submission response lacks a valid job id")
    return value


def batch_output_file(job):
    value = job.get("output_file")
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str) or not BATCH_SAFE_ID.fullmatch(value):
        raise BatchOutputInvalid("Batch job output file id is invalid")
    return value


def batch_output_row(value, custom_id):
    if not isinstance(value, dict):
        raise BatchOutputInvalid("Batch output row is not an object")
    if value.get("custom_id") != custom_id:
        raise BatchOutputInvalid("Batch output custom_id does not match the submitted request")
    if value.get("error") is not None:
        raise BatchOutputInvalid("Batch output contains a request error")
    response = value.get("response")
    if not isinstance(response, dict):
        raise BatchOutputInvalid("Batch output lacks a response object")
    if response.get("status_code") != 200:
        raise BatchOutputInvalid("Batch output request did not succeed")
    body = response.get("body")
    if not isinstance(body, dict):
        raise BatchOutputInvalid("Batch output body is not an object")
    return body


def batch_output_from_jsonl(raw, custom_id):
    try:
        text = raw.decode("utf-8")
    except UnicodeError:
        raise BatchOutputInvalid("Batch output is not valid UTF-8") from None
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) != 1:
        raise BatchOutputInvalid("Batch output must contain exactly one response")
    try:
        value = json.loads(lines[0])
    except ValueError:
        raise BatchOutputInvalid("Batch output is not valid JSONL") from None
    return batch_output_row(value, custom_id)


def batch_output_from_inline(outputs, custom_id):
    if not isinstance(outputs, list) or len(outputs) != 1:
        raise BatchOutputInvalid("Batch job must contain exactly one output")
    return batch_output_row(outputs[0], custom_id)


def batch_error_detail(error, limit=300):
    """Bounded provider error text; never echoes credentials or the key."""
    try:
        raw = error.read(2049)
    except Exception:
        return ""
    try:
        value = json.loads(raw)
        message = value.get("detail") if isinstance(value, dict) else ""
        if not message and isinstance(value, dict):
            message = value.get("message", "")
        if not isinstance(message, str):
            message = json.dumps(message)
    except (ValueError, UnicodeError):
        message = raw.decode("utf-8", "replace")
    return re.sub(r"[\x00-\x1f\x7f]+", " ", message)[:limit].strip()


def submit_batch_job(url, payload, key, timeout=BATCH_CONTROL_TIMEOUT):
    headers = {
        "Accept": "application/json",
        "User-Agent": "homelab-security-watch/1",
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }
    data = json.dumps(payload).encode()
    try:
        with build_opener(NoRedirect()).open(
                Request(url, data=data, headers=headers, method="POST"), timeout=timeout) as response:
            raw = response.read(8_000_001)
            if len(raw) > 8_000_000:
                raise BatchSubmitUnknown("Batch submission response exceeds size limit")
            try:
                result = json.loads(raw)
            except (ValueError, UnicodeError):
                raise BatchSubmitUnknown("Batch submission response is invalid JSON") from None
            if not isinstance(result, dict):
                raise BatchSubmitUnknown("Batch submission response is not an object")
            return result
    except HTTPError as error:
        detail = batch_error_detail(error)
        suffix = f": {detail}" if detail else ""
        # 402 is a definitive refusal: payment was declined before the job was
        # created, so it belongs with the definite rejections, not 'unknown'.
        if error.code in (400, 401, 402, 403, 404, 422):
            raise BatchSubmitRejected(f"Batch submission rejected: HTTP {error.code}{suffix}") from None
        raise BatchSubmitUnknown(f"Batch submission state unknown: HTTP {error.code}{suffix}") from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise BatchSubmitUnknown("Batch submission state unknown: network failure") from None


def download_bytes(url, key, timeout=BATCH_DOWNLOAD_TIMEOUT, limit=BATCH_OUTPUT_LIMIT):
    headers = {
        "Accept": "application/jsonl, application/octet-stream",
        "User-Agent": "homelab-security-watch/1",
        "Authorization": "Bearer " + key,
    }
    try:
        with build_opener(NoRedirect()).open(Request(url, headers=headers), timeout=timeout) as response:
            raw = response.read(limit + 1)
            if len(raw) > limit:
                raise WatchError("Batch output exceeds size limit")
            return raw
    except HTTPError as error:
        raise BatchDownloadError(f"{urlsplit(url).hostname}: HTTP {error.code}", status=error.code) from None
    except (URLError, TimeoutError, OSError, HTTPException):
        raise BatchDownloadError(f"{urlsplit(url).hostname}: network failure") from None


def batch_job_body(job, custom_id, key, state, snapshot_hash):
    outputs = job.get("outputs")
    if outputs is not None:
        # Inline results are authoritative when present. An empty/multiple/
        # malformed inline list is rejected rather than silently replaced by a
        # file that may describe a different or partial result.
        raw = (json.dumps(outputs, indent=2) + "\n").encode()
        atomic_bytes(batch_paths(state, snapshot_hash)["inline"], raw)
        return batch_output_from_inline(outputs, custom_id)
    file_id = batch_output_file(job)
    try:
        raw = download_bytes(MISTRAL + "/files/" + file_id + "/content", key)
    except BatchDownloadError as error:
        if error.status in (404, 410):
            raise BatchOutputInvalid("Batch output file is missing") from None
        raise
    atomic_bytes(batch_paths(state, snapshot_hash)["raw"], raw)
    return batch_output_from_jsonl(raw, custom_id)


def valid_batch_report(report, active):
    if not isinstance(report, dict):
        return False
    required = {"checked_at", "findings", "limitations", "checked_components",
                "summary", "searches", "usage", "dependabot", "advisory_checks",
                "coverage", "batch"}
    if not required <= set(report):
        return False
    if report.get("checked_at") != active.get("finalized_at"):
        return False
    if not isinstance(report.get("findings"), list) or not isinstance(report.get("limitations"), list):
        return False
    dependabot = report.get("dependabot")
    if (not isinstance(dependabot, dict) or not isinstance(dependabot.get("open"), list)
            or not isinstance(dependabot.get("new"), list)):
        return False
    if not isinstance(report.get("advisory_checks"), dict):
        return False
    batch = report.get("batch")
    return (isinstance(batch, dict)
            and batch.get("report_id") == active.get("report_id")
            and batch.get("snapshot_hash") == active.get("snapshot_hash")
            and batch.get("custom_id") == active.get("custom_id"))


def batch_submit(config):
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WatchError("Another security-watch run is already active") from None
        active = load_batch_active(state)
        if active is not None:
            # Single-use by design for the opt-in trial. Terminal states stay as
            # audit evidence and must be reviewed/archived manually before any
            # new paid submission.
            raise WatchError(
                f"A batch submission is already recorded in state {active['state']}; "
                f"review {state / 'batch' / 'active.json'} and archive it manually before "
                "another submission. No automatic queue/resubmit is performed.")
        previous_path = state / "latest.json"
        previous = json.loads(previous_path.read_text()) if previous_path.exists() else {}
        if not isinstance(previous, dict):
            raise WatchError("Previous report is invalid; refusing batch submission")
        key = read_key(config["mistral_key_file"], "MISTRAL_API_KEY")
        agent = configured_agent(config, key)
        definition = agent_definition(config)
        if not agent_matches(agent, definition):
            raise WatchError("Agent settings differ from tracked settings; run --setup-agent to apply them")
        if not isinstance(agent.get("id"), str) or not BATCH_SAFE_ID.fullmatch(agent["id"]):
            raise WatchError("Agent id is invalid; refusing batch submission")
        inventory = collect_inventory(config)
        dependabot, dependabot_gap = collect_dependabot()
        delivered_path = state / "dependabot.json"
        delivered = json.loads(delivered_path.read_text()) if delivered_path.exists() else []
        if not isinstance(delivered, list):
            raise WatchError("Dependabot state is invalid; refusing batch submission")
        new_branches = [entry for entry in dependabot if entry["branch"] not in delivered]
        checks = advisory_checks(inventory)
        checks["errors"] += inventory.get("collection_errors", [])
        research_input = research_payload(inventory, checks, previous)
        submitted_at = now()
        timeout_hours = batch_timeout_hours(config)
        agent_record = {
            "id": agent["id"],
            "definition": definition,
            "settings_hash": sha256_text(canonical_json(definition)),
            "prompt_sha256": sha256_text(definition["instructions"]),
            "actual": {
                "name": agent.get("name"),
                "model": agent.get("model"),
                "tools": agent.get("tools"),
                "completion_args": agent.get("completion_args"),
                "handoffs": agent.get("handoffs"),
                "instructions_sha256": sha256_text(str(agent.get("instructions", ""))),
            },
        }
        snapshot = batch_snapshot({
            "version": 1,
            "submitted_at": submitted_at,
            "inventory": inventory,
            "checks": checks,
            "dependabot": dependabot,
            "dependabot_gap": dependabot_gap,
            "new_branches": new_branches,
            "previous": previous,
            "agent": agent_record,
            "research_input": research_input,
            "timeout_hours": timeout_hours,
        })
        verify_batch_snapshot(snapshot)
        if len(json.dumps(snapshot["request"])) > BATCH_INPUT_BUDGET:
            raise WatchError("Batch research input exceeds budget; reduce inventory scope rather than silently truncate")
        paths = batch_paths(state, snapshot["snapshot_hash"])
        atomic_json(paths["snapshot"], snapshot)
        active = {
            "version": 1,
            "state": "submitting",
            "custom_id": snapshot["custom_id"],
            "snapshot_hash": snapshot["snapshot_hash"],
            "snapshot_file": f"snapshots/{snapshot['snapshot_hash']}.json",
            "submitted_at": submitted_at,
            "prepared_at": submitted_at,
            "timeout_hours": timeout_hours,
            "metadata": snapshot["metadata"],
            "latest_checked_at": previous.get("checked_at") if isinstance(previous.get("checked_at"), str) else None,
            "job_id": None,
            "request_hash": sha256_text(json.dumps(snapshot["request"])),
        }
        write_batch_active(state, active)
        try:
            job = submit_batch_job(MISTRAL + "/batch/jobs", snapshot["request"], key)
        except BatchSubmitRejected as error:
            active.update(state="rejected", error=str(error), attempted_at=now())
            write_batch_active(state, active)
            print("Batch submission rejected; no job was recorded. " + str(error), file=sys.stderr)
            return 2
        except BatchSubmitUnknown as error:
            active.update(state="unknown", error=str(error), attempted_at=now())
            write_batch_active(state, active)
            print("Batch submission state is UNKNOWN; do not resubmit. " + str(error), file=sys.stderr)
            print(batch_reconcile_hint(state, active), file=sys.stderr)
            return 2
        try:
            job_id = batch_job_id(job)
        except BatchSubmitUnknown as error:
            active.update(state="unknown", error=str(error), attempted_at=now())
            write_batch_active(state, active)
            print("Batch submission response was ambiguous; do not resubmit. " + str(error), file=sys.stderr)
            print(batch_reconcile_hint(state, active), file=sys.stderr)
            return 2
        job_status = job.get("status") if isinstance(job.get("status"), str) else "UNKNOWN"
        active.update(state="submitted", job_id=job_id, job_status=job_status,
                      accepted_at=now(), error=None)
        write_batch_active(state, active)
        print(f"Batch submitted: job {job_id}; custom_id {snapshot['custom_id']}; status {job_status}. "
              "No security result yet; run --batch-collect later.")
        return 0


def batch_collect(config, send_notifications):
    state = Path(config["state_dir"]).expanduser()
    state.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (state / "run.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise WatchError("Another security-watch run is already active") from None
        active = load_batch_active(state)
        if active is None:
            raise WatchError("No batch submission is recorded")
        status = active["state"]
        if status == "collected":
            print(f"Batch already collected: {active.get('report_path') or active.get('report_id')}; "
                  "no new report or notification.")
            return 0
        if status == "stale":
            print("Batch result is stale: " + str(active.get("stale_reason", "latest report changed"))
                  + ". No security result; no report/latest/notification. A new explicit "
                  "--batch-submit requires manual review and archival of batch/active.json.",
                  file=sys.stderr)
            return 2
        if status == "failed":
            print(f"Batch job ended with {active.get('job_status', 'a terminal failure')}; "
                  "no security result and no automatic resubmission.", file=sys.stderr)
            return 2
        if status == "rejected":
            print("Batch submission was rejected; no job exists. No security result. "
                  "A new explicit --batch-submit requires manual review and archival of "
                  "batch/active.json.", file=sys.stderr)
            return 2
        if status == "unknown":
            print("Batch submission state is unknown; no security result. "
                  + batch_reconcile_hint(state, active), file=sys.stderr)
            return 2
        if status == "invalid":
            print("Batch result failed local validation; no report/latest/notification. "
                  + str(active.get("error", "")) + " " + batch_reconcile_hint(state, active),
                  file=sys.stderr)
            return 2
        if status == "submitting":
            print("Batch submission was not confirmed; no security result. "
                  + batch_reconcile_hint(state, active), file=sys.stderr)
            return 2
        if status not in ("submitted", "finalizing"):
            raise WatchError("Batch state is invalid; manual review required")
        try:
            snapshot = load_batch_snapshot(state, active)
        except WatchError as error:
            return batch_invalid(state, active, "Batch snapshot integrity check failed: " + str(error))
        paths = batch_paths(state, active["snapshot_hash"])
        report = None
        job = None
        if status == "finalizing" and paths["report"].exists():
            try:
                loaded = json.loads(paths["report"].read_text())
            except (OSError, ValueError):
                return batch_invalid(state, active, "Stored batch report is unreadable")
            if not isinstance(loaded, dict):
                return batch_invalid(state, active, "Stored batch report is invalid")
            report = loaded
            if not isinstance(active.get("finalized_at"), str):
                checked = report.get("checked_at")
                if not isinstance(checked, str):
                    return batch_invalid(state, active, "Stored batch report lacks a finalization time")
                active["finalized_at"] = checked
            if not isinstance(active.get("report_id"), str):
                try:
                    active["report_id"] = batch_report_id(active["finalized_at"])
                except WatchError as error:
                    return batch_invalid(state, active, str(error))
            if not valid_batch_report(report, active):
                return batch_invalid(state, active, "Stored batch report does not match the active submission")
        if report is None:
            job_id = active.get("job_id")
            if not isinstance(job_id, str) or not BATCH_SAFE_ID.fullmatch(job_id):
                return batch_invalid(state, active, "Batch job id is missing or invalid")
            key = read_key(config["mistral_key_file"], "MISTRAL_API_KEY")
            try:
                # Inline results are the primary collection path; the output file
                # is only a fallback when the job returns outputs=null.
                job = request_json(MISTRAL + "/batch/jobs/" + job_id + "?inline=true",
                                   key=key, timeout=BATCH_CONTROL_TIMEOUT)
            except WatchError as error:
                active.update(last_error=str(error), last_error_at=now())
                write_batch_active(state, active)
                print("Batch status check failed: " + str(error)
                      + ". No report/latest/notification; retry --batch-collect later.",
                      file=sys.stderr)
                return 2
            if not isinstance(job, dict) or job.get("id") != job_id:
                active.update(last_error="Batch status response did not match the recorded job id",
                              last_error_at=now())
                write_batch_active(state, active)
                print("Batch status response did not match the recorded job id; "
                      "no report/latest/notification.", file=sys.stderr)
                return 2
            try:
                job_status = batch_job_status(job)
            except WatchError as error:
                active.update(last_error=str(error), last_error_at=now())
                write_batch_active(state, active)
                print("Batch status is unrecognized; no report/latest/notification. "
                      "Retry later or reconcile manually.", file=sys.stderr)
                return 2
            active.update(job_status=job_status, last_status_at=now(), last_error=None)
            write_batch_active(state, active)
            if job_status in BATCH_PENDING_STATUSES:
                print(f"Batch job {job_id} status: {job_status} — pending — not a security result. "
                      "No report/latest/notification written.")
                return 0
            if job_status in BATCH_FAILED_STATUSES:
                active.update(state="failed",
                              completed_at=job.get("completed_at") if isinstance(job.get("completed_at"), str) else None,
                              error="Batch job ended with status " + job_status)
                write_batch_active(state, active)
                print(f"Batch job {job_id} ended with {job_status}; no security result and "
                      "no automatic resubmission.", file=sys.stderr)
                return 2
            try:
                batch_job_counts_ok(job)
            except WatchError as error:
                active.update(last_error=str(error), last_error_at=now())
                write_batch_active(state, active)
                print("Batch job reports SUCCESS but not exactly one successful request; "
                      "no report/latest/notification. Retry later or reconcile manually.",
                      file=sys.stderr)
                return 2
            max_age = batch_max_age_hours(config)
            age = batch_age_hours(snapshot)
            if age > max_age:
                return batch_stale(state, active, snapshot,
                                   f"snapshot age {age:.1f}h exceeds the {max_age:g}h limit")
            try:
                body = batch_job_body(job, active["custom_id"], key, state, active["snapshot_hash"])
            except BatchOutputInvalid as error:
                return batch_invalid(state, active, "Batch output is invalid: " + str(error))
            except BatchDownloadError as error:
                active.update(last_error=str(error), last_error_at=now())
                write_batch_active(state, active)
                print("Batch output download failed: " + str(error)
                      + ". No report/latest/notification; retry --batch-collect later.",
                      file=sys.stderr)
                return 2
            try:
                research = research_response(body, snapshot["inventory"], snapshot["checks"])
            except WatchError as error:
                return batch_invalid(state, active, "Batch result failed local validation: " + str(error))
            if not batch_latest_allowed(state, snapshot, active):
                return batch_stale(state, active, snapshot,
                                   "latest report changed since the batch snapshot was submitted")
            if not isinstance(active.get("finalized_at"), str):
                active["finalized_at"] = now_precise()
            try:
                report_id = batch_report_id(active["finalized_at"])
            except WatchError as error:
                return batch_invalid(state, active, str(error))
            active.update(state="finalizing", report_id=report_id,
                          result_file=f"results/{active['snapshot_hash']}.report.json")
            write_batch_active(state, active)
            try:
                latest_for_merge = snapshot["previous"]
                latest_path = state / "latest.json"
                if latest_path.exists():
                    current_latest = json.loads(latest_path.read_text())
                    if isinstance(current_latest, dict):
                        latest_for_merge = current_latest
                report = prepare_report(research, snapshot["inventory"], snapshot["checks"],
                                        snapshot["dependabot"], snapshot["new_branches"],
                                        latest_for_merge, active["finalized_at"],
                                        snapshot["dependabot_gap"])
                age = batch_age_hours(snapshot, at=datetime.fromisoformat(active["finalized_at"]))
                report["batch"] = batch_report_metadata(snapshot, active, active["finalized_at"], age)
                report["limitations"].append(batch_limitation(snapshot, age))
                if isinstance(job, dict) and isinstance(job.get("errors"), list) and job["errors"]:
                    report["limitations"].append(
                        f"Batch job reported {len(job['errors'])} provider error(s); the single request succeeded.")
                if not valid_batch_report(report, active):
                    return batch_invalid(state, active, "Prepared batch report is internally inconsistent")
                atomic_json(paths["report"], report)
            except (WatchError, ValueError, KeyError, TypeError) as error:
                return batch_invalid(state, active, "Batch report preparation failed: " + str(error))
        try:
            age = batch_age_hours(snapshot, at=datetime.fromisoformat(active["finalized_at"]))
        except (WatchError, ValueError, TypeError) as error:
            return batch_invalid(state, active, "Batch finalization time is invalid: " + str(error))
        max_age = batch_max_age_hours(config)
        if age > max_age:
            return batch_stale(state, active, snapshot,
                               f"snapshot age {age:.1f}h exceeds the {max_age:g}h limit")
        if not batch_latest_allowed(state, snapshot, active):
            return batch_stale(state, active, snapshot,
                               "latest report changed since the batch snapshot was submitted")
        try:
            code = publish_report(config, state, report, snapshot["previous"], send_notifications,
                                  report_id=active["report_id"])
        except WatchError:
            # Keep 'finalizing' so a later collect can retry notification without
            # repaying for another batch job.
            raise
        active.update(state="collected", collected_at=now(),
                      report_path=str(state / "reports" / (active["report_id"] + ".md")))
        write_batch_active(state, active)
        return code


def deadline(*_):
    raise WatchError("Run exceeded 30-minute deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "security-watch.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--inventory", action="store_true", help="Print sanitized local/SSH inventory; no LLM/API calls or credential-value reads")
    modes.add_argument("--setup-agent", action="store_true", help="Create/reconcile only the dedicated Studio agent with tracked settings")
    modes.add_argument("--batch-submit", action="store_true", help="Opt-in: submit one asynchronous batch research job; no report or notification")
    modes.add_argument("--batch-collect", action="store_true", help="Opt-in: collect one submitted batch job; publish only a validated completed result")
    parser.add_argument("--notify", action="store_true", help="Notify ntfy about new/changed actionable findings, new/changed coverage issues, and failures")
    args = parser.parse_args()
    os.umask(0o077)
    config = None
    try:
        config = json.loads(args.config.read_text())
        if args.inventory:
            print(json.dumps(collect_inventory(config), indent=2))
            return 0
        if args.setup_agent:
            setup_agent(config, read_key(config["mistral_key_file"], "MISTRAL_API_KEY"))
            return 0
        # Bound the whole run as well as individual HTTP/probe operations.
        signal.signal(signal.SIGALRM, deadline)
        signal.alarm(1800)
        if args.batch_submit:
            return batch_submit(config)
        if args.batch_collect:
            return batch_collect(config, args.notify)
        return run(config, args.notify)
    except (WatchError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, WatchError) else f"Local operation/configuration error ({type(error).__name__})"
        print("Security watch failed: " + message, file=sys.stderr)
        # Batch modes are opt-in and must not emit a failure notification unless
        # an explicit successful collection is published with --notify.
        if args.notify and config and not (args.batch_submit or args.batch_collect):
            try:
                notify(config, "Security check FAILED; no all-clear result. " + message)
            except (WatchError, OSError, KeyError, TypeError):
                print("Failure notification could not be delivered", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
