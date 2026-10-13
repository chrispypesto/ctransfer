# ctransfer

Move a live Claude Code session from your laptop to a cloud VM, keep working there, and pull it back. The transfer is verbatim: the transcript, `/rewind` checkpoints, project memory, and the workspace, including uncommitted and untracked changes.

This repo has the client (the `ctransfer` CLI and the agent it ships to the VM) and the `/ctransfer` Claude Code plugin. The client talks to the hosted control plane at `https://api.ctransfer.itschrispy.com` (AWS Lambda, Aurora DSQL, S3, SSM, Lightsail). For development against another deployment, set `CTRANSFER_API_URL`. The control plane's code and Terraform aren't published yet.

## Install

Into any Python 3.11+ environment (venv, pipx, uv, conda, …):

```bash
pip install "git+https://github.com/chrispypesto/ctransfer.git"
ctransfer login                            # GitHub device flow; signing in accepts the Terms of Use
claude setup-token | ctransfer token set   # the Claude token your VMs run on
```

From a checkout, `make install` installs it into the active environment's Python (`make install PYTHON=/path/to/python` picks another).

No AWS credentials are needed. `ctransfer ssh` uses a key that `push` generates for each transfer (`~/.config/ctransfer/keys/<id>`), so it works from the machine that pushed. `ctransfer usage` shows your plan, credit and usage.

## Plans

Every account starts on the free tier: one nano VM at a time, with daily and monthly limits that `ctransfer usage` shows. Pro is a monthly subscription that includes a month's worth of VM credit, lets you add more credit whenever you like, and raises the limits: more VMs at once, more launches, longer scheduled runs, bigger bundles, and bigger VMs (`--size small` for 2 GB, `--size large` for 8 GB on `push`, `new` and `cron add`). Pro VMs are charged from your credit per started hour; when it runs out they are brought home, and you can `pull` them as usual.

You subscribe, add credit and cancel only on the website's [account page](https://ctransfer.itschrispy.com/account), signed in with the same GitHub account. Prices and hourly rates are listed there.

## `/ctransfer` in Claude Code

```
/plugin marketplace add chrispypesto/ctransfer
/plugin install ctransfer@ctransfer
```

The plugin needs the CLI installed. It runs `$CTRANSFER_BIN` if set, otherwise `ctransfer` from `PATH`, and prints the install steps if it finds neither.

| Command | Does |
|---|---|
| `/ctransfer [push] [--remote-control] [--no-file-history] [--size=large]` | push **this** session once it is idle (`--size` needs Pro) |
| `/ctransfer push [--flags] <prompt>` | same, and the cloud session runs `<prompt>` as soon as it arrives |
| `/ctransfer new [--remote-control] [prompt]` | start a **new** session on a cloud VM in a copy of this project; this session carries on |
| `/ctransfer status [id]` | this session's push (state, cloud state, log tail), or one transfer |
| `/ctransfer ls`, `usage`, `pull <id>`, `rm <id>` | same as the CLI |

What `/ctransfer` does:
1. It queues a background `ctransfer push --current --when-idle`. `--current` resolves the session from `CLAUDE_CODE_SESSION_ID`, so it pushes this exact session, not the newest one in the directory.
2. The plugin's `UserPromptSubmit` hook then blocks the `/ctransfer` prompt itself. The skill's shell block runs before the hook, so the push is already queued. Blocking the prompt means no turn runs and the pushed transcript ends at your previous turn.
3. From then on, every prompt you send in that session is blocked, and the hook shows the push's progress instead (the worker logs to `~/.config/ctransfer/logs/<session>.log`). `/exit` once it says running. If the push fails, that is reported once and the session stays local.
4. The VM's `claude` runs with an appended system prompt that says the transfer is complete, so it doesn't repeat the handoff.

`/ctransfer new` runs `ctransfer new --detach` in the background (log in `~/.config/ctransfer/logs/<new-session>.log`) and doesn't touch the session you typed it in. The prompt is passed through as typed, quotes and all. `/ctransfer ls` shows the new transfer.

`ssh` and `resume` need a real terminal, so run them outside Claude Code. For `pull`, `/exit` the pushed session first, then run `/ctransfer pull <id>` from a new session in the same directory, or `ctransfer pull <id>` from a terminal. Then continue with `ctransfer resume <session-id>`: it runs `claude --resume` with an appended system prompt saying the session is back on the laptop, the counterpart of the VM's arrival note.

## CLI

```bash
ctransfer push [--remote-control]  # the newest session in this directory (it names it), to a VM; waits until running
ctransfer push --size large        # same, on an 8 GB VM (Pro; `small` is 2 GB, the default `nano` 0.5 GB)
ctransfer push "keep going, then run the tests"   # same, and the VM's claude starts on this right away
#   then /exit the local session: continuing locally makes the two copies diverge
ctransfer new ["first prompt"]     # start a fresh session on a VM in a copy of this directory
ctransfer ssh <transfer-id>        # attach to the VM's claude; Ctrl-b then d detaches and leaves it running
ctransfer ls | status <id> | usage
ctransfer pull <transfer-id>       # export at the next turn boundary, overwrite local, delete the VM
ctransfer resume <session-id>      # carry on locally (claude --resume, told the session is back)
ctransfer account delete           # delete your account: VMs, sessions, schedules, Claude token, secrets
```

With a prompt, `push` starts the VM's `claude --resume` with it, so the session gets to work without anyone attaching. Without one it waits idle in tmux for `ctransfer ssh`.

`ssh` runs claude in tmux on the VM, and the tmux status bar shows how to detach. Quitting claude there (`/exit`) stops it on the VM; the session is still there to `pull`.

A session brought home without `pull` (the service does this after a VM has been idle or running for long, or when your plan's limits are reached) waits in the cloud for you to `pull` it, but only for a limited time (see the [Privacy Policy](PRIVACY.md#retention)): `ctransfer ls` (KEPT UNTIL) and `ctransfer status <id>` show the date it is deleted.

`push --current` targets the Claude Code session running the command. `push --when-idle --detach` waits for that session's turn to end in a background worker, which is what `/ctransfer` uses.

`new` sends the workspace, your Claude config and the project's memory, but no transcript. The VM starts `claude --session-id <id>`, with the prompt if you gave one, and an appended system prompt saying how the session was started. `pull` brings it back like any pushed session. If nobody ever prompted it, `pull` brings back only the workspace.

Uncommitted changes that were pushed are part of the cloud copy, so `pull` replaces them with what the VM made of them. It refuses to run, and changes nothing locally or in the cloud (the VM keeps running), when:
- the session is open locally;
- the local transcript has records the cloud copy lacks (`--force` overwrites them, `--as-fork` pulls into a new session id instead);
- the local workspace has changes made after the push (`--stash` sets them aside as a git stash; pull then says how to get them back) or its branch has commits the cloud copy lacks (`--force`).
- the workspace is a folder that isn't a git repo, and files in it changed after the push (pull names them; `--force` copies them to a backup folder, says where, then pulls).

These checks run against what was pushed from this machine. For a session pushed from another machine, `pull` checks only that the session isn't open locally before stopping the VM, and anything else that is wrong shows up after the VM has stopped; the cloud copy is still there to `pull` again.

Untracked credential-looking files (`.env`, `*.pem`, `id_rsa*`, `*.tfvars`, …) are never uploaded. Pull leaves them alone.

## Credentials on the VM

The VM gets your Claude token and nothing else unless you store more. To give it credentials (a GitHub token, API keys for your MCP servers, ...), store them as secrets:

```bash
ctransfer secret set GITHUB_TOKEN      # value from a hidden prompt, or piped: ... | ctransfer secret set NAME
ctransfer secret ls                    # names only; values are never shown again
ctransfer secret rm GITHUB_TOKEN
```

- Every VM launched afterwards (pushes, `new`, scheduled runs) gets each secret as an environment variable of its `claude`, and so of the commands and MCP servers it starts. A VM that is already running keeps what it was given.
- Secrets are SSM SecureStrings on the control plane. The api can store and delete them but not read them back; only the function that bootstraps a VM reads them, once per VM. On the VM they live in `~/.ctransfer/env` (mode 0600). They are never part of a bundle.
- Names are environment variable names (`A-Z`, `0-9`, `_`). `CLAUDE_CODE_OAUTH_TOKEN` and `CTRANSFER_*` are reserved.

### GitHub

A secret named `GITHUB_TOKEN` or `GH_TOKEN` also sets up GitHub on the VM:
- git uses it for `https://github.com`, through a credential helper that reads `~/.ctransfer/env`. Remotes written as `git@github.com:...` are rewritten to HTTPS, so pushes work without an SSH key.
- `gh` is installed and picks the token up from the environment, so the session can open, review and comment on pull requests.
- Commits on the VM carry the author from your repo's git config (`user.name`, `user.email`).

Prefer a [fine-grained personal access token](https://github.com/settings/personal-access-tokens/new) limited to the repositories the VM works on, with Contents and Pull requests read/write. `gh auth token | ctransfer secret set GITHUB_TOKEN` also works, but hands the VM your laptop login's full scope.

### MCP servers

A push carries your MCP server configs along with the session:
- user-scope servers (`claude mcp add -s user`) and this project's local-scope ones (the default scope), from `~/.claude.json`, plus the project's choices about the servers in its `.mcp.json`. The `.mcp.json` itself is part of the workspace.
- A server whose `env` or `headers` contain a literal credential (a key named like `*TOKEN*`, `*KEY*`, `Authorization`, ...) stays on the laptop, and the push says so. Store the value as a secret and reference it instead: `"env": {"GITHUB_PERSONAL_ACCESS_TOKEN": "${GITHUB_TOKEN}"}`. Claude Code expands `${NAME}` from its environment, which on the VM holds your secrets.
- Servers that sign in with OAuth (most remote ones) have their tokens in your laptop's keychain. Those tokens don't move, so these servers need authenticating again on the VM.
- Stdio servers run on the VM, so their command must exist there (the VM has Python 3; there is no Node.js unless the session installs it).
- claude.ai connectors (the ones listed as `claude.ai <name>` in `claude mcp list`) are not available on the VM: they come with a full claude.ai sign-in, and the VM runs on the narrower `claude setup-token` token. Add the servers you need as user- or local-scope MCP servers instead.

## Scheduled runs

`ctransfer cron` snapshots a session once, then runs a task on it on a cron schedule. Each run gets its own short-lived VM:

```bash
ctransfer cron add --schedule '0 9 * * 1-5' --prompt 'Triage new issues and summarize'   # UTC, at most hourly
ctransfer cron ls
ctransfer cron run <schedule-id>      # start a run now
ctransfer cron logs <schedule-id>     # recent runs: status, cost, the final message of each
ctransfer cron pull <run-id>          # a run's transcript as a new local session (the workspace is left alone)
ctransfer cron rm <schedule-id>
```

How a run works:
- The VM boots from the snapshot taken by `cron add`, fast-forwards the branch from its remote (public remotes only, unless a GitHub token secret lets the VM read private ones), and runs one headless turn: `claude -p <task> --resume <session> --fork-session`.
- Every run forks the same snapshot, so runs never add to each other's context and the session stays the size it was when you pushed it. To keep some continuity, each run's system prompt includes short summaries of the last five runs (400 characters each, about 2 KB in all).
- Edits on the VM are thrown away when the run ends unless the task commits and pushes them.
- Runs can't ask for permission. `--permission-mode` (`default`, `acceptEdits`, `plan`, `bypassPermissions`) sets the mode. With `default`, the allow rules in your `settings.json` apply, and `cron logs` shows how many tool calls were denied.
- `--timeout` bounds each run (default and maximum: your plan's run limit, which `ctransfer usage` shows). `--size` picks the runs' VM size (Pro).
- Runs count against your plan like pushes do: launches per day, VMs at once, and VM-hours (free) or credit (Pro). A run that a limit refuses shows as `refused` in `cron logs`.
- A run doesn't start while the previous one is still going.

## Tasks

`ctransfer task` runs Claude on a GitHub repo without your laptop: no local folder or session is sent. You can then continue the same conversation with follow-ups, for example to address review comments:

```bash
ctransfer task start owner/repo --ref my-branch --prompt 'Fix the flaky test and open a PR' --label issue=42
ctransfer task show <task-id>          # status, Claude's final message, cost, turns, permission denials, session id
ctransfer task ls [--label issue=42]
ctransfer task follow-up <task-id> --prompt 'Address the review comment on line 12' --wait
ctransfer task rm <task-id>            # also stops a step that is still running
```

`start` and `follow-up` print the task id on stdout, so scripts can capture it. With `--wait` they print the result when the step ends and exit 1 unless it succeeded.

How a task works:
- Each step (the start, then each follow-up) gets its own short-lived VM and runs one headless turn under `--timeout`, like a scheduled run.
- The first step clones the repo at `--ref` (default: its default branch) with your `GH_TOKEN` or `GITHUB_TOKEN` secret, which a private repo needs (see GitHub above). Commits made on the VM are authored as your GitHub login with its noreply address. Your laptop's Claude config (`CLAUDE.md`, `settings.json`, MCP servers) is not part of a task.
- When a step ends, its transcript and the whole workspace, uncommitted changes included, are saved. A follow-up starts a new VM from that state and resumes the same session: the same session id, the full conversation so far, no fork.
- Only one step runs at a time: a follow-up sent while a step is running is refused. If no step got as far as saving its state (say its VM never started), a follow-up starts again from the repo. Saved state expires a week after its step ends; a follow-up after that is refused, so start a new task.
- If Claude stops on your account's usage limit, the task shows as `paused: usage limit`, with the time the limit resets. Send a follow-up after that time to carry on.
- Tasks count against your plan like pushes and scheduled runs: launches per day, VMs at once, and VM-hours (free) or credit (Pro).

### Setup script

The VM comes with git, tmux, curl, Python 3 and (with a GitHub token) `gh`. If your repo needs more to build or test (a language toolchain, Docker, a database), commit a script at `.ctransfer/setup.sh`. Before Claude starts, every task step and scheduled run runs it with bash:
- from the repo's root, as the VM's user, who can use `sudo` (for `apt-get install`, say);
- with your secrets as environment variables, as Claude gets them;
- within the step's `--timeout`, which covers setup and Claude together.

Each step runs on a fresh VM, so the script runs again every time; write it so a second run does no harm. Things it installs under your home directory aren't on Claude's `PATH` by itself: append shell assignments to the file named by `$CTRANSFER_SETUP_ENV`, and Claude starts with them:

```bash
echo 'PATH="$HOME/miniforge3/envs/myapp/bin:$PATH"' >> "$CTRANSFER_SETUP_ENV"
```

`task show` and `cron logs` show how long setup took. If the script fails or runs out of time, Claude doesn't start: the step ends as `error`, and they show the end of the script's output. Sessions you push or start with `new` don't run the script: you're there to run it yourself (`bash .ctransfer/setup.sh`) if the session needs it, and Claude starts without waiting for it.

## Automation (service tokens)

`ctransfer login` needs you at a browser to enter a code. To use ctransfer from CI, a script, a server or an agent, create a service token once and give it to that machine:

```bash
ctransfer service-token create github-ci --scope sessions:read --scope sessions:run [--expires 2027-01-31]
ctransfer service-token ls                 # names, scopes, expiry, last use
ctransfer service-token rm github-ci       # anything using it is refused from then on
```

`create` prints the token once; store it in the machine's secret store (e.g. your CI's secrets). On that machine, set `CTRANSFER_TOKEN` to it and run ctransfer as usual, without `ctransfer login`:

```bash
export CTRANSFER_TOKEN=cts_...
ctransfer ls
```

- A token acts as you, but only for what its scopes allow. `create` without `--scope` lists the scopes. Your plan's limits apply as usual.
- No token can create, list or remove service tokens, change your Claude token or secrets, open billing, or delete your account; sign in yourself for those.
- When `CTRANSFER_TOKEN` is set, it is used instead of any saved sign-in, and it is never written to disk.
- `--expires` is the last day (UTC) the token works; without it, it works until you remove it. Deleting your account removes every token. Each account can have a limited number at once; `ls` shows how many.

## Terms, privacy and license

Using the hosted service means agreeing to the [Terms of Use](https://ctransfer.itschrispy.com/terms) ([TERMS.md](TERMS.md)). The [Privacy Policy](https://ctransfer.itschrispy.com/privacy) ([PRIVACY.md](PRIVACY.md)) says what the service stores, for how long, and how `ctransfer account delete` removes it. The client and plugin in this repo are MIT licensed ([LICENSE](LICENSE)).

## Layout

- `src/ctransfer/`: the package. `sessions`, `workspace`, `bundle`, `net` and `agent` are standard-library only, because they ship to the VM inside each bundle.
- `plugin/`: the Claude Code plugin (`/ctransfer` skill, dispatcher script, prompt guard hook). `.claude-plugin/marketplace.json` publishes it.
- `tests/`: client and plugin tests, all on real logic. Tests that need the control plane live with it.

## Development

```bash
pip install -e '.[test]'     # any Python 3.11+ env; environment.yml is the conda equivalent
make test
claude --plugin-dir plugin   # try the plugin from this checkout
make install-plugin          # or install it from this checkout; edits apply on /reload-plugins
```
