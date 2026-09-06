#!/bin/sh
#
# Shared entrypoint wrapper: inject a secret (e.g. an API key) from a mounted
# read-only file into an environment variable, then exec the real command.
# Used by the litellm and webui stacks so the key in a secret file is the
# single source of truth instead of being duplicated into per-service env
# files.
#
# Env vars consumed by the wrapper (set in the compose service):
#   APP_KEY_FILE   path of the mounted secret file (default /run/secrets/api.key)
#   APP_KEY_ENV    name of the env var to export, e.g. LITELLM_MASTER_KEY or
#                  OPENAI_API_KEY
#
# Fails closed: if the key file is missing or empty the process exits instead
# of starting with an empty credential (which would silently disable auth).
#
# The target command is passed as "$@" (compose "command:"); the wrapper
# exports the key and then execs it unchanged.

set -eu

: "${APP_KEY_FILE:=/run/secrets/api.key}"
APP_KEY_ENV="${APP_KEY_ENV:-}"

[ -n "$APP_KEY_ENV" ] || {
    echo "FATAL: APP_KEY_ENV is not set (which env var should receive the key?)" >&2
    exit 1
}

K="$(cat "$APP_KEY_FILE" 2>/dev/null)" || {
    echo "FATAL: cannot read API key file ($APP_KEY_FILE)" >&2
    exit 1
}
[ -n "$K" ] || {
    echo "FATAL: API key file is empty ($APP_KEY_FILE)" >&2
    exit 1
}
export "$APP_KEY_ENV=$K"

exec "$@"