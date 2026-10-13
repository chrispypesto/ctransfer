"""CLI behavior that needs no control plane. Pushes that reach one are tested with the control plane."""

import io
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from ctransfer import api as api_mod
from ctransfer import cli, sessions
from tests.conftest import add_replies, configure_signed_in, write_transcript


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


def pushed_session(*argv) -> str:
    """The session `push` picks. Every session already has a push underway, so it refuses, naming its pick."""
    configure()
    for f in (sessions.claude_home() / "projects").glob("*/*.jsonl"):
        cli.write_marker(f.stem, state="pushing")
    with pytest.raises(SystemExit, match="already pushing") as e:
        cli.main(["push", "--detach", *argv])
    return str(e.value).split("a push of session ")[1].split(" ")[0]


def test_push_targets_the_newest_session_or_the_invoking_one(laptop, monkeypatch):
    other = add_newer_live_session(laptop)
    monkeypatch.chdir(laptop["repo"])
    assert pushed_session() == other
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", laptop["sid"])
    assert pushed_session("--current") == laptop["sid"]


def test_current_falls_back_to_ancestor_process(laptop):
    # The fixture registers this test process as running laptop["sid"]; the newer session is its parent.
    add_newer_live_session(laptop)
    assert pushed_session("--current") == laptop["sid"]


def test_current_outside_claude_fails(laptop):
    for f in (laptop["home"] / "sessions").iterdir():
        f.unlink()
    with pytest.raises(SystemExit, match="not running inside a Claude Code session"):
        cli.main(["push", "--current"])


def test_detached_push_needs_sign_in(laptop):
    with pytest.raises(SystemExit, match="not signed in"):
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


def test_detached_push_hands_the_prompt_to_its_worker(laptop):
    """The worker runs after the caller's shell is gone, so the prompt must reach it on its command line.
    The control plane is unreachable, so the worker fails after the bundle is built."""
    configure()
    cli.main(["push", "--session", laptop["sid"], "--detach", "keep", "going:", "don't", "stop"])
    log = Path(cli.read_marker(laptop["sid"])["log"])
    deadline = time.time() + 30
    while cli.read_marker(laptop["sid"])["state"] in cli.ACTIVE_STATES and time.time() < deadline:
        time.sleep(0.2)
    header = log.read_text().splitlines()[1]
    assert f"push --session {laptop['sid']} --track" in header and header.endswith("-- keep going: don't stop")
    assert cli.read_marker(laptop["sid"])["state"] == "failed"


def test_resume_tells_claude_the_session_is_back(laptop, tmp_path):
    last_ran_with = add_replies(laptop["transcript"], laptop["sid"], str(laptop["repo"]))
    bindir = tmp_path / "bin"  # a stand-in claude that prints the argv it was exec'd with
    bindir.mkdir()
    (bindir / "claude").write_text(f"#!{sys.executable}\nimport json, sys\nprint(json.dumps(sys.argv[1:]))\n")
    (bindir / "claude").chmod(0o755)
    out = subprocess.run([sys.executable, "-m", "ctransfer.cli", "resume", laptop["sid"]], capture_output=True, text=True,
                         env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin"}, check=True).stdout
    argv = json.loads(out)
    assert argv[:6] == ["--resume", laptop["sid"], *last_ran_with]
    note = argv[argv.index("--append-system-prompt") + 1]
    assert "back from a cloud VM" in note and "complete" in note


def test_resume_needs_a_local_transcript(laptop):
    with pytest.raises(SystemExit, match="pull it first"):
        cli.main(["resume", "not-a-session"])


def test_new_rejects_a_session_id_that_is_not_a_uuid(laptop):
    configure()
    with pytest.raises(SystemExit, match="--session-id must be a UUID"):
        cli.main(["new", "--session-id", "not-a-uuid", "hello"])


def test_detached_new_needs_sign_in(laptop):
    with pytest.raises(SystemExit, match="not signed in"):
        cli.main(["new", "--cwd", str(laptop["repo"]), "--detach", "hello"])
    assert not (api_mod.CONFIG_PATH.parent / "logs").exists()  # no worker was started


def test_account_delete_asks_first_and_changes_nothing_unless_confirmed(monkeypatch):
    configure_signed_in()  # api.invalid: reaching the control plane would fail the test
    with pytest.raises(SystemExit, match="--yes"):
        cli.main(["account", "delete"])  # stdin is not a terminal under pytest

    class Terminal(io.StringIO):
        def isatty(self):
            return True

    monkeypatch.setattr("sys.stdin", Terminal("yes\n"))
    with pytest.raises(SystemExit, match="not deleted"):
        cli.main(["account", "delete"])
    assert api_mod.load_config()["token"] == "test-token"


def test_a_service_token_in_the_environment_signs_commands_in_without_saving_it(monkeypatch, capsys):
    with pytest.raises(SystemExit, match="CTRANSFER_TOKEN"):  # neither signed in nor a token: says both ways
        cli.main(["account", "delete", "--yes"])
    monkeypatch.setenv("CTRANSFER_TOKEN", "cts_test")
    with pytest.raises(SystemExit, match="--yes"):  # past the sign-in check, at the confirmation
        cli.main(["account", "delete"])
    assert api_mod.Api.from_config().token == "cts_test"

    configure_signed_in()  # the env token wins over a saved sign-in...
    assert api_mod.Api.from_config().token == "cts_test"
    cli.main(["logout"])  # ...and is never written to the config
    assert "token" not in api_mod.load_config()
    assert "CTRANSFER_TOKEN is still set" in capsys.readouterr().err


def test_service_token_expiry_must_be_a_date():
    configure_signed_in()  # api.invalid: reaching the control plane would fail the test
    with pytest.raises(SystemExit, match="--expires takes a date like"):
        cli.main(["service-token", "create", "ci", "--scope", "sessions:read", "--expires", "31/12/2026"])
