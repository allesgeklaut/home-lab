"""Prompt sanitizer for multi-reference Qwen-Image-2.1 edits.

The Open WebUI edit workflow ships with 10 LoadImage slots (image_1 is the
edit target, image_2..image_10 are optional references). Open WebUI's node-map
applier can only WRITE uploaded filenames into those nodes positionally — it
cannot create or remove nodes — so unused slots keep this sentinel filename.
ComfyUI would fail the whole prompt on a LoadImage whose file does not exist,
so the proxy strips sentinel slots, their alpha-join helpers, and links to
them from POST /prompt bodies before forwarding. The remaining encoder inputs
are optional (autogrow), so an absent images.image_k is simply skipped by the
node — exactly what a request with fewer references needs.

Kept free of aiohttp imports so it can be unit-tested with the stdlib alone.
"""

from __future__ import annotations

import json

# Must match the default `image` value of the unused LoadImage slots in the
# edit workflows (webui/workflows/*_edit_api.json). A mismatch fails loudly:
# ComfyUI rejects the prompt with "image does not exist".
UNUSED_REFERENCE_IMAGE = "multi_ref_unused.png"


def strip_unused_references(prompt: dict) -> dict:
    """Remove sentinel LoadImage nodes, their alpha joins, and links to them.

    Mutates *prompt* in place and returns it. Any input on a remaining node
    whose link points at a removed node is deleted, so optional inputs become
    absent (skipped by the node) rather than dangling.
    """
    if not isinstance(prompt, dict):
        return prompt
    unused = {
        node_id
        for node_id, node in prompt.items()
        if isinstance(node, dict)
        and node.get("class_type") == "LoadImage"
        and isinstance(node.get("inputs"), dict)
        and node["inputs"].get("image") == UNUSED_REFERENCE_IMAGE
    }
    if not unused:
        return prompt
    # Alpha joins have required image/mask inputs. Leaving one behind after
    # removing its unused loader would make the encoder's graph invalid.
    while True:
        joins = {
            node_id
            for node_id, node in prompt.items()
            if node_id not in unused
            and isinstance(node, dict)
            and node.get("class_type") == "JoinImageWithAlpha"
            and isinstance(node.get("inputs"), dict)
            and any(
                isinstance(value, list) and value and str(value[0]) in unused
                for value in node["inputs"].values()
            )
        }
        if not joins:
            break
        unused.update(joins)
    for node_id in unused:
        del prompt[node_id]
    for node in prompt.values():
        if not isinstance(node, dict):
            continue
        inputs = node.get("inputs")
        if not isinstance(inputs, dict):
            continue
        for key, value in list(inputs.items()):
            # A link is [origin_node_id, output_slot]; ids may be str or int.
            if isinstance(value, list) and value and str(value[0]) in unused:
                del inputs[key]
    return prompt


def clean_prompt_body(body: bytes) -> bytes:
    """Apply strip_unused_references to a /prompt request body.

    Returns the original bytes untouched for anything that is not a JSON
    object with a `prompt` dict (ComfyUI rejects malformed bodies itself, so
    the proxy never has to guess).
    """
    try:
        data = json.loads(body)
    except (ValueError, UnicodeDecodeError):
        return body
    if not isinstance(data, dict) or not isinstance(data.get("prompt"), dict):
        return body
    strip_unused_references(data["prompt"])
    return json.dumps(data).encode()
