import copy
import importlib.util
import json
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import Mock
import urllib.error
import urllib.request

spec = importlib.util.spec_from_file_location("gateway_app", Path(__file__).with_name("app.py"))
app = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = app
spec.loader.exec_module(app)

CID = "a" * 64


class GatewayTests(unittest.TestCase):
    def setUp(self):
        data = json.loads(Path(__file__).with_name("policy.example.json").read_text())
        for name, c in data["clients"].items():
            c["token"] = (name + "-" + "x" * 40)
        self.data = data
        self.policy = app.Policy(data)
        self.upstream = Mock()
        self.gateway = app.Gateway(self.policy, self.upstream)
        self.inspect = {"Id": CID, "Name": "/comfyui", "State": {"Running": True},
                        "Config": {"Env": ["SECRET=must-not-leak"]}, "Mounts": ["private"]}
        self.upstream.request.return_value = self.inspect

    def call(self, method, path, who="dashboard"):
        return self.gateway.dispatch(self.data["clients"][who]["token"], method, path)

    def denied(self, method, path, who="dashboard", status=403):
        self.upstream.reset_mock()
        with self.assertRaises(app.Failure) as cm:
            self.call(method, path, who)
        self.assertEqual(cm.exception.status, status)
        self.upstream.request.assert_not_called()

    def test_authentication_required(self):
        for token in ("", "invalid", "\u2603", "a" * 257):
            with self.assertRaises(app.Failure) as cm:
                self.gateway.dispatch(token, "GET", "/api/endpoints")
            self.assertEqual(cm.exception.status, 401)
        self.upstream.request.assert_not_called()

    def test_credentials_independent_and_placeholders_rejected(self):
        for kind in ("duplicate", "placeholder", "short", "profile"):
            data = copy.deepcopy(self.data)
            if kind == "duplicate":
                data["clients"]["dashboard"]["token"] = data["clients"]["litellm"]["token"]
            elif kind == "profile":
                data["clients"]["litellm"]["profile"] = "admin"
            else:
                data["clients"]["dashboard"]["token"] = "REPLACE" + "x" * 40 if kind == "placeholder" else "short"
            with self.assertRaises(ValueError):
                app.Policy(data)

    def test_environment_discovery_fixed_not_autoexpanded(self):
        self.assertEqual([e["Id"] for e in self.call("GET", "/api/endpoints")[1]], [3, 4, 7])
        self.assertEqual([e["Id"] for e in self.call("GET", "/api/endpoints", "litellm")[1]], [3])
        self.upstream.request.assert_not_called()

    def test_dashboard_all_containers_all_approved_environments(self):
        for eid in (3, 4, 7):
            for action in ("start", "stop", "restart"):
                with self.subTest(eid=eid, action=action):
                    self.assertEqual(self.call("POST", f"/api/endpoints/{eid}/docker/containers/any-container/{action}"), (204, None))
                    self.upstream.request.assert_called_with("POST", f"/api/endpoints/{eid}/docker/containers/{CID}/{action}")

    def test_gpu_restrictions(self):
        for who in ("litellm", "comfyui-proxy"):
            for path in ("/api/endpoints/4/docker/containers/comfyui/start",
                         "/api/endpoints/7/docker/containers/comfyui/stop",
                         "/api/endpoints/3/docker/containers/portainer/start",
                         "/api/endpoints/3/docker/containers/comfyui/restart",
                         f"/api/endpoints/3/docker/containers/{CID}/start"):
                self.denied("POST", path, who)
            self.denied("GET", "/api/endpoints/3/docker/containers/json?all=true", who)

    def test_gpu_start_stop_and_sanitized_state(self):
        for name in self.policy.gpu_containers:
            self.upstream.request.return_value = {**self.inspect, "Name": "/" + name}
            for who in ("litellm", "comfyui-proxy"):
                for action in ("start", "stop"):
                    self.assertEqual(self.call("POST", f"/api/endpoints/3/docker/containers/{name}/{action}", who), (204, None))
                self.assertEqual(self.call("GET", f"/api/endpoints/3/docker/containers/{name}/json", who),
                                 (200, {"Id": CID, "State": {"Running": True}}))

    def test_no_general_proxy_paths_queries_bodies_or_encodings(self):
        paths = ["/api/users/me", "/api/settings", "/api/endpoints/99/docker/containers/comfyui/start",
                 "/api/endpoints/3/docker/containers/create", "/api/endpoints/3/docker/containers/comfyui/exec",
                 "/api/endpoints/3/docker/containers/comfyui/start?anything=true",
                 "/api/endpoints/3/docker/containers/comfyui/start/", "/api/endpoints/3/docker/containers/%63omfyui/start",
                 "/api/endpoints/3/docker/containers/../start", "https://evil.test/api/endpoints",
                 "/api/endpoints/3/docker/containers/comfyui/start#ignored"]
        for path in paths:
            self.denied("POST", path)
        for verb in ("DELETE", "PATCH", "PUT", "GET"):
            self.denied(verb, "/api/endpoints/3/docker/containers/comfyui/start")

    def test_oversized_environment_id_is_denied_without_integer_conversion(self):
        for suffix in ("json?all=true", "comfyui/start"):
            verb = "GET" if suffix.startswith("json") else "POST"
            self.denied(verb, "/api/endpoints/" + "9" * 5000 + "/docker/containers/" + suffix)

    def test_list_is_projected_and_infrastructure_marked_not_hidden(self):
        self.upstream.request.return_value = [{"Id": CID, "Names": ["/portainer"], "Image": "portainer:test",
            "State": "running", "Status": "Up", "Ports": [{"PrivatePort": 9443, "Type": "tcp", "IP": "private"}],
            "Labels": {"secret": "private"}, "Mounts": ["private"], "Env": ["SECRET=value"]}]
        status, data = self.call("GET", "/api/endpoints/4/docker/containers/json?all=true")
        self.assertEqual(status, 200)
        self.assertTrue(data[0]["LifecycleCritical"])
        self.assertEqual(set(data[0]), {"Id", "Names", "Image", "State", "Status", "Ports", "LifecycleCritical"})
        self.assertNotIn("private", json.dumps(data))

    def test_invalid_upstream_identity_never_mutates(self):
        for cid in ("short", "../escape", "b" * 63):
            self.upstream.reset_mock()
            self.upstream.request.return_value = {**self.inspect, "Id": cid}
            with self.assertRaises(app.Failure):
                self.call("POST", "/api/endpoints/3/docker/containers/comfyui/start")
            self.assertEqual(self.upstream.request.call_count, 1)

    def test_gpu_name_must_match_inspection(self):
        self.upstream.request.return_value = {**self.inspect, "Name": "/portainer"}
        with self.assertRaises(app.Failure):
            self.call("POST", "/api/endpoints/3/docker/containers/comfyui/start", "litellm")
        self.assertEqual(self.upstream.request.call_count, 1)


class HTTPTests(unittest.TestCase):
    def setUp(self):
        GatewayTests.setUp(self)
        self.server = app.Server(("127.0.0.1", 0), self.gateway, None)
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        self.thread.start()
        self.addCleanup(self.thread.join, 2)
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)

    def test_handler_rejects_payload_and_duplicate_auth(self):
        import http.client
        for headers in (("Content-Length", "2"), ("Transfer-Encoding", "chunked"), ("X-API-Key", "duplicate")):
            c = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=2)
            c.putrequest("POST", "/api/endpoints/3/docker/containers/comfyui/start")
            c.putheader("X-API-Key", self.data["clients"]["dashboard"]["token"])
            c.putheader(*headers)
            c.endheaders()
            r = c.getresponse()
            self.assertEqual(r.status, 403)
            r.read()
            c.close()
        self.upstream.request.assert_not_called()

    def test_upstream_exception_is_not_disclosed(self):
        self.upstream.request.side_effect = RuntimeError("SECRET=must-not-leak")
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/api/endpoints/3/docker/containers/json?all=true",
                                     headers={"X-API-Key": self.data["clients"]["dashboard"]["token"]})
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=2)
        self.assertEqual(cm.exception.code, 502)
        self.assertNotIn(b"SECRET", cm.exception.read())

    def test_wire_response_has_only_approved_metadata(self):
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/api/endpoints",
                                     headers={"X-API-Key": self.data["clients"]["dashboard"]["token"]})
        with urllib.request.urlopen(req, timeout=2) as r:
            self.assertEqual(r.headers["Cache-Control"], "no-store")
            self.assertEqual([e["Id"] for e in json.load(r)], [3, 4, 7])
        self.upstream.request.assert_not_called()

    def test_denial_log_contains_no_token_or_untrusted_path(self):
        secret_path = "do-not-log-me"
        token = self.data["clients"]["dashboard"]["token"]
        req = urllib.request.Request(f"http://127.0.0.1:{self.server.server_port}/{secret_path}",
                                     headers={"X-API-Key": token})
        with self.assertLogs(app.LOG, level="WARNING") as logs:
            with self.assertRaises(urllib.error.HTTPError) as cm:
                urllib.request.urlopen(req, timeout=2)
        self.assertEqual(cm.exception.code, 403)
        self.assertIn("caller=dashboard", logs.output[0])
        self.assertNotIn(token, logs.output[0])
        self.assertNotIn(secret_path, logs.output[0])


if __name__ == "__main__":
    unittest.main()
