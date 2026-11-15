"""Small compatibility shim for the SDK's environment parsing.

mistralai-agents 1.1.3 imports its workflow configuration while importing
``mistralai.agents``. That configuration reads a bare ``AGENT`` environment
variable as nested worker configuration. Generic environments (including the
one this prototype was developed in) may set ``AGENT=1`` for unrelated
reasons, which makes the SDK import fail before any agent code runs.

Keep a valid JSON-object ``AGENT`` value; remove only values the SDK cannot
parse as an object. This is import-time environment hygiene, not a secret
read and not a network operation.
"""

from __future__ import annotations

import json
import os


def prepare_sdk_environment() -> None:
    raw = os.environ.get("AGENT")
    if raw is None:
        return
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        value = None
    if not isinstance(value, dict):
        os.environ.pop("AGENT", None)


def disable_model_catalogue_lookup() -> None:
    """Skip the SDK's optional model-metadata HTTP lookup.

    Used only by the offline demo and tests, where no network is allowed.
    This does not change model token limits or retry behavior.
    """

    from mistralai.vibe.harness.runtime.completion import route_catalogue

    route_catalogue.default_catalogue_fetcher_factory = (
        route_catalogue.no_completion_catalogue
    )
