"""Security Watch agent entry point.

``agent`` is the module-level object the Mistral Agents SDK can serve or
deploy. Importing this module does not read API keys or start network calls.

The SDK entry point has no inventory binding and no post-turn validation hook.
The research tool therefore fails closed until a future host-side integration
registers a runtime bound to the sanitized inventory for the session. This
module is a development scaffold, not a deploy-ready service; see README.
"""

from __future__ import annotations

from security_watch.agent_factory import build_agent

agent = build_agent()


if __name__ == "__main__":
    from security_watch.cli import main

    raise SystemExit(main())
