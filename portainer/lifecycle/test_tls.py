"""Actual TLS checks against disposable localhost fixtures, never live Portainer."""
import json
from pathlib import Path
import shutil
import ssl
import subprocess
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from test_app import app


class Fixture(BaseHTTPRequestHandler):
    def do_GET(self):
        self.server.seen.append(self.headers.get("X-API-Key"))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/secret-target")
            body = b""
        else:
            self.send_response(200)
            body = json.dumps({"ok": True}).encode()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *_args):
        pass


@unittest.skipUnless(shutil.which("openssl"), "requires openssl for TLS fixtures")
class TLSTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.certs = []
        for name in ("trusted", "other"):
            cert, key = (Path(cls.tmp.name) / (name + ext) for ext in (".pem", ".key"))
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes",
                            "-keyout", str(key), "-out", str(cert), "-days", "1",
                            "-subj", "/CN=portainer.test", "-addext", "subjectAltName=DNS:portainer.test"],
                           check=True, capture_output=True)
            cls.certs.append((cert, key))

    def server(self, index):
        cert, key = self.certs[index]
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert, key)
        server = ThreadingHTTPServer(("127.0.0.1", 0), Fixture)
        server.seen = []
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(thread.join, 2)
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return server, f"https://127.0.0.1:{server.server_port}"

    def test_trusted_exact_certificate_works_with_legacy_san(self):
        server, url = self.server(0)
        client = app.Portainer(url, "dummy-admin-key", str(self.certs[0][0]))
        self.assertEqual(client.request("GET", "/fixture"), {"ok": True})
        self.assertEqual(server.seen, ["dummy-admin-key"])

    def test_other_certificate_never_receives_admin_key(self):
        server, url = self.server(1)
        client = app.Portainer(url, "dummy-admin-key", str(self.certs[0][0]))
        with self.assertRaises(ssl.SSLError):
            client.request("GET", "/fixture")
        self.assertEqual(server.seen, [])

    def test_exact_pin_enforced_even_if_chain_is_trusted(self):
        server, url = self.server(1)
        client = app.Portainer(url, "dummy-admin-key", str(self.certs[0][0]))
        client.context.load_verify_locations(cafile=str(self.certs[1][0]))
        with self.assertRaises(app.Failure):
            client.request("GET", "/fixture")
        self.assertEqual(server.seen, [])

    def test_no_redirects(self):
        server, url = self.server(0)
        client = app.Portainer(url, "dummy-admin-key", str(self.certs[0][0]))
        with self.assertRaises(app.Failure):
            client.request("GET", "/redirect")
        self.assertEqual(len(server.seen), 1)

    def test_invalid_origin_and_missing_credentials_fail_closed(self):
        for url in ("http://portainer:9000", "https://name:pass@portainer", "https://portainer/path",
                    "https://portainer?override=1", "https://portainer#fragment"):
            with self.assertRaises(ValueError):
                app.Portainer(url, "dummy", str(self.certs[0][0]))
        with self.assertRaises(ValueError):
            app.Portainer("https://portainer", "", str(self.certs[0][0]))


if __name__ == "__main__":
    unittest.main()
