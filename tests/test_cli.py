"""CLI behavior that needs no control plane. Pushes that reach one are tested in claude_transfer."""

import json
import os
import uuid

import pytest

from ctransfer import api as api_mod
from ctransfer import cli
from tests.conftest import write_transcript


def configure():
    cli.main(["configure", "--api-url", "https://api.invalid/", "--region", "us-east-1"])


def test_push_refuses_busy_session(laptop):
    entry = next((laptop["home"] / "sessions").iterdir())
    entry.write_text(entry.read_text().replace('"idle"', '"busy"'))
    with pytest.raises(SystemExit, match="mid-turn"):
        cli.main(["push", "--cwd", str(laptop["repo"])])


def add_newer_live_session(laptop) -> str:
    """A second, more recently active session in the same directory, which plain discovery would pick."""
    other = str(uuid.uuid4())
    write_transcript(laptop["home"], other, str(laptop["repo"]))
    (laptop["home"] / "sessions" / f"{os.getppid()}.json").write_text(json.dumps(
        {"pid": os.getppid(), "sessionId": other, "cwd": str(laptop["repo"]), "status": "idle", "updatedAt": 99}))
    return other


def discovered(capsys, *argv) -> str:
    cli.main(["discover", *argv])
    return json.loads(capsys.readouterr().out)["session_id"]


def test_current_targets_invoking_session_from_env(laptop, capsys, monkeypatch):
    other = add_newer_live_session(laptop)
    monkeypatch.chdir(laptop["repo"])
    assert discovered(capsys) == other
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", laptop["sid"])
    assert discovered(capsys, "--current") == laptop["sid"]


def test_current_falls_back_to_ancestor_process(laptop, capsys):
    # The fixture registers this test process as running laptop["sid"]; the newer session is its parent.
    add_newer_live_session(laptop)
    assert discovered(capsys, "--current") == laptop["sid"]


def test_current_outside_claude_fails(laptop):
    for f in (laptop["home"] / "sessions").iterdir():
        f.unlink()
    with pytest.raises(SystemExit, match="not running inside a Claude Code session"):
        cli.main(["discover", "--current"])


def test_detached_push_needs_configuration(laptop):
    with pytest.raises(SystemExit, match="not configured"):
        cli.main(["push", "--session", laptop["sid"], "--when-idle", "--detach"])
    assert cli.read_marker(laptop["sid"]) is None
    assert not (api_mod.CONFIG_PATH.parent / "logs").exists()  # no worker was started


def test_detached_push_refuses_while_one_is_active(laptop):
    configure()
    cli.write_marker(laptop["sid"], state="pushing")
    with pytest.raises(SystemExit, match="already pushing"):
        cli.main(["push", "--session", laptop["sid"], "--when-idle", "--detach"])
    assert cli.read_marker(laptop["sid"])["state"] == "pushing"
    assert not (api_mod.CONFIG_PATH.parent / "logs").exists()
