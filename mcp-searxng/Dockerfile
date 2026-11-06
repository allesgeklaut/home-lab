FROM node:25-slim

# Shared SearXNG MCP tool server.
#
# Runs the pinned mcp-searxng stdio MCP server behind supergateway, which
# exposes it over Streamable HTTP on :8808 for consumers on the shared
# docker network (stacks-shared): mcpo proxies it for Open WebUI, Python
# agents speak MCP directly. SEARXNG_URL is set in compose.yml.
#
# Version pins live in npm/package.json — the same Dependabot-managed
# manifest pattern as webui/mcpo/npm — and are resolved from the
# manifest at build time, so the manifest is the single source of truth:
#   - mcp-searxng 2.8.1 fixed CVE-2026-111757/-111997/-111497/-111181;
#     2.9.0 adds the multi-page `pages` search argument. Node >= 22 is
#     required; 24 matches the version upstream ships.
#   - supergateway is the stdio -> Streamable-HTTP bridge.
COPY npm/package.json /tmp/npm/package.json
RUN npm install -g --no-fund --no-audit \
        "mcp-searxng@$(node -p "require('/tmp/npm/package.json').dependencies['mcp-searxng']")" \
        "supergateway@$(node -p "require('/tmp/npm/package.json').dependencies['supergateway']")"

EXPOSE 8808
# /healthz (healthCheck "server") also initializes and pings the
# mcp-searxng child, so a dead child turns the container unhealthy.
CMD ["supergateway", \
     "--stdio", "mcp-searxng", \
     "--outputTransport", "streamableHttp", \
     "--port", "8808", \
     "--healthEndpoint", "/healthz", \
     "--healthCheck", "server"]
