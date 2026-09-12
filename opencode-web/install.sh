#!/usr/bin/env bash
set -euo pipefail

PORT="${OPENCODE_WEB_PORT:-4096}"
RUN_USER="${OPENCODE_WEB_USER:-}"
SECRET_FILE="${OPENCODE_WEB_SECRET:-/opt/secrets/opencode-web.env}"
SERVICE="opencode-web"
UNIT="/etc/systemd/system/${SERVICE}.service"
WORKDIR="${OPENCODE_WEB_WORKDIR:-}"
OPENCODE_BIN="${OPENCODE_BIN:-}"
WITH_TAILSCALE=0
DO_UNINSTALL=0
DRY_RUN=0

usage() {
  cat <<'EOF'
Usage: install.sh [options]

Installs the opencode web interface as a systemd system service bound to
127.0.0.1, optionally exposed to your tailnet with Tailscale Serve.

Options:
  --port <port>     Port to listen on (default: 4096)
  --user <user>     User to run as (default: the invoking user)
  --workdir <dir>   Default project directory (default: repository root)
  --bin <path>      Path to the opencode binary (default: autodetect)
  --tailscale       Run `tailscale serve --bg <port>` after install
  --uninstall       Stop and remove the service
  --dry-run         Show what would be done, change nothing
  -h, --help        Show this help

Environment overrides: OPENCODE_WEB_PORT, OPENCODE_WEB_USER,
OPENCODE_WEB_WORKDIR, OPENCODE_WEB_SECRET, OPENCODE_BIN.
EOF
}

ORIG_ARGS=("$@")
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    --user) RUN_USER="$2"; shift 2 ;;
    --workdir) WORKDIR="$2"; shift 2 ;;
    --bin) OPENCODE_BIN="$2"; shift 2 ;;
    --tailscale) WITH_TAILSCALE=1; shift ;;
    --uninstall) DO_UNINSTALL=1; shift ;;
    --dry-run) DRY_RUN=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1" >&2; usage >&2; exit 2 ;;
  esac
done

if [ "$(id -u)" -ne 0 ] && [ "$DRY_RUN" -eq 0 ]; then
  SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
  exec sudo -- "$SELF" "${ORIG_ARGS[@]}"
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${RUN_USER:-${SUDO_USER:-$(id -un)}}"
if ! HOME_DIR="$(getent passwd "$RUN_USER" | cut -d: -f6)" || [ -z "$HOME_DIR" ]; then
  echo "user not found: $RUN_USER" >&2
  exit 1
fi
GROUP="$(id -gn "$RUN_USER")"
WORKDIR="${WORKDIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"

if [ "$DO_UNINSTALL" -eq 1 ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ systemctl disable --now $SERVICE"
    echo "+ rm -f $UNIT"
    echo "+ systemctl daemon-reload"
    echo "Would remove $SERVICE (secret kept: $SECRET_FILE)"
  else
    systemctl disable --now "$SERVICE" 2>/dev/null || true
    rm -f "$UNIT"
    systemctl daemon-reload
    echo "Removed $SERVICE."
    echo "Secret left in place: $SECRET_FILE"
    echo "If Tailscale Serve was configured: sudo tailscale serve reset"
  fi
  exit 0
fi

LEGACY_USER_UNIT="$HOME_DIR/.config/systemd/user/${SERVICE}.service"
LEGACY_PRESENT=0
if [ -f "$LEGACY_USER_UNIT" ]; then
  LEGACY_PRESENT=1
  USER_UID="$(id -u "$RUN_USER")"
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ systemctl --user disable --now $SERVICE   (as $RUN_USER)"
    echo "+ mv $LEGACY_USER_UNIT ${LEGACY_USER_UNIT}.disabled"
    echo "+ rm -f $HOME_DIR/.config/systemd/user/default.target.wants/${SERVICE}.service"
  else
    if [ -d "/run/user/$USER_UID" ]; then
      sudo -u "$RUN_USER" env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
        systemctl --user disable --now "$SERVICE" 2>/dev/null || true
    fi
    mv "$LEGACY_USER_UNIT" "${LEGACY_USER_UNIT}.disabled"
    rm -f "$HOME_DIR/.config/systemd/user/default.target.wants/${SERVICE}.service"
    if [ -d "/run/user/$USER_UID" ]; then
      sudo -u "$RUN_USER" env XDG_RUNTIME_DIR="/run/user/$USER_UID" \
        systemctl --user daemon-reload 2>/dev/null || true
    fi
    echo "Disabled legacy user unit (kept as ${LEGACY_USER_UNIT}.disabled)"
  fi
  for _ in $(seq 1 20); do
    ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PORT " || break
    sleep 0.25
  done
fi

install -d -m 700 "$(dirname "$SECRET_FILE")"
if [ ! -s "$SECRET_FILE" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ generate $SECRET_FILE with a random OPENCODE_SERVER_PASSWORD"
  else
    umask 077
    printf 'OPENCODE_SERVER_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$SECRET_FILE"
    echo "Generated $SECRET_FILE"
  fi
fi
if [ "$DRY_RUN" -eq 0 ]; then
  chown "$RUN_USER:$GROUP" "$SECRET_FILE"
  chmod 600 "$SECRET_FILE"
fi

if [ -z "$OPENCODE_BIN" ]; then
  for candidate in "$HOME_DIR/.opencode/bin/opencode" /usr/local/bin/opencode /usr/bin/opencode; do
    if [ -x "$candidate" ]; then
      OPENCODE_BIN="$candidate"
      break
    fi
  done
fi
if [ -z "$OPENCODE_BIN" ]; then
  OPENCODE_BIN="$(sudo -u "$RUN_USER" env PATH="$HOME_DIR/.opencode/bin:/usr/local/bin:/usr/bin:/bin" sh -c 'command -v opencode' || true)"
fi
if [ -z "$OPENCODE_BIN" ] || [ ! -x "$OPENCODE_BIN" ]; then
  echo "opencode binary not found; install it first or pass --bin <path>" >&2
  exit 1
fi

if [ ! -d "$WORKDIR" ]; then
  if [ "$DRY_RUN" -eq 1 ]; then
    echo "+ mkdir -p $WORKDIR && chown $RUN_USER:$GROUP $WORKDIR"
  else
    mkdir -p "$WORKDIR"
    chown "$RUN_USER:$GROUP" "$WORKDIR"
  fi
fi

if ! systemctl is-active --quiet "$SERVICE"; then
  if ss -ltn 2>/dev/null | grep -q "127.0.0.1:$PORT "; then
    if [ "$DRY_RUN" -eq 1 ] && [ "$LEGACY_PRESENT" -eq 1 ]; then
      echo "note: port $PORT is in use by the legacy user unit; install would stop it first"
    else
      echo "port $PORT is already in use on 127.0.0.1" >&2
      exit 1
    fi
  fi
fi

UNIT_CONTENT="[Unit]
Description=opencode web (localhost + Tailscale Serve)
After=network-online.target
Wants=network-online.target

[Service]
User=$RUN_USER
Group=$GROUP
WorkingDirectory=$WORKDIR
Environment=HOME=$HOME_DIR
EnvironmentFile=$SECRET_FILE
ExecStart=$OPENCODE_BIN web --port $PORT
Restart=on-failure
RestartSec=3

[Install]
WantedBy=multi-user.target"

if [ "$DRY_RUN" -eq 1 ]; then
  echo "Would write $UNIT:"
  echo "----------------------------------------"
  echo "$UNIT_CONTENT"
  echo "----------------------------------------"
  echo "+ systemctl daemon-reload"
  echo "+ systemctl enable $SERVICE"
  if systemctl is-active --quiet "$SERVICE"; then
    echo "+ systemctl restart $SERVICE"
  else
    echo "+ systemctl start $SERVICE"
  fi
  if [ "$WITH_TAILSCALE" -eq 1 ]; then
    echo "+ tailscale serve --bg $PORT"
  fi
  exit 0
fi

printf '%s\n' "$UNIT_CONTENT" > "$UNIT"

systemctl daemon-reload
systemctl enable "$SERVICE"
if systemctl is-active --quiet "$SERVICE"; then
  systemctl restart "$SERVICE"
else
  systemctl start "$SERVICE"
fi

CODE=""
for _ in $(seq 1 30); do
  CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/" || true)"
  if [ "$CODE" = "401" ] || [ "$CODE" = "200" ]; then
    break
  fi
  sleep 0.5
done
if [ "$CODE" = "200" ]; then
  echo "warning: server answered 200 - $SECRET_FILE is missing or empty" >&2
fi

if [ "$WITH_TAILSCALE" -eq 1 ]; then
  if command -v tailscale >/dev/null 2>&1; then
    tailscale serve --bg "$PORT" || true
  else
    echo "tailscale not installed; skipping Tailscale Serve" >&2
  fi
fi

echo
echo "service:  systemctl status $SERVICE"
echo "logs:     journalctl -u $SERVICE -f"
echo "password: grep OPENCODE_SERVER_PASSWORD $SECRET_FILE  (user: opencode)"
echo "local:    http://127.0.0.1:$PORT  (HTTP check: ${CODE:-failed})"
if [ "$WITH_TAILSCALE" -eq 1 ]; then
  echo "tailnet:  sudo tailscale serve status"
fi
echo "attach:   $OPENCODE_BIN attach http://127.0.0.1:$PORT"
