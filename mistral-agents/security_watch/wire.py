"""Provider-compatible wire schema for the Security Report.

During the earlier DeepSeek diagnostic, the provider rejected the full
generated JSON schema with HTTP 500. The schema-aware relaxed probe was accepted
when field annotations and constraints were removed but the report shape
($defs/$ref, properties, enums, required, additionalProperties) was kept. The
exact single keyword is not isolated, so this module removes the whole observed
annotation/constraint group.

The SDK agent now uses ``mistral-large-4``/``medium``; there is no evidence
Large 4 needs the relaxation, but it is retained as a defensive compatibility
layer. Only ``model_json_schema()`` is relaxed. ``WireSecurityReport`` inherits
every field, default, and validator from ``SecurityReport``, so the SDK still
parses the model reply into a locally validated report.
"""

from __future__ import annotations

from typing import Any

from pydantic.json_schema import GenerateJsonSchema

from .models import SecurityReport

# Field-level annotations and constraints observed in the generated report
# schema. The exact provider trigger is not isolated; this is the group the
# schema-aware live probe accepted.
_UNSUPPORTED_KEYWORDS = frozenset(
    {
        "title",
        "description",
        "default",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "uniqueItems",
    }
)

# Values of these keywords are maps of names to schemas. The names are
# property/definition names (including "title"), not schema keywords.
_SCHEMA_MAP_KEYWORDS = frozenset(
    {"properties", "patternProperties", "$defs", "definitions", "dependentSchemas"}
)

# Values of these keywords are a single schema (or a bool for
# additionalProperties).
_SCHEMA_KEYWORDS = frozenset(
    {"additionalProperties", "contains", "propertyNames", "not", "if", "then", "else"}
)

# Values of these keywords are lists of schemas.
_SCHEMA_LIST_KEYWORDS = frozenset({"anyOf", "oneOf", "allOf", "prefixItems"})


def _sanitize(node: Any) -> Any:
    """Remove unsupported keywords without touching property/definition names."""

    if not isinstance(node, dict):
        return node

    result: dict[str, Any] = {}
    for key, value in node.items():
        if key in _UNSUPPORTED_KEYWORDS:
            continue
        if key in _SCHEMA_MAP_KEYWORDS and isinstance(value, dict):
            result[key] = {
                name: _sanitize(subschema) for name, subschema in value.items()
            }
        elif key == "items":
            result[key] = (
                [_sanitize(item) for item in value]
                if isinstance(value, list)
                else _sanitize(value)
            )
        elif key in _SCHEMA_KEYWORDS:
            result[key] = _sanitize(value)
        elif key in _SCHEMA_LIST_KEYWORDS and isinstance(value, list):
            result[key] = [_sanitize(item) for item in value]
        else:
            result[key] = value
    return result


class _WireSchema(GenerateJsonSchema):
    def generate(self, schema: Any, mode: str = "validation") -> dict[str, Any]:
        return _sanitize(super().generate(schema, mode=mode))


class WireSecurityReport(SecurityReport):
    """SecurityReport with a provider-compatible JSON schema.

    The field types, defaults, and validators are inherited unchanged. The SDK
    still sends ``response_format`` and parses the reply into this class, so
    local validation remains strict.
    """

    @classmethod
    def model_json_schema(cls, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs.setdefault("schema_generator", _WireSchema)
        return super().model_json_schema(*args, **kwargs)
