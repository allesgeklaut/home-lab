"""Small, deny-by-default lifecycle API. Never a general Portainer proxy."""
from __future__ import annotations

import hashlib
import hmac
import http.client
import json
import logging
import os
from pathlib import Path
import re
import ssl
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

LOG = logging.getLogger("lifecycle")
IDENTIFIER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,127}\Z")
FULL_ID = re.compile(r"[0-9a-f]{64}\Z")
ROUTE = re.compile(r"/api/endpoints/([1-9][0-9]{0,9})/docker/containers/([A-Za-z0-9_.-]+)/(json|start|stop|restart)\Z")
LIST = re.compile(r"/api/endpoints/([1-9][0-9]{0,9})/docker/containers/json\?all=true\Z")
MAX_RESPONSE = 4 * 1024 * 1024


class Failure(Exception):
    def __init__(self, status: int):
        self.status = status


class Policy:
    def __init__(self, data: dict):
        self.environments = {int(k): str(v) for k, v in data["environments"].items()}
        self.gpu_environment = int(data["gpu_environment"])
        self.gpu_containers = frozenset(data["gpu_containers"])
        self.critical_containers = frozenset(data["critical_containers"])
        self.clients = data["clients"]
        if not self.environments or self.gpu_environment not in self.environments:
            raise ValueError("Invalid environment policy")
        for name in self.gpu_containers | self.critical_containers:
            if not isinstance(name, str) or not IDENTIFIER.fullmatch(name):
                raise ValueError("Invalid container name")
        tokens = []
        for name, client in self.clients.items():
            token = client["token"]
            if client["profile"] not in ("dashboard", "gpu") or not isinstance(token, str):
                raise ValueError("Invalid client policy")
            if len(token) < 32 or not token.isascii() or token.startswith("REPLACE"):
                raise ValueError("Provision independent random client tokens")
            tokens.append(token)
        if not tokens or len(tokens) != len(set(tokens)):
            raise ValueError("Client tokens must be independent")

    def authenticate(self, token: str):
        if not token or len(token) > 256 or not token.isascii():
            raise Failure(401)
        for name, client in self.clients.items():
            if hmac.compare_digest(token, client["token"]):
                return name, client["profile"]
        raise Failure(401)


class Portainer:
    """Fixed HTTPS destination; exact certificate pin before sending the admin key."""
    def __init__(self, url: str, key: str, certificate: str):
        parsed = urlsplit(url)
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username
                or parsed.password or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError("Portainer must be a fixed HTTPS origin")
        if not key:
            raise ValueError("Missing Portainer credential")
        pem = Path(certificate).read_text(encoding="ascii")
        if pem.count("-----BEGIN CERTIFICATE-----") != 1 or pem.count("-----END CERTIFICATE-----") != 1:
            raise ValueError("Pin exactly one Portainer server certificate")
        self.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).digest()
        self.context = ssl.create_default_context(cafile=certificate)
        self.context.check_hostname = False  # exact leaf pin replaces hostname matching
        self.host, self.port, self.key = parsed.hostname, parsed.port or 443, key

    def request(self, method: str, path: str):
        connection = http.client.HTTPSConnection(self.host, self.port, timeout=12, context=self.context)
        try:
            connection.connect()
            peer = connection.sock.getpeercert(binary_form=True)
            if not hmac.compare_digest(hashlib.sha256(peer).digest(), self.fingerprint):
                raise Failure(502)
            connection.request(method, path, headers={"X-API-Key": self.key, "Accept": "application/json"})
            response = connection.getresponse()
            if response.status == 404:
                raise Failure(404)
            if method == "POST" and response.status in (200, 204, 304):
                return None
            if response.status != 200:  # Includes redirects: never follow or relay upstream errors.
                raise Failure(502)
            body = response.read(MAX_RESPONSE + 1)
            if len(body) > MAX_RESPONSE:
                raise Failure(502)
            return json.loads(body)
        finally:
            connection.close()


class Gateway:
    def __init__(self, policy: Policy, upstream):
        self.policy, self.upstream = policy, upstream

    def dispatch(self, token: str, method: str, path: str):
        caller, profile = self.policy.authenticate(token)
        if method == "GET" and path == "/api/endpoints":
            ids = self.policy.environments if profile == "dashboard" else [self.policy.gpu_environment]
            return 200, [{"Id": eid, "Name": self.policy.environments[eid]} for eid in ids]
        listing = LIST.fullmatch(path) if method == "GET" else None
        route = ROUTE.fullmatch(path)
        if listing:
            eid = int(listing[1])
            container = None
            operation = "list"
        elif route and ((method == "GET" and route[3] == "json")
                        or (method == "POST" and route[3] in ("start", "stop", "restart"))):
            eid, container, operation = int(route[1]), route[2], route[3]
        else:
            raise Failure(403)
        if eid not in self.policy.environments:
            raise Failure(403)
        if profile == "gpu" and (eid != self.policy.gpu_environment or operation in ("list", "restart")
                                  or container not in self.policy.gpu_containers):
            raise Failure(403)
        if listing:
            data = self.upstream.request("GET", f"/api/endpoints/{eid}/docker/containers/json?all=true")
            return 200, [self.summary(c) for c in data]
        if not IDENTIFIER.fullmatch(container):
            raise Failure(403)
        # Resolve once and operate on the FULL immutable ID, not a name or short ID.
        info = self.upstream.request("GET", f"/api/endpoints/{eid}/docker/containers/{container}/json")
        cid = info["Id"]
        if not isinstance(cid, str) or not FULL_ID.fullmatch(cid):
            raise Failure(502)
        if profile == "gpu" and info.get("Name") != "/" + container:
            raise Failure(403)
        if operation == "json":
            return 200, {"Id": cid, "State": {"Running": bool(info["State"]["Running"])}}
        self.upstream.request("POST", f"/api/endpoints/{eid}/docker/containers/{cid}/{operation}")
        LOG.info("caller=%s environment=%s container=%s action=%s outcome=success", caller, eid, cid, operation)
        return 204, None

    def summary(self, c):
        # Explicit projection: never return Config.Env, labels, mounts or raw inspect.
        names = [str(n) for n in c.get("Names", [])]
        return {
            "Id": c["Id"], "Names": names, "Image": str(c.get("Image", "")),
            "State": str(c.get("State", "")), "Status": str(c.get("Status", "")),
            "Ports": [{k: p[k] for k in ("PrivatePort", "PublicPort", "Type") if k in p}
                      for p in c.get("Ports", [])],
            "LifecycleCritical": any(n.lstrip("/") in self.policy.critical_containers for n in names),
        }


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, gateway, context):
        self.gateway, self.context = gateway, context
        self.slots = threading.BoundedSemaphore(16)
        super().__init__(address, Handler)

    def process_request(self, request, address):
        if not self.slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, address)
        except Exception:
            self.slots.release()
            raise

    def process_request_thread(self, request, address):
        try:
            request.settimeout(15)
            # Handshake in the bounded worker, not the accept loop.
            if self.context is not None:
                request = self.context.wrap_socket(request, server_side=True)
            super().process_request_thread(request, address)
        except (OSError, ssl.SSLError):
            self.shutdown_request(request)
        finally:
            self.slots.release()


class Handler(BaseHTTPRequestHandler):
    server_version = "Lifecycle"

    def log_message(self, *_args):
        pass  # Only structured mutations/denials; never arbitrary URLs/headers.

    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (OSError, ssl.SSLError):
            self.close_connection = True

    def respond(self):
        self.close_connection = True
        try:
            if (len(self.headers.get_all("X-API-Key", [])) != 1
                    or self.headers.get("Transfer-Encoding") is not None
                    or self.headers.get("Content-Length", "0") != "0"):
                raise Failure(403)
            status, data = self.server.gateway.dispatch(self.headers["X-API-Key"], self.command, self.path)
        except Failure as exc:
            status, data = exc.status, {"error": "request denied" if exc.status in (401, 403) else "upstream unavailable"}
        except Exception:
            status, data = 502, {"error": "upstream unavailable"}
        if status in (401, 403):
            caller = "unauthenticated"
            try:
                caller, _profile = self.server.gateway.policy.authenticate(self.headers.get("X-API-Key", ""))
            except Failure:
                pass
            shape = "endpoints" if self.path == "/api/endpoints" else "other"
            if LIST.fullmatch(self.path):
                shape = "container-list"
            elif ROUTE.fullmatch(self.path):
                shape = "container-operation"
            method = self.command if self.command in ("GET", "POST", "PUT", "DELETE", "PATCH") else "other"
            LOG.warning("caller=%s method=%s route=%s status=%s outcome=denied", caller, method, shape, status)
        body = json.dumps(data).encode() if data is not None else b""
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    do_GET = respond
    do_POST = respond
    do_PUT = respond
    do_DELETE = respond
    do_PATCH = respond


def main():
    policy = Policy(json.loads(Path(os.environ["LIFECYCLE_POLICY_FILE"]).read_text()))
    upstream = Portainer(os.environ["PORTAINER_URL"], os.environ["PORTAINER_API_KEY"], os.environ["PORTAINER_CA_FILE"])
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(os.environ["LIFECYCLE_TLS_CERT"], os.environ["LIFECYCLE_TLS_KEY"])
    logging.basicConfig(level=logging.INFO)
    Server(("0.0.0.0", 8443), Gateway(policy, upstream), context).serve_forever()


if __name__ == "__main__":
    main()
