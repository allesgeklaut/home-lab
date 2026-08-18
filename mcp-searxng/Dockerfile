FROM node:20-slim

# Install the SearXNG MCP server globally
RUN npm install -g mcp-searxng

# MCP servers communicate over stdio — no port needed
CMD ["mcp-searxng"]