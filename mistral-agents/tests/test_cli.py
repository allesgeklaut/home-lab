from __future__ import annotations

import json
import os

import pytest

from security_watch.cli import load_env_file, main
from security_watch.inventory import MAX_INVENTORY_BYTES

from .helpers import FIXTURE, INVENTORY


def test_offline_demo_json(tmp_path, capsys, no_network) -> None:
    code = main(
        [
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["status"] == "completed"
    assert payload["report"]["assessment"] == "actionable_findings"
    assert payload["report"]["checked_component_ids"] == ["web-app", "api-service"]
    assert payload["ledger"]["successful_calls"] == 2
    assert payload["ledger"]["searched_component_ids"] == ["api-service", "web-app"]
    assert payload["usage"]["outer"]["calls"] == 3
    assert payload["usage"]["model_token_estimate_usd"]["total"] == 0.0
    assert payload["usage"]["pricing"]["outer"]["model"] == "mistral-large-4"
    assert payload["usage"]["pricing"]["search"]["model"] == "mistral-small-2603"
    assert payload["state_path"]
    assert os.path.isdir(payload["state_path"])


def test_offline_demo_human(tmp_path, capsys, no_network) -> None:
    code = main(
        [
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "Security Watch: completed" in captured.out
    assert "Findings: 2" in captured.out
    assert "Checked components: web-app, api-service" in captured.out
    assert "edge-proxy" not in captured.out
    assert "Evidence: E1" in captured.out


def test_live_requires_key(tmp_path, capsys, monkeypatch, no_network) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    code = main(
        [
            "--live",
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "MISTRAL_API_KEY" in captured.err


def test_env_file_requires_live(tmp_path, capsys) -> None:
    env_file = tmp_path / "env"
    env_file.write_text("MISTRAL_API_KEY=secret\n")
    with pytest.raises(SystemExit) as error:
        main(["--env-file", str(env_file)])
    assert error.value.code == 2


def test_env_file_loads_without_printing(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    env_file = tmp_path / "mistral.env"
    env_file.write_text("MISTRAL_API_KEY=super-secret-value\nOTHER=value\n")
    env_file.chmod(0o600)
    load_env_file(env_file)
    captured = capsys.readouterr()
    assert os.environ["MISTRAL_API_KEY"] == "super-secret-value"
    assert "super-secret-value" not in captured.out
    assert "super-secret-value" not in captured.err


def test_env_file_does_not_override_existing(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("MISTRAL_API_KEY", "existing-value")
    env_file = tmp_path / "mistral.env"
    env_file.write_text("MISTRAL_API_KEY=new-value\n")
    env_file.chmod(0o600)
    load_env_file(env_file)
    assert os.environ["MISTRAL_API_KEY"] == "existing-value"


def test_env_file_warns_on_unsafe_mode(tmp_path, capsys) -> None:
    env_file = tmp_path / "mistral.env"
    env_file.write_text("MISTRAL_API_KEY=secret\n")
    env_file.chmod(0o644)
    load_env_file(env_file)
    captured = capsys.readouterr()
    assert "readable by group or other users" in captured.err
    assert "secret" not in captured.err


def test_oversized_inventory_cli(tmp_path, capsys) -> None:
    inventory = tmp_path / "inventory.json"
    inventory.write_bytes(b"{" + b" " * MAX_INVENTORY_BYTES + b"}")
    code = main(["--inventory", str(inventory), "--state-dir", str(tmp_path / "state")])
    captured = capsys.readouterr()
    assert code == 2
    assert "exceeds" in captured.err


def test_search_limit_warning(tmp_path, capsys, no_network) -> None:
    code = main(
        [
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path),
            "--max-search-calls",
            "999",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "hard-capped" in captured.err
    payload = json.loads(captured.out)
    assert payload["limits"]["max_search_calls"] == 64


def test_iteration_limit_warning(tmp_path, capsys, no_network) -> None:
    code = main(
        [
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path),
            "--max-iterations",
            "999",
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "hard-capped" in captured.err
    payload = json.loads(captured.out)
    assert payload["limits"]["max_iterations"] == 80


def test_timeout_ceiling(tmp_path, capsys, no_network) -> None:
    code = main(
        [
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path),
            "--timeout",
            "1800",
            "--json",
        ]
    )
    assert code == 0
    with pytest.raises(SystemExit) as error:
        main(
            [
                "--inventory",
                str(FIXTURE),
                "--state-dir",
                str(tmp_path),
                "--timeout",
                "1801",
            ]
        )
    assert error.value.code == 2


def test_live_env_file_without_key(tmp_path, capsys, monkeypatch) -> None:
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    env_file = tmp_path / "mistral.env"
    env_file.write_text("OTHER=value\n")
    env_file.chmod(0o600)
    code = main(
        [
            "--live",
            "--env-file",
            str(env_file),
            "--inventory",
            str(FIXTURE),
            "--state-dir",
            str(tmp_path / "state"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "MISTRAL_API_KEY" in captured.err


def test_stdin_inventory_cli(tmp_path, capsys, monkeypatch, no_network) -> None:
    import io
    import sys

    monkeypatch.setattr(
        sys,
        "stdin",
        type("FakeStdin", (), {"buffer": io.BytesIO(json.dumps(INVENTORY).encode())})(),
    )
    code = main(
        [
            "--inventory",
            "-",
            "--state-dir",
            str(tmp_path),
            "--json",
        ]
    )
    captured = capsys.readouterr()
    assert code == 0
    assert json.loads(captured.out)["status"] == "completed"
