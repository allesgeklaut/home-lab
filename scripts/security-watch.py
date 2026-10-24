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
    "postgres": ["postgres", "--version"],
    "redis": ["redis-server", "--version"],
    "valkey": ["valkey-server", "--version"],
    "cloudflared": ["cloudflared", "--version"],
    "adguardhome": ["/opt/adguardhome/AdGuardHome", "--version"],
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


class WatchError(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None  # Never forward authentication to a redirect target.


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


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
        except (URLError, TimeoutError, OSError):
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
    host = parsed.hostname.lower()
    try:
        ipaddress.ip_address(host)
        return False  # Reports should cite public named advisory sites, not IPs.
    except ValueError:
        return "." in host and not host.endswith((".local", ".internal", ".localhost"))


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
        "components": [], "packages": [],
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
                for package, package_version in packages.items():
                    if version(package_version):
                        inventory["packages"].append({"component": name, "ecosystem": "PyPI", "name": package, "version": package_version, "source": "runtime Python metadata"})
                if name in packages:
                    component.update(version=packages[name], version_source="runtime Python metadata")
            except WatchError:
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
            inventory["coverage_gaps"] += [alias + ": " + gap for gap in remote["coverage_gaps"]]
        except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
            message = alias + ": SSH inventory failed (connection, authentication, host-key, timeout or collector error)."
            inventory["collection_errors"].append(message)
            inventory["coverage_gaps"].append(message)
    return inventory


def advisory_checks(inventory):
    result = {"checked_at": now(), "osv_matches": [], "kev_candidates": [], "errors": []}
    packages = inventory["packages"]
    try:
        queries = [{"package": {"name": p["name"], "ecosystem": p["ecosystem"]}, "version": p["version"]} for p in packages]
        response = request_json(OSV, {"queries": queries}, retry=True) if queries else {"results": []}
        if len(response["results"]) != len(packages):
            raise WatchError("OSV returned incomplete results")
        for package, matches in zip(packages, response["results"]):
            for match in matches.get("vulns", []):
                identifier = match["id"]
                if not re.fullmatch(r"[A-Za-z0-9_.-]{1,150}", identifier):
                    raise WatchError("OSV returned an invalid advisory id")
                result["osv_matches"].append({**package, "advisory": identifier, "url": "https://osv.dev/vulnerability/" + identifier})
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
            # GLM's hosted tools do not reliably execute with forced JSON
            # decoding. Request JSON in the prompt and validate it locally.
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


def validate_report(report, inventory, allowed_urls):
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
        if not isinstance(sources, list) or not 1 <= len(sources) <= 8 or not all(isinstance(url, str) and url in allowed_urls for url in sources):
            raise WatchError("Agent finding lacks a source from the actual search/advisory results")
        key = finding_key(finding)
        if key in seen:
            raise WatchError("Agent returned duplicate findings")
        seen.add(key)
    return report


def validated_findings(report, inventory, allowed_urls):
    # Reject malformed reports, but do not throw away sound findings because
    # one assessment overclaims. Rejected claims cannot alert or resolve issues.
    if not isinstance(report, dict) or not isinstance(report.get("findings"), list) or len(report["findings"]) > 25:
        raise WatchError("Agent returned an invalid report")
    validate_report({**report, "findings": []}, inventory, allowed_urls)
    accepted, rejected, seen = [], [], set()
    for finding in report["findings"]:
        try:
            validate_report({**report, "findings": [finding]}, inventory, allowed_urls)
            key = finding_key(finding)
            if key in seen:
                raise WatchError("Duplicate assessment")
            seen.add(key)
            accepted.append(finding)
        except WatchError as error:
            rejected.append({
                "component": str(finding.get("component", "unknown"))[:100] if isinstance(finding, dict) else "unknown",
                "advisory": str(finding.get("advisory", "unknown"))[:150] if isinstance(finding, dict) else "unknown",
                "reason": str(error),
            })
    if rejected and not accepted:
        raise WatchError("All agent assessments failed local validation")
    report["findings"] = accepted
    report["rejected_assessments"] = rejected
    if rejected:
        # Do not display a model summary that may repeat the rejected claims.
        report["summary"] = f"Research returned {len(accepted)} accepted assessments; {len(rejected)} claims were excluded by local validation. Coverage is partial."
        report["limitations"] += [f"Excluded {item['component']} / {item['advisory']}: {item['reason']}" for item in rejected]
    return report


def research(config, key, inventory, checks, previous):
    agent = configured_agent(config, key)
    if not agent_matches(agent, agent_definition(config)):
        raise WatchError("Agent settings differ from tracked settings; run --setup-agent to apply them")
    payload = {
        # Agent conversations reject completion_args overrides. Define and
        # verify the budget/output mode/tools on the agent before every run.
        "agent_id": agent["id"], "store": False,
        "inputs": [{"role": "user", "content": json.dumps({
            "as_of_utc": now(), "inventory": inventory, "advisory_checks": checks,
            "valid_component_ids": sorted(component_versions(inventory)),
            "response_schema": REPORT_SCHEMA,
            "previous_findings": previous.get("findings", []),
            "previous_report_at": previous.get("checked_at", "first run"),
        })}],
    }
    if len(json.dumps(payload)) > 180_000:
        raise WatchError("Research input exceeds budget; reduce inventory scope rather than silently truncate")
    # No automatic retry: a timed-out research POST may still be running/billed.
    response = request_json(MISTRAL + "/conversations", payload, key, config["timeout_seconds"])
    outputs = response.get("outputs", [])
    searches = [entry for entry in outputs if entry.get("type") == "tool.execution" and entry.get("name") in ("web_search", "web_search_premium")]
    if not searches:
        raise WatchError("Agent did not perform a web search; report rejected")
    messages = [entry for entry in outputs if entry.get("type") == "message.output"]
    content = messages[-1].get("content", []) if messages else []
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
    references.update(match["url"] for match in checks["osv_matches"])
    if checks.get("kev_source"):
        references.add(checks["kev_source"])
    report = validated_findings(report, inventory, references)
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


def finding_key(finding):
    return finding["component"] + ":" + finding["advisory"].upper()


def merge_findings(previous, current, complete, checked_components=None):
    merged = {finding_key(f): {**f, "not_rechecked": True} for f in previous}
    for finding in current:
        key = finding_key(finding)
        if finding["status"] == "not_affected":
            # Partial checks must never dismiss an existing finding.
            if (complete and finding["confidence"] == "high"
                    and (checked_components is None or finding["component"] in checked_components)):
                merged.pop(key, None)
            continue
        merged[key] = finding
    return list(merged.values())


def fingerprint(finding):
    # Exclude prose, ordering and timestamps: wording drift must not spam ntfy.
    fields = ("priority", "status", "installed_version", "fixed_version", "exploited")
    return hashlib.sha256(json.dumps([finding[field] for field in fields]).encode()).hexdigest()


def changed_alerts(findings, notified):
    return [f for f in findings if not f.get("not_rechecked") and f["priority"] in ("act_now", "update_soon")
            and notified.get(finding_key(f)) != fingerprint(f)]


def notification_body(alerts, errors, rejected):
    lines = [f"{len(alerts)} new/changed actionable findings. Full report: ~/.local/state/security-watch/latest.md"]
    ordered = sorted(alerts, key=lambda f: (ENUMS["priority"].index(f["priority"]), finding_key(f)))
    notices = []
    if errors:
        notices.append(f"Incomplete advisory checks: {len(errors)} errors; inspect the local report.")
    if rejected:
        notices.append(f"Partial report: {len(rejected)} unverified claims excluded; inspect limitations.")
    shown = 0
    for finding in ordered[:8]:
        line = f"{finding['priority']}: {finding['component']} / {finding['advisory']} ({finding['status']}); fixed: {finding['fixed_version'][:60]}"
        # Reserve space for an overflow count even when IDs are long/Unicode.
        reserve = [f"+ {len(ordered)} additional findings in the local report."]
        if len("\n".join(lines + [line] + reserve + notices).encode()) > 3500:
            break
        lines.append(line)
        shown += 1
    if shown < len(ordered):
        lines.append(f"+ {len(ordered) - shown} additional findings in the local report.")
    return "\n".join(lines + notices)


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
    lines = ["# Security watch", "", f"Checked: {report['checked_at']}", "", report["summary"], "",
             "Advisory synthesis, not a full vulnerability scan or an all-clear certificate.", "", "## Findings"]
    for finding in report["findings"]:
        stale = " — NOT RECHECKED" if finding.get("not_rechecked") else ""
        lines += ["", f"### {finding['priority'].upper()}: {finding['component']} / {finding['advisory']}{stale}",
                  finding["title"], f"Status: {finding['status']}; confidence: {finding['confidence']}; exploited: {finding['exploited']}",
                  f"Installed: {finding['installed_version']}; affected: {finding['affected_versions']}; fixed: {finding['fixed_version']}",
                  finding["reason"], "Action: " + finding["action"], *["- " + url for url in finding["sources"]]]
    if not report["findings"]:
        lines.append("No actionable findings identified within the checked sources; see coverage limits below.")
    excluded = [f for f in report.get("assessments", []) if f["status"] == "not_affected"]
    if excluded:
        lines += ["", "## Assessed as not affected (review evidence)"]
        for finding in excluded:
            lines += ["", f"### {finding['component']} / {finding['advisory']}", finding["reason"],
                      "Advice: " + finding["action"], *["- " + url for url in finding["sources"]]]
    lines += ["", "## Coverage and limitations", *["- " + item for item in report["limitations"]], "",
              f"Web search executions: {report['searches']}; usage: {json.dumps(report['usage'])}"]
    return "\n".join(lines) + "\n"


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
        checks = advisory_checks(inventory)
        checks["errors"] += inventory.get("collection_errors", [])
        report = research(config, read_key(config["mistral_key_file"], "MISTRAL_API_KEY"), inventory, checks, previous)
        unchecked = set(component_versions(inventory)) - set(report["checked_components"])
        report["limitations"] += inventory["coverage_gaps"] + checks["errors"]
        if unchecked:
            report["limitations"].append("Not researched this run: " + ", ".join(sorted(unchecked)))
        rejected = report.get("rejected_assessments", [])
        # Unrelated unchecked components do not invalidate explicit, sourced
        # resolutions for checked components. Source/validation failures do.
        complete = not checks["errors"] and not rejected
        report["assessments"] = report["findings"]  # Preserve explicit not-affected evidence in the archive.
        report["findings"] = merge_findings(previous.get("findings", []), report["findings"], complete, set(report["checked_components"]))
        report.update(checked_at=now(), inventory=inventory, advisory_checks=checks, source_checks_complete=not checks["errors"])
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        reports = state / "reports"
        atomic_json(reports / (stamp + ".json"), report)
        # JSON is authoritative; Markdown is a convenience copy of that report.
        rendered = markdown(report)
        atomic_text(reports / (stamp + ".md"), rendered)
        atomic_json(previous_path, report)
        atomic_text(state / "latest.md", rendered)
        notified_path = state / "notified.json"
        notified = json.loads(notified_path.read_text()) if notified_path.exists() else {}
        active = {finding_key(f) for f in report["findings"]}
        retained = {key: value for key, value in notified.items() if key in active}
        if retained != notified:
            # A later recurrence must alert again, even at the same version.
            atomic_json(notified_path, retained)
        notified = retained
        alerts = changed_alerts(report["findings"], notified)
        if send_notifications and (alerts or checks["errors"] or rejected):
            body = notification_body(alerts, checks["errors"], rejected)
            notify(config, body, urgent=any(f["priority"] == "act_now" for f in alerts))
            # Record delivery only after success. A manual run consumes no alerts.
            notified.update({finding_key(f): fingerprint(f) for f in alerts})
            atomic_json(notified_path, notified)
        cutoff = time.time() - config["retention_days"] * 86400
        for path in reports.iterdir():
            if re.fullmatch(r"\d{8}T\d{12}Z\.(?:json|md)", path.name) and path.stat().st_mtime < cutoff:
                path.unlink()
        print(f"Report: {reports / (stamp + '.md')}")
        print(f"Findings: {len(report['findings'])}; new/changed actionable: {len(alerts)}; source errors: {len(checks['errors'])}; excluded claims: {len(rejected)}")
        return 2 if checks["errors"] or rejected else 0


def deadline(*_):
    raise WatchError("Run exceeded 15-minute deadline")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=ROOT / "security-watch.json")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--inventory", action="store_true", help="Print sanitized local/SSH inventory; no LLM/API calls or credential-value reads")
    modes.add_argument("--setup-agent", action="store_true", help="Create/reconcile only the dedicated Studio agent with tracked settings")
    parser.add_argument("--notify", action="store_true", help="Notify ntfy about new/changed actionable findings and failures")
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
        signal.alarm(900)
        return run(config, args.notify)
    except (WatchError, OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        message = str(error) if isinstance(error, WatchError) else f"Local operation/configuration error ({type(error).__name__})"
        print("Security watch failed: " + message, file=sys.stderr)
        if args.notify and config:
            try:
                notify(config, "Security check FAILED; no all-clear result. " + message)
            except (WatchError, OSError, KeyError, TypeError):
                print("Failure notification could not be delivered", file=sys.stderr)
        return 2
    finally:
        signal.alarm(0)


if __name__ == "__main__":
    sys.exit(main())
