#!/bin/sh
# Authenticate outside the proxy before enabling any chatgpt/ model routes.
set -eu

SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
if [ ! -d /opt/secrets/chatgpt ]; then
    echo 'Create /opt/secrets/chatgpt with mode 0700 before signing in.' >&2
    exit 1
fi

# No service ports, no proxy startup, and no token printed to the terminal.
# Keep the token directory mounted read-write for subsequent OAuth refreshes.
exec docker compose --project-directory "$SCRIPT_DIR" \
    -f "$SCRIPT_DIR/compose.yml" run --rm --no-deps \
    --entrypoint /bin/sh litellm -c '
        set -eu
        umask 077
        python -c "from litellm.llms.chatgpt.authenticator import Authenticator; Authenticator().get_access_token(); print(\"ChatGPT login ready.\")"
        chmod 600 "$CHATGPT_TOKEN_DIR/auth.json"
    '
