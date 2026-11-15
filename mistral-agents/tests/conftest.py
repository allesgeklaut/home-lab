from __future__ import annotations

import os
import socket

import pytest

from security_watch.sdk_compat import (
    disable_model_catalogue_lookup,
    prepare_sdk_environment,
)

prepare_sdk_environment()


@pytest.fixture(autouse=True)
def restore_mistral_key():
    """Undo direct ``os.environ`` writes made by env-file tests."""

    original = os.environ.get("MISTRAL_API_KEY")
    yield
    if original is None:
        os.environ.pop("MISTRAL_API_KEY", None)
    else:
        os.environ["MISTRAL_API_KEY"] = original


@pytest.fixture(autouse=True)
def offline_sdk() -> None:
    """Keep every test's SDK session from fetching model metadata."""

    disable_model_catalogue_lookup()


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail loudly if a test attempts a network connection."""

    def guard(*args: object, **kwargs: object) -> None:
        raise AssertionError("network access attempted in an offline test")

    monkeypatch.setattr(socket, "getaddrinfo", guard)
    monkeypatch.setattr(socket, "create_connection", guard)
    try:
        import httpx2

        monkeypatch.setattr(httpx2.AsyncClient, "send", guard)
        monkeypatch.setattr(httpx2.Client, "send", guard)
    except (ImportError, AttributeError):
        pass
