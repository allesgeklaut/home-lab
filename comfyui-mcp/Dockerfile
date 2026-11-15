FROM python:3.12-slim

# The server code is bind-mounted at runtime (one source of truth with the
# host opencode MCP); only the runtime dependencies are baked into the image.
# av + Pillow are for the GIF preset; image/video generation itself happens in
# the ComfyUI container.
RUN pip install --no-cache-dir \
    mcp==2.3.0 \
    av==18.1.0 \
    pillow==12.1.1

WORKDIR /app
ENV COMFYUI_MCP_TRANSPORT=streamable-http \
    COMFYUI_MCP_HOST=0.0.0.0 \
    COMFYUI_MCP_PORT=8000
EXPOSE 8000
CMD ["python", "/app/server.py"]
