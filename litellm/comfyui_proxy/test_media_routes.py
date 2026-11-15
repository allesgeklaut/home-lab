"""Offline lifecycle/media route tests; no Docker or live HTTP."""

import ast
import asyncio
import logging
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock


class AsyncContext:
    def __init__(self, value):
        self.value = value

    async def __aenter__(self):
        return self.value

    async def __aexit__(self, *args):
        pass


def routes():
    tree = ast.parse(Path(__file__).with_name("app.py").read_text())
    names = {"handle_view", "_queue_busy", "handle_recycle"}
    functions = [node for node in tree.body
                 if isinstance(node, ast.AsyncFunctionDef) and node.name in names]
    web = SimpleNamespace(
        Request=object, Response=object, StreamResponse=object,
        json_response=lambda data, status=200: SimpleNamespace(data=data, status=status),
        FileResponse=lambda path: SimpleNamespace(path=path, status=200),
    )
    ns = {"web": web, "os": os, "COMFYUI_CONTAINER": "comfyui",
          "UPSTREAM": "http://upstream", "PortainerContainers": object,
          "log": logging.getLogger(__name__)}
    exec(compile(ast.Module(body=functions, type_ignores=[]), "route-tests", "exec"), ns)
    return ns


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.ns = routes()
        self.pc = SimpleNamespace(is_running=AsyncMock(return_value=True),
                                  stop_and_wait=AsyncMock(return_value=True))
        self.app = {"portainer": self.pc, "inflight": 1, "start_lock": asyncio.Lock()}
        self.req = SimpleNamespace(app=self.app, json=AsyncMock(return_value={}))

    async def test_recycle_does_not_stop_busy_queue(self):
        self.ns["_queue_busy"] = AsyncMock(return_value=True)
        result = await self.ns["handle_recycle"](self.req)
        self.assertEqual(result.status, 409)
        self.pc.stop_and_wait.assert_not_awaited()

    async def test_recycle_does_not_stop_with_other_request_inflight(self):
        self.app["inflight"] = 2
        result = await self.ns["handle_recycle"](self.req)
        self.assertEqual(result.status, 409)
        self.pc.stop_and_wait.assert_not_awaited()

    async def test_recycle_passes_only_own_prompt_for_queue_check(self):
        self.req.json.return_value = {"prompt_id": "mine"}
        self.ns["_queue_busy"] = AsyncMock(return_value=False)
        result = await self.ns["handle_recycle"](self.req)
        self.assertEqual(result.status, 200)
        self.ns["_queue_busy"].assert_awaited_once_with("mine")
        self.pc.stop_and_wait.assert_awaited_once_with("comfyui")

    async def test_queue_busy_excludes_only_own_job(self):
        for queued, own, expected in [
            (["mine"], "mine", False), (["mine", "other"], "mine", True),
            (["mine"], None, True), ([], None, False),
        ]:
            response = SimpleNamespace(status=200, json=AsyncMock(return_value={
                "queue_running": [], "queue_pending": [[0, pid] for pid in queued]}))
            session = SimpleNamespace(get=lambda _: AsyncContext(response))
            self.ns["aiohttp"] = SimpleNamespace(
                ClientTimeout=lambda **kw: None,
                ClientSession=lambda **kw: AsyncContext(session))
            self.assertEqual(await self.ns["_queue_busy"](own), expected)

    async def test_queue_check_failure_is_busy(self):
        def fail(**kwargs):
            raise OSError("unreachable")
        self.ns["aiohttp"] = SimpleNamespace(ClientTimeout=lambda **kw: None, ClientSession=fail)
        self.assertTrue(await self.ns["_queue_busy"]("mine"))

    async def test_cold_view_serves_only_files_within_output(self):
        self.pc.is_running.return_value = False
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "output"
            root.mkdir()
            outside = Path(directory) / "secret"
            outside.write_bytes(b"not output")
            (root / "clip.mp4").write_bytes(b"video")
            (root / "link").symlink_to(outside)
            self.ns["OUTPUT_DIR"] = str(root)
            for query, expected in [
                ({"filename": "clip.mp4"}, 200),
                ({"filename": "clip.mp4", "type": "input"}, 404),
                ({"filename": "../secret"}, 404),
                ({"filename": "link"}, 404),
                ({"filename": "missing"}, 404),
                ({"filename": "secret", "subfolder": str(outside.parent)}, 404),
            ]:
                self.req.query = query
                self.assertEqual((await self.ns["handle_view"](self.req)).status, expected)


if __name__ == "__main__":
    unittest.main()
