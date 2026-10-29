"""Offline edit-graph regression tests; no MCP SDK, GPU, or server required."""

import ast
import copy
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / "webui/workflows"
EDIT_FILES = sorted(WORKFLOWS.glob("qwen_image_2_1*edit_api.json"))


def load_cleaner():
    spec = importlib.util.spec_from_file_location(
        "prompt_clean", ROOT / "litellm/comfyui_proxy/prompt_clean.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.strip_unused_references


def load_builder(path, name="_edit_once"):
    # Exercise the real builder without importing/registering the MCP server.
    source = ast.parse((ROOT / "comfyui-mcp/server.py").read_text())
    names = {"_load_workflow", "_node_by_class", "_load_image_nodes", "_apply_sampler", name}
    functions = [n for n in source.body if isinstance(n, ast.FunctionDef) and n.name in names]
    namespace = {
        "json": json,
        "EDIT_WORKFLOW": str(path),
        "T2I_WORKFLOW": str(path),
        "_submit_and_wait": lambda workflow: workflow,
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), "edit-builder-test", "exec"), namespace)
    return namespace[name]


class EditWorkflowTest(unittest.TestCase):
    def test_edit_rejects_insufficient_slots_before_submission(self):
        path = WORKFLOWS / "qwen_image_2_1_turbo_edit_api.json"
        for slots in (0, 1, 2):
            with self.subTest(slots=slots):
                build = load_builder(path)
                workflow = {
                    str(i): {"class_type": "LoadImage", "inputs": {"image": "input.png"}}
                    for i in range(slots)
                }
                build.__globals__["_load_workflow"] = lambda _: workflow
                submit = Mock()
                build.__globals__["_submit_and_wait"] = submit
                with self.assertRaisesRegex(
                    RuntimeError, f"{slots} image slots, but 3 images were supplied"
                ):
                    build(["target.png", "ref-a.png", "ref-b.png"], "test", 40, 123)
                submit.assert_not_called()

    def test_t2i_builders_preserve_models_and_sampling(self):
        paths = sorted(WORKFLOWS.glob("qwen_image_2_1*t2i_api.json"))
        self.assertEqual(len(paths), 2)
        for path in paths:
            template = json.loads(path.read_text())
            build = load_builder(path, "_generate_once")
            for width, height in [(1024, 1024), (1536, 1024)]:
                with self.subTest(workflow=path.name, size=(width, height)):
                    workflow, *_ = build("test prompt", "", width, height, 40, 123)
                    for nid, node in template.items():
                        cls = node["class_type"]
                        if cls in {"ManualSigmas", "KSamplerSelect", "UNETLoader", "CLIPLoader"}:
                            self.assertEqual(workflow[nid], node)
                        if cls == "EmptyLatentImage":
                            self.assertEqual(workflow[nid]["inputs"]["width"], width)
                            self.assertEqual(workflow[nid]["inputs"]["height"], height)
                        if cls == "TextEncodeQwenImage21":
                            self.assertEqual(workflow[nid]["inputs"]["prompt"], "test prompt")
                            self.assertEqual(workflow[nid]["inputs"]["resolution"],
                                             round((width * height) ** 0.5 / 32) * 32)
                        if cls == "KSampler":
                            self.assertEqual(workflow[nid]["inputs"]["steps"], 40)

    def test_model_roles_and_sampling_in_all_templates(self):
        paths = sorted(WORKFLOWS.glob("qwen_image_2_1*api.json"))
        self.assertEqual(len(paths), 4)
        for path in paths:
            with self.subTest(workflow=path.name):
                workflow = json.loads(path.read_text())
                turbo = "_turbo_" in path.name
                find = lambda cls: next(n for n in workflow.values() if n["class_type"] == cls)
                unet = "qwen_image_2.1_" + ("turbo_" if turbo else "") + "int8_convrot.safetensors"
                self.assertEqual(find("UNETLoader")["inputs"]["unet_name"], unet)
                encoder = find("TextEncodeQwenImage21")
                clip_id, output = encoder["inputs"]["clip"]
                self.assertEqual(output, 0)
                self.assertEqual(workflow[clip_id]["inputs"]["clip_name"], "qwen3vl_8b_int8_convrot.safetensors")
                self.assertNotIn("_pe_", path.name)
                self.assertEqual(sum(n["class_type"] == "CLIPLoader" for n in workflow.values()), 1)
                self.assertIsInstance(encoder["inputs"]["prompt"], str)
                self.assertFalse(any("PromptRewrite" in n["class_type"] for n in workflow.values()))
                if turbo:
                    sigmas = [float(v) for v in find("ManualSigmas")["inputs"]["sigmas"].split(",")]
                    self.assertEqual(sigmas, [1.0, 0.978453, 0.95418, 0.926626, 0.89508,
                                              0.845148, 0.704534, 0.414568, 0.0])
                    self.assertEqual(find("SamplerCustom")["inputs"]["cfg"], 1)
                    self.assertEqual(find("KSamplerSelect")["inputs"]["sampler_name"], "euler")
                else:
                    sampler = find("KSampler")["inputs"]
                    self.assertEqual(sampler["steps"], 40)
                    self.assertEqual(sampler["cfg"], 1)
                    self.assertEqual(sampler["sampler_name"], "euler")

    def check_graph(self, workflow, count, resolution):
        loaders = sorted(
            (nid for nid, n in workflow.items() if n["class_type"] == "LoadImage"), key=int
        )
        joins = [n for n in workflow.values() if n["class_type"] == "JoinImageWithAlpha"]
        self.assertEqual(len(loaders), count)
        self.assertEqual(len(joins), count)
        encoder_id, encoder = next(
            (nid, n) for nid, n in workflow.items() if n["class_type"] == "TextEncodeQwenImage21"
        )
        self.assertEqual(encoder["inputs"]["resolution"], resolution)
        sampler = next(n for n in workflow.values() if n["class_type"] in {"SamplerCustom", "KSampler"})
        self.assertEqual(sampler["inputs"]["latent_image"], [encoder_id, 2])
        self.assertFalse(any(n["class_type"] == "EmptyLatentImage" for n in workflow.values()))
        self.assertEqual(sum(k.startswith("images.image_") for k in encoder["inputs"]), count)
        for index, loader in enumerate(loaders, 1):
            join_id, output = encoder["inputs"][f"images.image_{index}"]
            self.assertEqual(output, 0)
            self.assertEqual(workflow[join_id]["class_type"], "JoinImageWithAlpha")
            self.assertEqual(workflow[join_id]["inputs"], {
                "image": [loader, 0], "alpha": [loader, 1],
            })
        for node in workflow.values():
            for value in node["inputs"].values():
                if isinstance(value, list) and len(value) == 2 and isinstance(value[1], int):
                    self.assertIn(str(value[0]), workflow, "Dangling graph link")

    def test_all_edit_templates_and_mcp_builds(self):
        clean = load_cleaner()
        self.assertEqual(len(EDIT_FILES), 2)
        for path in EDIT_FILES:
            template = json.loads(path.read_text())
            build = load_builder(path)
            resolution = 1536 if "_turbo_" in path.name else 1024
            loaders = sorted(
                (nid for nid, n in template.items() if n["class_type"] == "LoadImage"), key=int
            )
            for count in [1, 2, 10]:
                with self.subTest(workflow=path.name, images=count):
                    names = [f"test-{i}.png" for i in range(count)]
                    # Open WebUI applies filenames positionally to the loaders.
                    webui = copy.deepcopy(template)
                    for loader, name in zip(loaders, names):
                        webui[loader]["inputs"]["image"] = name
                    self.check_graph(clean(webui), count, resolution)
                    mcp, *_ = build(names, "test instruction", 40, 123)
                    self.check_graph(clean(mcp), count, resolution)
                    encoder = next(n for n in mcp.values() if n["class_type"] == "TextEncodeQwenImage21")
                    self.assertEqual(encoder["inputs"]["prompt"], "test instruction")
                    # The builder must not change the sampler's schedule.
                    for nid, node in template.items():
                        if node["class_type"] in {"ManualSigmas", "KSamplerSelect", "UNETLoader", "CLIPLoader"}:
                            self.assertEqual(mcp[nid], node)
                        if node["class_type"] == "KSampler":
                            self.assertEqual(mcp[nid]["inputs"]["steps"], 40)

    def test_public_tools_have_no_enhancement_parameter(self):
        source = ast.parse((ROOT / "comfyui-mcp/server.py").read_text())
        for node in source.body:
            if isinstance(node, ast.FunctionDef) and node.name in {"generate_image", "edit_image"}:
                self.assertNotIn("enhance", [arg.arg for arg in node.args.args])


if __name__ == "__main__":
    unittest.main()
