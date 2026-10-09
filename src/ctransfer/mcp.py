"""MCP servers configured in ~/.claude.json, carried to the VM.

Claude Code keeps user-scope servers (`claude mcp add -s user`) in ~/.claude.json's top-level mcpServers, and
local-scope ones (its default scope) under projects[<path>].mcpServers, applying them only to sessions at that exact
path. Project-scope servers are in the repo's .mcp.json, which travels with the workspace; the choices made about them
(enabledMcpjsonServers, ...) sit next to the local-scope servers and travel with them.

A server whose env or headers hold a literal credential stays behind, like credential-looking files do: store the value
with `ctransfer secret set NAME` and write it as ${NAME} in the config, which Claude Code expands from its environment
(the VM's claude runs with the user's secrets in it). claude.ai connectors come with the Claude account, not a file.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT_KEYS = ("mcpServers", "enabledMcpjsonServers", "disabledMcpjsonServers", "enableAllProjectMcpServers")
SECRET_KEY = re.compile(r"token|secret|passw|key|auth|credential|cookie|session", re.IGNORECASE)


def _holds_literal_secret(server: dict) -> bool:
    for field in ("env", "headers"):
        for key, value in (server.get(field) or {}).items():
            if isinstance(value, str) and value and "${" not in value and SECRET_KEY.search(key):
                return True
    return False


def collect(config_path: Path, project_paths: list[str], skipped: list[str]) -> dict:
    """The user-scope servers, and the local-scope servers and .mcp.json choices of project_paths, minus servers with
    literal credentials (named in skipped as mcp:<scope>:<name>). Empty when there is nothing to carry."""
    try:
        cfg = json.loads(Path(config_path).read_text())
    except (OSError, ValueError):
        return {}

    def keep(scope: str, servers) -> dict:
        kept = {}
        for name, server in (servers or {}).items():
            if not isinstance(server, dict):
                continue
            if _holds_literal_secret(server):
                skipped.append(f"mcp:{scope}:{name}")
            else:
                kept[name] = server
        return kept

    carried: dict = {}
    user = keep("user", cfg.get("mcpServers"))
    if user:
        carried["user"] = user
    for path in dict.fromkeys(project_paths):
        entry = (cfg.get("projects") or {}).get(path) or {}
        project = {key: entry[key] for key in PROJECT_KEYS[1:] if entry.get(key)}
        servers = keep("local", entry.get("mcpServers"))
        if servers:
            project["mcpServers"] = servers
        if project:
            carried.setdefault("projects", {})[path] = project
    return carried


def names(carried: dict) -> list[str]:
    """What a push reports it carried, e.g. ["github (user)", "notion (local)"]."""
    out = [f"{name} (user)" for name in carried.get("user") or {}]
    for project in (carried.get("projects") or {}).values():
        out += [f"{name} (local)" for name in project.get("mcpServers") or {}]
    return out


def install(cfg: dict, carried: dict, root_prefix: str = "") -> None:
    """Merge carried servers into the VM's ~/.claude.json contents, at the same project paths (under root_prefix)."""
    if carried.get("user"):
        cfg.setdefault("mcpServers", {}).update(carried["user"])
    for path, project in (carried.get("projects") or {}).items():
        target = cfg.setdefault("projects", {}).setdefault(root_prefix + path, {})
        for key, value in project.items():
            if key == "mcpServers":
                target.setdefault("mcpServers", {}).update(value)
            else:
                target[key] = value
