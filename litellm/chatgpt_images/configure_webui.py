"""Switch only Open WebUI image-generation and image-edit settings, with
private rollback.

Run on the host. Never prints credentials; never modifies chat settings.
Open WebUI's current per-key config is read from SQLite on every request.
"""

import argparse
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time

KEYS = (
    "image_generation.enable", "image_generation.engine", "image_generation.model",
    "image_generation.size", "image_generation.openai.api_base_url",
    "image_generation.openai.api_key", "image_generation.openai.api_version",
    "image_generation.openai.params",
    "images.edit.engine", "images.edit.model", "images.edit.size",
    "images.edit.openai.api_base_url", "images.edit.openai.api_key",
    "images.edit.openai.api_version",
)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", default="/opt/stacks/webui/data/webui.db")
    parser.add_argument("--gateway-key", default="/opt/secrets/litellm.key")
    parser.add_argument("--restore", type=Path, help="Restore only the keys saved by this script")
    args = parser.parse_args()
    # Refuse SQLite's default create-on-missing behavior.
    database = Path(args.database).resolve(strict=True)
    connection = sqlite3.connect(database, timeout=20)
    try:
        connection.execute("BEGIN IMMEDIATE")
        if args.restore:
            previous = json.loads(args.restore.read_text())
            if not isinstance(previous, dict) or set(previous) != set(KEYS):
                raise SystemExit("Not an image settings backup")
            for key, row in previous.items():
                if row is None:
                    connection.execute("DELETE FROM config WHERE key=?", (key,))
                else:
                    connection.execute(
                        "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) "
                        "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                        (key, row[0], row[1]),
                    )
            connection.commit()
            print("Restored previous image generation and edit settings. Chat settings unchanged.")
            return

        token = Path(args.gateway_key).read_text().strip()
        if not token:
            raise SystemExit("Gateway key file is empty")
        previous = {
            key: connection.execute("SELECT value,updated_at FROM config WHERE key=?", (key,)).fetchone()
            for key in KEYS
        }
        directory = Path(tempfile.mkdtemp(prefix="webui-image-settings-", dir="/tmp/opencode"))
        backup = directory / "previous.json"
        fd = os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as output:
            json.dump(previous, output)
        adapter = "http://chatgpt-images:8180/v1"
        updates = {
            "image_generation.enable": True,
            "image_generation.engine": "openai",
            "image_generation.model": "chatgpt-image",
            "image_generation.size": "1024x1024",
            "image_generation.openai.api_base_url": adapter,
            "image_generation.openai.api_key": token,
            "image_generation.openai.api_version": "",
            "image_generation.openai.params": {"quality": "high", "output_format": "png"},
            "images.edit.engine": "openai",
            "images.edit.model": "chatgpt-image",
            "images.edit.size": "",
            "images.edit.openai.api_base_url": adapter,
            "images.edit.openai.api_key": token,
            "images.edit.openai.api_version": "",
        }
        for key, value in updates.items():
            connection.execute(
                "INSERT INTO config(key,value,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,updated_at=excluded.updated_at",
                (key, json.dumps(value), int(time.time())),
            )
        connection.commit()
        print("Image generation and editing switched to chatgpt-image. Chat settings unchanged.")
        print("Private rollback file:", backup)
    finally:
        connection.close()


if __name__ == "__main__":
    main()
