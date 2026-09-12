# opencode web

Run the [opencode](https://opencode.ai) web interface as a boot-time
systemd service, reachable from any device in your Tailscale tailnet or
through an SSH tunnel (never from the public internet or the LAN).

## Why

The opencode TUI reads images and the clipboard from the machine it runs
on, so over SSH a pasted screenshot arrives as a path the server cannot
open. The web interface receives the actual image bytes from the browser,
which makes screenshots and drag & drop work from any device.

Binding to `127.0.0.1` and exposing it through
[Tailscale Serve](https://tailscale.com/kb/1242/tailscale-serve) means no
port is published on the LAN, and only devices logged into your tailnet
can reach it — encrypted with a real TLS certificate. The opencode Basic
Auth password is kept as a second layer.

## Requirements

- Linux with systemd
- opencode installed for the target user (`~/.opencode/bin/opencode` or in `PATH`)
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
   with the repo root as working directory,
3. enables and starts it on boot (`multi-user.target`),
4. with `--tailscale`, runs `tailscale serve --bg 4096` so the service is
   available at `https://<host>.<tailnet>.ts.net`.

The first Tailscale Serve run may ask you to enable HTTPS certificates for
your tailnet (one-time prompt).

Then open the tailnet URL, or `http://127.0.0.1:4096` locally, and log in
with user `opencode` and the password:

```bash
grep OPENCODE_SERVER_PASSWORD /opt/secrets/opencode-web.env
```

To keep using the TUI against the same sessions:

```bash
opencode attach http://127.0.0.1:4096
```

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
normal session). The same Basic Auth password applies. This works from
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

- `opencode web` listens on `127.0.0.1` only; nothing is reachable from
  the LAN or the internet.
- Tailscale Serve terminates HTTPS on the tailnet hostname and proxies to
  `127.0.0.1:<port>`. Access is limited by your tailnet ACLs.
- Requests without the Basic Auth password get `401`; both layers are
  required.

### Notes

- The opencode server lazily starts its MCP servers on the first message
  (measured: ~280 MB idle, ~540 MB after first use with engram, Trilium
  and Playwright MCPs).
- The service runs as your user, so the web UI has the same shell access
  that user has. Do not expose it beyond a trusted tailnet.
- `Restart=on-failure` restarts the service on crashes; a restart drops
  whatever turn was in flight.

## Uninstall

```bash
sudo ./install.sh --uninstall
sudo tailscale serve reset    # only if Serve was configured
```
