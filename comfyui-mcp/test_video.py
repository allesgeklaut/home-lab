"""Offline video orchestration regression tests: no MCP SDK, GPU or HTTP."""

import ast
import copy
import glob
import hashlib
import io
import json
import os
from pathlib import Path
import random
import shutil
import tempfile
import threading
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import urllib.parse
import uuid


ROOT = Path(__file__).resolve().parent


def builders():
    tree = ast.parse((ROOT / "server.py").read_text())
    functions = [node for node in tree.body if isinstance(node, ast.FunctionDef)]
    for node in functions:
        node.decorator_list = []
    namespace = {
        "os": os, "glob": glob, "hashlib": hashlib, "json": json,
        "shutil": shutil, "uuid": uuid, "urllib": urllib, "io": io,
        "random": random,
        "Image": lambda **kwargs: SimpleNamespace(**kwargs),
        "INLINE_IMAGE_BYTES": 700_000,
        "VIDEO_WORKFLOW": str(ROOT / "workflows/ltx25_distilled_t2v_api.json"),
        "VIDEO_PREFIX": "mcp-video",
        "LTX_ENCODER": "gemma4-12b-with-proj-ltx-2.5-comfy-int8-convrot.safetensors",
        "LTX_HQ_VIDEO_VAE": "ltx-2.5-video-vae-bf16.safetensors",
        "LTX_UPSCALER": "ltx-2.5-latent-spatial-upscaler-x2-bf16-1.0.safetensors",
        "OUTPUT_HOST_DIR": "/host/output", "OUTPUT_DIR": "/output",
        "INPUT_HOST_DIR": "/host/input", "INPUT_DIR": "/input",
        "PUBLIC_URL": "http://media:8189",
        "_VIDEO_LOCK": threading.Lock(),
        "_LONG_CLIP_PIXELS": {"draft": 512 * 512},
        "_MAX_PIXELS": 1024 * 1024,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), str(ROOT / "server.py"), "exec"), namespace)
    return namespace


class VideoTests(unittest.TestCase):
    def setUp(self):
        self.ns = builders()

    def test_draft_and_staged_i2v_graph_links(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                graph = self.ns["_video_graph"](
                    "test", 1024, 1024, 49, 123, "cached.safetensors", staged,
                    image_name="source.png")
                self.assertNotIn("2", graph)  # no encoder in the video pass
                self.assertNotIn("53", graph)  # the HQ VAE encodes the first frame
                self.assertEqual(graph["52"]["class_type"], "LTXVImgToVideoInplace")
                self.assertEqual(graph["52"]["inputs"]["latent"], ["5", 0])
                self.assertEqual(graph["52"]["inputs"]["vae"], ["15", 0])
                self.assertEqual(graph["52"]["inputs"]["strength"], 0.7)
                self.assertEqual(graph["51"]["inputs"]["img_compression"], 18)
                self.assertEqual(graph["18"]["inputs"]["video_latent"], ["52", 0])
                self.assertEqual(graph["13"]["inputs"]["positive"], ["6", 0])
                self.assertEqual(graph["5"]["inputs"]["width"], 512 if staged else 1024)
                if staged:
                    self.assertEqual(graph["32"]["class_type"], "LTXVImgToVideoInplace")
                    self.assertEqual(graph["32"]["inputs"]["latent"], ["22", 0])
                    self.assertEqual(graph["32"]["inputs"]["strength"], 1.0)
                    self.assertEqual(graph["23"]["inputs"]["video_latent"], ["32", 0])
                else:
                    self.assertNotIn("32", graph)
                for node in graph.values():
                    for value in node["inputs"].values():
                        if isinstance(value, list) and len(value) == 2:
                            self.assertIn(value[0], graph)

    def test_phase_output_paths_and_recycle_order(self):
        for staged in (False, True):
            with self.subTest(staged=staged):
                ns = self.ns
                events, phases = [], []
                ns["_conditioning_cached"] = lambda _: False
                ns["_ensure_conditioning"] = lambda *_: events.append("encode") or True
                ns["_recycle"] = lambda: events.append("recycle")
                ns["_upload_image_file"] = lambda _: "source.png"
                ns["_copy_latent"] = lambda entry, node, run, label: f"{run}_{label}.latent"
                ns["_cleanup_latents"] = Mock()
                ns["_video_result"] = lambda entry, note: {"note": note}

                def submit(graph, **kwargs):
                    events.append("render")
                    phases.append(copy.deepcopy(graph))
                    return {}

                ns["_submit_and_wait"] = submit
                ns["_generate_video"]("test", staged, 1024, 1024, 49, 123, "final",
                                      image_path="/input/source.png")
                expected = ["recycle", "encode", "recycle", "render"]
                if staged:
                    expected += ["recycle", "render"] * 3
                    ns["_cleanup_latents"].assert_called_once()
                self.assertEqual(events, expected)
                self.assertEqual(len(phases), 4 if staged else 1)
                for graph in phases:
                    for node in graph.values():
                        prefix = node["inputs"].get("filename_prefix")
                        if prefix:
                            self.assertTrue(prefix.startswith("mcp-video/"), prefix)
                        for value in node["inputs"].values():
                            if isinstance(value, list) and len(value) == 2:
                                self.assertIn(value[0], graph)
                self.assertEqual(phases[-1]["12"]["class_type"], "SaveVideo")
                self.assertIn("41", phases[-1])  # poster is generated too

    def test_video_result_host_paths_and_encoded_media_url(self):
        entry = {"outputs": {
            "12": {"images": [{"filename": "test clip.mp4", "subfolder": "mcp-video"}]},
            "41": {"images": [{"filename": "poster.png", "subfolder": "mcp-video"}]},
        }}
        result = self.ns["_video_result"](entry, "done")
        self.assertEqual(result["video_host"], "/host/output/mcp-video/test clip.mp4")
        self.assertEqual(result["poster"], "/output/mcp-video/poster.png")
        query = urllib.parse.parse_qs(urllib.parse.urlparse(result["video_url"]).query)
        self.assertEqual(query["filename"], ["test clip.mp4"])
        self.assertEqual(query["subfolder"], ["mcp-video"])

    def test_cleanup_keeps_other_runs_and_failed_run_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs, staged = root / "input", root / "mcp-video/staged"
            inputs.mkdir()
            staged.mkdir(parents=True)
            for folder in (inputs, staged):
                (folder / "mine_video.latent").touch()
                (folder / "other_video.latent").touch()
            self.ns.update(INPUT_DIR=str(inputs), OUTPUT_DIR=str(root))
            self.ns["_cleanup_latents"]("mine")
            for folder in (inputs, staged):
                self.assertFalse((folder / "mine_video.latent").exists())
                self.assertTrue((folder / "other_video.latent").exists())

    def test_cancel_only_interrupts_own_running_job_and_deletes_own_queue_entry(self):
        for running in ("mine", "other"):
            calls = []
            self.ns["_get_json"] = lambda _: {"queue_running": [[0, running]]}
            self.ns["_post_json"] = lambda path, body, **kw: calls.append((path, body))
            self.ns["_recycle"] = Mock()
            self.ns["_cancel_job"]("mine")
            self.assertIn(("/queue", {"delete": ["mine"]}), calls)
            self.assertEqual(("/interrupt", {}) in calls, running == "mine")
            self.assertFalse(any(body.get("clear") for _, body in calls))
            self.ns["_recycle"].assert_called_once_with(prompt_id="mine")

    def test_large_inline_preview_is_bounded_and_preserves_alpha_and_original(self):
        from PIL import Image

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "noise.png"
            Image.frombytes("RGBA", (1024, 1024), os.urandom(1024 * 1024 * 4)).save(path)
            original = path.read_bytes()
            preview = self.ns["_inline_still"](str(path))
            self.assertLessEqual(len(preview.data), 700_000)
            with Image.open(io.BytesIO(preview.data)) as image:
                self.assertEqual(image.mode, "RGBA")
            self.assertEqual(path.read_bytes(), original)

    def test_image_tools_refuse_to_overlap_video_before_submission(self):
        self.ns["_VIDEO_LOCK"].acquire()
        self.ns["_generate_once"] = Mock()
        with self.assertRaisesRegex(RuntimeError, "another generation"):
            self.ns["generate_image"]("test", seed=1)
        self.ns["_generate_once"].assert_not_called()

    def test_conditioning_intermediate_removed_after_successful_copy(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output, embeddings = root / "output", root / "embeddings"
            (output / "mcp-video/conditioning").mkdir(parents=True)
            intermediate = output / "mcp-video/conditioning/mcp_run_00001_.safetensors"
            intermediate.write_bytes(b"cond")
            self.ns.update(OUTPUT_DIR=str(output), EMBEDDINGS_DIR=str(embeddings),
                           uuid=SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="run")))
            self.ns["_conditioning_cached"] = lambda _: False
            self.ns["_submit_and_wait"] = lambda graph, **kwargs: None
            self.assertTrue(self.ns["_ensure_conditioning"]("prompt", "cache.safetensors"))
            self.assertEqual((embeddings / "cache.safetensors").read_bytes(), b"cond")
            self.assertFalse(intermediate.exists(), "Redundant intermediate kept"
                             " after the embeddings copy succeeded")

    def test_conditioning_intermediate_kept_when_copy_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "output"
            (output / "mcp-video/conditioning").mkdir(parents=True)
            intermediate = output / "mcp-video/conditioning/mcp_run_00001_.safetensors"
            intermediate.write_bytes(b"cond")
            self.ns.update(OUTPUT_DIR=str(output), EMBEDDINGS_DIR=str(root / "missing"),
                           uuid=SimpleNamespace(uuid4=lambda: SimpleNamespace(hex="run")))
            self.ns["_conditioning_cached"] = lambda _: False
            self.ns["_submit_and_wait"] = lambda graph, **kwargs: None
            with patch("shutil.copy2", side_effect=OSError("disk full")):
                with self.assertRaises(OSError):
                    self.ns["_ensure_conditioning"]("prompt", "cache.safetensors")
            self.assertTrue(intermediate.exists(), "Failed-copy intermediate deleted")

    def test_host_paths_roundtrip_through_narrow_mounts(self):
        resolve = self.ns["_local_image_path"]
        self.assertEqual(resolve("/host/output/mcp-video/poster.png"),
                         "/output/mcp-video/poster.png")
        self.assertEqual(resolve("/host/input/photo.png"), "/input/photo.png")
        for path in ("/host/output-other/secret", "/host/output/../secret", "/input/photo.png"):
            self.assertEqual(resolve(path), path)

    def test_aspect_presets_and_alignment(self):
        dimensions = self.ns["_video_dimensions"]
        for quality, square, landscape in [
            ("draft", (512, 512), (512, 288)),
            ("final", (1024, 1024), (1024, 576)),
        ]:
            self.assertEqual(dimensions(quality, None, "1:1"), square)
            self.assertEqual(dimensions(quality, None, "16:9"), landscape)
            self.assertEqual(dimensions(quality, None, "9:16"), landscape[::-1])
            for size in range(256, 1025, 64):
                width, height = dimensions(quality, size, "16:9")
                alignment = 64 if quality == "final" else 32
                self.assertEqual(width % alignment, 0)
                self.assertEqual(height % alignment, 0)
                self.assertLessEqual(width * height, 1024 ** 2)
        with self.assertRaisesRegex(RuntimeError, "aspect_ratio"):
            dimensions("draft", 512, "arbitrary")

    def test_video_length_cap_and_long_clip_pixel_budget(self):
        frames = self.ns["_video_frames"]
        self.assertEqual(frames(0.5), 9)
        self.assertEqual(frames(4), 97)
        self.assertEqual(frames(10), 241)
        self.assertEqual(frames(30), 241)  # clamped to the 10 s cap
        for seconds in (0.5, 1, 2, 3.4, 6, 8, 10):
            self.assertEqual(frames(seconds) % 8, 1)
        check = self.ns["_check_long_clip"]
        check(6, "draft", 1024, 1024)  # 6 s still fits the short-clip budget
        check(10, "draft", 512, 512)
        check(10, "final", 1024, 1024)  # measured: 484 s, 3.8 GiB RAM free
        check(10, "final", 1024, 576)
        check(10, "final", 768, 768)
        for quality, width, height in [
            ("draft", 768, 768), ("draft", 512, 544), ("draft", 1024, 1024),
        ]:
            with self.assertRaisesRegex(RuntimeError, "or smaller"):
                check(10, quality, width, height)

    def test_official_1280_geometry_and_pixel_budget(self):
        dimensions = self.ns["_video_dimensions"]
        self.assertEqual(dimensions("final", 1280, "16:9"), (1280, 704))
        self.assertEqual(dimensions("final", 1280, "9:16"), (704, 1280))
        self.assertEqual(dimensions("final", 1024, "1:1"), (1024, 1024))
        with self.assertRaisesRegex(RuntimeError, "1 MP"):
            dimensions("final", 1280, "1:1")
        with self.assertRaisesRegex(RuntimeError, "256..1280"):
            dimensions("final", 1344, "16:9")

    def test_decode_vae_matches_official_template(self):
        for staged in (False, True):
            graph = self.ns["_video_graph"](
                "test", 512, 512, 49, 123, "cached.safetensors", staged)
            self.assertEqual(graph["15"]["inputs"]["vae_name"], self.ns["LTX_HQ_VIDEO_VAE"])
        # I2V encodes the first frame with the same HQ VAE, as the official
        # template does.
        i2v = self.ns["_video_graph"](
            "test", 512, 512, 49, 123, "cached.safetensors", False,
            image_name="source.png")
        self.assertEqual(i2v["15"]["inputs"]["vae_name"], self.ns["LTX_HQ_VIDEO_VAE"])

    def test_decode_tiling_matches_official_template(self):
        graph = self.ns["_video_graph"](
            "test", 512, 512, 49, 123, "cached.safetensors", False)
        self.assertEqual(
            graph["10"]["inputs"],
            {"samples": ["19", 0], "vae": ["15", 0], "tile_size": 512,
             "overlap": 64, "temporal_size": 64, "temporal_overlap": 16},
        )

    def test_landscape_i2v_staged_base_and_decode_dimensions(self):
        graph = self.ns["_video_graph"](
            "test", 1024, 576, 49, 123, "cached.safetensors", True,
            image_name="source.png")
        self.assertEqual(graph["5"]["inputs"]["width"], 512)
        self.assertEqual(graph["5"]["inputs"]["height"], 288)
        self.assertEqual(graph["52"]["inputs"]["vae"], ["15", 0])
        self.assertEqual(graph["32"]["inputs"]["latent"], ["22", 0])
        self.assertEqual(graph["23"]["inputs"]["video_latent"], ["32", 0])

    def test_i2v_source_is_scaled_to_official_1536_longer_edge(self):
        from PIL import Image as PILImage

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "source.png")
            PILImage.new("RGB", (2000, 1000), "red").save(source)
            prepared = self.ns["_prepare_i2v_image"](source)
            try:
                with PILImage.open(prepared) as image:
                    self.assertEqual(image.size, (1536, 768))
                    self.assertEqual(image.mode, "RGB")
            finally:
                os.remove(prepared)

    def test_i2v_tool_prepares_source_and_cleans_up(self):
        from PIL import Image as PILImage

        with tempfile.TemporaryDirectory() as tmp:
            source = os.path.join(tmp, "portrait.png")
            PILImage.new("RGB", (64, 64), "red").save(source)
            for fail in (True, False):
                with self.subTest(fail=fail):
                    prepared_paths, calls = [], []

                    def prepare(path):
                        self.assertEqual(path, source)
                        fd, prepared = tempfile.mkstemp(suffix=".png")
                        os.close(fd)
                        prepared_paths.append(prepared)
                        return prepared

                    def generate(prompt, staged, width, height, frames, seed,
                                 label, image_path=None, strength=0.7,
                                 source_name=None):
                        calls.append({
                            "image_path": image_path, "strength": strength,
                            "source_name": source_name,
                        })
                        if fail:
                            raise RuntimeError("boom")
                        return {"note": "ok"}

                    ns = self.ns
                    ns["_prepare_i2v_image"] = prepare
                    ns["_generate_video"] = generate
                    ns["_format_video_result"] = lambda result: result
                    if fail:
                        with self.assertRaisesRegex(RuntimeError, "boom"):
                            ns["generate_video_from_image"](
                                source, "test", seconds=2.0)
                    else:
                        self.assertEqual(
                            ns["generate_video_from_image"](
                                source, "test", seconds=2.0),
                            {"note": "ok"},
                        )
                    self.assertEqual(len(prepared_paths), 1)
                    self.assertFalse(os.path.exists(prepared_paths[0]))
                    self.assertEqual(calls[0]["source_name"], "portrait.png")
                    self.assertEqual(calls[0]["image_path"], prepared_paths[0])
                    self.assertEqual(calls[0]["strength"], 0.7)
                    self.assertFalse(ns["_VIDEO_LOCK"].locked())


if __name__ == "__main__":
    unittest.main()
