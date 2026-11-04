# Daily server health monitor

`server-health.sh` remains the single daily report/ntfy publisher, scheduled by
the existing `johannes` crontab at **07:00 Europe/Vienna**. No new timer or
automatic reboot is required. Run a read-only snapshot without sending a push:

```bash
/opt/stacks/scripts/server-health.sh --no-color
/opt/stacks/scripts/server-health.sh --json
```

Exit codes: `0` healthy, `1` warnings or unknown coverage, `2` critical findings.
`--notify` sends problems; `--notify-always` also sends the healthy heartbeat.
Delivery failures are included in the final report and produce at least exit 1.
Clean quiet runs still print nothing; an unchanged log is not proof cron failed.

The bearer token is read from the existing private token file and supplied to
curl through an anonymous pipe (`-H @-`), never a command-line argument, exported
environment variable, or new temporary file. Token syntax is validated to
prevent injected headers. Curl's user configuration is disabled for this call.
A missing/invalid token is a delivery warning; no unauthenticated fallback.
`backup.sh` uses the same private-header channel for this shared token; its
backup operations/schedule and warning-only notification-failure behavior are
unchanged.

The unprivileged worker has a **180-second overall deadline**, with a five-second
kill grace. A supervisor discards partial output and returns a complete critical
report if the worker times out or terminates unexpectedly; requested alerts are
then attempted separately (up to ten seconds). Resource fields are `null` in
this incomplete-coverage JSON report, not invented healthy readings.
Docker probes have individual ten-second deadlines with two-second kill grace;
inner timeouts remain in the worker's process group so the supervisor can
terminate the unprivileged probe tree. `HEALTH_RUN_TIMEOUT` (positive integer
seconds) and `HEALTH_PROBE_TIMEOUT` (positive seconds, at most 60) are configurable.
As with any userspace timeout, uninterruptible kernel I/O and independently
privileged children cannot be guaranteed killable by the unprivileged monitor.

## Dashboard state file

Every completed run — the human-readable and `--quiet` cron invocations
(`--quiet --notify-always`) as well as the supervisor's deadline report —
atomically writes `~/.local/state/server-health/latest.json` (directory
`0700`, file `0600`, written via a temp file plus `os.replace`, so a reader
never sees a partial document; no temp files are left behind). The file
content is exactly the `--json` document of that run. Set `HEALTH_STATE_DIR`
to override the directory; set it **empty** (`HEALTH_STATE_DIR=`) to disable
the state write entirely. A failed state write is a single stderr warning and
never changes the exit code.

The JSON document gains a `details` array holding the `OK` and `info`
findings (`[{"level": "OK", "message": ...}, ...]`); `issues` keeps its
existing meaning — `WARN`/`CRIT` findings only, still used for the ntfy body,
the warning/critical counts and the exit code. In the deadline report the
resource fields are `null`, `disks` and `details` are empty, and `issues`
holds the deadline `CRIT`.

The HOMELAB//CTRL dashboard reads the state file through a read-only bind
mount at container path `/run/server-health` for its HEALTH tab, the same
pattern as the security-watch state under `~/.local/state/security-watch/`.

## Hardware checks

The unprivileged, standard-library-only `server-health-hardware.py` companion
adds:

- CPU and NVMe hottest-sensor temperatures from sysfs (not guessed ACPI sensors).
- Read-only SMART data for **both** `/dev/nvme0n1` and `/dev/sda`.
- NVMe critical warnings, available spare, endurance used and lifetime media
  errors; SATA health, selected sector/error counters and recognized wear values.
- Kernel MCE/EDAC, OOM kill records, storage/filesystem errors and CPU lockups
  in the previous **25 hours across boots**. The overlap covers normal daily
  scheduling/DST jitter; events can appear in two reports. It is not a durable
  event cursor: an outage or missed runs exceeding 25h can leave older events
  outside the window. Unreadable/failed probes report unknown, not healthy.
  The query and classifier share patterns and are case-insensitive, including
  EDAC messages using "corrected"/"uncorrectable" without a literal "error".

This does **not** run SSD self-tests, write benchmarks, change device settings,
repair filesystems or reboot. SMART cannot predict every failure. Temperature
sampling is daily, not continuous overheating protection. Existing `smartd`
settings are left unchanged; the script integrates readings with existing ntfy.

NVMe error-log entries and unsafe shutdown counts alone do not trigger a drive
failure alert: unsupported commands/power loss can increment them. Media errors
and selected SATA counters are lifetime values, labelled accordingly, not
claimed to be newly occurring errors. Vendor-specific wear is not guessed.
SMART counters are checked for integer types and device-field ranges; malformed
values and malformed probe records cannot produce an all-OK report. Failing
health/attribute exit-status bits stay critical even with partial read errors or
malformed JSON. Optional log pages the drive does not implement (e.g. an
unsupported NVMe self-test log) are reported as informational — a capability
gap is not a health failure, and health coverage is validated independently.
Interface CRC errors use a small alert threshold because a single lifetime
glitch is common and not actionable, while a failing cable grows the count
quickly.

Default thresholds (override via environment): CPU warn/critical **85/95 C**,
SSD warn/critical **60/70 C**, endurance-used warning **80%**, critical **100%**,
interface-CRC warning **20** lifetime errors. Variables: `CPU_TEMP_WARN`,
`CPU_TEMP_CRIT`, `SSD_TEMP_WARN`, `SSD_TEMP_CRIT`, `SSD_WEAR_WARN`,
`SMART_CRC_WARN`. Existing disk/load/RAM/GPU thresholds remain unchanged.
Review temperature thresholds against each drive's specifications if needed.

## One-time restricted SMART access

Install `smartmontools` first. The binaries are in `/usr/sbin`, which may not
be on the interactive/cron PATH. The companion uses absolute paths.

The daily user cannot open drive devices directly. Review
`server-health-smart.sudoers`, then install its two **exact read-only commands**:

```bash
sudo /usr/sbin/visudo -cf /opt/stacks/scripts/server-health-smart.sudoers
sudo install -o root -g root -m 0440 /opt/stacks/scripts/server-health-smart.sudoers /etc/sudoers.d/server-health-smart
sudo /usr/sbin/visudo -cf /etc/sudoers.d/server-health-smart
```

No wildcard arguments, passwordless shell/project script, sudo group changes,
or `disk`-group membership. The user cannot use this rule to run self-tests,
enable/disable SMART or change firmware. `sudo -n` never prompts in cron.
The root-owned copy does not change when the repository file is edited.
Missing tooling, permission, malformed data and timeout are explicit warnings.

Verify with a non-notifying snapshot, not by running the entire script as root:

```bash
/opt/stacks/scripts/server-health.sh --no-color
```

If drives are replaced/reordered, review the actual device identities, update
`SMART_DEVICES` and the sudoers allowlist together, and revalidate with `visudo`.
The present defaults intentionally cover the host's two known drives, rather
than silently dropping one that disappears.

## Tests

```bash
bash -n /opt/stacks/scripts/server-health.sh
python3 -S -B -m unittest discover -s /opt/stacks/scripts -p test_server_health.py -v
```

Tests mock Docker, NAS access, SMART and kernel queries; they do not change live
containers/devices or send real pushes. Notifications are mocked except for one
real-curl test against a temporary loopback-only receiver with a dummy token.
That test inspects process arguments/environment to check token privacy. Includes
regressions for restart parsing, mixed critical/read-error SMART bitmasks, invalid
counter types/ranges and probe records, case-variant cross-boot journal queries,
stuck Docker probes, whole-run timeouts, JSON output and failed heartbeat delivery.

Backup freshness checks still do not establish that every database/file can be
restored; keep periodic restore verification separate from this daily snapshot.
