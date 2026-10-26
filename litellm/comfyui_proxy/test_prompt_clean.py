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

    def test_unused_alpha_joins_and_consumer_links_are_removed(self):
        p = edit_prompt(2)
        for loader, join, slot in [("4", "21", 1), ("12", "22", 2), ("13", "23", 3)]:
            p[join] = {"class_type": "JoinImageWithAlpha", "inputs": {
                "image": [loader, 0], "alpha": [loader, 1],
            }}
            p["5"]["inputs"][f"images.image_{slot}"] = [join, 0]
        p["24"] = {"class_type": "ReferenceConsumer", "inputs": {
            "image_1": ["4", 0], "image_2": ["12", 0], "image_3": ["13", 0],
        }}
        strip_unused_references(p)
        self.assertNotIn("12", p)
        self.assertNotIn("22", p)
        self.assertNotIn("images.image_2", p["5"]["inputs"])
        self.assertNotIn("image_2", p["24"]["inputs"])
        self.assertEqual(p["21"]["inputs"], {"image": ["4", 0], "alpha": ["4", 1]})
        self.assertEqual(p["5"]["inputs"]["images.image_3"], ["23", 0])
        self.assertEqual(p["24"]["inputs"]["image_3"], ["13", 0])

    def test_non_join_dependents_are_not_recursively_removed(self):
        p = edit_prompt(1)
        p["30"] = {"class_type": "OtherNode", "inputs": {"image": ["12", 0]}}
        strip_unused_references(p)
        self.assertIn("30", p)
        self.assertEqual(p["30"]["inputs"], {})

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
