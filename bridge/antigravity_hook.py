#!/usr/bin/env python3
"""Sign Antigravity's native workspace hint without changing its approval policy.

Clients launch this with an absolute Python executable, this script and a private
bridge config. The sibling bridge module validates that config using stdlib only.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import sys

from stdio import load_config


MAX_INPUT_BYTES = 1024 * 1024
MAX_CONFIG_BYTES = 1024 * 1024


def reject_constant(_value):
    raise ValueError("Invalid JSON number")


def find_config(argument):
    if argument:
        return argument
    if os.environ.get("AGENTS_BRIDGE_CONFIG"):
        return os.environ["AGENTS_BRIDGE_CONFIG"]
    path = Path.home() / ".gemini" / "config" / "mcp_config.json"
    # Avoid blocking on a non-file or reading an unbounded native configuration.
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0))
    with os.fdopen(descriptor, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            return None
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        return None
    config = json.loads(raw, parse_constant=reject_constant)
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    entry = servers.get("Agents-Core") if isinstance(servers, dict) else None
    args = entry.get("args") if isinstance(entry, dict) else None
    # Both Node and Python native registrations use [absolute_script, config].
    return args[1] if isinstance(args, list) and len(args) > 1 and isinstance(args[1], str) else None


def overwrite(input, token):
    if not isinstance(input, dict):
        return None
    tool = input.get("toolCall")
    paths = input.get("workspacePaths")
    if not isinstance(tool, dict) or not isinstance(paths, list) or not paths or not isinstance(paths[0], str):
        return None
    workspace = paths[0].strip()
    if not workspace or "\x00" in workspace:
        return None
    name = tool.get("name")
    if not isinstance(name, str):
        return None
    args = tool.get("args")
    if args is None:
        args = {}
    if not isinstance(args, dict):
        return None
    lazy = name == "call_mcp_tool" and args.get("ServerName") in ("Agents-Core", "Agents_Core")
    eager = name.startswith(("mcp_Agents-Core_", "mcp_Agents_Core_"))
    if not lazy and not eager:
        return None
    signature = hmac.new(token.encode("utf-8"), ("workspace:" + workspace).encode("utf-8"), hashlib.sha256).hexdigest()
    if lazy:
        arguments = args.get("Arguments")
        is_string = isinstance(arguments, str)
        if is_string:
            arguments = json.loads(arguments, parse_constant=reject_constant)
        elif arguments is None:
            arguments = {}
        if not isinstance(arguments, dict):
            return None
        updated = {**arguments, "workspace": workspace, "workspace_signature": signature}
        # PreToolUse's overwrite merges tool-call fields, preserving ServerName,
        # ToolName and every other native wrapper field.
        return {"Arguments": json.dumps(updated, ensure_ascii=True, allow_nan=False, separators=(",", ":")) if is_string else updated}
    return {**args, "workspace": workspace, "workspace_signature": signature}


def main():
    response = {"decision": "ask"}
    try:
        path = find_config(sys.argv[1] if len(sys.argv) > 1 else None)
        if path:
            config, _endpoint = load_config(path, allow_stdio=True)
            token = re.sub(r"^Bearer\s+", "", config["headers"]["Authorization"], flags=re.I).strip()
            raw = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
            if token and len(raw) <= MAX_INPUT_BYTES and raw.strip():
                updated = overwrite(json.loads(raw, parse_constant=reject_constant), token)
                if updated is not None:
                    response["overwrite"] = updated
    except Exception:
        # A hook failure must preserve the client's normal confirmation policy.
        # Diagnostics must never expose the private config, URL or bearer token.
        pass
    sys.stdout.write(json.dumps(response, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
