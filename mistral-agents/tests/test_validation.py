from __future__ import annotations

import pytest

from security_watch.evidence import (
    EvidenceLedger,
    SearchResult,
    normalize_url,
    redact_text,
)
from security_watch.validation import ReportValidationError, validate_report

from .helpers import (
    URL1,
    URL2,
    inventory,
    ledger_with_evidence,
    ledger_with_full_coverage,
    report,
    report_payload,
    results,
)


def all_clear_payload() -> dict:
    return report_payload(
        assessment="no_actionable_findings",
        findings=[],
        checked_component_ids=["web-app", "api-service", "edge-proxy"],
        evidence_ids=["E1", "E2"],
        sources=[URL1, URL2],
    )


def zero_evidence_inconclusive_payload() -> dict:
    return report_payload(
        assessment="inconclusive",
        findings=[],
        checked_component_ids=[],
        evidence_ids=[],
        sources=[],
        limitations=["No usable search evidence was returned."],
    )


def test_valid_report_passes() -> None:
    validated = validate_report(report(), inventory(), ledger_with_evidence())
    assert validated.assessment == "actionable_findings"
    assert validated.findings[0].component_id == "web-app"


def test_invented_component_id_rejected() -> None:
    payload = report_payload()
    payload["findings"][0]["component_id"] = "ghost-service"
    with pytest.raises(ReportValidationError, match="unknown component ID"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_checked_invented_component_rejected() -> None:
    payload = report_payload(checked_component_ids=["web-app", "ghost-service"])
    with pytest.raises(ReportValidationError, match="checks unknown component IDs"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_checked_component_without_search_rejected() -> None:
    payload = report_payload(checked_component_ids=["web-app", "api-service"])
    with pytest.raises(
        ReportValidationError,
        match="checked components without a successful evidence-bearing search",
    ):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_unsupported_citation_rejected() -> None:
    payload = report_payload()
    payload["findings"][0]["sources"] = ["https://invented.example.test/not-in-ledger"]
    payload["sources"] = [URL1, URL2, "https://invented.example.test/not-in-ledger"]
    with pytest.raises(ReportValidationError, match="not present in its cited evidence"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_source_from_other_evidence_rejected() -> None:
    payload = report_payload()
    payload["findings"][0]["sources"] = [URL2]
    with pytest.raises(ReportValidationError, match="not present in its cited evidence"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_all_clear_without_evidence_rejected() -> None:
    payload = report_payload(
        assessment="no_actionable_findings",
        findings=[],
        checked_component_ids=["web-app", "api-service", "edge-proxy"],
        evidence_ids=["E1"],
        sources=[URL1],
    )
    with pytest.raises(ReportValidationError, match="no successful search evidence"):
        validate_report(report(**payload), inventory(), EvidenceLedger())


def test_all_clear_with_partial_reported_coverage_rejected() -> None:
    payload = report_payload(
        assessment="no_actionable_findings",
        findings=[],
        checked_component_ids=["web-app"],
        evidence_ids=["E1"],
        sources=[URL1],
    )
    with pytest.raises(ReportValidationError, match="every inventory component"):
        validate_report(report(**payload), inventory(), ledger_with_full_coverage())


def test_all_clear_requires_searched_coverage_even_when_checked_claims_all() -> None:
    ledger = EvidenceLedger()
    ledger.record_success("web app advisory", results(), component_id="web-app")
    ledger.record_success("api advisory", results(), component_id="api-service")
    with pytest.raises(
        ReportValidationError,
        match="successful evidence-bearing search for every inventory component",
    ):
        validate_report(report(**all_clear_payload()), inventory(), ledger)


def test_all_clear_rejected_after_failed_search() -> None:
    ledger = ledger_with_full_coverage()
    ledger.record_failure("failed query", "upstream unavailable", component_id="web-app")
    with pytest.raises(ReportValidationError, match="failed search attempts"):
        validate_report(report(**all_clear_payload()), inventory(), ledger)


def test_all_clear_rejected_after_empty_search() -> None:
    ledger = ledger_with_full_coverage()
    ledger.record_success("empty query", [], component_id="web-app")
    with pytest.raises(ReportValidationError, match="empty search results"):
        validate_report(report(**all_clear_payload()), inventory(), ledger)


def test_all_clear_rejected_while_search_in_flight() -> None:
    ledger = ledger_with_full_coverage()
    reservation = ledger.reserve_search(
        "slow query",
        max_calls=4,
        component_id="web-app",
    )
    try:
        with pytest.raises(ReportValidationError, match="in flight"):
            validate_report(report(**all_clear_payload()), inventory(), ledger)
    finally:
        ledger.cancel_reservation(reservation, "test cleanup")


def test_all_clear_accepted_with_clean_successful_evidence() -> None:
    validated = validate_report(
        report(**all_clear_payload()), inventory(), ledger_with_full_coverage()
    )
    assert validated.assessment == "no_actionable_findings"
    assert validated.findings == []


def test_actionable_without_findings_rejected() -> None:
    payload = report_payload(assessment="actionable_findings", findings=[])
    with pytest.raises(ReportValidationError, match="requires at least one finding"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_actionable_without_any_search_rejected() -> None:
    with pytest.raises(ReportValidationError, match="no search tool/backend attempt"):
        validate_report(report(), inventory(), EvidenceLedger())


def test_inconclusive_with_findings_rejected() -> None:
    payload = report_payload(assessment="inconclusive")
    with pytest.raises(ReportValidationError, match="cannot contain findings"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_inconclusive_zero_evidence_allowed_after_failed_search() -> None:
    ledger = EvidenceLedger()
    ledger.record_failure("failed query", "upstream unavailable", component_id="web-app")
    validated = validate_report(
        report(**zero_evidence_inconclusive_payload()), inventory(), ledger
    )
    assert validated.assessment == "inconclusive"
    assert validated.checked_component_ids == []
    assert validated.evidence_ids == []
    assert validated.sources == []


def test_inconclusive_zero_evidence_allowed_after_empty_search() -> None:
    ledger = EvidenceLedger()
    ledger.record_success("empty query", [], component_id="web-app")
    validated = validate_report(
        report(**zero_evidence_inconclusive_payload()), inventory(), ledger
    )
    assert validated.assessment == "inconclusive"


def test_inconclusive_zero_evidence_allowed_after_cancelled_search() -> None:
    ledger = EvidenceLedger()
    reservation = ledger.reserve_search(
        "slow query",
        max_calls=1,
        component_id="web-app",
    )
    ledger.cancel_reservation(reservation, "search call was cancelled")
    validated = validate_report(
        report(**zero_evidence_inconclusive_payload()), inventory(), ledger
    )
    assert validated.assessment == "inconclusive"
    assert ledger.failed_calls == 1


def test_inconclusive_zero_evidence_requires_an_attempt() -> None:
    with pytest.raises(ReportValidationError, match="no search tool/backend attempt"):
        validate_report(
            report(**zero_evidence_inconclusive_payload()),
            inventory(),
            EvidenceLedger(),
        )


def test_finding_evidence_must_be_at_report_level() -> None:
    payload = report_payload()
    payload["findings"][0]["evidence_ids"] = ["E2"]
    payload["findings"][0]["sources"] = [URL2]
    # E2 exists in the ledger but is not listed in the report's evidence_ids.
    payload["evidence_ids"] = ["E1"]
    payload["sources"] = [URL1]
    with pytest.raises(ReportValidationError, match="not listed at the report level"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_duplicate_finding_rejected() -> None:
    payload = report_payload()
    payload["findings"].append(dict(payload["findings"][0]))
    with pytest.raises(ReportValidationError, match="duplicates an earlier finding"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_finding_component_must_be_checked() -> None:
    payload = report_payload(checked_component_ids=["api-service", "edge-proxy"])
    with pytest.raises(ReportValidationError, match="not in checked_component_ids"):
        validate_report(report(**payload), inventory(), ledger_with_full_coverage())


def test_empty_ledger_with_cited_ids_rejected() -> None:
    with pytest.raises(ReportValidationError, match="not recorded by the search tool"):
        validate_report(report(), inventory(), EvidenceLedger())


def test_unknown_evidence_id_rejected() -> None:
    payload = report_payload(evidence_ids=["E99"], sources=[URL1])
    payload["findings"][0]["evidence_ids"] = ["E99"]
    with pytest.raises(ReportValidationError, match="not recorded by the search tool"):
        validate_report(report(**payload), inventory(), ledger_with_evidence())


def test_empty_results_do_not_support_a_report() -> None:
    ledger = EvidenceLedger()
    ledger.record_success("empty query", [], component_id="web-app")
    with pytest.raises(ReportValidationError, match="no successful search evidence"):
        validate_report(report(), inventory(), ledger)


def test_schema_rejects_missing_limitations() -> None:
    from pydantic import ValidationError

    from security_watch.models import SecurityReport

    payload = report_payload()
    del payload["limitations"]
    with pytest.raises(ValidationError):
        SecurityReport.model_validate(payload)


def test_schema_rejects_finding_without_evidence_or_sources() -> None:
    from pydantic import ValidationError

    from security_watch.models import SecurityReport

    no_evidence = report_payload()
    no_evidence["findings"][0]["evidence_ids"] = []
    with pytest.raises(ValidationError):
        SecurityReport.model_validate(no_evidence)

    no_sources = report_payload()
    no_sources["findings"][0]["sources"] = []
    with pytest.raises(ValidationError):
        SecurityReport.model_validate(no_sources)


def test_redact_text_removes_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "super-secret-value")
    assert redact_text("failed super-secret-value", 100) == "failed [REDACTED]"


def test_ledger_urls_are_normalized() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [results()[0], results()[0]],
        component_id="web-app",
    )
    assert ledger.calls == 1
    assert len(ledger.evidence) == 1
    assert ledger.evidence[0].evidence_id == "E1"
    assert ledger.evidence[0].url == URL1


def test_normalize_url_uses_httpurl_canonicalization() -> None:
    assert normalize_url("https://example.test:443/x") == "https://example.test/x"
    assert normalize_url("http://example.test:80/x") == "http://example.test/x"
    assert (
        normalize_url("https://bücher.example/x")
        == "https://xn--bcher-kva.example/x"
    )
    assert normalize_url("https://faß.de/advisory") == "https://xn--fa-hia.de/advisory"
    assert (
        normalize_url("https://example.com/über")
        == "https://example.com/%C3%BCber"
    )
    assert (
        normalize_url("https://example.com/a/../advisory")
        == "https://example.com/advisory"
    )
    assert normalize_url("https://user:pass@example.test/x") is None
    assert normalize_url("https://user@example.test/x") is None
    assert normalize_url("file:///etc/passwd") is None
    assert normalize_url("https:///path") is None
    assert normalize_url("https://example.com/" + "a" * 3000) is None
    # Raw length fits, but percent-encoding expands past the bound.
    assert normalize_url("https://example.com/" + "ü" * 1000) is None


def test_default_port_url_matches_report() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [
            SearchResult(
                title="Default port",
                url="https://advisories.example.test:443/web-app-1",
            )
        ],
        component_id="web-app",
    )
    payload = report_payload(evidence_ids=["E1"], sources=[URL1])
    payload["findings"][0]["sources"] = [URL1]
    validated = validate_report(report(**payload), inventory(), ledger)
    assert str(validated.sources[0]) == URL1


def test_idn_url_matches_report() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [
            SearchResult(
                title="IDN",
                url="https://bücher.example/advisory",
            )
        ],
        component_id="web-app",
    )
    payload = report_payload(
        evidence_ids=["E1"],
        sources=["https://xn--bcher-kva.example/advisory"],
    )
    payload["findings"][0]["sources"] = ["https://xn--bcher-kva.example/advisory"]
    validated = validate_report(report(**payload), inventory(), ledger)
    assert str(validated.sources[0]) == "https://xn--bcher-kva.example/advisory"


def test_fass_idn_url_matches_report() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [SearchResult(title="IDN", url="https://faß.de/advisory")],
        component_id="web-app",
    )
    assert ledger.evidence[0].url == "https://xn--fa-hia.de/advisory"
    payload = report_payload(
        evidence_ids=["E1"],
        sources=["https://faß.de/advisory"],
    )
    payload["findings"][0]["sources"] = ["https://faß.de/advisory"]
    validated = validate_report(report(**payload), inventory(), ledger)
    assert str(validated.sources[0]) == "https://xn--fa-hia.de/advisory"


def test_unicode_path_url_matches_report() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [SearchResult(title="Unicode path", url="https://example.com/über")],
        component_id="web-app",
    )
    assert ledger.evidence[0].url == "https://example.com/%C3%BCber"
    payload = report_payload(
        evidence_ids=["E1"],
        sources=["https://example.com/über"],
    )
    payload["findings"][0]["sources"] = ["https://example.com/über"]
    validated = validate_report(report(**payload), inventory(), ledger)
    assert str(validated.sources[0]) == "https://example.com/%C3%BCber"


def test_dot_segment_url_matches_report() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [SearchResult(title="Dot segments", url="https://example.com/a/../advisory")],
        component_id="web-app",
    )
    assert ledger.evidence[0].url == "https://example.com/advisory"
    payload = report_payload(
        evidence_ids=["E1"],
        sources=["https://example.com/a/../advisory"],
    )
    payload["findings"][0]["sources"] = ["https://example.com/a/../advisory"]
    validated = validate_report(report(**payload), inventory(), ledger)
    assert str(validated.sources[0]) == "https://example.com/advisory"


def test_credentials_url_rejected() -> None:
    ledger = EvidenceLedger()
    ledger.record_success(
        "q",
        [SearchResult(title="Credentials", url="https://example.test/advisory")],
        component_id="web-app",
    )
    payload = report_payload(
        evidence_ids=["E1"],
        sources=["https://user:pass@example.test/advisory"],
    )
    payload["findings"][0]["sources"] = ["https://user:pass@example.test/advisory"]
    with pytest.raises(ReportValidationError, match="not a canonical http"):
        validate_report(report(**payload), inventory(), ledger)
