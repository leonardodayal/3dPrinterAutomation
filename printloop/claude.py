"""Call Claude through the local Claude Code CLI (`claude -p`), using your claude.ai subscription.

No API key involved: ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are stripped from the child
environment so Claude Code can't silently switch to API billing. Log in once with `claude`
(or `claude auth login`) on the machine that runs the workflow.

Each call is a fresh, non-persisted session with a JSON schema for the answer. The only tool
Claude gets is Read, and only when there are images (e.g. the failure snapshot) to look at.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

CLAUDE_BIN = os.environ.get("CLAUDE_BIN", "claude")
TIMEOUT_SECONDS = int(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "1200"))
_STRIP_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDECODE")


class ClaudeError(RuntimeError):
    pass


def available() -> str | None:
    """Return a problem description, or None if the CLI is installed and logged in to claude.ai."""
    if not shutil.which(CLAUDE_BIN):
        return f"Claude Code CLI not found ({CLAUDE_BIN}). Install it and run `claude` once to log in."
    r = subprocess.run([CLAUDE_BIN, "auth", "status"], capture_output=True, text=True, env=_env(), timeout=60)
    try:
        status = json.loads(r.stdout)
    except ValueError:
        return f"Could not read `claude auth status`: {(r.stderr or r.stdout)[:300]}"
    if not status.get("loggedIn"):
        return "Claude Code is not logged in. Run `claude auth login` with your claude.ai account."
    if status.get("authMethod") != "claude.ai":
        return f"Claude Code is using {status.get('authMethod')!r}, not your claude.ai subscription."
    return None


def _env() -> dict[str, str]:
    return {k: v for k, v in os.environ.items() if k not in _STRIP_ENV}


def ask_json(model: str, effort: str, system: str, text: str, schema: type[T],
             images: Sequence[Path] = (), mcp_tools: Sequence[str] = (),
             timeout: int | None = None) -> T:
    """mcp_tools: exact claude.ai connector tool names to allow (e.g. Slack). Nothing else is allowed."""
    images = [Path(p).resolve() for p in images if p]
    env = _env()
    cmd = [
        CLAUDE_BIN, "-p",
        "--output-format", "json",
        "--json-schema", json.dumps(schema.model_json_schema()),
        "--system-prompt", system,
        "--model", model,
        "--effort", effort,
        "--no-session-persistence",
    ]
    builtin, allowed = [], []
    if images:
        builtin.append("Read")
        for d in {str(p.parent) for p in images}:
            cmd += ["--add-dir", d]
        text = ("First use the Read tool to look at these images:\n"
                + "\n".join(f"- {p}" for p in images) + "\n\n" + text)
    if mcp_tools:
        # Connector tools are deferred behind ToolSearch, and in -p mode claude.ai connectors
        # connect asynchronously; make Claude Code wait for them before the first turn.
        builtin.append("ToolSearch")
        allowed.extend(mcp_tools)
        env["MCP_CONNECTION_NONBLOCKING"] = "false"
        env.setdefault("CLAUDE_CODE_MCP_CONNECTOR_PREWAIT_MS", "20000")
        text = ("Load these tools first with ToolSearch (query `select:" + ",".join(mcp_tools) + "`).\n\n" + text)
    cmd += ["--tools", ",".join(builtin)]
    if builtin or allowed:
        cmd += ["--allowedTools", *builtin, *allowed]

    cwd = images[0].parent if images else Path.cwd()
    try:
        r = subprocess.run(cmd, input=text, capture_output=True, text=True, env=env, cwd=cwd,
                           timeout=timeout or TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as e:
        raise ClaudeError(f"Claude Code timed out after {timeout or TIMEOUT_SECONDS}s") from e
    try:
        out = json.loads(r.stdout)
    except ValueError:
        raise ClaudeError(f"claude -p failed (exit {r.returncode}): {(r.stderr or r.stdout)[-1000:]}") from None
    if out.get("is_error") or out.get("subtype") != "success":
        raise ClaudeError(f"Claude Code error ({out.get('subtype')}): {str(out.get('result'))[:1000]}")
    data = out.get("structured_output")
    if data is None:
        raise ClaudeError(f"No structured output returned: {str(out.get('result'))[:1000]}")
    return schema.model_validate(data)
