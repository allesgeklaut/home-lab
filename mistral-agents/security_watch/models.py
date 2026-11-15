"""Validated data shapes for the Security Watch prototype.

The inventory is deliberately small and host-free: it describes software
components, not machines, paths, users, or shell access.
"""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, field_validator

COMPONENT_ID_PATTERN = r"^[a-z][a-z0-9._-]{1,63}$"
EVIDENCE_ID_PATTERN = r"^E[1-9][0-9]{0,3}$"

ComponentId = Annotated[str, Field(pattern=COMPONENT_ID_PATTERN)]
EvidenceId = Annotated[str, Field(pattern=EVIDENCE_ID_PATTERN)]


class InventoryComponent(BaseModel):
    """One software component in a sanitized inventory."""

    model_config = ConfigDict(extra="forbid")

    id: ComponentId
    name: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=1, max_length=80)
    ecosystem: Literal["npm", "PyPI", "container", "OS", "other"]
    notes: str | None = Field(default=None, max_length=300)


class Inventory(BaseModel):
    """A sanitized, bounded inventory supplied by the CLI."""

    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1]
    environment: str = Field(min_length=1, max_length=80)
    components: list[InventoryComponent] = Field(min_length=1, max_length=50)

    @field_validator("components")
    @classmethod
    def _unique_component_ids(cls, value: list[InventoryComponent]) -> list[InventoryComponent]:
        ids = [component.id for component in value]
        if len(ids) != len(set(ids)):
            raise ValueError("component ids must be unique")
        return value

    def component_ids(self) -> list[str]:
        return [component.id for component in self.components]

    def get_component(self, component_id: str) -> InventoryComponent | None:
        for component in self.components:
            if component.id == component_id:
                return component
        return None


class Finding(BaseModel):
    """One evidence-backed security finding."""

    model_config = ConfigDict(extra="forbid")

    component_id: ComponentId
    title: str = Field(min_length=1, max_length=200)
    severity: Literal["critical", "high", "medium", "low", "info"]
    status: Literal["confirmed", "possible", "unverified"]
    evidence_ids: list[EvidenceId] = Field(min_length=1, max_length=8)
    sources: list[HttpUrl] = Field(min_length=1, max_length=8)
    impact: str = Field(min_length=1, max_length=1000)
    recommendation: str = Field(min_length=1, max_length=1000)
    uncertainty: str = Field(min_length=1, max_length=1000)


class SecurityReport(BaseModel):
    """The only model output the local runner will accept as a report."""

    model_config = ConfigDict(extra="forbid")

    assessment: Literal[
        "actionable_findings",
        "no_actionable_findings",
        "inconclusive",
    ]
    summary: str = Field(min_length=1, max_length=2000)
    findings: list[Finding] = Field(default_factory=list, max_length=20)
    checked_component_ids: list[ComponentId] = Field(default_factory=list, max_length=50)
    evidence_ids: list[EvidenceId] = Field(default_factory=list, max_length=40)
    sources: list[HttpUrl] = Field(default_factory=list, max_length=40)
    limitations: list[Annotated[str, Field(min_length=1, max_length=500)]] = Field(
        min_length=1,
        max_length=10,
    )
    research_notes: str = Field(min_length=1, max_length=2000)
