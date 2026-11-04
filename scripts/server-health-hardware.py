#!/usr/bin/env python3
"""Read-only hardware probes for server-health.sh (standard library only).

Outputs LEVEL|message records. Findings do not change the exit status; a failed
collector does, so the caller can distinguish unknown coverage from good health.
No persistent state, notifications, device tests, or device-setting changes.
"""

import argparse
import json
import math
import os
from pathlib import Path
import re
import subprocess


def run(args, timeout=20):
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout)


def number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    # Avoid converting arbitrarily large JSON integers to float.
    return value if value >= 0 and (isinstance(value, int) or math.isfinite(value)) else None


def counter(value, maximum):
    """SMART counters are integers, not booleans, fractions or numeric strings."""
    return value if type(value) is int and 0 <= value <= maximum else None


def threshold(name, default):
    value = float(os.environ.get(name, default))
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"invalid {name}")
    return value


def temperature_finding(label, value, warn, crit):
    if number(value) is None or value > 150:
        return "WARN", f"{label}: temperature unavailable or invalid"
    level = "CRIT" if value >= crit else "WARN" if value >= warn else "OK"
    return level, f"{label}: {value:g}C"


def temperatures(root=Path("/sys/class/hwmon")):
    findings = []
    found_cpu = False
    found_nvme = False
    for hwmon in sorted(root.glob("hwmon*")):
        try:
            driver = (hwmon / "name").read_text().strip()
        except OSError:
            continue
        cpu = driver in {"k10temp", "coretemp", "zenpower", "cpu_thermal"}
        if not cpu and driver != "nvme":
            continue  # ACPI and board sensors may be uncalibrated/unlabelled.
        found_cpu |= cpu
        found_nvme |= driver == "nvme"
        label = "CPU" if cpu else f"NVMe ({hwmon.name})"
        readings = []
        for sensor in hwmon.glob("temp*_input"):
            try:
                value = float(sensor.read_text()) / 1000
                if number(value) is None or value > 150:
                    raise ValueError("invalid sensor reading")
                readings.append(value)
            except (OSError, ValueError):
                findings.append(("WARN", f"{label}: temperature sensor unreadable/invalid"))
        if readings:
            prefix = "CPU" if cpu else "SSD"
            findings.append(temperature_finding(
                label + " hottest sensor", max(readings),
                threshold(prefix + "_TEMP_WARN", 85 if cpu else 60),
                threshold(prefix + "_TEMP_CRIT", 95 if cpu else 70),
            ))
        else:
            findings.append(("WARN", f"{label}: no readable temperature sensors"))
    if not found_cpu:
        findings.append(("WARN", "CPU temperature unknown (no supported hwmon sensor)"))
    if not found_nvme:
        findings.append(("WARN", "NVMe temperature unknown (no hwmon sensor)"))
    return findings


def unsupported_only(data):
    """True when every smartctl error message is an unsupported-command failure.

    Drives commonly lack optional log pages (e.g. the NVMe self-test log); the
    "some command failed" exit bit then reflects a capability gap, not bad
    health. Malformed or unrecognized messages stay conservative (False)."""
    header = data.get("smartctl")
    if not isinstance(header, dict):
        return False
    messages = header.get("messages")
    if not isinstance(messages, list) or not messages:
        return False
    errors = 0
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("string"), str):
            return False
        if message.get("severity") == "error":
            errors += 1
            text = message["string"].lower()
            if not any(marker in text for marker in ("invalid field", "not supported", "unsupported")):
                return False
    return errors > 0


def smart_findings(device, data, exit_status):
    findings = []
    if type(exit_status) is not int or not 0 <= exit_status <= 255:
        return [("WARN", f"SMART {device}: invalid command status (health unknown)")]
    # smartctl uses a bitmask: a nonzero result can contain valid health data.
    # Preserve critical flags even when a read failed or the JSON is malformed.
    if exit_status & 24:
        findings.append(("CRIT", f"SMART {device}: failing health/attribute status"))
    if exit_status & 224:
        findings.append(("WARN", f"SMART {device}: historical attribute/error/self-test log flags"))
    # A failed/denied read is the root cause even when stdout is unparseable.
    if exit_status & 3:
        return findings + [("WARN", f"SMART {device}: read denied/failed; check restricted sudo setup")]
    if not isinstance(data, dict):
        return findings + [("WARN", f"SMART {device}: invalid JSON object (health unknown)")]

    status = data.get("smart_status")
    passed = status.get("passed") if isinstance(status, dict) else None
    if passed is False:
        findings.append(("CRIT", f"SMART {device}: overall health FAILED"))
    elif passed is not True:
        findings.append(("WARN", f"SMART {device}: overall health status unavailable"))
    if exit_status & 4:
        if unsupported_only(data):
            findings.append(("info", f"SMART {device}: optional log(s) unsupported by the drive — capability gap, not a health failure"))
        else:
            findings.append(("WARN", f"SMART {device}: some read commands failed (partial coverage)"))

    temp = data.get("temperature")
    current = temp.get("current") if isinstance(temp, dict) else None
    findings.append(temperature_finding(
        f"SSD {device}", current,
        threshold("SSD_TEMP_WARN", 60), threshold("SSD_TEMP_CRIT", 70),
    ))
    nvme = data.get("nvme_smart_health_information_log")
    if isinstance(nvme, dict):
        limits = {"critical_warning": 255, "available_spare": 100,
                  "available_spare_threshold": 100, "percentage_used": 255,
                  "media_errors": (1 << 128) - 1}
        values = {key: counter(nvme.get(key), maximum) for key, maximum in limits.items()}
        for key, value in values.items():
            if value is None:
                findings.append(("WARN", f"NVMe {device}: missing/invalid {key} (partial coverage)"))
        critical = values["critical_warning"]
        if critical:
            findings.append(("CRIT", f"NVMe {device}: critical-warning flags {critical:g}"))
        spare = values["available_spare"]
        minimum = values["available_spare_threshold"]
        if spare is not None and minimum is not None and spare < minimum:
            findings.append(("CRIT", f"NVMe {device}: spare {spare:g}% below threshold {minimum:g}%"))
        wear = values["percentage_used"]
        if wear is not None:
            level = "CRIT" if wear >= 100 else "WARN" if wear >= threshold("SSD_WEAR_WARN", 80) else "OK"
            findings.append((level, f"NVMe {device}: endurance used {wear:g}%"))
        errors = values["media_errors"]
        if errors:
            findings.append(("CRIT", f"NVMe {device}: {errors:g} lifetime media/data-integrity errors"))
        # Error-log entries and unsafe shutdowns alone do not prove bad media.
        # Unsupported commands and power loss can increment these counters.
    else:
        attrs = data.get("ata_smart_attributes", {})
        table = attrs.get("table") if isinstance(attrs, dict) else None
        if not isinstance(table, list) or not table:
            findings.append(("WARN", f"SMART {device}: detailed health counters unavailable"))
        else:
            counters = {5: ("WARN", "reallocated sectors"),
                        184: ("CRIT", "end-to-end errors"),
                        187: ("CRIT", "reported uncorrectable errors"),
                        197: ("CRIT", "pending sectors"),
                        198: ("CRIT", "offline uncorrectable sectors"),
                        199: ("WARN", "interface CRC errors (check SATA cabling)")}
            for attr in table:
                if not isinstance(attr, dict):
                    findings.append(("WARN", f"SMART {device}: invalid ATA attribute"))
                    continue
                attribute_id = counter(attr.get("id"), 255)
                if attribute_id is None:
                    findings.append(("WARN", f"SMART {device}: invalid ATA attribute ID"))
                    continue
                if attribute_id in counters:
                    raw = attr.get("raw", {})
                    value = counter(raw.get("value"), (1 << 64) - 1) if isinstance(raw, dict) else None
                    if value is None:
                        findings.append(("WARN", f"SMART {device}: unreadable ATA counter {attribute_id}"))
                    elif value:
                        level, label = counters[attribute_id]
                        if attribute_id == 199 and value < threshold("SMART_CRC_WARN", 20):
                            # A single lifetime interface CRC glitch is common and
                            # not actionable; a failing cable grows this fast.
                            findings.append(("OK", f"SMART {device}: {value:g} lifetime {label} (below alert threshold)"))
                        else:
                            findings.append((level, f"SMART {device}: {value:g} lifetime {label}"))
                # Only known remaining-life names; raw wear is vendor-specific.
                if attr.get("name") in {"Percent_Lifetime_Remain", "SSD_Life_Left"}:
                    remaining = counter(attr.get("value"), 100)
                    if remaining is not None:
                        used = 100 - remaining
                        level = "CRIT" if used >= 100 else "WARN" if used >= threshold("SSD_WEAR_WARN", 80) else "OK"
                        findings.append((level, f"SMART {device}: endurance used {used:g}%"))
                    else:
                        findings.append(("WARN", f"SMART {device}: invalid remaining-life value"))
    if passed is True and not any(level in {"WARN", "CRIT"} for level, _ in findings):
        findings.append(("OK", f"SMART {device}: health and available counters passed"))
    return findings


def smart(devices=None):
    devices = devices if devices is not None else os.environ.get(
        "SMART_DEVICES", "/dev/nvme0n1 /dev/sda").split()
    if not devices:
        return [("WARN", "SMART coverage unknown (no drives configured)")]
    findings = []
    for device in devices:
        if not re.fullmatch(r"/dev/(?:nvme\d+n\d+|sd[a-z]+)", device):
            findings.append(("WARN", "SMART: invalid configured drive path"))
            continue
        args = ["/usr/sbin/smartctl", "-a", "-j", device]
        if os.geteuid() != 0:
            args = ["/usr/bin/sudo", "-n", "--", *args]
        try:
            result = run(args)
            try:
                data = json.loads(result.stdout)
            except ValueError:
                data = None  # Exit-status critical flags must survive invalid JSON.
            findings.extend(smart_findings(device, data, result.returncode))
        except (OSError, subprocess.TimeoutExpired, ValueError):
            # Never include raw device output/stderr (serial numbers, etc.).
            findings.append(("WARN", f"SMART {device}: unavailable; check smartmontools/restricted sudo setup"))
    return findings


EDAC_PATTERN = r"\bEDAC\b.*(?:\b(?:CE|UE)\b|(?:un)?correct(?:ed|able)|error)"
STORAGE_PATTERN = r"I/O error|EXT4-fs error|BTRFS.*(?:error|corrupt)|nvme.*(?:I/O.*timeout|reset controller|controller is down)"
LOCKUP_PATTERN = r"watchdog.*(?:soft lockup|hard LOCKUP)"
OOM_PATTERN = r"out of memory: Killed process"
HARDWARE_PATTERN = r"Hardware Error|Machine check|MC\d+_STATUS|System Fatal error|Uncorrect(?:ed|able) error"
KERNEL_PATTERN = "|".join(f"(?:{pattern})" for pattern in (
    HARDWARE_PATTERN, EDAC_PATTERN, OOM_PATTERN, STORAGE_PATTERN, LOCKUP_PATTERN))


def kernel_findings(entries):
    messages = []
    for entry in entries:
        if not isinstance(entry, dict) or not isinstance(entry.get("MESSAGE"), str):
            raise ValueError("invalid kernel journal entry")
        messages.append(entry["MESSAGE"])
    edac_messages = [m for m in messages if re.search(EDAC_PATTERN, m, re.I)]
    mce_messages = [m for m in messages if not re.search(EDAC_PATTERN, m, re.I)]
    fatal = sum(bool(re.search(r"System Fatal error|Uncorrected error|Uncorrectable error", m, re.I)) for m in mce_messages)
    corrected = sum(bool(re.search(r"\bCorrected error", m, re.I)) for m in mce_messages)
    records = sum(bool(re.search(r"MC\d+_STATUS", m, re.I)) for m in mce_messages)
    announcements = sum(bool(re.search("Machine check events logged", m, re.I)) for m in mce_messages)
    edac_uncorrected = sum(bool(re.search(r"\bUE\b|uncorrect(?:ed|able)", m, re.I)) for m in edac_messages)
    other_hardware = any(re.search(HARDWARE_PATTERN, m, re.I) for m in mce_messages)
    unknown = max(0, max(records, announcements) - fatal - corrected)
    findings = []
    if fatal:
        findings.append(("CRIT", f"kernel: {fatal} fatal/uncorrected-labelled hardware reports in last 25h"))
    if corrected:
        findings.append(("WARN", f"kernel: {corrected} corrected hardware reports in last 25h"))
    if edac_uncorrected:
        findings.append(("CRIT", f"kernel: {edac_uncorrected} uncorrected EDAC error log entries in last 25h"))
    if len(edac_messages) > edac_uncorrected:
        findings.append(("WARN", "kernel: corrected/unclassified EDAC error log entries in last 25h"))
    if unknown or (other_hardware and not fatal and not corrected):
        findings.append(("WARN", "kernel: additional/unclassified MCE or EDAC reports in last 25h"))
    if not findings:
        findings.append(("OK", "no MCE/EDAC hardware reports in last 25h (across boots)"))
    oom = sum(bool(re.search(OOM_PATTERN, m, re.I)) for m in messages)
    findings.append(("WARN" if oom else "OK", f"{oom} kernel OOM kill records in last 25h (across boots)"))
    storage = sum(bool(re.search(STORAGE_PATTERN, m, re.I)) for m in messages)
    if storage:
        findings.append(("CRIT", f"kernel: {storage} storage/filesystem error log entries in last 25h"))
    if any(re.search(LOCKUP_PATTERN, m, re.I) for m in messages):
        findings.append(("CRIT", "kernel: CPU lockup reports in last 25h"))
    return findings


def kernel():
    base = ["journalctl", "_TRANSPORT=kernel", "--no-pager", "-o", "json"]
    try:
        # An empty/inaccessible journal must not be interpreted as good health.
        probe = run([*base, "-n", "1"])
        if probe.returncode or probe.stderr.strip() or not probe.stdout.strip():
            raise ValueError("cannot verify kernel journal coverage")
        entry = json.loads(probe.stdout.strip())
        if not isinstance(entry, dict) or not isinstance(entry.get("MESSAGE"), str):
            raise ValueError("invalid journal probe")
        result = run([*base, "--since", "25 hours ago", "--case-sensitive=no", "--grep", KERNEL_PATTERN])
        # journalctl --grep returns 1 when nothing matches, not just on failure.
        if result.stderr.strip() or result.returncode not in (0, 1):
            raise ValueError("kernel query failed")
        if result.returncode == 1 and result.stdout.strip():
            raise ValueError("partial kernel query")
        entries = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
        return kernel_findings(entries)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return [("WARN", "kernel hardware/OOM/storage checks unavailable (journal unreadable, query failed or timed out)")]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("section", choices=("temperatures", "smart", "kernel"))
    args = parser.parse_args()
    try:
        findings = {"temperatures": temperatures, "smart": smart, "kernel": kernel}[args.section]()
        for level, message in findings:
            print(level + "|" + message.replace("\n", " ").replace("|", "/"))
    except (ValueError, TypeError, KeyError):
        return 1  # Caller reports unknown coverage, including invalid thresholds.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
