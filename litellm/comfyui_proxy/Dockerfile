FROM python:3.12-alpine

WORKDIR /app
RUN pip install --no-cache-dir aiohttp==3.11.*

# gpu_containers.py is bind-mounted at runtime (single source of truth with the
# litellm idle manager); only the proxy itself is baked into the image.
COPY app.py .

ENV PORT=8189
EXPOSE 8189

CMD ["python", "app.py"]
