import asyncio
import os
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
from gpu_containers import PortainerContainers


class GatewayClientTests(unittest.TestCase):
    def setUp(self):
        self.env = {
            "LIFECYCLE_URL": "https://lifecycle-gateway:8443",
            "LIFECYCLE_API_KEY": "restricted-dummy",
            "LIFECYCLE_ENV_ID": "3",
            "LIFECYCLE_CA_FILE": "/fixture/gateway.pem",
        }

    def client(self):
        with patch.dict(os.environ, self.env, clear=True), \
                patch("gpu_containers.ssl.create_default_context") as context:
            client = PortainerContainers()
        context.assert_called_once_with(cafile="/fixture/gateway.pem")
        self.assertEqual(client._headers, {"X-API-Key": "restricted-dummy"})
        self.assertEqual(client.env_id, "3")
        return client

    def test_gateway_configuration_needs_no_admin_token(self):
        self.client()

    def test_insecure_or_ambiguous_origin_rejected(self):
        for url in ("http://gateway", "https://user:pass@gateway", "https://gateway/path", "https://gateway?x=1"):
            with patch.dict(os.environ, {**self.env, "LIFECYCLE_URL": url}, clear=True):
                with self.assertRaises(ValueError):
                    PortainerContainers()

    def test_missing_credentials_or_certificate_do_not_fallback(self):
        for missing in ("LIFECYCLE_API_KEY", "LIFECYCLE_ENV_ID", "LIFECYCLE_CA_FILE"):
            env = dict(self.env)
            del env[missing]
            with patch.dict(os.environ, env, clear=True):
                with self.assertRaises(KeyError):
                    PortainerContainers()

    def test_missing_certificate_fails_closed(self):
        with patch.dict(os.environ, self.env, clear=True):
            with self.assertRaises(FileNotFoundError):
                PortainerContainers()

    def test_operations_use_gateway_and_disable_redirects(self):
        client = self.client()
        response = Mock(status=200, content_length=None)
        response.json = AsyncMock(return_value={"State": {"Running": True}})
        cm = AsyncMock()
        cm.__aenter__.return_value = response
        session = Mock()
        session.get.return_value = cm
        session.post.return_value = cm
        session_cm = AsyncMock()
        session_cm.__aenter__.return_value = session
        with patch("gpu_containers.aiohttp.ClientSession", return_value=session_cm):
            self.assertTrue(asyncio.run(client.is_running("comfyui")))
            self.assertTrue(asyncio.run(client.start("comfyui")))
            self.assertTrue(asyncio.run(client.stop("comfyui")))
        calls = session.get.call_args_list + session.post.call_args_list
        for call in calls:
            self.assertTrue(call.args[0].startswith("https://lifecycle-gateway:8443/api/endpoints/3/"))
            self.assertFalse(call.kwargs["allow_redirects"])
            self.assertEqual(call.kwargs["headers"], {"X-API-Key": "restricted-dummy"})

    def test_outage_is_conservative_and_no_direct_fallback(self):
        client = self.client()
        with patch("gpu_containers.aiohttp.ClientSession", side_effect=OSError):
            self.assertTrue(asyncio.run(client.is_running("comfyui")))
            self.assertFalse(asyncio.run(client.start("comfyui")))
            self.assertFalse(asyncio.run(client.stop("comfyui")))


if __name__ == "__main__":
    unittest.main()
