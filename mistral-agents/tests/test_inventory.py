from __future__ import annotations

import json

import pytest

from security_watch.inventory import (
    MAX_INVENTORY_BYTES,
    InventoryError,
    load_inventory,
    load_inventory_bytes,
)

from .helpers import INVENTORY, FIXTURE


def test_fixture_loads() -> None:
    inventory = load_inventory(FIXTURE)
    assert inventory.component_ids() == ["web-app", "api-service", "edge-proxy"]


def test_unknown_fields_are_rejected() -> None:
    payload = dict(INVENTORY)
    payload["hostname"] = "should-not-be-here"
    with pytest.raises(InventoryError, match="invalid inventory"):
        load_inventory_bytes(json.dumps(payload).encode())


def test_duplicate_component_ids_are_rejected() -> None:
    payload = json.loads(json.dumps(INVENTORY))
    payload["components"].append(dict(payload["components"][0]))
    with pytest.raises(InventoryError, match="unique"):
        load_inventory_bytes(json.dumps(payload).encode())


def test_invalid_component_id_is_rejected() -> None:
    payload = json.loads(json.dumps(INVENTORY))
    payload["components"][0]["id"] = "Web App"
    with pytest.raises(InventoryError, match="invalid inventory"):
        load_inventory_bytes(json.dumps(payload).encode())


def test_oversized_inventory_is_rejected() -> None:
    payload = b"{" + b" " * MAX_INVENTORY_BYTES + b"}"
    with pytest.raises(InventoryError, match="exceeds"):
        load_inventory_bytes(payload)


def test_non_object_inventory_is_rejected() -> None:
    with pytest.raises(InventoryError, match="JSON object"):
        load_inventory_bytes(b"[]")


def test_invalid_json_is_rejected() -> None:
    with pytest.raises(InventoryError, match="valid UTF-8 JSON"):
        load_inventory_bytes(b"{not json")


def test_missing_file_is_rejected(tmp_path) -> None:
    with pytest.raises(InventoryError, match="cannot read inventory"):
        load_inventory(tmp_path / "missing.json")


def test_stdin_inventory(monkeypatch) -> None:
    import io
    import sys

    monkeypatch.setattr(sys, "stdin", type("FakeStdin", (), {"buffer": io.BytesIO(json.dumps(INVENTORY).encode())})())
    inventory = load_inventory("-")
    assert inventory.component_ids() == ["web-app", "api-service", "edge-proxy"]
