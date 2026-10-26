"""Offline tests for prompt_clean (stdlib only). Run:

    python3 -m unittest -v comfyui_proxy/test_prompt_clean.py
"""

import copy
import json
import unittest

from prompt_clean import UNUSED_REFERENCE_IMAGE, clean_prompt_body, strip_unused_references


def edit_prompt(n_refs_filled=1):
    """A minimal Open WebUI edit workflow: target + 9 reference slots."""
    prompt = {
        "1": {"class_type": "UNETLoader", "inputs": {"unet_name": "turbo.safetensors"}},
        "4": {"class_type": "LoadImage", "inputs": {"image": "target.png"}},
        "5": {
            "class_type": "TextEncodeQwenImage21",
            "inputs": {
                "clip": ["2", 0],
                "prompt": "edit it",
                "images.image_1": ["4", 0],
                "images.image_2": ["12", 0],
                "images.image_3": ["13", 0],
            },
        },
        "6": {"class_type": "SamplerCustom", "inputs": {"latent_image": ["5", 2]}},
        "9": {"class_type": "SaveImage", "inputs": {"images": ["8", 0]}},
    }
    # slots 12, 13; 12 unused, 13 optionally filled
    prompt["12"] = {"class_type": "LoadImage", "inputs": {"image": UNUSED_REFERENCE_IMAGE}}
    prompt["13"] = {
        "class_type": "LoadImage",
        "inputs": {"image": "ref.png" if n_refs_filled >= 2 else UNUSED_REFERENCE_IMAGE}
    }
    return prompt


class StripTest(unittest.TestCase):
    def test_single_target_strips_all_slots(self):
        p = strip_unused_references(edit_prompt(1))
        self.assertNotIn("12", p)
        self.assertNotIn("13", p)
        self.assertIn("images.image_1", p["5"]["inputs"])
        self.assertNotIn("images.image_2", p["5"]["inputs"])
        self.assertNotIn("images.image_3", p["5"]["inputs"])

    def test_used_references_survive(self):
        p = strip_unused_references(edit_prompt(2))
        self.assertNotIn("12", p)
        self.assertIn("13", p)
        self.assertEqual(p["5"]["inputs"]["images.image_3"], ["13", 0])
        self.assertNotIn("images.image_2", p["5"]["inputs"])

    def test_no_sentinel_is_identity(self):
        original = edit_prompt(2)
        original["12"]["inputs"]["image"] = "also-used.png"
        clone = copy.deepcopy(original)
        self.assertEqual(strip_unused_references(clone), original)

    def test_non_dict_and_links_with_int_ids(self):
        self.assertEqual(strip_unused_references("nope"), "nope")
        p = {"7": {"class_type": "LoadImage", "inputs": {"image": UNUSED_REFERENCE_IMAGE}},
             "5": {"inputs": {"images.image_2": [7, 0]}}}
        strip_unused_references(p)
        self.assertEqual(p, {"5": {"inputs": {}}})

    def test_body_passthrough_and_clean(self):
        garbage = b"not json"
        self.assertIs(clean_prompt_body(garbage), garbage)
        wf = edit_prompt(1)
        body = json.dumps({"prompt": wf, "client_id": "x"}).encode()
        out = json.loads(clean_prompt_body(body))
        # untouched string inputs survive, sentinel slots and their links go
        self.assertEqual(out["prompt"]["5"]["inputs"]["prompt"], "edit it")
        self.assertNotIn("12", out["prompt"])
        self.assertEqual(out["client_id"], "x")
        # a non-prompt JSON object passes through untouched
        other = json.dumps({"foo": 1}).encode()
        self.assertEqual(json.loads(clean_prompt_body(other)), {"foo": 1})


if __name__ == "__main__":
    unittest.main()
