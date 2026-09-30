# opencode web

Run the [opencode](https://opencode.ai) v2 server as a boot-time systemd
service, reachable from any device in your Tailscale tailnet or through an
SSH tunnel (never from the public internet or the LAN).

## Why

The opencode TUI reads images and the clipboard from the machine it runs
on, so over SSH a pasted screenshot arrives as a path the server cannot
open. The web interface receives the actual image bytes from the browser,
which makes screenshots and drag & drop work from any device.

In opencode v2, `opencode serve` runs the same server that powers the TUI,
including the web UI — there is no separate `opencode web` command anymore.

Binding to `127.0.0.1` and exposing it through
[Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) means no
port is published on the LAN, and only devices logged into your tailnet
can reach it — encrypted with a real TLS certificate. The server password
is kept as a second layer; the web UI signs in once per device and keeps
a session cookie for 30 days (no repeated password prompts like v1's
HTTP Basic Auth).

## Requirements

- Linux with systemd
- opencode **v2** installed for the target user (`~/.opencode/bin/opencode`
  or in `PATH`; `opencode --version` must print `v2.x`)
- Tailscale **optional** — only if you want tailnet access
- `openssl` (for generating the password)

## Install

```bash
sudo ./install.sh --tailscale
```

Preview first without changing anything:

```bash
./install.sh --dry-run --tailscale
```

The script:

1. writes `/opt/secrets/opencode-web.env` with a random
   `OPENCODE_SERVER_PASSWORD` (mode `600`, owned by the run user) if it
   does not exist yet,
2. installs `/etc/systemd/system/opencode-web.service` as the run user
   with the repo root as working directory, running
   `opencode serve --port 4096 --hostname 127.0.0.1`,
3. enables and starts it on boot (`multi-user.target`),
4. with `--tailscale`, runs `tailscale serve --bg 4096` so the service is
   available at `https://<host>.<tailnet>.ts.net`.

The first Tailscale Serve run may ask you to enable HTTPS certificates for
your tailnet (one-time prompt).

Then open the tailnet URL, or `http://127.0.0.1:4096` locally. The v2 web UI
shows a **Connect to a server** form — the address is prefilled, enter the
password:

```bash
grep OPENCODE_SERVER_PASSWORD /opt/secrets/opencode-web.env
```

Sign-in creates a session cookie that lasts 30 days (per browser), so you
enter the password once per device instead of on every request like v1's
Basic Auth. Changing the server password signs every session out.

To keep using the TUI against the same sessions:

```bash
opencode --server http://127.0.0.1:4096
```

The password guards CLI clients too, not just the web UI: the client
reads it from `OPENCODE_PASSWORD` and refuses to connect without it
(`Error: Server at … requires a password; set OPENCODE_PASSWORD`). A
shell function injects it from the secret file for that command only,
so the password never lands in the persistent shell environment:

```bash
oc() {
  local pw
  pw=$(sed -n 's/^OPENCODE_SERVER_PASSWORD=//p' /opt/secrets/opencode-web.env)
  [ -n "$pw" ] || { echo "oc: password missing" >&2; return 1; }
  OPENCODE_PASSWORD="$pw" opencode "$@" --server http://127.0.0.1:4096
}
```

`--server` goes after `"$@"` because the CLI rejects it when placed
before a subcommand (`oc api get /api/info` works,
`opencode --server … api get …` prints usage). The same
`OPENCODE_PASSWORD` trick works for any other client invocation, e.g.
`OPENCODE_PASSWORD=… opencode run "…" --server http://127.0.0.1:4096`.

### Access without Tailscale (SSH tunnel)

Any client that can SSH to the server can use the web UI without joining
the tailnet. The tunnel is encrypted by SSH and listens only on the
client's loopback:

```bash
ssh -N -L 4096:127.0.0.1:4096 <user>@<server>
# then open http://localhost:4096 on the client
```

To make it permanent, add a host to `~/.ssh/config`:

```text
Host opencode-web
  HostName <server>
  User <user>
  LocalForward 4096 127.0.0.1:4096
```

Then `ssh opencode-web` brings the tunnel up (combine with `-N` or any
normal session). The same server password applies. This works from
Linux, macOS and Windows (OpenSSH client) and needs no Tailscale install.

### Options

| Flag | Default | Meaning |
| ---- | ------- | ------- |
| `--port <port>` | `4096` | Port on `127.0.0.1` |
| `--user <user>` | invoking user | User the service runs as |
| `--workdir <dir>` | repo root | Default project directory |
| `--bin <path>` | autodetected | opencode binary |
| `--tailscale` | off | Configure Tailscale Serve |
| `--dry-run` | — | Print intended changes, touch nothing |
| `--uninstall` | — | Stop and remove the service |

`--no-auth` from the v1 installer is accepted but ignored with a warning —
opencode v2 has no unauthenticated mode; the server always requires a
password.

Environment overrides: `OPENCODE_WEB_PORT`, `OPENCODE_WEB_USER`,
`OPENCODE_WEB_WORKDIR`, `OPENCODE_WEB_SECRET`, `OPENCODE_BIN`.

## Usage

```bash
systemctl status opencode-web      # state
journalctl -u opencode-web -f      # logs
sudo systemctl restart opencode-web
sudo tailscale serve status        # tailnet URL
```

### How it is exposed

- `opencode serve` listens on `127.0.0.1` only; nothing is reachable from
  the LAN or the internet.
- Tailscale Serve terminates HTTPS on the tailnet hostname and proxies to
  `127.0.0.1:<port>`. Access is limited by your tailnet ACLs.
- The v2 server requires a password for the API; the web UI signs in once
  and holds a 30-day session cookie. CLI clients authenticate with HTTP
  Basic Auth (`opencode` / password).

### Security

The password is a second layer in front of an already tailnet-only
service. opencode v2 always requires it — there is no `--no-auth`. The
web sign-in is one-time per device (30-day session), so the v1-era
constant Basic Auth re-prompts (especially on mobile) are gone.

Never combine this with `tailscale funnel` — Funnel publishes to the
public internet and would expose the server to the public (behind only
the password).

### Notes

- The service runs with a minimal environment: shell rc files (`~/.bashrc`) are
  not read. `install.sh` therefore mirrors the opencode-related exports
  (`OPENCODE_ENABLE_EXA`, `OPENCODE_WEBSEARCH_PROVIDER`, `EXA_API_KEY`,
  `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS`, Engram cloud sync) into
  `/opt/secrets/opencode-web.env`. These keys are refreshed on every install
  run, so re-run `install.sh` after rotating `exa.key` or
  `engram-cloud.token` — it updates the file in place and restarts the
  service. `OPENCODE_SERVER_PASSWORD` is created once and never rotated by
  the script. `OPENCODE_EXPERIMENTAL_BACKGROUND_SUBAGENTS` is a v1 leftover;
  v2 delegates commands to background subagents by default, and the variable
  is harmless if v2 ignores it.
- The opencode server lazily starts its MCP servers on the first message
  (measured: ~280 MB idle, ~540 MB after first use with engram, Trilium
  and Playwright MCPs).
- v2 also runs a shared background service (`opencode serve --service`,
  port 49374) for local terminal use. Both processes share the same
  session database; the systemd server on 4096 is the one exposed to
  the tailnet.
- The service runs as your user, so the web UI has the same shell access
  that user has. Do not expose it beyond a trusted tailnet.
- `Restart=on-failure` restarts the service on crashes; a restart drops
  whatever turn was in flight.

## Uninstall

```bash
sudo ./install.sh --uninstall
sudo tailscale serve reset    # only if Serve was configured
```
