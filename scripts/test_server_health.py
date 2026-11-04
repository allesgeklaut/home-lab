import copy
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parent
SPEC = importlib.util.spec_from_file_location("health_hardware", SCRIPTS / "server-health-hardware.py")
hardware = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hardware)


def nvme():
    return {
        "smart_status": {"passed": True}, "temperature": {"current": 36},
        "nvme_smart_health_information_log": {
            "critical_warning": 0, "available_spare": 100, "available_spare_threshold": 10,
            "percentage_used": 3, "media_errors": 0,
            "num_err_log_entries": 120, "unsafe_shutdowns": 20,
        },
    }


def result(stdout="", returncode=0, stderr=""):
    return SimpleNamespace(stdout=stdout, returncode=returncode, stderr=stderr)


def levels(findings):
    return {level for level, _ in findings}


class SmartTests(unittest.TestCase):
    def test_healthy_nvme_does_not_alarm_on_command_errors_or_unsafe_shutdowns(self):
        self.assertEqual(levels(hardware.smart_findings("drive", nvme(), 0)), {"OK"})

    def test_nvme_warning_spare_media_and_wear(self):
        cases = [("critical_warning", 1, "CRIT"), ("available_spare", 1, "CRIT"),
                 ("media_errors", 1, "CRIT"), ("percentage_used", 80, "WARN"),
                 ("percentage_used", 100, "CRIT"), ("percentage_used", 150, "CRIT")]
        for key, value, level in cases:
            with self.subTest(key=key, value=value):
                data = nvme()
                data["nvme_smart_health_information_log"][key] = value
                self.assertIn(level, levels(hardware.smart_findings("drive", data, 0)))

    def test_nvme_missing_or_invalid_fields_never_pass(self):
        for value in [None, "0", True, -1, float("nan")]:
            data = nvme()
            data["nvme_smart_health_information_log"]["media_errors"] = value
            self.assertIn("WARN", levels(hardware.smart_findings("drive", data, 0)))

    def test_failing_status_still_parsed_with_nonzero_bitmask(self):
        data = nvme()
        data["smart_status"]["passed"] = False
        findings = hardware.smart_findings("drive", data, 8)
        self.assertIn("CRIT", levels(findings))
        self.assertTrue(any("endurance" in message for _, message in findings))

    def test_critical_flags_survive_read_errors_and_malformed_json(self):
        for status in (8 | 1, 8 | 2, 16 | 2, 8 | 4):
            for data in (nvme(), None, []):
                with self.subTest(status=status, data=type(data).__name__):
                    self.assertIn("CRIT", levels(hardware.smart_findings("drive", data, status)))
        with patch.object(hardware, "run", return_value=result("invalid", returncode=10)):
            self.assertIn("CRIT", levels(hardware.smart(["/dev/sda"])))

    def test_nvme_counter_ranges_and_types_are_validated(self):
        cases = [("available_spare", 999), ("available_spare_threshold", 101),
                 ("critical_warning", 256), ("percentage_used", 256),
                 ("media_errors", 1 << 128), ("media_errors", 0.5),
                 ("media_errors", 1 << 2000), ("available_spare", 100.0)]
        for key, value in cases:
            with self.subTest(key=key, value=str(value)[:20]):
                data = nvme()
                data["nvme_smart_health_information_log"][key] = value
                findings = hardware.smart_findings("drive", data, 0)
                self.assertIn("WARN", levels(findings))
                self.assertFalse(any("health and available counters passed" in m for _, m in findings))

    def test_open_command_failure_and_timeout_are_unknown_not_healthy(self):
        for status in (1, 2, -9, 300):
            self.assertEqual(levels(hardware.smart_findings("drive", {}, status)), {"WARN"})
        with patch.object(hardware, "run", side_effect=subprocess.TimeoutExpired("smartctl", 20)):
            self.assertEqual(levels(hardware.smart(["/dev/sda"])), {"WARN"})

    def test_partial_and_historical_exit_flags_warn(self):
        for status in (4, 32, 64, 128):
            self.assertIn("WARN", levels(hardware.smart_findings("drive", nvme(), status)))
        self.assertIn("CRIT", levels(hardware.smart_findings("drive", nvme(), 16)))

    def test_unsupported_optional_log_is_capability_gap_not_warning(self):
        data = nvme()
        data["smartctl"] = {"messages": [
            {"severity": "error", "string": "Read Self-test Log failed: Invalid Field in Command (0x002)"}]}
        findings = hardware.smart_findings("drive", data, 4)
        self.assertNotIn("WARN", levels(findings))
        self.assertTrue(any(level == "info" and "unsupported" in message for level, message in findings))

    def test_real_read_errors_and_malformed_messages_still_warn(self):
        for messages in (
            [{"severity": "error", "string": "Checksum error in SMART data structure"}],
            [{"severity": "error", "string": 42}],
            "not-a-list",
            [],
            [{"severity": "info", "string": "Read Self-test Log failed: Invalid Field in Command"}],
        ):
            data = nvme()
            data["smartctl"] = {"messages": messages}
            with self.subTest(messages=messages):
                self.assertIn("WARN", levels(hardware.smart_findings("drive", data, 4)))

    def test_crc_counter_threshold_prevents_single_lifetime_noise(self):
        def sata(crc):
            return {"smart_status": {"passed": True}, "temperature": {"current": 30},
                    "ata_smart_attributes": {"table": [{"id": 199, "raw": {"value": crc}}]}}
        findings = hardware.smart_findings("drive", sata(1), 0)
        self.assertNotIn("WARN", levels(findings))
        self.assertTrue(any(level == "OK" and "below alert threshold" in message for level, message in findings))
        self.assertIn("WARN", levels(hardware.smart_findings("drive", sata(20), 0)))
        with patch.dict(hardware.os.environ, {"SMART_CRC_WARN": "1"}):
            self.assertIn("WARN", levels(hardware.smart_findings("drive", sata(1), 0)))

    def test_missing_status_and_unknown_device_counters_warn(self):
        data = nvme()
        del data["smart_status"]
        self.assertIn("WARN", levels(hardware.smart_findings("drive", data, 0)))
        self.assertEqual(levels(hardware.smart_findings("drive", [], 0)), {"WARN"})
        self.assertIn("WARN", levels(hardware.smart_findings("drive", {"smart_status": {"passed": True}}, 0)))

    def test_only_exact_read_commands_and_no_serials_in_findings(self):
        data = nvme()
        data["serial_number"] = "PRIVATE-SERIAL"
        with patch.object(hardware.os, "geteuid", return_value=1000), \
             patch.object(hardware, "run", return_value=result(json.dumps(data))) as run:
            findings = hardware.smart(["/dev/nvme0n1", "/dev/sda"])
        self.assertEqual(run.call_args_list[0].args[0], [
            "/usr/bin/sudo", "-n", "--", "/usr/sbin/smartctl", "-a", "-j", "/dev/nvme0n1"])
        self.assertEqual(run.call_args_list[1].args[0][-1], "/dev/sda")
        self.assertNotIn("PRIVATE-SERIAL", str(findings))

    def test_no_devices_bad_paths_bad_json_and_missing_binary_warn(self):
        self.assertEqual(levels(hardware.smart([])), {"WARN"})
        with patch.object(hardware, "run") as run:
            self.assertEqual(levels(hardware.smart(["/dev/sda;reboot"])), {"WARN"})
            run.assert_not_called()
        with patch.object(hardware, "run", return_value=result("invalid")):
            self.assertEqual(levels(hardware.smart(["/dev/sda"])), {"WARN"})
        with patch.object(hardware, "run", side_effect=FileNotFoundError):
            self.assertEqual(levels(hardware.smart(["/dev/sda"])), {"WARN"})

    def test_sata_counters_and_vendor_normalized_remaining_life(self):
        data = {"smart_status": {"passed": True}, "temperature": {"current": 30},
                "ata_smart_attributes": {"table": [
                    {"id": 202, "name": "Percent_Lifetime_Remain", "value": 15,
                     "raw": {"value": 85}},
                    {"id": 5, "raw": {"value": 0}},
                ]}}
        findings = hardware.smart_findings("drive", data, 0)
        self.assertTrue(any(level == "WARN" and "85%" in message for level, message in findings))
        for attribute_id, expected in [(5, "WARN"), (197, "CRIT"), (198, "CRIT")]:
            changed = copy.deepcopy(data)
            changed["ata_smart_attributes"]["table"] = [{"id": attribute_id, "raw": {"value": 2}}]
            self.assertIn(expected, levels(hardware.smart_findings("drive", changed, 0)))
        # CRC errors warn only from the alert threshold (default 20), not on
        # single lifetime glitches — covered separately below.
        changed = copy.deepcopy(data)
        changed["ata_smart_attributes"]["table"] = [{"id": 199, "raw": {"value": 20}}]
        self.assertIn("WARN", levels(hardware.smart_findings("drive", changed, 0)))

    def test_temperature_thresholds(self):
        for temp, level in [(30, "OK"), (60, "WARN"), (70, "CRIT"), (None, "WARN"), (999, "WARN")]:
            self.assertEqual(hardware.temperature_finding("SSD", temp, 60, 70)[0], level)

    def test_malformed_ata_fields_do_not_pass(self):
        for attr in [{"id": []}, {"id": 5, "raw": {"value": True}},
                     {"id": 202, "name": "Percent_Lifetime_Remain", "value": 999}]:
            data = {"smart_status": {"passed": True}, "temperature": {"current": 30},
                    "ata_smart_attributes": {"table": [attr]}}
            self.assertIn("WARN", levels(hardware.smart_findings("drive", data, 0)))


class TemperatureTests(unittest.TestCase):
    def test_reads_hottest_supported_sensors_not_unreliable_acpi(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for index, (name, values) in enumerate([
                    ("k10temp", [41000, 43000]), ("nvme", [35000]), ("acpitz", [999000])]):
                hwmon = root / f"hwmon{index}"
                hwmon.mkdir()
                (hwmon / "name").write_text(name)
                for sensor, value in enumerate(values, 1):
                    (hwmon / f"temp{sensor}_input").write_text(str(value))
            findings = hardware.temperatures(root)
            self.assertEqual(levels(findings), {"OK"})
            self.assertTrue(any("43C" in message for _, message in findings))

    def test_missing_and_invalid_sensors_warn(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.assertEqual(levels(hardware.temperatures(root)), {"WARN"})
            hwmon = root / "hwmon0"
            hwmon.mkdir()
            (hwmon / "name").write_text("k10temp")
            (hwmon / "temp1_input").write_text("invalid")
            self.assertEqual(levels(hardware.temperatures(root)), {"WARN"})


class KernelTests(unittest.TestCase):
    def test_count_reports_not_multiline_records_and_classify_fatal(self):
        entries = [{"MESSAGE": message} for message in [
            "mce: [Hardware Error]: Machine check events logged",
            "[Hardware Error]: System Fatal error.",
            "[Hardware Error]: CPU:1 MC19_STATUS: 0xffffffff",
            "[Hardware Error]: Bank 19 is reserved.",
        ]]
        findings = hardware.kernel_findings(entries)
        self.assertEqual([message for level, message in findings if level == "CRIT"], [
            "kernel: 1 fatal/uncorrected-labelled hardware reports in last 25h"])
        self.assertNotIn("WARN", levels(findings))

    def test_corrected_edac_and_unclassified_hardware_warn(self):
        for message in ["[Hardware Error]: Corrected error, no action required.",
                        "EDAC MC0: 1 CE memory error", "mce: Machine check events logged",
                        "[Hardware Error]: Unknown bank"]:
            with self.subTest(message=message):
                self.assertIn("WARN", levels(hardware.kernel_findings([{"MESSAGE": message}])))

    def test_oom_storage_and_lockup_classification(self):
        entries = [{"MESSAGE": message} for message in [
            "oom-kill: preceding diagnostic, not a kill record",
            "Out of memory: Killed process 123 (python)",
            "Memory cgroup out of memory: Killed process 456 (python)",
            "EXT4-fs error: something failed", "watchdog: BUG: soft lockup",
        ]]
        findings = hardware.kernel_findings(entries)
        self.assertTrue(any("2 kernel OOM kill records" in message for _, message in findings))
        self.assertIn("CRIT", levels(findings))

    def test_uncorrected_edac_is_critical(self):
        for message in ["EDAC MC0: 1 UE on DIMM0", "EDAC MC0: uncorrectable memory error"]:
            self.assertIn("CRIT", levels(hardware.kernel_findings([{"MESSAGE": message}])))

    def test_case_variants_and_edac_without_literal_error_survive_filter(self):
        cases = [("edac MC0: 1 UE memory event", "CRIT"),
                 ("EDAC MC0: 1 uncorrectable memory event", "CRIT"),
                 ("EdAc MC0: corrected memory event", "WARN"),
                 ("[hardware error]: system fatal error.", "CRIT"),
                 ("out of memory: killed process 123", "WARN")]
        for message, expected in cases:
            with self.subTest(message=message):
                self.assertIsNotNone(re.search(hardware.KERNEL_PATTERN, message, re.I))
                # Simulate journalctl filtering, rather than testing the classifier alone.
                filtered = result(json.dumps({"MESSAGE": message}))
                with patch.object(hardware, "run", side_effect=[result('{"MESSAGE":"boot"}'), filtered]):
                    self.assertIn(expected, levels(hardware.kernel()))

    def test_no_edac_startup_false_alarm_or_uncorrected_double_count(self):
        self.assertIsNone(re.search(hardware.KERNEL_PATTERN, "EDAC MC: Ver: 3.0.0", re.I))
        findings = hardware.kernel_findings([{"MESSAGE": "EDAC MC0: Uncorrectable error"}])
        critical = [m for level, m in findings if level == "CRIT"]
        self.assertEqual(len(critical), 1)
        self.assertIn("EDAC", critical[0])

    def test_query_across_boots_and_distinguish_no_matches(self):
        with patch.object(hardware, "run", side_effect=[
                result('{"MESSAGE":"Linux boot"}'), result(returncode=1)]) as run:
            self.assertEqual(levels(hardware.kernel()), {"OK"})
        args = run.call_args_list[1].args[0]
        self.assertIn("_TRANSPORT=kernel", args)
        self.assertIn("25 hours ago", args)
        self.assertNotIn("-k", args)
        self.assertNotIn("-b", args)
        self.assertIn("--case-sensitive=no", args)

    def test_unreadable_empty_malformed_and_timed_out_journals_warn(self):
        bad = [result(returncode=1, stderr="Permission denied"), result(),
               result("invalid"), result("[]")]
        for probe in bad:
            with patch.object(hardware, "run", return_value=probe):
                self.assertEqual(levels(hardware.kernel()), {"WARN"})
        with patch.object(hardware, "run", side_effect=subprocess.TimeoutExpired("journalctl", 20)):
            self.assertEqual(levels(hardware.kernel()), {"WARN"})
        for query in [result(returncode=2), result(stderr="partial access"),
                      result("invalid"), result('{"MESSAGE":3}')]:
            with patch.object(hardware, "run", side_effect=[result('{"MESSAGE":"boot"}'), query]):
                self.assertEqual(levels(hardware.kernel()), {"WARN"})


# Shell integration tests use fake external probes, never live Docker/NAS/ntfy.
FAKE_COMMAND = r'''
import json, os, pathlib, sys, time
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
if name == "python3":
    if any(a.endswith("server-health-hardware.py") for a in args):
        if os.environ.get("MOCK_HEALTH_RUN_HANG") == "1":
            time.sleep(10)
        if os.environ.get("MOCK_HARDWARE_FAIL") == "1":
            sys.exit(1)
        print(os.environ.get("MOCK_HARDWARE", "OK|hardware checks passed"))
    else:
        os.execv(os.environ["REAL_PYTHON"], [os.environ["REAL_PYTHON"], *args])
elif name == "docker":
    if args[:1] == ["info"]:
        sys.exit(0)
    if args[:1] == ["ps"]:
        if "-a" in args:
            sys.exit(int(os.environ.get("MOCK_STOPPED_FAIL", "0")))
        if "-q" in args:
            if os.environ.get("MOCK_DOCKER_HANG") == "ids":
                time.sleep(10)
            print("abc")
        else:
            print("app|" + os.environ.get("MOCK_CONTAINER_STATUS", "Up 1 hour"))
    elif args[:1] == ["inspect"]:
        if "RestartCount" in args[2]:
            if os.environ.get("MOCK_RESTART_FAIL") == "1":
                sys.exit(1)
            print("/app|" + os.environ.get("MOCK_RESTARTS", "0"))
        elif "com.docker.compose.project" in args[2]:
            if os.environ.get("MOCK_DOCKER_HANG") == "stray":
                time.sleep(10)
            print("/app|project")
elif name == "df":
    if "-Pi" in args:
        if os.environ.get("MOCK_INODE_FAIL") == "1":
            sys.exit(1)
        print("Filesystem Inodes IUsed IFree IUse% Mounted on")
        print("/dev/test 1000 100 900 10% /")
    else:
        print("Filesystem 1024-blocks Used Available Capacity Mounted on")
        print("/dev/test 1000 100 900 10% /")
elif name == "systemctl":
    sys.exit(int(os.environ.get("MOCK_SYSTEMD_FAIL", "0")))
elif name == "rocm-smi":
    print(json.dumps({"card0": {"Temperature (Sensor edge) (C)": 32}}))
elif name == "ls":
    print("backup-data")
elif name == "ps":
    print("S")
elif name == "curl":
    headers = sys.stdin.read()
    if os.environ.get("MOCK_CURL_CAPTURE"):
        pathlib.Path(os.environ["MOCK_CURL_CAPTURE"]).write_text(json.dumps({
            "args": args, "headers": headers, "env": dict(os.environ)}))
    sys.exit(int(os.environ.get("MOCK_CURL_FAIL", "0")))
else:
    raise RuntimeError(name)
'''


class ShellTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        bin_path = root / "bin"
        bin_path.mkdir()
        for name in ("python3", "docker", "df", "systemctl", "rocm-smi", "ls", "ps", "curl"):
            path = bin_path / name
            path.write_text(f"#!{sys.executable}\n" + FAKE_COMMAND)
            path.chmod(0o755)
        dated = root / subprocess.check_output(["date", "+%Y-%m-%d"], text=True).strip()
        (dated / "stacks").mkdir(parents=True)
        latest = root / "latest"
        latest.symlink_to(dated)
        self.token = "tk_REVIEW_DUMMY_TOKEN"
        token_file = root / "token"
        token_file.write_text(self.token + "\n")
        token_file.chmod(0o600)
        self.env = {**os.environ, "PATH": str(bin_path) + ":" + os.environ["PATH"],
                    "REAL_PYTHON": sys.executable, "BACKUP_LATEST": str(latest),
                    "NAS_MOUNTS": str(root), "NTFY_TOKEN_FILE": str(token_file),
                    "MEM_WARN": "101", "MEM_CRIT": "102", "SWAP_WARN": "101",
                    "HEALTH_RUN_TIMEOUT": "180", "HEALTH_PROBE_TIMEOUT": "10",
                    # Never write the user's real state: every run gets a
                    # throwaway state dir unless a test overrides it.
                    "HEALTH_STATE_DIR": str(root / "state")}

    def check_report(self, changes=None, flags=()):
        run = subprocess.run(["bash", str(SCRIPTS / "server-health.sh"), "--json", *flags],
                             env={**self.env, **(changes or {})}, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.stderr, "", run.stderr)
        return run.returncode, json.loads(run.stdout)

    def test_clean_json_compatible_and_healthy(self):
        code, report = self.check_report()
        self.assertEqual(code, 0)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["issues"], [])

    def test_restart_delimiter_bug_regression(self):
        code, report = self.check_report({"MOCK_RESTARTS": "7"})
        self.assertEqual(code, 1)
        self.assertTrue(any("container 'app' restarted 7x" in i["message"] for i in report["issues"]))

    def test_restart_threshold_and_invalid_count(self):
        self.assertEqual(self.check_report({"MOCK_RESTARTS": "4"})[0], 0)
        self.assertEqual(self.check_report({"MOCK_RESTARTS": "5"})[0], 1)
        self.assertEqual(self.check_report({"MOCK_RESTARTS": "invalid"})[0], 1)

    def test_restarting_is_critical_even_below_restart_threshold(self):
        code, report = self.check_report({"MOCK_CONTAINER_STATUS": "Restarting (1) 2 seconds ago"})
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "crit")

    def test_failed_checks_cannot_return_all_ok(self):
        for setting in ("MOCK_RESTART_FAIL", "MOCK_STOPPED_FAIL", "MOCK_INODE_FAIL",
                        "MOCK_SYSTEMD_FAIL", "MOCK_HARDWARE_FAIL"):
            with self.subTest(setting=setting):
                code, report = self.check_report({setting: "1"})
                self.assertEqual(code, 1)
                self.assertGreater(report["warnings"], 0)

    def test_hardware_critical_and_missing_permissions_flow_into_json(self):
        self.assertEqual(self.check_report({"MOCK_HARDWARE": "CRIT|fatal MCE"})[0], 2)
        code, report = self.check_report({"MOCK_HARDWARE": "WARN|SMART health unknown"})
        self.assertEqual(code, 1)
        self.assertTrue(any(i["message"] == "SMART health unknown" for i in report["issues"]))

    def test_failed_healthy_heartbeat_is_visible_in_json_and_exit_code(self):
        code, report = self.check_report({"MOCK_CURL_FAIL": "1"}, ["--notify-always"])
        self.assertEqual(code, 1)
        self.assertEqual(report["warnings"], 1)
        self.assertIn("delivery unconfirmed", report["issues"][0]["message"])

    def test_failed_warning_notification_and_successful_heartbeat(self):
        code, report = self.check_report({"MOCK_RESTARTS": "7", "MOCK_CURL_FAIL": "1"}, ["--notify"])
        self.assertEqual(code, 1)
        self.assertEqual(report["warnings"], 2)
        self.assertEqual(self.check_report(flags=["--notify-always"])[0], 0)

    def test_notification_token_is_only_in_header_stdin_not_argv_or_env(self):
        capture = Path(self.tmp.name) / "curl.json"
        code, report = self.check_report({"MOCK_CURL_CAPTURE": str(capture)}, ["--notify-always"])
        self.assertEqual(code, 0)
        sent = json.loads(capture.read_text())
        self.assertEqual(sent["headers"], "Authorization: Bearer " + self.token + "\n")
        self.assertIn("@-", sent["args"])
        self.assertIn("--data-raw", sent["args"])
        self.assertEqual(sent["args"][0], "-q")
        for value in (sent["args"], sent["env"], report):
            self.assertNotIn(self.token, json.dumps(value))

    def test_backup_notification_uses_same_private_header_channel(self):
        # Execute only the extracted notify function, NEVER the backup job.
        source = (SCRIPTS / "backup.sh").read_text()
        function = source.split("notify() {", 1)[1].split("\n}\n", 1)[0]
        code = ('set -euo pipefail\nlog() { printf "%s\\n" "$*"; }\n'
                'notify() {' + function + '\n}\nnotify "$@"\n')
        capture = Path(self.tmp.name) / "backup-curl.json"
        env = {**self.env, "NTFY_HOST": "http://localhost:10000", "NTFY_TOPIC": "test-topic",
               "MOCK_CURL_CAPTURE": str(capture)}
        for fail in ("0", "1"):
            run = subprocess.run(["bash", "-c", code, "test", "Backup test", "low", "@not-a-file"],
                                 env={**env, "MOCK_CURL_FAIL": fail}, text=True, capture_output=True, timeout=10)
            self.assertEqual(run.returncode, 0, run.stderr)
            sent = json.loads(capture.read_text())
            self.assertEqual(sent["headers"], "Authorization: Bearer " + self.token + "\n")
            self.assertNotIn(self.token, json.dumps(sent["args"]))
            self.assertNotIn(self.token, json.dumps(sent["env"]))
            self.assertIn("--data-raw", sent["args"])
            self.assertEqual("WARN:" in run.stdout, fail == "1")

    def test_malformed_or_missing_token_never_reaches_curl(self):
        capture = Path(self.tmp.name) / "curl.json"
        for token in ("", "tk_bad\nInjected: header", "bad token", "x" * 4097):
            Path(self.env["NTFY_TOKEN_FILE"]).write_text(token)
            self.assertEqual(self.check_report({"MOCK_CURL_CAPTURE": str(capture)}, ["--notify-always"])[0], 1)
            self.assertFalse(capture.exists())
        Path(self.env["NTFY_TOKEN_FILE"]).unlink()
        self.assertEqual(self.check_report(flags=["--notify-always"])[0], 1)

    def test_docker_listing_and_project_inspect_timeouts_report_unknown(self):
        for target in ("ids", "stray"):
            with self.subTest(target=target):
                started = time.monotonic()
                code, report = self.check_report({"MOCK_DOCKER_HANG": target, "HEALTH_PROBE_TIMEOUT": "0.5"})
                self.assertEqual(code, 1)
                self.assertLess(time.monotonic() - started, 6)
                self.assertTrue(any("coverage unknown" in i["message"] for i in report["issues"]))

    def test_whole_run_deadline_returns_complete_critical_json_and_can_notify(self):
        capture = Path(self.tmp.name) / "curl.json"
        started = time.monotonic()
        code, report = self.check_report({"MOCK_HEALTH_RUN_HANG": "1", "HEALTH_RUN_TIMEOUT": "1",
                                          "MOCK_CURL_CAPTURE": str(capture)}, ["--notify-always"])
        self.assertEqual(code, 2)
        self.assertLess(time.monotonic() - started, 6)
        self.assertEqual(report["status"], "crit")
        self.assertIsNone(report["memory_percent"])
        self.assertIn("worker did not complete", report["issues"][0]["message"])
        self.assertNotIn(self.token, json.dumps(report))
        self.assertTrue(capture.exists())

    def test_actual_curl_keeps_dummy_token_out_of_process_arguments(self):
        # Loopback-only receiver, never the actual ntfy service or real token.
        accepted = threading.Event()
        release = threading.Event()
        seen = []

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen.append(self.headers.get("Authorization"))
                accepted.set()
                release.wait(10)
                self.send_response(204)
                self.end_headers()

            def log_message(self, *args):
                pass

        server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.handle_request, daemon=True)
        thread.start()
        curl = Path(self.tmp.name) / "bin" / "curl"
        curl.unlink()
        curl.symlink_to("/usr/bin/curl")
        env = {**self.env, "NTFY_HOST": "http://127.0.0.1:" + str(server.server_port),
               "no_proxy": "127.0.0.1", "NO_PROXY": "127.0.0.1"}
        process = subprocess.Popen(["bash", str(SCRIPTS / "server-health.sh"), "--json", "--notify-always"],
                                   env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            self.assertTrue(accepted.wait(10), "loopback request never arrived")
            pending = [process.pid]
            found_curl = False
            while pending:
                pid = pending.pop()
                root = Path("/proc") / str(pid)
                try:
                    args = (root / "cmdline").read_bytes()
                    environ = (root / "environ").read_bytes()
                    pending.extend(map(int, (root / "task" / str(pid) / "children").read_text().split()))
                except FileNotFoundError:
                    continue  # A short-lived printf/probe may have exited.
                self.assertNotIn(self.token.encode(), args)
                self.assertNotIn(self.token.encode(), environ)
                found_curl |= Path(os.fsdecode(args.split(b"\0", 1)[0])).name == "curl"
            self.assertTrue(found_curl)
            self.assertEqual(seen, ["Bearer " + self.token])
        finally:
            release.set()
            stdout, stderr = process.communicate(timeout=15)
            thread.join(timeout=15)
            server.server_close()
        self.assertEqual(process.returncode, 0, stderr)
        self.assertEqual(json.loads(stdout)["status"], "ok")

    def test_quiet_clean_output_and_malformed_hardware_output(self):
        run = subprocess.run(["bash", str(SCRIPTS / "server-health.sh"), "--quiet"],
                             env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        for output in ("", "invalid-output", "OK", "OK|", "OK|  ", "OK|extra|field", "OK|bad\rmessage"):
            code, report = self.check_report({"MOCK_HARDWARE": output})
            self.assertEqual(code, 1)
            self.assertGreater(report["warnings"], 0)

    # ---- dashboard state file (HEALTH_STATE_DIR) --------------------------
    # setUp points HEALTH_STATE_DIR at a throwaway temp dir, so no test ever
    # writes the user's real ~/.local/state/server-health.

    def test_json_details_holds_ok_and_info_only_and_issues_warn_crit_only(self):
        code, report = self.check_report({"MOCK_HARDWARE": "OK|temps fine\ninfo|capability note"})
        self.assertEqual(code, 0)
        self.assertEqual(report["issues"], [])
        self.assertIn("details", report)
        self.assertIn("OK", {d["level"] for d in report["details"]})
        self.assertIn("info", {d["level"] for d in report["details"]})
        self.assertTrue(all(d["level"] in ("OK", "info") for d in report["details"]))
        self.assertTrue(any("temps fine" in d["message"] for d in report["details"]))
        code, report = self.check_report({"MOCK_HARDWARE": "OK|temps fine\ninfo|capability note",
                                          "MOCK_RESTARTS": "7"})
        self.assertEqual(code, 1)
        self.assertTrue(all(i["level"] in ("WARN", "CRIT") for i in report["issues"]))
        self.assertTrue(any("restarted 7x" in i["message"] for i in report["issues"]))
        self.assertTrue(all(d["level"] in ("OK", "info") for d in report["details"]))
        self.assertFalse(any("restarted 7x" in d["message"] for d in report["details"]))

    def test_state_file_written_on_json_run_and_identical_to_stdout(self):
        state = Path(self.env["HEALTH_STATE_DIR"])
        run = subprocess.run(["bash", str(SCRIPTS / "server-health.sh"), "--json"],
                             env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual(run.stderr, "", run.stderr)
        self.assertEqual(run.returncode, 0)
        report = json.loads(run.stdout)  # stdout stays pure JSON while state is written
        path = state / "latest.json"
        self.assertTrue(path.is_file())
        self.assertEqual(json.loads(path.read_text()), report)
        self.assertEqual(path.read_text(), run.stdout)  # byte-identical document

    def test_state_file_written_on_plain_quiet_run(self):
        state = Path(self.env["HEALTH_STATE_DIR"])
        run = subprocess.run(["bash", str(SCRIPTS / "server-health.sh"), "--quiet"],
                             env=self.env, text=True, capture_output=True, timeout=30)
        self.assertEqual((run.returncode, run.stdout, run.stderr), (0, "", ""))
        saved = json.loads((state / "latest.json").read_text())
        self.assertEqual(saved["status"], "ok")
        self.assertEqual(saved["issues"], [])
        self.assertGreater(len(saved["details"]), 0)
        self.assertTrue(all(d["level"] in ("OK", "info") for d in saved["details"]))

    def test_empty_state_dir_disables_state_file(self):
        state = Path(self.env["HEALTH_STATE_DIR"])
        code, report = self.check_report({"HEALTH_STATE_DIR": ""})
        self.assertEqual(code, 0)
        self.assertIn("details", report)
        self.assertFalse(state.exists())

    def test_custom_state_dir_is_honored(self):
        custom = Path(self.tmp.name) / "elsewhere" / "health"
        code, report = self.check_report({"HEALTH_STATE_DIR": str(custom)})
        self.assertEqual(code, 0)
        self.assertEqual(json.loads((custom / "latest.json").read_text()), report)
        self.assertFalse((Path(self.env["HEALTH_STATE_DIR"]) / "latest.json").exists())

    def test_state_dir_and_file_permissions(self):
        state = Path(self.env["HEALTH_STATE_DIR"])
        self.check_report()
        path = state / "latest.json"
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(state.stat().st_mode & 0o777, 0o700)

    def test_no_leftover_temp_files_in_state_dir(self):
        state = Path(self.env["HEALTH_STATE_DIR"])
        self.check_report()
        self.check_report({"MOCK_RESTARTS": "7"})
        self.assertEqual([p.name for p in state.iterdir()], ["latest.json"])

    def test_deadline_supervisor_writes_crit_state_file(self):
        state = Path(self.env["HEALTH_STATE_DIR"])
        code, report = self.check_report({"MOCK_HEALTH_RUN_HANG": "1", "HEALTH_RUN_TIMEOUT": "1"})
        self.assertEqual(code, 2)
        self.assertEqual(report["status"], "crit")
        saved = json.loads((state / "latest.json").read_text())
        self.assertEqual(saved, report)
        self.assertIsNone(saved["load"])
        self.assertIsNone(saved["cpus"])
        self.assertIsNone(saved["memory_percent"])
        self.assertEqual(saved["disks"], [])
        self.assertEqual(saved["details"], [])
        self.assertTrue(any("worker did not complete" in i["message"] for i in saved["issues"]))
        self.assertEqual([p.name for p in state.iterdir()], ["latest.json"])

    def test_state_write_failure_warns_once_on_stderr_and_keeps_exit_code(self):
        blocker = Path(self.tmp.name) / "blocker"
        blocker.write_text("regular file, not a directory")
        run = subprocess.run(["bash", str(SCRIPTS / "server-health.sh"), "--json"],
                             env={**self.env, "HEALTH_STATE_DIR": str(blocker)},
                             text=True, capture_output=True, timeout=30)
        self.assertEqual(run.returncode, 0)
        report = json.loads(run.stdout)  # stdout stays pure JSON despite the failure
        self.assertEqual(report["status"], "ok")
        self.assertEqual(len(run.stderr.strip().splitlines()), 1)
        self.assertIn("state file", run.stderr)

    def test_exit_codes_unchanged_with_state_writing_enabled(self):
        self.assertEqual(self.check_report()[0], 0)
        self.assertEqual(self.check_report({"MOCK_RESTARTS": "7"})[0], 1)
        self.assertEqual(self.check_report({"MOCK_CONTAINER_STATUS": "Restarting (1) 2 seconds ago"})[0], 2)

    def test_sudoers_allows_only_exact_read_commands(self):
        lines = [line for line in (SCRIPTS / "server-health-smart.sudoers").read_text().splitlines()
                 if line and not line.startswith("#")]
        self.assertEqual(lines, ["johannes ALL=(root) NOPASSWD: NOSETENV: "
                                 "/usr/sbin/smartctl -a -j /dev/nvme0n1, "
                                 "/usr/sbin/smartctl -a -j /dev/sda"])


if __name__ == "__main__":
    unittest.main()
