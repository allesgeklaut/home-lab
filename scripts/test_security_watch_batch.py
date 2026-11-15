import copy
import importlib.util
import io
import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


SPEC = importlib.util.spec_from_file_location("security_watch_batch", Path(__file__).with_name("security-watch.py"))
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
URL = "https://github.com/example/app/security/advisories/GHSA-abcd-1234-5678"
ADVISORY_URL = "https://github.com/advisories/GHSA-abcd-1234-5678"
OSV_URL = "https://osv.dev/vulnerability/GHSA-abcd-1234-5678"


def inventory():
    return {
        "host": {"packages": []},
        "components": [{"id": "app", "image": "example/app:1.0.0", "version": "1.0.0"}],
        "packages": [], "coverage_gaps": [],
    }


def finding(**changes):
    value = {
        "component": "app", "advisory": "GHSA-abcd-1234-5678", "title": "Example vulnerability",
        "installed_version": "1.0.0", "affected_versions": "<1.0.1", "fixed_version": "1.0.1",
        "priority": "update_soon", "status": "confirmed", "exploited": "unknown", "confidence": "high",
        "reason": "The installed upstream version matches the advisory.",
        "action": "Review and update to 1.0.1.", "sources": [URL],
    }
    return {**value, **changes}


def report(**changes):
    return {
        "summary": "Example report; coverage is limited.", "checked_components": ["app"],
        "limitations": [], "findings": [finding()], **changes,
    }


def checks(**changes):
    value = {"errors": [], "osv_matches": [], "osv_queries": []}
    return {**value, **changes}


def osv_match():
    return {
        "component": "app", "ecosystem": "PyPI", "name": "example", "version": "1.0.0",
        "source": "runtime Python metadata", "advisory": "GHSA-abcd-1234-5678",
        "url": OSV_URL,
    }


def conversation(report_value=None, search=True, url=URL):
    outputs = []
    if search:
        outputs.append({
            "type": "tool.execution", "name": "web_search",
            "info": {"result": json.dumps({"opaque": {"url": url}})},
        })
    outputs.append({
        "type": "message.output",
        "content": json.dumps(report_value if report_value is not None else report()),
    })
    return {"outputs": outputs, "usage": {"completion_tokens": 1}}


def output_row(custom_id, body=None, status_code=200, error=None):
    return {
        "id": "batch-1", "custom_id": custom_id,
        "response": {"status_code": status_code, "body": body if body is not None else conversation()},
        "error": error,
    }


def jsonl_row(custom_id, body=None, **changes):
    return (json.dumps(output_row(custom_id, body=body, **changes)) + "\n").encode()


def job(status="SUCCESS", outputs=None, output_file=None, **changes):
    success = status == "SUCCESS"
    value = {
        "id": "job-123", "status": status,
        "total_requests": 1 if success else 0,
        "succeeded_requests": 1 if success else 0,
        "failed_requests": 0,
        "completed_requests": 1 if success else 0,
        "outputs": outputs, "output_file": output_file, "errors": [],
        "created_at": "2026-11-15T00:00:00+00:00",
        "completed_at": "2026-11-15T00:05:00+00:00",
    }
    return {**value, **changes}


class BatchTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.config = {
            "state_dir": str(self.state),
            "agent_file": str(self.state / "agent.json"),
            "model": "mistral-small-2603",
            "max_tokens": 1000,
            "timeout_seconds": 30,
            "retention_days": 30,
            "mistral_key_file": "not-read",
            "ntfy_token_file": "not-read",
            "ntfy_url": "http://127.0.0.1/topic",
        }
        self.agent = {**watch.agent_definition(self.config), "id": "agent-1"}

    def submit(self, job_value=None, inventory_value=None, checks_value=None,
               submit_side_effect=None, expect=0):
        job_value = job_value or job(status="QUEUED")
        inventory_value = inventory_value or inventory()
        checks_value = checks_value if checks_value is not None else checks()
        with patch.object(watch, "read_key", return_value="test-key"), \
             patch.object(watch, "configured_agent", return_value=self.agent) as configured, \
             patch.object(watch, "collect_inventory", return_value=inventory_value) as collector, \
             patch.object(watch, "collect_dependabot", return_value=([], None)), \
             patch.object(watch, "advisory_checks", return_value=checks_value), \
             patch.object(watch, "submit_batch_job", side_effect=submit_side_effect,
                          return_value=job_value) as submitter:
            code = watch.batch_submit(self.config)
        self.assertEqual(code, expect)
        return submitter, collector, configured

    def submitted(self, **kwargs):
        submitter, collector, configured = self.submit(**kwargs)
        active = json.loads((self.state / "batch" / "active.json").read_text())
        return active, submitter

    def collect(self, job_value, send_notifications=False, download=None,
                notify_side_effect=None, request_json_side_effect=None):
        if callable(download):
            download_side_effect = download
        elif download is not None:
            download_side_effect = lambda *args, **kwargs: download
        else:
            download_side_effect = AssertionError("download not expected")
        with patch.object(watch, "read_key", return_value="test-key"), \
             patch.object(watch, "request_json", side_effect=request_json_side_effect,
                          return_value=job_value) as status, \
             patch.object(watch, "collect_resolution_advisories", return_value=({}, [])), \
             patch.object(watch, "download_bytes", side_effect=download_side_effect) as downloader, \
             patch.object(watch, "notify", side_effect=notify_side_effect) as notifier, \
             patch.object(watch, "submit_batch_job") as submitter:
            code = watch.batch_collect(self.config, send_notifications)
        return code, status, downloader, notifier, submitter

    def active(self):
        return json.loads((self.state / "batch" / "active.json").read_text())

    def write_active(self, active):
        watch.atomic_json(self.state / "batch" / "active.json", active)

    def seed_latest(self, checked_at="2026-11-14T00:00:00+00:00"):
        watch.atomic_json(self.state / "latest.json", {"checked_at": checked_at, "findings": []})

    def report_files(self):
        reports = self.state / "reports"
        return sorted(reports.glob("*")) if reports.exists() else []

    def inline_job(self, active, body=None, **changes):
        return job(status="SUCCESS", outputs=[output_row(active["custom_id"], body=body)], **changes)

    def file_job(self, **changes):
        return job(status="SUCCESS", outputs=None, output_file="file-123", **changes)

    def test_submit_posts_exactly_one_official_request_and_persists_private_snapshot(self):
        submitter, collector, configured = self.submit()
        self.assertEqual(submitter.call_count, 1)
        self.assertEqual(collector.call_count, 1)
        self.assertEqual(configured.call_count, 1)
        url, payload, key = submitter.call_args.args
        self.assertEqual(url, watch.MISTRAL + "/batch/jobs")
        self.assertEqual(key, "test-key")
        self.assertEqual(payload["endpoint"], "/v1/conversations")
        self.assertEqual(payload["agent_id"], "agent-1")
        self.assertNotIn("model", payload)
        self.assertEqual(payload["timeout_hours"], 24)
        self.assertEqual(len(payload["requests"]), 1)
        request = payload["requests"][0]
        active = self.active()
        self.assertEqual(request["custom_id"], active["custom_id"])
        self.assertEqual(request["body"]["agent_id"], "agent-1")
        self.assertIs(request["body"]["store"], False)
        self.assertEqual(len(request["body"]["inputs"]), 1)
        self.assertIn('"inventory"', request["body"]["inputs"][0]["content"])
        self.assertNotIn("test-key", json.dumps(payload))
        self.assertTrue(payload["metadata"])
        self.assertTrue(all(isinstance(key, str) and isinstance(value, str)
                            for key, value in payload["metadata"].items()))
        self.assertTrue(all(len(key) <= 32 and len(value) <= 512
                            for key, value in payload["metadata"].items()))
        self.assertEqual(active["state"], "submitted")
        self.assertEqual(active["job_id"], "job-123")
        self.assertEqual(active["job_status"], "QUEUED")
        self.assertFalse((self.state / "latest.json").exists())
        self.assertEqual(self.report_files(), [])
        self.assertEqual((self.state / "batch" / "active.json").stat().st_mode & 0o777, 0o600)
        self.assertEqual((self.state / "batch").stat().st_mode & 0o777, 0o700)
        snapshot_path = self.state / "batch" / "snapshots" / (active["snapshot_hash"] + ".json")
        self.assertEqual(snapshot_path.stat().st_mode & 0o777, 0o600)
        snapshot = json.loads(snapshot_path.read_text())
        watch.verify_batch_snapshot(snapshot)
        self.assertEqual(snapshot["snapshot_hash"], active["snapshot_hash"])
        self.assertEqual(snapshot["custom_id"], active["custom_id"])
        self.assertEqual(snapshot["custom_id"], watch.batch_custom_id(watch.batch_core(snapshot)))
        self.assertEqual(snapshot["snapshot_hash"],
                         watch.batch_snapshot_hash(watch.batch_core(snapshot), snapshot["custom_id"]))
        self.assertEqual(snapshot["request"], payload)
        self.assertEqual(snapshot["inventory"], inventory())
        self.assertEqual(snapshot["checks"], checks())
        self.assertEqual(snapshot["previous"], {})
        self.assertEqual(snapshot["dependabot"], [])
        self.assertIsNone(snapshot["dependabot_gap"])
        self.assertEqual(snapshot["agent"]["id"], "agent-1")
        self.assertEqual(snapshot["agent"]["actual"]["instructions_sha256"],
                         snapshot["agent"]["prompt_sha256"])
        self.assertTrue(watch.BATCH_CUSTOM_ID.fullmatch(snapshot["custom_id"]))

    def test_any_existing_batch_marker_blocks_submit_before_network(self):
        self.submitted()
        for state in ("submitted", "finalizing", "unknown", "invalid", "failed", "stale", "rejected", "collected"):
            with self.subTest(state=state):
                active = self.active()
                active["state"] = state
                if state in ("submitted", "finalizing"):
                    active["job_id"] = "job-123"
                self.write_active(active)
                with patch.object(watch, "collect_inventory") as collector, \
                     patch.object(watch, "configured_agent") as configured, \
                     patch.object(watch, "submit_batch_job") as submitter:
                    with self.assertRaisesRegex(watch.WatchError, "already recorded"):
                        watch.batch_submit(self.config)
                collector.assert_not_called()
                configured.assert_not_called()
                submitter.assert_not_called()

    def test_ambiguous_post_marks_unknown_and_never_retries(self):
        submitter, collector, configured = self.submit(
            submit_side_effect=watch.BatchSubmitUnknown("Batch submission state unknown: network failure"),
            expect=2)
        self.assertEqual(submitter.call_count, 1)
        active = self.active()
        self.assertEqual(active["state"], "unknown")
        self.assertIsNone(active["job_id"])
        self.assertIn("unknown", active["error"].lower())
        self.assertFalse((self.state / "latest.json").exists())
        with patch.object(watch, "collect_inventory") as collector, \
             patch.object(watch, "configured_agent") as configured, \
             patch.object(watch, "submit_batch_job") as submitter:
            with self.assertRaisesRegex(watch.WatchError, "already recorded"):
                watch.batch_submit(self.config)
        collector.assert_not_called()
        configured.assert_not_called()
        submitter.assert_not_called()

    def test_definite_rejection_stays_audited_and_blocks_new_submit(self):
        self.submit(submit_side_effect=watch.BatchSubmitRejected("Batch submission rejected: HTTP 422"),
                    expect=2)
        active = self.active()
        self.assertEqual(active["state"], "rejected")
        self.assertFalse((self.state / "latest.json").exists())
        with patch.object(watch, "collect_inventory") as collector, \
             patch.object(watch, "submit_batch_job") as submitter:
            with self.assertRaisesRegex(watch.WatchError, "already recorded"):
                watch.batch_submit(self.config)
        collector.assert_not_called()
        submitter.assert_not_called()

    def test_pending_statuses_return_zero_without_report_or_notification(self):
        self.submitted()
        for status in ("QUEUED", "RUNNING", "CANCELLATION_REQUESTED"):
            with self.subTest(status=status):
                code, status_call, downloader, notifier, submitter = self.collect(
                    job(status=status), send_notifications=True)
                self.assertEqual(code, 0)
                self.assertIn("?inline=true", status_call.call_args.args[0])
                self.assertFalse((self.state / "latest.json").exists())
                self.assertEqual(self.report_files(), [])
                notifier.assert_not_called()
                downloader.assert_not_called()
                submitter.assert_not_called()
                active = self.active()
                self.assertEqual(active["state"], "submitted")
                self.assertEqual(active["job_status"], status)

    def test_terminal_failures_record_state_without_allclear_or_resubmit(self):
        self.submitted()
        for status in ("FAILED", "TIMEOUT_EXCEEDED", "CANCELLED"):
            with self.subTest(status=status):
                active = self.active()
                active.update(state="submitted", job_id="job-123", job_status=status)
                self.write_active(active)
                code, status_call, downloader, notifier, submitter = self.collect(
                    job(status=status), send_notifications=True)
                self.assertEqual(code, 2)
                self.assertFalse((self.state / "latest.json").exists())
                self.assertEqual(self.report_files(), [])
                notifier.assert_not_called()
                downloader.assert_not_called()
                submitter.assert_not_called()
                active = self.active()
                self.assertEqual(active["state"], "failed")
                self.assertEqual(active["job_status"], status)

    def test_inline_success_publishes_strict_validated_report_with_citation_mapping(self):
        active, _ = self.submitted(checks_value=checks(osv_matches=[osv_match()]))
        body = conversation(report_value=report(findings=[finding(sources=[ADVISORY_URL])]),
                            url=ADVISORY_URL)
        code, status_call, downloader, notifier, submitter = self.collect(
            self.inline_job(active, body=body), send_notifications=False)
        self.assertEqual(code, 0)
        self.assertIn("?inline=true", status_call.call_args.args[0])
        downloader.assert_not_called()
        notifier.assert_not_called()
        submitter.assert_not_called()
        latest = json.loads((self.state / "latest.json").read_text())
        self.assertEqual(latest["checked_at"], self.active()["finalized_at"])
        self.assertEqual(latest["inventory"], inventory())
        self.assertEqual(latest["findings"][0]["sources"], [OSV_URL])
        self.assertEqual(latest["citation_mappings"][0]["from"], ADVISORY_URL)
        self.assertEqual(latest["batch"]["snapshot_hash"], active["snapshot_hash"])
        self.assertEqual(latest["batch"]["custom_id"], active["custom_id"])
        self.assertEqual(latest["batch"]["job_id"], "job-123")
        self.assertGreaterEqual(latest["batch"]["snapshot_age_hours"], 0)
        self.assertIn("not a live collection", latest["batch"]["note"])
        self.assertTrue(any("not a live collection" in item for item in latest["limitations"]))
        rendered = (self.state / "latest.md").read_text()
        self.assertIn("Batch snapshot:", rendered)
        self.assertIn("not a live collection", rendered)
        active = self.active()
        self.assertEqual(active["state"], "collected")
        self.assertRegex(active["report_id"], r"^\d{8}T\d{12}Z$")
        self.assertTrue(Path(active["report_path"]).exists())
        self.assertEqual(len(self.report_files()), 2)
        inline = self.state / "batch" / "results" / (active["snapshot_hash"] + ".outputs.json")
        self.assertEqual(inline.stat().st_mode & 0o777, 0o600)

    def test_file_output_is_fallback_only_when_outputs_null(self):
        active, _ = self.submitted()
        jsonl = jsonl_row(active["custom_id"])
        code, _, downloader, notifier, submitter = self.collect(
            self.file_job(), send_notifications=False, download=jsonl)
        self.assertEqual(code, 0)
        self.assertEqual(downloader.call_count, 1)
        self.assertEqual(downloader.call_args.args[0],
                         watch.MISTRAL + "/files/file-123/content")
        self.assertEqual(downloader.call_args.args[1], "test-key")
        notifier.assert_not_called()
        submitter.assert_not_called()
        raw = self.state / "batch" / "results" / (active["snapshot_hash"] + ".jsonl")
        self.assertEqual(raw.stat().st_mode & 0o777, 0o600)
        self.assertTrue((self.state / "latest.json").exists())

        # Inline results win when both are present.
        active = self.active()
        active.update(state="submitted", job_id="job-123")
        self.write_active(active)
        code, _, downloader, notifier, submitter = self.collect(
            job(status="SUCCESS", outputs=[output_row(active["custom_id"])],
                output_file="file-123"), send_notifications=False)
        self.assertEqual(code, 0)
        downloader.assert_not_called()

        # Empty inline outputs are not a fallback signal; only null is.
        active = self.active()
        active.update(state="submitted", job_id="job-123")
        self.write_active(active)
        code, _, downloader, notifier, submitter = self.collect(
            job(status="SUCCESS", outputs=[], output_file="file-123"),
            send_notifications=False, download=jsonl)
        self.assertEqual(code, 2)
        downloader.assert_not_called()
        self.assertEqual(self.active()["state"], "invalid")

    def test_malformed_inline_and_file_results_never_publish(self):
        active, _ = self.submitted()
        cases = [
            ("inline-empty", job(status="SUCCESS", outputs=[]), None),
            ("inline-wrong-id", job(status="SUCCESS", outputs=[output_row("wrong")]), None),
            ("inline-two", job(status="SUCCESS", outputs=[output_row(active["custom_id"]),
                                                          output_row(active["custom_id"])]), None),
            ("inline-error", job(status="SUCCESS", outputs=[output_row(active["custom_id"], error="boom")]), None),
            ("inline-500", job(status="SUCCESS", outputs=[output_row(active["custom_id"], status_code=500)]), None),
            ("inline-body", job(status="SUCCESS", outputs=[output_row(active["custom_id"], body="not-a-dict")]), None),
            ("file-two-lines", self.file_job(), jsonl_row(active["custom_id"]) + jsonl_row(active["custom_id"])),
            ("file-wrong-id", self.file_job(), jsonl_row("wrong")),
            ("file-500", self.file_job(), jsonl_row(active["custom_id"], status_code=500)),
            ("file-error", self.file_job(), jsonl_row(active["custom_id"], error="boom")),
            ("file-body", self.file_job(), jsonl_row(active["custom_id"], body="not-a-dict")),
            ("file-utf8", self.file_job(), b"\xff"),
            ("missing-output", job(status="SUCCESS", outputs=None, output_file=None), None),
        ]
        for name, job_value, download in cases:
            with self.subTest(name=name):
                current = self.active()
                current.update(state="submitted", job_id="job-123", job_status="SUCCESS")
                current.pop("error", None)
                self.write_active(current)
                code, _, downloader, notifier, submitter = self.collect(
                    job_value, send_notifications=True, download=download)
                self.assertEqual(code, 2)
                self.assertFalse((self.state / "latest.json").exists())
                self.assertEqual(self.report_files(), [])
                notifier.assert_not_called()
                submitter.assert_not_called()
                self.assertEqual(self.active()["state"], "invalid")

    def test_websearch_and_report_validation_failures_mark_invalid(self):
        active, _ = self.submitted()
        for name, body in [
            ("no-search", conversation(search=False)),
            ("bad-source", conversation(report_value=report(
                findings=[finding(sources=["https://invented.example/advisory"])]))),
        ]:
            with self.subTest(name=name):
                current = self.active()
                current.update(state="submitted", job_id="job-123")
                current.pop("error", None)
                self.write_active(current)
                code, _, downloader, notifier, submitter = self.collect(
                    self.inline_job(current, body=body), send_notifications=True)
                self.assertEqual(code, 2)
                self.assertFalse((self.state / "latest.json").exists())
                self.assertEqual(self.report_files(), [])
                notifier.assert_not_called()
                downloader.assert_not_called()
                submitter.assert_not_called()
                self.assertEqual(self.active()["state"], "invalid")
                self.assertTrue(self.active()["error"])

    def test_latest_generation_change_marks_stale_without_publish(self):
        self.seed_latest()
        active, _ = self.submitted()
        watch.atomic_json(self.state / "latest.json",
                          {"checked_at": "2026-11-15T00:00:00+00:00", "findings": []})
        code, _, downloader, notifier, submitter = self.collect(
            self.inline_job(active), send_notifications=True)
        self.assertEqual(code, 2)
        self.assertEqual(json.loads((self.state / "latest.json").read_text())["checked_at"],
                         "2026-11-15T00:00:00+00:00")
        self.assertEqual(self.report_files(), [])
        notifier.assert_not_called()
        downloader.assert_not_called()
        submitter.assert_not_called()
        self.assertEqual(self.active()["state"], "stale")
        self.assertIn("latest report changed", self.active()["stale_reason"])

        # A deleted previous latest is also a generation change.
        current = self.active()
        current.update(state="submitted", job_id="job-123")
        self.write_active(current)
        (self.state / "latest.json").unlink()
        code, _, downloader, notifier, submitter = self.collect(
            self.inline_job(current), send_notifications=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.active()["state"], "stale")

    def test_sync_run_is_not_blocked_by_pending_batch(self):
        self.submitted()
        with patch.object(watch, "collect_inventory", return_value=inventory()), \
             patch.object(watch, "collect_dependabot", return_value=([], None)), \
             patch.object(watch, "advisory_checks", return_value=checks()), \
             patch.object(watch, "read_key", return_value="test-key"), \
             patch.object(watch, "collect_resolution_advisories", return_value=({}, [])), \
             patch.object(watch, "research", return_value=report(findings=[], usage={}, searches=1)), \
             patch.object(watch, "batch_submit") as batch_submit, \
             patch.object(watch, "batch_collect") as batch_collect:
            self.assertEqual(watch.run(self.config, False), 0)
        batch_submit.assert_not_called()
        batch_collect.assert_not_called()
        self.assertTrue((self.state / "latest.json").exists())
        self.assertEqual(self.active()["state"], "submitted")

    def test_duplicate_collect_is_idempotent_and_does_not_notify(self):
        active, _ = self.submitted()
        job_value = self.inline_job(active)
        code, _, _, notifier, _ = self.collect(job_value, send_notifications=True)
        self.assertEqual(code, 0)
        self.assertEqual(notifier.call_count, 1)
        self.assertEqual(self.active()["state"], "collected")
        report_count = len(self.report_files())
        latest = (self.state / "latest.json").read_text()
        code, status_call, downloader, notifier, submitter = self.collect(
            job_value, send_notifications=True)
        self.assertEqual(code, 0)
        status_call.assert_not_called()
        downloader.assert_not_called()
        notifier.assert_not_called()
        submitter.assert_not_called()
        self.assertEqual(len(self.report_files()), report_count)
        self.assertEqual((self.state / "latest.json").read_text(), latest)
        self.assertEqual(self.active()["state"], "collected")

    def test_failed_notification_keeps_finalizing_and_retries_without_redownload(self):
        active, _ = self.submitted()
        job_value = self.inline_job(active)
        with self.assertRaises(watch.WatchError):
            self.collect(job_value, send_notifications=True,
                         notify_side_effect=watch.WatchError("ntfy down"))
        self.assertEqual(self.active()["state"], "finalizing")
        self.assertTrue((self.state / "latest.json").exists())
        self.assertFalse((self.state / "notified.json").exists())
        result = (self.state / "batch" / "results" /
                  (active["snapshot_hash"] + ".report.json"))
        self.assertTrue(result.exists())
        code, status_call, downloader, notifier, submitter = self.collect(
            job_value, send_notifications=True)
        self.assertEqual(code, 0)
        status_call.assert_not_called()
        downloader.assert_not_called()
        self.assertEqual(notifier.call_count, 1)
        self.assertTrue((self.state / "notified.json").exists())
        self.assertEqual(self.active()["state"], "collected")

    def test_crash_before_collected_commit_republishes_without_status_call_or_duplicate_alerts(self):
        active, _ = self.submitted()
        job_value = self.inline_job(active)
        code, _, _, notifier, _ = self.collect(job_value, send_notifications=True)
        self.assertEqual(code, 0)
        self.assertEqual(notifier.call_count, 1)
        self.assertEqual(self.active()["state"], "collected")
        report_count = len(self.report_files())
        # Simulate a crash after successful delivery but before the 'collected'
        # commit: only the state record is reverted. Delivery is at-least-once
        # (a dependabot-only line may re-send when branches exist), but finding
        # and coverage dedup must prevent duplicate alerts.
        reverted = self.active()
        reverted["state"] = "finalizing"
        self.write_active(reverted)
        code, status_call, downloader, notifier, submitter = self.collect(
            job_value, send_notifications=True)
        self.assertEqual(code, 0)
        status_call.assert_not_called()
        downloader.assert_not_called()
        self.assertEqual(len(self.report_files()), report_count)
        self.assertEqual(notifier.call_count, 0)
        self.assertTrue((self.state / "notified.json").exists())
        self.assertEqual(self.active()["state"], "collected")

    def test_status_response_with_foreign_job_id_is_rejected(self):
        active, _ = self.submitted()
        wrong = self.inline_job(active, id="job-999")
        code, status_call, downloader, notifier, submitter = self.collect(
            wrong, send_notifications=True)
        self.assertEqual(code, 2)
        self.assertEqual(self.active()["state"], "submitted")
        self.assertIn("did not match", self.active()["last_error"])
        self.assertFalse((self.state / "latest.json").exists())
        self.assertEqual(self.report_files(), [])
        notifier.assert_not_called()
        downloader.assert_not_called()

    def test_snapshot_tamper_is_rejected(self):
        active, _ = self.submitted()
        path = self.state / "batch" / "snapshots" / (active["snapshot_hash"] + ".json")
        snapshot = json.loads(path.read_text())
        snapshot["inventory"]["components"][0]["version"] = "9.9.9"
        watch.atomic_json(path, snapshot)
        code, _, downloader, notifier, submitter = self.collect(
            self.inline_job(active), send_notifications=True)
        self.assertEqual(code, 2)
        self.assertFalse((self.state / "latest.json").exists())
        self.assertEqual(self.report_files(), [])
        notifier.assert_not_called()
        downloader.assert_not_called()
        submitter.assert_not_called()
        self.assertEqual(self.active()["state"], "invalid")
        self.assertIn("snapshot", self.active()["error"].lower())

    def test_agent_settings_change_after_submit_is_ignored(self):
        active, _ = self.submitted()
        self.config["model"] = "different-model"
        with patch.object(watch, "configured_agent", side_effect=AssertionError("agent fetched")), \
             patch.object(watch, "agent_definition", side_effect=AssertionError("prompt read")):
            code, _, _, notifier, submitter = self.collect(
                self.inline_job(active), send_notifications=False)
        self.assertEqual(code, 0)
        self.assertTrue((self.state / "latest.json").exists())
        notifier.assert_not_called()
        submitter.assert_not_called()
        self.assertEqual(self.active()["state"], "collected")

    def test_partial_success_counts_are_rejected(self):
        active, _ = self.submitted()
        for changes in ({"total_requests": 2}, {"succeeded_requests": 0},
                        {"failed_requests": 1}, {"total_requests": None}):
            with self.subTest(changes=changes):
                current = self.active()
                current.update(state="submitted", job_id="job-123")
                current.pop("last_error", None)
                self.write_active(current)
                code, _, downloader, notifier, submitter = self.collect(
                    self.inline_job(current, **changes), send_notifications=True)
                self.assertEqual(code, 2)
                self.assertFalse((self.state / "latest.json").exists())
                self.assertEqual(self.report_files(), [])
                notifier.assert_not_called()
                downloader.assert_not_called()
                submitter.assert_not_called()
                self.assertEqual(self.active()["state"], "submitted")
                self.assertIn("exactly one", self.active()["last_error"])

    def test_batch_age_cap_is_48_hours(self):
        old = (datetime.now(timezone.utc) - timedelta(hours=49)).isoformat(timespec="seconds")
        with patch.object(watch, "now", return_value=old):
            active, _ = self.submitted()
        code, _, downloader, notifier, submitter = self.collect(
            self.inline_job(active), send_notifications=True)
        self.assertEqual(code, 2)
        self.assertFalse((self.state / "latest.json").exists())
        self.assertEqual(self.report_files(), [])
        notifier.assert_not_called()
        downloader.assert_not_called()
        submitter.assert_not_called()
        self.assertEqual(self.active()["state"], "stale")
        self.assertIn("48", self.active()["stale_reason"])

    def test_input_budget_blocks_before_marker_and_post(self):
        big = inventory()
        big["components"] = [
            {"id": f"app-{i}", "image": f"example/app-{i}:1.0.0", "version": "1.0.0"}
            for i in range(3000)
        ]
        big["packages"] = [
            {"component": f"app-{i}", "ecosystem": "PyPI", "name": f"package-{i}", "version": "1.0.0"}
            for i in range(3000)
        ]
        with patch.object(watch, "read_key", return_value="test-key"), \
             patch.object(watch, "configured_agent", return_value=self.agent), \
             patch.object(watch, "collect_inventory", return_value=big), \
             patch.object(watch, "collect_dependabot", return_value=([], None)), \
             patch.object(watch, "advisory_checks", return_value=checks()), \
             patch.object(watch, "submit_batch_job") as submitter:
            with self.assertRaisesRegex(watch.WatchError, "budget"):
                watch.batch_submit(self.config)
        submitter.assert_not_called()
        self.assertFalse((self.state / "batch" / "active.json").exists())
        snapshots = self.state / "batch" / "snapshots"
        self.assertFalse(snapshots.exists() and list(snapshots.iterdir()))

    def test_batch_modes_are_opt_in_and_sync_is_default(self):
        config_path = self.state / "config.json"
        config_path.write_text("{}")
        with patch.object(watch.sys, "argv", ["security-watch.py", "--config", str(config_path)]), \
             patch.object(watch, "run", return_value=0) as run, \
             patch.object(watch, "batch_submit", return_value=0) as batch_submit, \
             patch.object(watch, "batch_collect", return_value=0) as batch_collect:
            self.assertEqual(watch.main(), 0)
        run.assert_called_once()
        batch_submit.assert_not_called()
        batch_collect.assert_not_called()

        with patch.object(watch.sys, "argv", ["security-watch.py", "--config", str(config_path),
                                              "--batch-submit"]), \
             patch.object(watch, "run", return_value=0) as run, \
             patch.object(watch, "batch_submit", return_value=0) as batch_submit, \
             patch.object(watch, "batch_collect", return_value=0) as batch_collect:
            self.assertEqual(watch.main(), 0)
        batch_submit.assert_called_once()
        run.assert_not_called()
        batch_collect.assert_not_called()

        with patch.object(watch.sys, "argv", ["security-watch.py", "--config", str(config_path),
                                              "--batch-collect", "--notify"]), \
             patch.object(watch, "run", return_value=0) as run, \
             patch.object(watch, "batch_submit", return_value=0) as batch_submit, \
             patch.object(watch, "batch_collect", return_value=0) as batch_collect:
            self.assertEqual(watch.main(), 0)
        batch_collect.assert_called_once()
        self.assertTrue(batch_collect.call_args.args[1])
        run.assert_not_called()
        batch_submit.assert_not_called()

    def test_download_and_submit_helpers_are_bounded_no_redirect_and_sanitized(self):
        response = MagicMock()
        response.read.return_value = b"data"
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(watch, "build_opener", return_value=opener) as build:
            result = watch.download_bytes("https://api.mistral.ai/v1/files/file-1/content", "secret")
        self.assertEqual(result, b"data")
        self.assertIsInstance(build.call_args.args[0], watch.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], watch.BATCH_DOWNLOAD_TIMEOUT)
        self.assertEqual(response.read.call_args.args[0], watch.BATCH_OUTPUT_LIMIT + 1)

        response.read.return_value = b"x" * (watch.BATCH_OUTPUT_LIMIT + 1)
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaisesRegex(watch.WatchError, "size limit"):
                watch.download_bytes("https://api.mistral.ai/v1/files/file-1/content", "secret")

        opener.open.side_effect = watch.HTTPError("https://api.mistral.ai", 404, "Not Found", {}, None)
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaises(watch.BatchDownloadError) as raised:
                watch.download_bytes("https://api.mistral.ai/v1/files/file-1/content", "secret")
        self.assertEqual(raised.exception.status, 404)

        response = MagicMock()
        response.read.return_value = b'{"id":"job-1","status":"QUEUED"}'
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value = response
        with patch.object(watch, "build_opener", return_value=opener) as build:
            result = watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs",
                                            {"endpoint": "/v1/conversations"}, "secret")
        self.assertEqual(result["id"], "job-1")
        self.assertIsInstance(build.call_args.args[0], watch.NoRedirect)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.method, "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], watch.BATCH_CONTROL_TIMEOUT)

        response.read.return_value = b"not-json"
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaises(watch.BatchSubmitUnknown):
                watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs", {}, "secret")

        opener.open.side_effect = watch.HTTPError("https://api.mistral.ai", 422, "Unprocessable", {}, None)
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaises(watch.BatchSubmitRejected):
                watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs", {}, "secret")

        opener.open.side_effect = watch.HTTPError("https://api.mistral.ai", 500, "Server Error", {}, None)
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaises(watch.BatchSubmitUnknown):
                watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs", {}, "secret")

    def test_payment_required_is_a_definite_rejection_with_provider_detail(self):
        # Live-observed format: Mistral batch errors use "detail".
        body = io.BytesIO(json.dumps({
            "object": "error",
            "detail": "You do not have access to this service. You can enable billing via the console.",
        }).encode())
        error = watch.HTTPError("https://api.mistral.ai", 402, "Payment Required", {}, body)
        opener = MagicMock()
        opener.open.side_effect = error
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaisesRegex(
                    watch.BatchSubmitRejected, "HTTP 402: You do not have access to this service") as raised:
                watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs", {}, "secret")
        self.assertNotIn("secret", str(raised.exception))
        # Legacy "message" bodies are still captured.
        body = io.BytesIO(json.dumps({"message": "Add credits to continue"}).encode())
        error = watch.HTTPError("https://api.mistral.ai", 402, "Payment Required", {}, body)
        opener.open.side_effect = error
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaisesRegex(watch.BatchSubmitRejected, "Add credits to continue"):
                watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs", {}, "secret")
        # A body without JSON still yields bounded sanitized text.
        error = watch.HTTPError("https://api.mistral.ai", 402, "Payment Required", {},
                                io.BytesIO(b"plain text failure"))
        opener.open.side_effect = error
        with patch.object(watch, "build_opener", return_value=opener):
            with self.assertRaisesRegex(watch.BatchSubmitRejected, "plain text failure"):
                watch.submit_batch_job("https://api.mistral.ai/v1/batch/jobs", {}, "secret")


class MainBatchModeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.config_path = self.state / "config.json"
        self.config_path.write_text(json.dumps({
            "state_dir": str(self.state / "watch-state"),
            "mistral_key_file": str(self.state / "missing.env"),
        }))

    def run_main(self, *modes):
        argv = ["security-watch.py", "--config", str(self.config_path), *modes]
        with patch.object(sys, "argv", argv):
            return watch.main()

    def test_batch_mode_failures_never_send_failure_notifications(self):
        # --batch-collect fails: no submission is recorded.
        with patch.object(watch, "notify") as notifier:
            self.assertEqual(self.run_main("--batch-collect", "--notify"), 2)
            notifier.assert_not_called()
        # --batch-submit fails: an existing marker blocks before any network use.
        batch = self.state / "watch-state" / "batch"
        batch.mkdir(parents=True)
        (batch / "active.json").write_text(json.dumps({
            "version": 1, "state": "collected", "custom_id": "sw-blocked",
            "snapshot_hash": "0" * 64, "snapshot_file": "snapshots/" + "0" * 64 + ".json",
            "submitted_at": "2026-11-15T00:00:00+00:00",
        }))
        with patch.object(watch, "notify") as notifier:
            self.assertEqual(self.run_main("--batch-submit", "--notify"), 2)
            notifier.assert_not_called()

    def test_synchronous_failure_still_sends_failure_notification(self):
        with patch.object(watch, "run", side_effect=watch.WatchError("research failed")), \
             patch.object(watch, "notify") as notifier:
            self.assertEqual(self.run_main("--notify"), 2)
            notifier.assert_called_once()


if __name__ == "__main__":
    unittest.main()
