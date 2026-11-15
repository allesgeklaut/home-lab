"""Local report validation against the inventory and evidence ledger."""

from __future__ import annotations

from collections.abc import Iterable

from .evidence import EvidenceLedger, normalize_url
from .models import Inventory, SecurityReport


class ReportValidationError(ValueError):
    """The model's report is structurally valid but not supported by evidence."""


def _brief(values: set[str], limit: int = 5) -> str:
    ordered = sorted(values)
    shown = ", ".join(ordered[:limit])
    if len(ordered) > limit:
        shown += f", ... (+{len(ordered) - limit})"
    return shown


def _canonical_sources(sources: Iterable[object]) -> tuple[set[str], set[str]]:
    """Return canonical URLs and raw values that are not acceptable http(s)."""

    canonical: set[str] = set()
    invalid: set[str] = set()
    for source in sources:
        raw = str(source)
        normalized = normalize_url(raw)
        if normalized is None:
            invalid.add(raw)
        else:
            canonical.add(normalized)
    return canonical, invalid


def validate_report(
    report: SecurityReport,
    inventory: Inventory,
    ledger: EvidenceLedger,
) -> SecurityReport:
    """Reject invented components, unsupported citations, and empty all-clears.

    The ledger is the only source of provenance. Model-authored prose, URLs,
    and evidence IDs are never trusted on their own. Component coverage is
    derived from the ledger's structured search attribution, not from the
    model's checked list or from matching query text. A recorded search proves
    only that a component-attributed search was made; it does not prove that
    the component is unaffected or that an advisory applies to this deployment.
    """

    errors: list[str] = []
    inventory_ids = set(inventory.component_ids())
    checked = report.checked_component_ids
    checked_set = set(checked)
    searched_components = ledger.successful_component_ids

    if ledger.calls < 1:
        errors.append("no search tool/backend attempt was recorded for this run")
    if ledger.successful_calls < 1 and report.assessment != "inconclusive":
        errors.append("no successful search evidence was recorded for this run")

    if report.assessment == "no_actionable_findings":
        if ledger.failed_calls:
            errors.append(
                "no_actionable_findings is not allowed after failed search attempts; "
                "use inconclusive"
            )
        if ledger.empty_calls:
            errors.append(
                "no_actionable_findings is not allowed after empty search results; "
                "use inconclusive"
            )
        if ledger.in_flight_calls:
            errors.append(
                "no_actionable_findings is not allowed while search calls are in flight; "
                "use inconclusive"
            )
        missing_searched = inventory_ids - searched_components
        if missing_searched:
            errors.append(
                "no_actionable_findings requires a successful evidence-bearing search "
                f"for every inventory component; missing: {_brief(missing_searched)}"
            )
        missing_checked = inventory_ids - checked_set
        if missing_checked:
            errors.append(
                "no_actionable_findings requires every inventory component to be checked; "
                f"missing: {_brief(missing_checked)}"
            )

    if len(checked) != len(checked_set):
        errors.append("checked_component_ids contains duplicates")
    unknown_checked = checked_set - inventory_ids
    if unknown_checked:
        errors.append(f"report checks unknown component IDs: {_brief(unknown_checked)}")
    unsearched_checked = checked_set - searched_components
    if unsearched_checked:
        errors.append(
            "report claims checked components without a successful evidence-bearing "
            f"search: {_brief(unsearched_checked)}"
        )

    top_evidence_ids = report.evidence_ids
    top_evidence_set = set(top_evidence_ids)
    if len(top_evidence_ids) != len(top_evidence_set):
        errors.append("evidence_ids contains duplicates")
    unknown_evidence = top_evidence_set - ledger.evidence_ids
    if unknown_evidence:
        errors.append(
            f"report cites evidence IDs not recorded by the search tool: {_brief(unknown_evidence)}"
        )

    top_sources, invalid_top_sources = _canonical_sources(report.sources)
    if invalid_top_sources:
        errors.append(
            "report cites a URL that is not a canonical http(s) URL: "
            f"{_brief(invalid_top_sources)}"
        )
    allowed_top_urls = ledger.urls_for(top_evidence_set)
    unsupported_top = top_sources - allowed_top_urls
    if unsupported_top:
        errors.append(
            f"report cites URLs not present in its cited tool evidence: {_brief(unsupported_top)}"
        )

    if report.assessment == "actionable_findings" and not report.findings:
        errors.append("actionable_findings requires at least one finding")
    if report.assessment in {"no_actionable_findings", "inconclusive"} and report.findings:
        errors.append(f"{report.assessment} cannot contain findings")

    seen_findings: set[tuple[str, str]] = set()
    for index, finding in enumerate(report.findings, start=1):
        if finding.component_id not in inventory_ids:
            errors.append(f"finding {index} uses unknown component ID: {finding.component_id}")
        if finding.component_id not in checked_set:
            errors.append(
                f"finding {index} component is not in checked_component_ids: {finding.component_id}"
            )

        finding_evidence = set(finding.evidence_ids)
        if len(finding.evidence_ids) != len(finding_evidence):
            errors.append(f"finding {index} evidence_ids contains duplicates")
        unknown_finding_evidence = finding_evidence - top_evidence_set
        if unknown_finding_evidence:
            errors.append(
                f"finding {index} cites evidence IDs not listed at the report level: "
                f"{_brief(unknown_finding_evidence)}"
            )

        finding_sources, invalid_finding_sources = _canonical_sources(finding.sources)
        if invalid_finding_sources:
            errors.append(
                f"finding {index} cites a URL that is not a canonical http(s) URL: "
                f"{_brief(invalid_finding_sources)}"
            )
        allowed_finding_urls = ledger.urls_for(finding_evidence)
        unsupported_finding = finding_sources - allowed_finding_urls
        if unsupported_finding:
            errors.append(
                "finding "
                f"{index} cites URLs not present in its cited evidence: "
                f"{_brief(unsupported_finding)}"
            )
        missing_top_sources = finding_sources - top_sources
        if missing_top_sources:
            errors.append(
                f"finding {index} sources are missing from the report-level sources: "
                f"{_brief(missing_top_sources)}"
            )

        identity = (finding.component_id, finding.title.strip().lower())
        if identity in seen_findings:
            errors.append(f"finding {index} duplicates an earlier finding")
        seen_findings.add(identity)

    if errors:
        raise ReportValidationError("; ".join(errors[:6]))
    return report
