import copy
import importlib.metadata
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("security_watch", Path(__file__).with_name("security-watch.py"))
watch = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(watch)
URL = "https://github.com/example/app/security/advisories/GHSA-abcd-1234-5678"


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


class InventoryTests(unittest.TestCase):
    def test_python_probe_includes_brotli_bindings_and_skips_missing_packages(self):
        self.assertTrue({"brotli", "brotlicffi"}.issubset(watch.PYTHON_PACKAGES))
        packages = {"brotli": "1.0.0", "brotlicffi": "1.0.0.1"}

        def installed_version(name):
            if name not in packages:
                raise importlib.metadata.PackageNotFoundError(name)
            return packages[name]

        with patch.object(importlib.metadata, "version", side_effect=installed_version), \
             patch.object(watch.sys, "argv", ["probe", *watch.PYTHON_PACKAGES]), \
             patch("builtins.print") as output:
            exec(watch.PROBE_PYTHON, {})
        output.assert_called_once_with(json.dumps(packages))

    def test_brotli_metadata_is_collected_for_paperless_and_calibre(self):
        config = json.loads(Path(watch.__file__).with_suffix(".json").read_text())
        names = ["paperless-webserver-1", "calibre-web"]
        self.assertTrue(set(names).issubset(config["python_containers"]))
        packages = {"brotli": "1.0.0", "brotlicffi": "1.0.0.1"}

        def command(args, **kwargs):
            if args[0] == "uname":
                return "1.2.3\n"
            if args[0] == "dpkg-query":
                return ""
            if args[0] == "apt":
                return "Listing...\n"
            if args[:3] == ["docker", "ps", "-aq"]:
                return "\n".join(names)
            if args[:2] == ["docker", "inspect"]:
                return "\n".join(json.dumps({
                    "id": "/" + name, "image": "example/app:1.0.0", "state": "running",
                    "image_id": "sha256:abc", "network": "bridge", "ports": {},
                }) for name in names)
            if args[:3] == ["docker", "image", "inspect"]:
                return json.dumps({"version": None, "source": None, "revision": None})
            if args[:2] == ["docker", "exec"]:
                self.assertEqual(args[3:7], ["python", "-B", "-c", watch.PROBE_PYTHON])
                self.assertEqual(args[7:], list(watch.PYTHON_PACKAGES))
                return json.dumps(packages)
            raise AssertionError(args)

        with patch.object(watch, "command", side_effect=command):
            value = watch.collect_local_inventory(config)
        self.assertEqual(value["packages"], [
            {"component": name, "ecosystem": "PyPI", "name": package,
             "version": installed, "source": "runtime Python metadata"}
            for name in names for package, installed in packages.items()
        ])

    def test_remote_collector_contains_only_fixed_inventory_code(self):
        config = {"host_alias": "remote", "host_packages": [], "exposure": {}, "python_containers": []}
        source = watch.remote_collector_source(config)
        compile(source, "remote-collector", "exec")
        for forbidden in ("MISTRAL", "read_key", "request_json", "research(", "ssh", "setup_agent"):
            self.assertNotIn(forbidden, source)
        namespace = {}
        # Load definitions without running the final collection call.
        exec(source.rsplit("print(json.dumps", 1)[0], namespace)
        self.assertIn("collect_local_inventory", namespace)
        self.assertNotIn("collect_inventory", namespace)

    def test_ssh_inventory_is_namespaced_and_targets_are_not_forwarded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.json"
            path.write_text(json.dumps([{"alias": "remote", "target": "user@private.example"}]))
            remote = inventory()
            remote.update(collected_at="2026-10-23T00:00:00Z", host={"alias": "remote", "packages": [{"name": "openssl", "version": "3.0.0"}], "kernel": "6.1.0", "release": "12"})
            remote["packages"] = [{"component": "app", "ecosystem": "PyPI", "name": "example", "version": "1.0.0"}]
            config = {"host_alias": "main", "host_packages": [], "remote_hosts_file": str(path)}
            with patch.object(watch, "collect_local_inventory", return_value=inventory()), \
                 patch.object(watch, "remote_collector_source", return_value="collect_local_inventory"), \
                 patch.object(watch.subprocess, "run", return_value=SimpleNamespace(stdout=json.dumps(remote))) as ssh:
                result = watch.collect_inventory(config)
            args = ssh.call_args.args[0]
            self.assertIn("BatchMode=yes", args)
            self.assertIn("StrictHostKeyChecking=yes", args)
            self.assertIn("ForwardAgent=no", args)
            self.assertIn("ClearAllForwardings=yes", args)
            self.assertEqual(ssh.call_args.kwargs["timeout"], 120)
            self.assertIn("collect_local_inventory", ssh.call_args.kwargs["input"])
            self.assertNotIn("private.example", json.dumps(result))
            self.assertNotIn('"target"', json.dumps(result))
            versions = watch.component_versions(result)
            self.assertEqual(versions["remote:app"], {"1.0.0"})
            self.assertEqual(versions["host:remote:openssl"], {"3.0.0"})
            self.assertIn("host:remote", versions)

    def test_ssh_failure_keeps_main_inventory_and_hides_stderr(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.json"
            path.write_text(json.dumps([{"alias": "remote", "target": "user@private.example"}]))
            error = watch.subprocess.CalledProcessError(255, ["ssh"], stderr="private.example SECRET")
            with patch.object(watch, "collect_local_inventory", return_value=inventory()), \
                 patch.object(watch, "remote_collector_source", return_value="fixed collector"), \
                 patch.object(watch.subprocess, "run", side_effect=error):
                result = watch.collect_inventory({"host_alias": "main", "host_packages": [], "remote_hosts_file": str(path)})
            self.assertEqual(result["components"], inventory()["components"])
            self.assertEqual(len(result["collection_errors"]), 1)
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertNotIn("private.example", json.dumps(result))

    def test_invalid_remote_targets_are_not_executed(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hosts.json"
            for target in ("-oProxyCommand=bad", "user@host; bad", "host\nother"):
                path.write_text(json.dumps([{"alias": "remote", "target": target}]))
                with patch.object(watch, "collect_local_inventory", return_value=inventory()), \
                     patch.object(watch.subprocess, "run") as ssh:
                    result = watch.collect_inventory({"host_alias": "main", "host_packages": [], "remote_hosts_file": str(path)})
                ssh.assert_not_called()
                self.assertTrue(result["collection_errors"])

    def test_version_hints_reject_floating_tags_and_unsafe_values(self):
        for value in ["latest", "main", "stable", "1.0.0\nSECRET", "/opt/secrets/key"]:
            self.assertIsNone(watch.version(value))
        self.assertEqual(watch.version("v1.2.3"), "1.2.3")
        self.assertEqual(watch.version("1:9.6p1-3ubuntu13.20"), "1:9.6p1-3ubuntu13.20")

    def test_inherited_base_labels_are_not_application_versions(self):
        self.assertFalse(watch.label_matches_image("training-log:local", "https://github.com/astral-sh/uv"))
        self.assertFalse(watch.label_matches_image("storyteller:latest", None))
        self.assertTrue(watch.label_matches_image("ghcr.io/immich-app/immich-server:release", "https://github.com/immich-app/immich"))

    def test_private_image_domains_are_not_disclosed(self):
        self.assertEqual(watch.image_reference("192.168.0.10:5000/private/app:1"), "private-image")
        self.assertEqual(watch.image_reference("private.example/app:1"), "private-image")
        self.assertEqual(watch.image_reference("ghcr.io/open-webui/open-webui:v0.12.3"), "ghcr.io/open-webui/open-webui:v0.12.3")
        self.assertEqual(watch.image_reference("image:1?token=secret"), "local-image")

    def test_exposure_does_not_assume_unpublished_means_private(self):
        self.assertIn("unknown", watch.exposure({}, "bridge"))
        self.assertIn("unknown", watch.exposure({}, "host"))
        bindings = {"80/tcp": [{"HostIp": "127.0.0.1", "HostPort": "1234"}]}
        self.assertIn("loopback", watch.exposure(bindings, "bridge"))
        self.assertNotIn("127.0.0.1", watch.exposure(bindings, "bridge"))
        self.assertEqual(watch.exposure({}, "bridge", "Cloudflare Access"), "Cloudflare Access")

    def test_collector_uses_allowlisted_fields_not_docker_environment(self):
        config = {"host_alias": "main", "host_packages": [], "exposure": {}, "python_containers": []}
        docker_row = {"id": "/app", "image": "private.example/private/app:latest", "state": "running",
                      "image_id": "sha256:abc", "network": "bridge", "ports": {}}
        calls = []

        def command(args, **kwargs):
            calls.append(args)
            if args == ["uname", "-r"]:
                return "1.2.3\n"
            if args[0] == "dpkg-query":
                return ""
            if args[0] == "apt":
                return "Listing...\n"
            if args[:3] == ["docker", "ps", "-aq"]:
                return "abc\n"
            if args[:2] == ["docker", "inspect"]:
                return json.dumps(docker_row)
            if args[:3] == ["docker", "image", "inspect"]:
                return json.dumps({"version": "DO_NOT_SEND_SECRET", "source": None, "revision": "not-a-revision"})
            raise AssertionError(args)

        with patch.object(watch, "command", side_effect=command):
            value = watch.collect_inventory(config)
        text = json.dumps(value)
        self.assertNotIn("DO_NOT_SEND_SECRET", text)
        self.assertNotIn("private.example", text)
        self.assertNotIn("image_id", text)
        self.assertNotIn("Config.Env", str(calls))
        self.assertEqual(value["components"][0]["version"], "unknown")


class ApiTests(unittest.TestCase):
    def test_credentials_are_parsed_without_executing_shell(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "key.env"
            path.write_text('export MISTRAL_API_KEY="test-key" # comment\n')
            self.assertEqual(watch.read_key(path, "MISTRAL_API_KEY"), "test-key")
            path.write_text('MISTRAL_API_KEY=$(touch /not-executed)\n')
            with self.assertRaises(watch.WatchError):
                watch.read_key(path, "MISTRAL_API_KEY")

    def test_public_source_urls_exclude_local_or_credentialed_links(self):
        for value in ["http://example.com", "https://127.0.0.1/", "https://server.local/", "https://a:b@example.com", "https://[", "https://example.com/\nTitle: injected"]:
            self.assertFalse(watch.public_url(value))
        self.assertTrue(watch.public_url(URL))

    def test_advisory_failure_is_explicit_not_empty_all_clear(self):
        data = inventory()
        data["packages"] = [{"component": "app", "ecosystem": "PyPI", "name": "example", "version": "1.0.0"}]
        with patch.object(watch, "request_json", side_effect=watch.WatchError("network failure")):
            checks = watch.advisory_checks(data)
        self.assertEqual(len(checks["errors"]), 2)

    def test_report_rejects_hallucinated_sources_components_and_classifications(self):
        watch.validate_report(report(), inventory(), {URL})
        for change in [
            {"sources": ["https://invented.example/advisory"]},
            {"sources": []}, {"sources": "not-a-list"},
            {"component": "not-in-inventory"}, {"priority": "SUPERCRITICAL"},
            {"advisory": "bad\nheader"},
        ]:
            with self.subTest(change=change), self.assertRaises(watch.WatchError):
                watch.validate_report(report(findings=[finding(**change)]), inventory(), {URL})

    def test_research_requires_search_and_preserves_citations(self):
        with tempfile.TemporaryDirectory() as directory:
            agent_file = Path(directory) / "agent.json"
            agent_file.write_text('{"agent_id":"test-agent"}')
            config = {"agent_file": str(agent_file), "model": "mistral-small-2603", "max_tokens": 1000, "timeout_seconds": 30}
            agent = {**watch.agent_definition(config), "id": "test-agent"}
            response = {"outputs": [
                {"type": "tool.execution", "name": "web_search", "info": {"result": json.dumps({"opaque_id": {"url": URL}})}},
                {"type": "message.output", "content": json.dumps(report())},
            ], "usage": {"completion_tokens": 300}}
            checks = {"osv_matches": []}
            with patch.object(watch, "request_json", side_effect=[agent, response]) as api:
                value = watch.research(config, "test-key", inventory(), checks, {})
            self.assertEqual(value["search_sources"], [URL])
            payload = api.call_args.args[1]
            self.assertFalse(payload["store"])
            self.assertNotIn("completion_args", payload)
            self.assertNotIn("tools", payload)
            self.assertNotIn("test-key", json.dumps(payload))
            self.assertNotIn("retry", api.call_args.kwargs)
            response["outputs"][-1]["content"] = [
                {"type": "thinking", "thinking": [{"type": "text", "text": "Not report content"}]},
                {"type": "text", "text": json.dumps(report())},
            ]
            with patch.object(watch, "request_json", side_effect=[agent, response]):
                result = watch.research(config, "test-key", inventory(), checks, {})
                self.assertEqual(result["findings"][0]["status"], "possible")
                self.assertIn("Deployment applicability unverified", result["findings"][0]["reason"])
            response["outputs"].pop(0)
            with patch.object(watch, "request_json", side_effect=[agent, response]), self.assertRaises(watch.WatchError):
                watch.research(config, "test-key", inventory(), checks, {})

    def test_confirmed_findings_must_match_observed_versions_and_not_be_fixed(self):
        for change in [{"installed_version": "0.1.0"}, {"installed_version": "unknown"}, {"fixed_version": "1.0.0"}]:
            with self.subTest(change=change), self.assertRaises(watch.WatchError):
                watch.validate_report(report(findings=[finding(**change)]), inventory(), {URL})

    def test_all_statuses_reject_invented_versions_and_active_findings_reject_fixed_versions(self):
        for status in watch.ENUMS["status"]:
            with self.subTest(status=status), self.assertRaises(watch.WatchError):
                watch.validate_report(report(findings=[finding(status=status, installed_version="9.9.9")]), inventory(), {URL})
        for status in ("confirmed", "possible", "unknown"):
            for fixed in ("1.0.0", "0.9.0"):
                with self.subTest(status=status, fixed=fixed), self.assertRaises(watch.WatchError):
                    watch.validate_report(report(findings=[finding(status=status, fixed_version=fixed)]), inventory(), {URL})
        # Unknown versions are allowed as uncertainty, but not as confirmation.
        watch.validate_report(report(findings=[finding(status="possible", installed_version="unknown")]), inventory(), {URL})

    def test_ecosystem_version_ordering_is_not_lexical(self):
        for installed, fixed, ecosystem, expected in [
            ("1.10.0", "1.9.0", "npm", 1),
            ("1.0.0-rc.10", "1.0.0-rc.2", "npm", 1),
            ("1.0.0-rc.1", "1.0.0", "npm", -1),
            ("1.0.0+build.2", "1.0.0+build.1", "npm", 0),
            ("1.0.0-01", "1.0.0", "npm", None),
            ("1.0", "1.0.0", "PyPI", 0),
            ("1.0rc1", "1.0", "PyPI", -1),
            ("1.0.dev1", "1.0a1", "PyPI", -1),
            ("1.0.post1", "1.0", "PyPI", 1),
            ("1.0.post1.dev1", "1.0.post1", "PyPI", -1),
            ("1!1.0", "2.0", "PyPI", 1),
            ("1.0+local", "1.0", "PyPI", 0),
            ("1.0", "1.0+local", "PyPI", None),
            ("1.0ubuntu1", "1.0", "PyPI", None),
        ]:
            with self.subTest(installed=installed, fixed=fixed, ecosystem=ecosystem):
                self.assertEqual(watch.version_order(installed, fixed, ecosystem), expected)

    def test_package_checks_select_pypi_ordering(self):
        data = inventory()
        data["packages"] = [{"component": "app", "ecosystem": "PyPI", "name": "example", "version": "1.0.post1"}]
        with self.assertRaises(watch.WatchError):
            watch.validate_report(report(findings=[finding(status="possible", installed_version="1.0.post1", fixed_version="1.0")]), data, {URL})
        watch.validate_report(report(findings=[finding(status="possible", installed_version="1.0.post1", fixed_version="1.1rc1")]), data, {URL})

    def test_distro_backported_versions_use_dpkg_order_not_upstream_semver(self):
        data = inventory()
        data["host"]["packages"] = [{"name": "openssh-server", "version": "1:9.6p1-3ubuntu13.20"}]
        value = finding(component="host:openssh-server", installed_version="1:9.6p1-3ubuntu13.20", fixed_version="1:9.6p1-3ubuntu13.19")
        with self.assertRaises(watch.WatchError):
            watch.validate_report(report(findings=[value]), data, {URL})
        value["fixed_version"] = "1:9.6p1-3ubuntu13.21"
        watch.validate_report(report(findings=[value]), data, {URL})
        value.update(status="possible", fixed_version="1:9.6p1-3ubuntu13.19")
        with self.assertRaises(watch.WatchError):
            watch.validate_report(report(findings=[value]), data, {URL})

    def test_invalid_assessment_is_excluded_without_alerting_or_repeating_its_summary(self):
        value = report(summary="Unsupported original claim", findings=[finding(), finding(advisory="CVE-2026-12345", installed_version="unobserved")])
        result = watch.validated_findings(value, inventory(), {URL})
        self.assertEqual(result["findings"], [finding()])
        self.assertEqual(len(result["rejected_assessments"]), 1)
        self.assertNotIn("Unsupported original claim", result["summary"])
        with self.assertRaises(watch.WatchError):
            watch.validated_findings(report(findings=[finding(installed_version="unobserved")]), inventory(), {URL})

    def test_setup_is_idempotent_and_never_modifies_other_agents(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "agent.json"
            config = {"agent_file": str(path), "model": "mistral-small-2603", "max_tokens": 1000}
            created = {**watch.agent_definition(config), "id": "our-agent"}
            created["completion_args"]["response_format"]["json_schema"] = None
            with patch.object(watch, "request_json", side_effect=[[{"name": "Existing research agent"}], created]) as api:
                watch.setup_agent(config, "test-key")
            self.assertEqual(api.call_count, 2)
            self.assertEqual(api.call_args.args[1]["name"], watch.AGENT_NAME)
            self.assertEqual(api.call_args.args[1]["model"], "mistral-small-2603")
            self.assertEqual(json.loads(path.read_text())["agent_id"], "our-agent")
            with patch.object(watch, "request_json", return_value=created) as api:
                watch.setup_agent(config, "test-key")
                self.assertEqual(api.call_count, 1)  # Verify, but never PATCH unchanged settings.


class StateTests(unittest.TestCase):
    def setUp(self):
        collector = patch.object(watch, "collect_resolution_advisories", return_value=({}, []))
        collector.start()
        self.addCleanup(collector.stop)

    def test_full_run_ground_truth_resolution_survives_same_key_rejected_claim(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                old = finding(installed_version="1.2.0", fixed_version="1.2.1")
                state = Path(directory)
                watch.atomic_json(state / "latest.json", {"findings": [old]})
                data = inventory()
                package = {"component": "app", "name": "brotli", "ecosystem": "PyPI", "version": "1.2.1"}
                data["packages"] = [package]
                metadata = {old["advisory"].upper(): {"id": old["advisory"], "aliases": [],
                            "affected": [{"package": {"name": "brotli", "ecosystem": "PyPI"}}]}}
                rejected = [{"component": "app", "advisory": old["advisory"], "reason": "conflict"}] if conflict else []
                value = report(findings=[], usage={}, searches=1, rejected_assessments=rejected)
                checks = {"errors": [], "osv_matches": [], "osv_queries": [{**package, "advisories": []}]}
                config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
                with patch.object(watch, "collect_inventory", return_value=data), \
                     patch.object(watch, "collect_dependabot", return_value=([], None)), \
                     patch.object(watch, "collect_resolution_advisories", return_value=(metadata, [])), \
                     patch.object(watch, "advisory_checks", return_value=checks), \
                     patch.object(watch, "read_key", return_value="test-key"), \
                     patch.object(watch, "research", return_value=value):
                    self.assertEqual(watch.run(config, False), 2 if conflict else 0)
                saved = json.loads((state / "latest.json").read_text())
                self.assertEqual(saved["findings"], [])
                self.assertEqual(len(saved["resolved_findings"]), 1)

    def test_full_run_same_key_rejection_still_vetoes_not_affected_dismissal(self):
        for conflict in (False, True):
            with self.subTest(conflict=conflict), tempfile.TemporaryDirectory() as directory:
                old = finding()
                state = Path(directory)
                watch.atomic_json(state / "latest.json", {"findings": [old]})
                resolved = finding(status="not_affected")
                rejected = [{"component": "app", "advisory": old["advisory"], "reason": "conflict"}] if conflict else []
                value = report(findings=[resolved], usage={}, searches=1, rejected_assessments=rejected)
                config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
                with patch.object(watch, "collect_inventory", return_value=inventory()), \
                     patch.object(watch, "collect_dependabot", return_value=([], None)), \
                     patch.object(watch, "collect_resolution_advisories", return_value=({}, [])), \
                     patch.object(watch, "advisory_checks", return_value={"errors": [], "osv_matches": [], "osv_queries": []}), \
                     patch.object(watch, "read_key", return_value="test-key"), \
                     patch.object(watch, "research", return_value=value):
                    self.assertEqual(watch.run(config, False), 2 if conflict else 0)
                saved = json.loads((state / "latest.json").read_text())
                self.assertEqual(len(saved["findings"]), int(conflict))

    def test_resolution_metadata_failure_notifies_and_keeps_history(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            watch.atomic_json(state / "latest.json", {"findings": [finding()]})
            config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
            with patch.object(watch, "collect_inventory", return_value=inventory()), \
                 patch.object(watch, "collect_dependabot", return_value=([], None)), \
                 patch.object(watch, "collect_resolution_advisories", return_value=({}, ["OSV metadata unavailable"])), \
                 patch.object(watch, "advisory_checks", return_value={"errors": []}), \
                 patch.object(watch, "read_key", return_value="test-key"), \
                 patch.object(watch, "research", return_value=report(findings=[], usage={}, searches=1)), \
                 patch.object(watch, "notify") as delivery:
                self.assertEqual(watch.run(config, True), 2)
            saved = json.loads((state / "latest.json").read_text())
            self.assertEqual(len(saved["findings"]), 1)
            self.assertFalse(saved["source_checks_complete"])
            self.assertIn("OSV metadata unavailable", saved["limitations"])
            self.assertIn("Incomplete advisory checks", delivery.call_args.args[1])

    def test_wording_drift_does_not_generate_duplicate_alerts(self):
        original = finding()
        sent = {watch.finding_key(original): watch.fingerprint(original)}
        rewritten = finding(reason="Different prose", action="Reworded advice", title="Different wording")
        self.assertEqual(watch.changed_alerts([rewritten], sent), [])
        self.assertEqual(len(watch.changed_alerts([finding(priority="act_now")], sent)), 1)

    def test_notification_counts_overflow_instead_of_silently_dropping_alerts(self):
        alerts = [finding(advisory=f"CVE-2026-{10000 + i}") for i in range(25)]
        message = watch.notification_body(alerts, [], [])
        self.assertIn("25 new/changed", message)
        self.assertIn("17 additional findings", message)
        self.assertLess(len(message.encode()), 3500)

    def test_notification_byte_budget_preserves_overflow_and_partial_notices(self):
        alerts = [finding(component="c" * 200 + str(i), advisory="GHSA-" + str(i) + "a" * 140, fixed_version="\u754c" * 60) for i in range(25)]
        message = watch.notification_body(alerts, ["very long error " * 1000], [{}])
        self.assertLessEqual(len(message.encode()), 3500)
        shown = message.count("update_soon:")
        self.assertLess(shown, 8)
        self.assertIn(f"{25 - shown} additional findings", message)
        self.assertIn("Incomplete advisory checks", message)
        self.assertIn("unverified claims excluded", message)
        with patch.object(watch, "read_key", return_value="test-key"), patch.object(watch, "build_opener") as opener:
            watch.notify({"ntfy_token_file": "not-read", "ntfy_url": "http://localhost/topic"}, message)
        self.assertEqual(opener.return_value.open.call_args.args[0].data, message.encode())

    def test_notification_transport_refuses_silent_truncation(self):
        with patch.object(watch, "read_key") as credential, patch.object(watch, "build_opener") as opener:
            with self.assertRaises(watch.WatchError):
                watch.notify({}, "\u754c" * 1200)
        credential.assert_not_called()
        opener.assert_not_called()

    def test_partial_or_omitted_findings_are_never_resolved(self):
        previous = [finding()]
        carried = watch.merge_findings(previous, [], complete=True)
        self.assertTrue(carried[0]["not_rechecked"])
        self.assertEqual(watch.changed_alerts(carried, {}), [])
        dismissed = finding(status="not_affected")
        self.assertEqual(len(watch.merge_findings(previous, [dismissed], complete=False)), 1)
        self.assertEqual(watch.merge_findings(previous, [dismissed], complete=True), [])
        self.assertEqual(len(watch.merge_findings(previous, [finding(status="not_affected", confidence="low")], complete=True)), 1)
        self.assertEqual(len(watch.merge_findings(previous, [dismissed], complete=True, checked_components={"other"})), 1)

    def test_manual_run_does_not_consume_alert_and_failed_delivery_is_retryable(self):
        for notify, fail in [(False, False), (True, True), (True, False)]:
            with self.subTest(notify=notify, fail=fail), tempfile.TemporaryDirectory() as directory:
                config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
                data = report(usage={}, searches=1)
                checks = {"errors": []}
                with patch.object(watch, "collect_inventory", return_value=inventory()), \
                     patch.object(watch, "collect_dependabot", return_value=([], None)), \
                     patch.object(watch, "advisory_checks", return_value=checks), \
                     patch.object(watch, "read_key", return_value="test-key"), \
                     patch.object(watch, "research", return_value=copy.deepcopy(data)), \
                     patch.object(watch, "notify", side_effect=watch.WatchError("ntfy delivery failed") if fail else None) as delivery:
                    if fail:
                        with self.assertRaises(watch.WatchError):
                            watch.run(config, notify)
                    else:
                        self.assertEqual(watch.run(config, notify), 0)
                self.assertEqual(delivery.call_count, int(notify))
                self.assertEqual((Path(directory) / "notified.json").exists(), notify and not fail)
                self.assertTrue((Path(directory) / "latest.json").exists())

    def test_atomic_state_is_private(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "state.json"
            watch.atomic_json(path, {"example": 1})
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(path.read_text()), {"example": 1})

    def test_overlap_lock_prevents_any_duplicate_research(self):
        with tempfile.TemporaryDirectory() as directory:
            with (Path(directory) / "run.lock").open("w") as lock:
                watch.fcntl.flock(lock, watch.fcntl.LOCK_EX | watch.fcntl.LOCK_NB)
                with patch.object(watch, "collect_inventory") as collector, self.assertRaises(watch.WatchError):
                    watch.run({"state_dir": directory}, False)
                collector.assert_not_called()

    def test_resolved_finding_retains_evidence_and_recurrence_can_alert(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            previous = finding()
            watch.atomic_json(state / "latest.json", {"findings": [previous]})
            watch.atomic_json(state / "notified.json", {watch.finding_key(previous): watch.fingerprint(previous)})
            config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
            assessment = finding(status="not_affected")
            value = report(findings=[assessment], checked_components=["app", "host"], usage={}, searches=1)
            data = inventory()
            data["components"].append({"id": "unrelated-unchecked", "version": "unknown"})
            with patch.object(watch, "collect_inventory", return_value=data), \
                 patch.object(watch, "collect_dependabot", return_value=([], None)), \
                 patch.object(watch, "advisory_checks", return_value={"errors": []}), \
                 patch.object(watch, "read_key", return_value="test-key"), \
                 patch.object(watch, "research", return_value=value):
                watch.run(config, False)
            saved = json.loads((state / "latest.json").read_text())
            self.assertEqual(saved["findings"], [])
            self.assertEqual(saved["assessments"], [assessment])
            self.assertEqual(json.loads((state / "notified.json").read_text()), {})
            self.assertEqual(watch.changed_alerts([previous], {}), [previous])

    def test_unrelated_failures_do_not_block_validated_explicit_resolution(self):
        for errors, rejected in [(["OSV unavailable"], []), ([], [{"component": "other", "advisory": "CVE-2026-12345"}])]:
            with self.subTest(errors=errors, rejected=rejected), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                watch.atomic_json(state / "latest.json", {"findings": [finding()]})
                config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
                value = report(findings=[finding(status="not_affected")], checked_components=["app", "host"], usage={}, searches=1, rejected_assessments=rejected)
                with patch.object(watch, "collect_inventory", return_value=inventory()), \
                     patch.object(watch, "collect_dependabot", return_value=([], None)), \
                     patch.object(watch, "advisory_checks", return_value={"errors": errors}), \
                     patch.object(watch, "read_key", return_value="test-key"), \
                     patch.object(watch, "research", return_value=value):
                    self.assertEqual(watch.run(config, False), 2)
                saved = json.loads((state / "latest.json").read_text())
                self.assertEqual(saved["findings"], [])

    def test_remote_collection_failure_is_partial_and_notifies(self):
        with tempfile.TemporaryDirectory() as directory:
            config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
            data = inventory()
            data["collection_errors"] = ["remote: SSH inventory failed"]
            with patch.object(watch, "collect_inventory", return_value=data), \
                 patch.object(watch, "collect_dependabot", return_value=([], None)), \
                 patch.object(watch, "advisory_checks", return_value={"errors": []}), \
                 patch.object(watch, "read_key", return_value="test-key"), \
                 patch.object(watch, "research", return_value=report(findings=[], usage={}, searches=1)), \
                 patch.object(watch, "notify") as delivery:
                self.assertEqual(watch.run(config, True), 2)
            self.assertIn("Incomplete advisory checks", delivery.call_args.args[1])


BRANCH_LISTING = ("origin/dependabot/docker_compose/paperless/library/postgres-18\t2026-10-24\tb1573cca\n"
                  "origin/dependabot/docker_compose/engram/postgres-18-alpine\t2026-10-25\t17aaa369\n")
BRANCHES = [{"branch": "dependabot/docker_compose/paperless/library/postgres-18", "opened": "2026-10-24", "tip": "b1573cca"},
            {"branch": "dependabot/docker_compose/engram/postgres-18-alpine", "opened": "2026-10-25", "tip": "17aaa369"}]


class ReconciliationTests(unittest.TestCase):
    def repository_fixture(self):
        old = finding(component="paperless-webserver-1", advisory="GHSA-c8f3-pqr2-rr24",
                      fixed_version="unknown", not_rechecked=True)
        source = "https://github.com/paperless-ngx/paperless-ngx/security/advisories/" + old["advisory"]
        vendor = {"ghsa_id": old["advisory"], "html_url": source, "state": "published", "withdrawn_at": None,
                  "vulnerabilities": [{"package": {"name": "papaerless-ngx", "ecosystem": ""},
                                       "patched_versions": "3.3.2"}]}
        data = inventory()
        data["components"] = [{"id": old["component"], "image": "ghcr.io/paperless-ngx/paperless-ngx:latest",
                               "state": "running", "version": "3.3.4", "version_source": "runtime binary --version"}]
        return old, source, vendor, data

    def test_repository_fallback_is_allowlisted_and_validates_source(self):
        old, source, vendor, data = self.repository_fixture()
        with patch.object(watch, "request_json", side_effect=[watch.WatchError("HTTP 404"), vendor]) as request:
            records, errors = watch.collect_resolution_advisories([old])
        self.assertEqual(errors, [])
        self.assertEqual(records[old["advisory"].upper()]["source_url"], source)
        self.assertEqual(request.call_args.args[0], "https://api.github.com/repos/paperless-ngx/paperless-ngx/security-advisories/" + old["advisory"])
        for bad in [{**vendor, "ghsa_id": "GHSA-wrong-id"}, {**vendor, "html_url": "https://example.com"},
                    {**vendor, "state": "draft"}, {**vendor, "withdrawn_at": "today"}]:
            with patch.object(watch, "request_json", side_effect=[watch.WatchError("HTTP 404"), bad]):
                records, errors = watch.collect_resolution_advisories([old])
            self.assertEqual(records, {})
            self.assertTrue(errors)
        with patch.object(watch, "request_json", side_effect=watch.WatchError("HTTP 404")) as request:
            watch.collect_resolution_advisories([{**old, "component": "unconfigured"}])
        self.assertEqual(request.call_count, 1)

    def test_repository_resolution_requires_runtime_image_and_strict_fixed_version(self):
        old, source, vendor, data = self.repository_fixture()
        record = {"repository": "paperless-ngx/paperless-ngx", "source_url": source, "repository_advisory": vendor}
        checks = {"errors": [], "resolution_advisories": {old["advisory"].upper(): record}}
        remaining, resolved = watch.reconcile_findings([old], data, checks)
        self.assertEqual(remaining, [])
        self.assertIn(source, resolved[0]["resolution"])
        for change in [{"version_source": "matching image source label (application unverified)"},
                       {"image": "untrusted/paperless:latest"}, {"version": "3.3.2"},
                       {"version": "3.3.1"}, {"version": "unknown"}, {"state": "exited"}]:
            altered = {**data, "components": [{**data["components"][0], **change}]}
            self.assertEqual(len(watch.reconcile_findings([old], altered, checks)[0]), 1)
        for fixed in ["unknown", ">=3.3.2", "3.3.2, 3.4.0"]:
            altered = copy.deepcopy(checks)
            altered["resolution_advisories"][old["advisory"].upper()]["repository_advisory"]["vulnerabilities"][0]["patched_versions"] = fixed
            self.assertEqual(len(watch.reconcile_findings([old], data, altered)[0]), 1)
        # A same-key rejected claim vetoes model dismissals, but repository
        # ground truth is independent of research claims and still clears.
        merged = watch.merge_findings([old], [], True, {"paperless-webserver-1"}, {watch.finding_key(old)})
        self.assertEqual(len(merged), 1)
        self.assertEqual(watch.reconcile_findings(merged, data, checks)[0], [])

    def test_authoritative_advisory_metadata_failure_retains_findings(self):
        old = finding(not_rechecked=True)
        good = {"id": old["advisory"], "aliases": ["CVE-2026-12345"],
                "affected": [{"package": {"name": "brotli", "ecosystem": "PyPI"}}]}
        with patch.object(watch, "request_json", return_value=good) as request:
            records, errors = watch.collect_resolution_advisories([old, old])
        self.assertEqual(records, {old["advisory"].upper(): good})
        self.assertEqual(errors, [])
        self.assertEqual(request.call_count, 1)
        for bad in [{}, {**good, "id": "GHSA-wrong-id"}, {**good, "affected": []},
                    {**good, "affected": [{"package": {"name": "brotli"}}]}]:
            with patch.object(watch, "request_json", return_value=bad):
                records, errors = watch.collect_resolution_advisories([old])
            self.assertEqual(records, {})
            self.assertEqual(len(errors), 1)
        with patch.object(watch, "request_json", side_effect=watch.WatchError("offline")):
            records, errors = watch.collect_resolution_advisories([old])
        self.assertEqual(records, {})
        self.assertTrue(errors)

    def test_osv_query_receipts_require_whole_valid_response(self):
        data = inventory()
        package = {"component": "app", "name": "brotli", "ecosystem": "PyPI", "version": "1.2.1"}
        data["packages"] = [package]
        feed = {"vulnerabilities": [], "catalogVersion": "test"}
        for results, expected in [([{}], [{**package, "advisories": []}]),
                                  ([], []), ([{"vulns": "invalid"}], [])]:
            with patch.object(watch, "request_json", side_effect=[{"results": results}, feed]):
                checks = watch.advisory_checks(data)
            self.assertEqual(checks["osv_queries"], expected)
            if not expected:
                self.assertTrue(checks["errors"])

    def test_same_key_rejection_and_low_confidence_stay_unresolved(self):
        old = finding()
        resolved = finding(status="not_affected")
        self.assertEqual(len(watch.merge_findings([old], [resolved], False, {"app"},
                                               {watch.finding_key(old)})), 1)
        self.assertEqual(len(watch.merge_findings([old], [finding(status="not_affected", confidence="low")],
                                               False, {"app"}, set())), 1)

    def test_distro_fix_uses_exact_architecture_and_dpkg_backport_order(self):
        old = finding(component="host:libbrotli1:amd64", installed_version="1.1.0-2build2",
                      fixed_version="1.1.0-2ubuntu0.1", status="confirmed", not_rechecked=True)
        data = inventory()
        data["host"]["packages"] = [{"name": "libbrotli1:amd64", "version": "1.1.0-2ubuntu0.1"}]
        active, resolved = watch.reconcile_findings([old], data, {"errors": []})
        self.assertEqual(active, [])
        self.assertIn("Current distro package", resolved[0]["resolution"])
        old["component"] = "host:libbrotli1:i386"
        self.assertEqual(len(watch.reconcile_findings([old], data, {"errors": []})[0]), 1)

    def test_pypi_fix_needs_unique_identity_current_version_and_successful_osv(self):
        package = {"component": "app", "name": "brotli", "ecosystem": "PyPI", "version": "1.2.0"}
        old = finding(installed_version="1.2.0", fixed_version="1.2.1", not_rechecked=True)
        data = inventory()
        data["packages"] = [{**package, "version": "1.2.1"}]
        record = {"id": old["advisory"], "aliases": ["CVE-2026-12345"],
                  "affected": [{"package": {"name": "brotli", "ecosystem": "PyPI"}}]}
        receipt = {**data["packages"][0], "advisories": []}
        checks = {"errors": [], "osv_queries": [receipt], "resolution_advisories": {old["advisory"].upper(): record}}
        self.assertEqual(watch.reconcile_findings([old], data, checks)[0], [])
        for altered_data, altered_checks in [
            (data, {**checks, "resolution_advisories": {}}),
            ({**data, "packages": []}, checks),
            ({**data, "packages": [package]}, checks),
            (data, {**checks, "osv_queries": []}),
            (data, {**checks, "osv_queries": [{**receipt, "advisories": ["CVE-2026-12345"]}]}),
            (data, {**checks, "osv_queries": [{**receipt, "advisories": ["PYSEC-2026-123"]}]}),
            (data, {"errors": [], "osv_matches": []}),
        ]:
            self.assertEqual(len(watch.reconcile_findings([old], altered_data, altered_checks)[0]), 1)
        # A same-key rejected claim vetoes the model's not-affected dismissal,
        # but independent package evidence still clears the finding.
        merged = watch.merge_findings([old], [finding(status="not_affected")], False, {"app"}, {watch.finding_key(old)})
        self.assertEqual(len(merged), 1)
        self.assertEqual(watch.reconcile_findings(merged, data, checks)[0], [])
        # A coincidentally equal old version does not bind an unrelated package.
        wrong = copy.deepcopy(checks)
        wrong["resolution_advisories"][old["advisory"].upper()]["affected"][0]["package"]["name"] = "other"
        self.assertEqual(len(watch.reconcile_findings([old], data, wrong)[0]), 1)

    def test_historical_findings_have_separate_section_and_resolution_evidence(self):
        value = report(findings=[finding(not_rechecked=True)], checked_at="today", searches=0, usage={},
                       resolved_findings=[{**finding(), "resolution": "Observed fixed package"}])
        text = watch.markdown(value)
        self.assertIn("## Historical findings — not reverified", text)
        self.assertIn("not a statement of the currently installed version", text)
        self.assertIn("Observed fixed package", text)


class DependabotBranchTests(unittest.TestCase):
    def setUp(self):
        collector = patch.object(watch, "collect_resolution_advisories", return_value=({}, []))
        collector.start()
        self.addCleanup(collector.stop)

    def test_fetch_scopes_the_refspec_and_normalizes_entries(self):
        with patch.object(watch, "command", side_effect=[None, BRANCH_LISTING]) as git:
            entries, gap = watch.collect_dependabot()
        self.assertIsNone(gap)
        self.assertEqual(entries, BRANCHES)
        self.assertEqual(git.call_args_list[0].args[0][-1], watch.DEPENDABOT_REFSPEC)
        self.assertIn("--prune", git.call_args_list[0].args[0])

    def test_fetch_failure_is_an_explicit_gap_never_a_false_all_clear(self):
        with patch.object(watch, "command", side_effect=watch.WatchError("no network")):
            entries, gap = watch.collect_dependabot()
        self.assertEqual(entries, [])
        self.assertIn("unknown", gap)

    def test_malformed_listing_rows_are_skipped_not_fatal(self):
        listing = BRANCH_LISTING + "origin/dependabot/docker_compose/orphan\n\n"
        with patch.object(watch, "command", side_effect=[None, listing]):
            entries, gap = watch.collect_dependabot()
        self.assertIsNone(gap)
        self.assertEqual(entries, BRANCHES)

    def test_short_branch_strips_variable_ecosystem_prefix(self):
        self.assertEqual(watch.short_branch(BRANCHES[0]["branch"]), "paperless/library/postgres-18")
        self.assertEqual(watch.short_branch("dependabot/docker-compose/web/app-1.0"), "web/app-1.0")

    def test_notification_lists_new_branches_and_stays_bounded(self):
        body = watch.notification_body([], [], [], {"open": BRANCHES, "new": BRANCHES[:1]})
        self.assertIn("Dependabot update PRs: 1 new of 2 open", body)
        self.assertIn("paperless/library/postgres-18", body)
        self.assertNotIn("postgres-18-alpine", body)
        stale = watch.notification_body([], [], [], {"open": BRANCHES, "new": []})
        self.assertIn("2 open, none new", stale)
        self.assertNotIn("Dependabot", watch.notification_body([], [], [], None))
        many = [{"branch": f"dependabot/docker_compose/app{i}/pkg-1.{i}", "opened": "2026-10-26", "tip": "abc"} for i in range(7)]
        line = watch.dependabot_lines({"open": many, "new": many})[0]
        self.assertIn("(+2 more in the local report)", line)
        self.assertLess(len(line.encode()), 500)

    def test_manual_and_failed_runs_do_not_consume_branch_alerts(self):
        for notify, fail in [(False, False), (True, True), (True, False)]:
            with self.subTest(notify=notify, fail=fail), tempfile.TemporaryDirectory() as directory:
                state = Path(directory)
                config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
                with patch.object(watch, "collect_inventory", return_value=inventory()), \
                     patch.object(watch, "collect_dependabot", return_value=(BRANCHES, None)), \
                     patch.object(watch, "advisory_checks", return_value={"errors": []}), \
                     patch.object(watch, "read_key", return_value="test-key"), \
                     patch.object(watch, "research", return_value=report(findings=[], usage={}, searches=1)), \
                     patch.object(watch, "notify", side_effect=watch.WatchError("ntfy down") if fail else None) as delivery:
                    if fail:
                        with self.assertRaises(watch.WatchError):
                            watch.run(config, notify)
                    else:
                        self.assertEqual(watch.run(config, notify), 0)
                delivered = state / "dependabot.json"
                self.assertEqual(delivered.exists(), notify and not fail)
                if delivered.exists():
                    self.assertEqual(json.loads(delivered.read_text()), [entry["branch"] for entry in BRANCHES])
                if notify:
                    self.assertIn("2 new of 2 open", delivery.call_args.args[1])

    def test_delivered_branches_do_not_realert(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            watch.atomic_json(state / "dependabot.json", [entry["branch"] for entry in BRANCHES])
            config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
            with patch.object(watch, "collect_inventory", return_value=inventory()), \
                 patch.object(watch, "collect_dependabot", return_value=(BRANCHES, None)), \
                 patch.object(watch, "advisory_checks", return_value={"errors": []}), \
                 patch.object(watch, "read_key", return_value="test-key"), \
                 patch.object(watch, "research", return_value=report(findings=[], usage={}, searches=1)), \
                 patch.object(watch, "notify") as delivery:
                self.assertEqual(watch.run(config, True), 0)
            delivery.assert_not_called()

    def test_fetch_gap_is_reported_without_inventing_branches(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            config = {"state_dir": directory, "retention_days": 30, "mistral_key_file": "not-read"}
            with patch.object(watch, "collect_inventory", return_value=inventory()), \
                 patch.object(watch, "collect_dependabot", return_value=([], "Dependabot branches could not be fetched; pending update PRs are unknown this run.")), \
                 patch.object(watch, "advisory_checks", return_value={"errors": []}), \
                 patch.object(watch, "read_key", return_value="test-key"), \
                 patch.object(watch, "research", return_value=report(findings=[], usage={}, searches=1)), \
                 patch.object(watch, "notify") as delivery:
                self.assertEqual(watch.run(config, True), 0)
            delivery.assert_not_called()
            saved = json.loads((state / "latest.json").read_text())
            self.assertEqual(saved["dependabot"], {"open": [], "new": []})
            self.assertTrue(any("Dependabot branches" in item for item in saved["limitations"]))


if __name__ == "__main__":
    unittest.main()
