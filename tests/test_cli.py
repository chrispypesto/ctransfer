"""CLI behavior that needs no control plane. Pushes that reach one are tested with the control plane."""

import json
import os
import subprocess
import sys
import uuid

import pytest

from ctransfer import api as api_mod
from ctransfer import cli
from tests.conftest import configure_signed_in, write_transcript


def configure():
    configure_signed_in()


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


def test_resume_tells_claude_the_session_is_back(laptop, tmp_path):
    bindir = tmp_path / "bin"  # a stand-in claude that prints the argv it was exec'd with
    bindir.mkdir()
    (bindir / "claude").write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    (bindir / "claude").chmod(0o755)
    out = subprocess.run([sys.executable, "-m", "ctransfer.cli", "resume", laptop["sid"]], capture_output=True, text=True,
                         env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}, check=True).stdout
    argv = json.loads(out)
    assert argv[:2] == ["--resume", laptop["sid"]]
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "back from a cloud VM" in note and "complete" in note


def test_resume_needs_a_local_transcript(laptop):
    with pytest.raises(SystemExit, match="pull it first"):
        cli.main(["resume", "not-a-session"])
