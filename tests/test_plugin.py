"""The Claude Code plugin: /ctransfer dispatcher, prompt guard hook, and manifests."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ctransfer import api as api_mod
from ctransfer import cli
from tests.conftest import configure_signed_in

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "plugin"
DISPATCH = PLUGIN / "scripts" / "ctransfer.sh"
BASE_ENV = {"PATH": "/usr/bin:/bin", "HOME": "/nonexistent"}


def run(cmd, env, stdin=None):
    return subprocess.run(cmd, env={**BASE_ENV, **env}, input=stdin, capture_output=True, text=True, cwd=ROOT)


@pytest.fixture
def cli_env(tmp_path):
    """Run the dispatcher against the real CLI, with ctransfer and Claude state under tmp_path."""
    wrapper = tmp_path / "ctransfer-cli"
    wrapper.write_text(f"#!/bin/bash\nexec {sys.executable} -m ctransfer.cli \"$@\"\n")
    wrapper.chmod(0o755)
    return {"CTRANSFER_BIN": str(wrapper), "CTRANSFER_CONFIG": str(api_mod.CONFIG_PATH), "PYTHONPATH": str(ROOT / "src"),
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude"), "CLAUDE_CODE_SESSION_ID": "s1"}


def test_dispatcher_status_reports_this_sessions_push(cli_env):
    out = run(["bash", str(DISPATCH), "status"], cli_env)
    assert out.returncode == 1 and "no ctransfer push recorded for session s1" in out.stdout
    cli.write_marker("s1", state="launching", transfer_id="t9")
    out = run(["bash", str(DISPATCH), "status"], cli_env)
    report = json.loads(out.stdout)
    assert report["transfer_id"] == "t9" and report["cloud_state"].startswith("unknown (not configured")


def test_dispatcher_passes_other_commands_to_cli(cli_env, laptop):
    for args in (["status", "abc"], ["ls"], ["pull", "abc", "--force"], ["rm", "abc"]):
        out = run(["bash", str(DISPATCH), *args], cli_env)
        assert out.returncode == 1 and "not configured" in out.stdout, (args, out.stdout)
    out = run(["bash", str(DISPATCH), "pull"], cli_env)
    assert out.returncode == 2 and "transfer_id" in out.stdout
    out = run(["bash", str(DISPATCH), "discover"], {**cli_env, "CLAUDE_CONFIG_DIR": str(laptop["home"]),
                                                    "CLAUDE_CODE_SESSION_ID": laptop["sid"]})
    assert json.loads(out.stdout)["session_id"] == laptop["sid"]


def test_dispatcher_rejects_unknown_and_interactive_commands(cli_env):
    assert run(["bash", str(DISPATCH), "frobnicate"], cli_env).returncode == 2
    for args in (["ssh", "abc"], ["resume", "sid-1"]):
        out = run(["bash", str(DISPATCH), *args], cli_env)
        assert out.returncode == 1 and f"ctransfer {' '.join(args)}" in out.stdout


def test_dispatcher_explains_install_when_cli_missing():
    out = run(["bash", str(DISPATCH)], {})
    assert out.returncode == 1 and "ctransfer CLI not found" in out.stdout and "pip install" in out.stdout


def guard(sid, prompt="hello"):
    out = run(["bash", str(PLUGIN / "hooks" / "guard.sh")], {"CTRANSFER_CONFIG": str(api_mod.CONFIG_PATH)},
              stdin=json.dumps({"session_id": sid, "prompt": prompt, "hook_event_name": "UserPromptSubmit"}))
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout) if out.stdout.strip() else None


def test_guard_allows_prompts_without_a_push():
    assert guard("s1") is None
    cli.write_marker("other-session", state="running", transfer_id="t9")
    assert guard("s1") is None


@pytest.mark.parametrize("state", ["queued", "pushing", "launching", "running"])
def test_guard_blocks_prompts_while_pushed(state):
    cli.write_marker("s1", state=state, transfer_id="t9", log="/nonexistent.log")
    verdict = guard("s1", "/ctransfer status")
    assert verdict["decision"] == "block" and "diverge" in verdict["reason"]
    assert ("ctransfer pull t9" in verdict["reason"]) == (state == "running")


@pytest.mark.parametrize("prompt", ["/ctransfer", "/ctransfer push --remote-control", "/ctransfer:ctransfer"])
def test_guard_confirms_the_invocation_that_queued_the_push(prompt):
    # The skill's shell block runs before the hook, so the marker already exists for this very prompt.
    cli.write_marker("s1", state="queued")
    verdict = guard("s1", prompt)
    assert verdict["decision"] == "block" and verdict["reason"].startswith("ctransfer: push queued.")


def test_guard_reports_a_failed_push_once():
    cli.write_marker("s1", state="failed", error="not configured")
    assert "not configured" in guard("s1")["reason"]
    assert guard("s1") is None


@pytest.mark.parametrize("args", [[], ["--remote-control"]])
def test_slash_command_push_end_to_end(cli_env, args):
    """Dispatcher -> CLI --detach -> background worker -> marker the guard reads. The session has no
    transcript, so the worker fails after its wait, which the guard then reports."""
    configure_signed_in()
    out = run(["bash", str(DISPATCH), *args], cli_env)
    assert out.returncode == 0 and "queued push" in out.stdout, out.stdout

    deadline = time.time() + 30
    while (cli.read_marker("s1") or {}).get("state") in ("queued", "pushing") and time.time() < deadline:
        time.sleep(0.2)
    marker = cli.read_marker("s1")
    assert marker["state"] == "failed" and "no transcript for session s1" in marker["error"]
    log = Path(marker["log"]).read_text()
    assert "push --session s1 --track" in log and "--when-idle" in log and ("--remote-control" in log) == bool(args)
    assert "push of this session failed" in guard("s1")["reason"]
    assert guard("s1") is None  # reported once


def test_manifests_point_at_real_files():
    market = json.loads((ROOT / ".claude-plugin" / "marketplace.json").read_text())
    entry, = market["plugins"]
    plugin_dir = (ROOT / entry["source"]).resolve()
    manifest = json.loads((plugin_dir / ".claude-plugin" / "plugin.json").read_text())
    assert manifest["name"] == entry["name"] == "ctransfer"

    skill = (plugin_dir / "skills" / "ctransfer" / "SKILL.md").read_text()
    assert skill.startswith("---\nname: ctransfer\n")  # a skill, so the bare /ctransfer resolves
    assert '"${CLAUDE_PLUGIN_ROOT}/scripts/ctransfer.sh" $ARGUMENTS' in skill
    hooks = json.loads((plugin_dir / "hooks" / "hooks.json").read_text())
    hook_cmd = hooks["hooks"]["UserPromptSubmit"][0]["hooks"][0]["command"]
    assert "${CLAUDE_PLUGIN_ROOT}/hooks/guard.sh" in hook_cmd
    for script in (DISPATCH, plugin_dir / "hooks" / "guard.sh"):
        assert os.access(script, os.X_OK)
