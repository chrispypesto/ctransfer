# ctransfer

Move a live Claude Code session from your laptop to a cloud VM, keep working there, and pull it back. The transfer is verbatim: the transcript, `/rewind` checkpoints, project memory, and the workspace, including uncommitted and untracked changes.

This repo has the client (the `ctransfer` CLI and the agent it ships to the VM) and the `/ctransfer` Claude Code plugin. The client talks to the hosted control plane at `https://api.ctransfer.itschrispy.com` (AWS Lambda, Aurora DSQL, S3, SSM, Lightsail); `ctransfer configure --api-url <url>` or `CTRANSFER_API_URL` points it at another deployment. The control plane's code and Terraform aren't published yet.

## Install

Into any Python 3.10+ environment (venv, pipx, uv, conda, …):

```bash
pip install "git+https://github.com/chrispypesto/ctransfer.git"
ctransfer login                            # GitHub device flow
claude setup-token | ctransfer token set   # the Claude token your VMs run on
```

From a checkout, `make install` installs it into the active environment's Python (`make install PYTHON=/path/to/python` picks another).

No AWS credentials are needed. `ctransfer ssh` uses a key that `push` generates for each transfer (`~/.config/ctransfer/keys/<id>`), so it works from the machine that pushed. `ctransfer usage` shows your free-tier usage.

## `/ctransfer` in Claude Code

```
/plugin marketplace add chrispypesto/ctransfer
/plugin install ctransfer@ctransfer
```

The plugin needs the CLI installed. It runs `$CTRANSFER_BIN` if set, otherwise `ctransfer` from `PATH`, and prints the install steps if it finds neither.

| Command | Does |
|---|---|
| `/ctransfer [--remote-control] [--no-file-history]` | push **this** session once it is idle |
| `/ctransfer status [id]` | this session's push (state, cloud state, log tail), or one transfer |
| `/ctransfer ls`, `pull <id>`, `rm <id>` | same as the CLI |

What `/ctransfer` does:
1. It queues a background `ctransfer push --current --when-idle`. `--current` resolves the session from `CLAUDE_CODE_SESSION_ID`, so it pushes this exact session, not the newest one in the directory.
2. The plugin's `UserPromptSubmit` hook then blocks the `/ctransfer` prompt itself. The skill's shell block runs before the hook, so the push is already queued. Blocking the prompt means no turn runs and the pushed transcript ends at your previous turn.
3. From then on, every prompt you send in that session is blocked, and the hook shows the push's progress instead (the worker logs to `~/.config/ctransfer/logs/<session>.log`). `/exit` once it says running. If the push fails, that is reported once and the session stays local.
4. The VM's `claude` runs with an appended system prompt that says the transfer is complete, so it doesn't repeat the handoff.

`ssh` and `resume` need a real terminal, so run them outside Claude Code. For `pull`, `/exit` the pushed session first, then run `/ctransfer pull <id>` from a new session in the same directory, or `ctransfer pull <id>` from a terminal. Then continue with `ctransfer resume <session-id>`: it runs `claude --resume` with an appended system prompt saying the session is back on the laptop, the counterpart of the VM's arrival note.

## CLI

```bash
ctransfer discover                 # which session would be pushed (newest one for this directory)
ctransfer push [--remote-control]  # bundle, upload, launch, wait until the VM is running
#   then /exit the local session: continuing locally makes the two copies diverge
ctransfer ssh <transfer-id>        # attach to the VM's `claude` tmux session (Ctrl-b d to detach)
ctransfer ls | status <id>
ctransfer pull <transfer-id>       # export at the next turn boundary, overwrite local, delete the VM
ctransfer resume <session-id>      # carry on locally (claude --resume, told the session is back)
```

`push --current` targets the Claude Code session running the command. `push --when-idle --detach` waits for that session's turn to end in a background worker, which is what `/ctransfer` uses.

`pull` refuses to run, and changes nothing, when:
- the session is open locally;
- the local transcript has records the cloud copy lacks (`--force` overwrites them, `--as-fork` pulls into a new session id instead);
- the local workspace has uncommitted changes (`--stash` stashes them) or its branch diverged (`--force`).

Untracked credential-looking files (`.env`, `*.pem`, `id_rsa*`, `*.tfvars`, …) are never uploaded. Pull leaves them alone.

## Layout

- `src/ctransfer/`: the package. `sessions`, `workspace`, `bundle`, `net` and `agent` are standard-library only, because they ship to the VM inside each bundle.
- `plugin/`: the Claude Code plugin (`/ctransfer` skill, dispatcher script, prompt guard hook). `.claude-plugin/marketplace.json` publishes it.
- `tests/`: client and plugin tests, all on real logic. Tests that need the control plane live with it.

## Development

```bash
pip install -e '.[test]'     # any Python 3.10+ env; environment.yml is the conda equivalent
make test
claude --plugin-dir plugin   # try the plugin from this checkout
make install-plugin          # or install it from this checkout; edits apply on /reload-plugins
```
