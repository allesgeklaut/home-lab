"""Bounded loading of sanitized inventory JSON."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .models import Inventory

MAX_INVENTORY_BYTES = 256 * 1024


class InventoryError(ValueError):
    """The inventory is unreadable, oversized, or does not match the schema."""


def _validation_summary(error: ValidationError) -> str:
    parts: list[str] = []
    for item in error.errors()[:5]:
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        message = str(item.get("msg", "invalid value"))
        parts.append(f"{location}: {message}")
    return "; ".join(parts)


def load_inventory_bytes(data: bytes) -> Inventory:
    if len(data) > MAX_INVENTORY_BYTES:
        raise InventoryError(
            f"inventory exceeds the {MAX_INVENTORY_BYTES}-byte limit"
        )
    try:
        raw = json.loads(data)
    except (UnicodeDecodeError, ValueError):
        raise InventoryError("inventory is not valid UTF-8 JSON") from None
    if not isinstance(raw, dict):
        raise InventoryError("inventory must be a JSON object")
    try:
        return Inventory.model_validate(raw)
    except ValidationError as error:
        raise InventoryError(f"invalid inventory: {_validation_summary(error)}") from None


def load_inventory(path: str | Path) -> Inventory:
    if str(path) == "-":
        data = sys.stdin.buffer.read(MAX_INVENTORY_BYTES + 1)
        return load_inventory_bytes(data)
    inventory_path = Path(path)
    try:
        size = inventory_path.stat().st_size
    except OSError as error:
        raise InventoryError(f"cannot read inventory: {error.strerror or error}") from None
    if size > MAX_INVENTORY_BYTES:
        raise InventoryError(
            f"inventory exceeds the {MAX_INVENTORY_BYTES}-byte limit"
        )
    try:
        data = inventory_path.read_bytes()
    except OSError as error:
        raise InventoryError(f"cannot read inventory: {error.strerror or error}") from None
    return load_inventory_bytes(data)
