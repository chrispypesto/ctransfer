# CLAUDE.md

## PR review loop (authoring agents)
Reviewer = `.github/workflows/claude.yml` (claude-code-action, comment as `github-actions[bot]`). Triggered by `@claude` in a PR comment; same-repo PRs only.
1. Open PR. Post `@claude review` as conversation comment via pr-watch helper: `~/.claude/skills/pr-watch/pr-watch.sh reply <o/r#N> --conversation --body-file F` (watcher then ignores own comment).
2. Start `pr-watch` skill on the PR.
3. Reviewer comment is created as placeholder ("Claude Code is working…") then edited in place; watcher emits only the placeholder. On a `github-actions[bot]` event: `gh run watch <run id from its View job link> --exit-status`, then re-fetch body (`gh api repos/<o>/<r>/issues/comments/<id> --jq .body`, or `pulls/comments/<id>` for inline).
4. Address every finding per pr-watch (fix+push or reply with reason).
5. Post `@claude review` again (step 1 method). Repeat 3–5.
6. Stop when reviewer's last line is exactly `All good: ready for a human to merge.` → TaskStop pr-watch monitor, done. Never merge; a human merges.
- Run failed/no reply → check `gh run view <id> --log-failed`, fix or tell user; don't loop blindly.
