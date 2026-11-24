"""Offline audit of what Agents-Core puts into a client's context.

Issue #251 proposed an offline context-size audit; this is it, written to measure
the effect of the #734 changes (a headless one-shot run cost 3.6x the tokens with
Agents-Core) without a model. It reports characters (Python ``len``) of:

* the managed template ``scripts/templates/routing-protocol-core.md`` (and its
  UTF-8 bytes);
* the MCP server instructions a stdio initialize carries, in full and the first
  4,096 characters Claude Code shows;
* the full ``SUCCESS`` instruction and its pointer;
* a ``ROUTE_REQUIRED`` response for a sample query on an empty router cache;
* the first-activation ``SUCCESS`` per block (instruction, persona, rules, skills,
  implants) for data_analyst, software_engineer and security_expert;
* per tool, the description plus the compact ``inputSchema``, and the sum for
  route_and_load, get_agent_context and log_interaction;
* for every persona result above, its length and backslashes as the server returns
  it and as the client receives it.

Each ``AGENTS_CLIENT_PROFILE`` value is measured in its own child process, with the
variable set before anything is imported, so a change bound at import time (a tool
description, the server instructions) is measured as a client would meet it. The
child points the data directory at a throwaway copy before importing the engine, as
``evals/runners/run_routing.py`` does, so no live router cache is read or written.

Without ``--embedder`` nothing loads the embedding model: skills and implants are
selected in the agent's declared order (preferred, then capable skills; preferred
implants), which can differ from retrieval. ``--embedder`` retrieves them and builds
the skill and implant stores in the throwaway directory first, once per profile.

Kept out on purpose, because they depend on the machine rather than on the code:
the ``repo_context`` a first activation in a workspace carries, the outdated-section
``warnings``, the personal library's rules and switches, and the router cache write
a ``SUCCESS`` triggers. The client text is modelled as the compact JSON of the
result's ``structuredContent`` when the tool returns one, otherwise the text content.
A string result FastMCP wraps as ``{"result": text}`` arrives escaped twice
(``wrapped``); the persona tools deliver the object itself over stdio (#734). No size here is a
budget: other changes are still moving these numbers.

Usage (from the repository root):
    python evals/scripts/context_size_audit.py
    python evals/scripts/context_size_audit.py --json
    python evals/scripts/context_size_audit.py --profile headless --embedder
"""
from __future__ import annotations

import argparse
import asyncio
import atexit
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

PROFILES = ("interactive", "headless")
TEMPLATE = Path("scripts/templates/routing-protocol-core.md")
# Claude Code shows this much of the server instructions (src/engine/protocol_text.py).
CLAUDE_CODE_WINDOW = 4096
ROUTE_QUERY = "Find why the nightly CSV export drops rows with non-ASCII names and fix it."
# Queries of standard tier: longer than 50 characters, no deep-tier signal word.
ACTIVATIONS = {
    "data_analyst": "Summarize monthly churn by customer segment from the attached CSV and chart the trend.",
    "software_engineer": "Add input validation to the signup endpoint and cover the error paths with tests.",
    "security_expert": "Check this login handler for injection and session fixation before the release.",
}
BLOCKS = {"instruction": "instruction", "persona": "persona_block", "rules": "rules_block",
          "skills": "skills_block", "implants": "implants_block"}
PERSONA_TOOLS = ("route_and_load", "get_agent_context", "log_interaction")


# --- Measurement: runs inside a process whose data directory is already isolated ------------

def _declared_skills(query, *, mandatory=None, preferred=None, capable=None, n_results=3, **_):
    """The first ``n_results`` preferred, then capable, skills that are not core."""
    ids = [value for value in dict.fromkeys([*(preferred or []), *(capable or [])])
           if value not in (mandatory or [])]
    return [{"filename": value + ".mdc"} for value in ids[:n_results]]


def _declared_implants(query, *, preferred_implants=None, n_results=2, **_):
    return [{"filename": value + ".mdc"} for value in (preferred_implants or [])[:n_results]]


async def _no_repo_context(*_args, **_kwargs):
    return None


async def _no_cache_write(*_args, **_kwargs):
    return None


class _EmbedderGuard:
    """Stands in for the model loader; records and refuses every attempt to load it."""

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise RuntimeError("the embedder is not loaded without --embedder")


async def _call_tool(server, name: str, arguments: dict) -> tuple[str, str, bool]:
    """``(text the server returns, text the client receives, whether it arrives wrapped)``."""
    result = await server.mcp.call_tool(name, arguments)
    content, structured = result if isinstance(result, tuple) else (result, None)
    text = "".join(block.text for block in content if getattr(block, "type", None) == "text")
    if structured is None:
        return text, text, False
    wrapped = isinstance(structured, dict) and set(structured) == {"result"} and isinstance(
        structured["result"], str)
    return text, json.dumps(structured, ensure_ascii=False, separators=(",", ":")), wrapped


def _sizes(text: str, client: str, wrapped: bool) -> dict:
    return {"response_chars": len(text), "response_backslashes": text.count("\\"),
            "client_chars": len(client), "client_backslashes": client.count("\\"),
            "wrapped": wrapped}


async def _route_required(server) -> dict:
    entries = server.router.store.count()
    if entries:
        raise RuntimeError(f"the router cache holds {entries} entries; the audit needs an empty one")
    text, client, wrapped = await _call_tool(
        server, "route_and_load", {"query": ROUTE_QUERY, "protocol_version": 2})
    payload = json.loads(text)
    return {"query": ROUTE_QUERY, "router_cache_entries": entries, "status": payload.get("status"),
            "candidates": len(payload.get("candidates") or []), **_sizes(text, client, wrapped)}


async def _first_activation(server, agent: str, query: str) -> dict:
    text, client, wrapped = await _call_tool(
        server, "get_agent_context", {"agent_name": agent, "query": query, "protocol_version": 2})
    payload = json.loads(text)
    persona = payload.get("persona") or {}
    return {"query": query, "status": payload.get("status"),
            "skills_loaded": persona.get("skills_loaded", []),
            "implants_loaded": persona.get("implants_loaded", []),
            "blocks": {name: len(payload.get(key) or "") for name, key in BLOCKS.items()},
            **_sizes(text, client, wrapped)}


async def _tools(server) -> dict:
    per_tool = {}
    for tool in await server.mcp.list_tools():
        data = tool.model_dump(exclude_none=True)
        description = len(data.get("description", ""))
        schema = len(json.dumps(data.get("inputSchema", {}), separators=(",", ":")))
        per_tool[tool.name] = {"description": description, "input_schema": schema,
                               "total": description + schema}
    return {"count": len(per_tool), "total_chars": sum(row["total"] for row in per_tool.values()),
            "per_tool": per_tool, "persona_tools": list(PERSONA_TOOLS),
            "persona_tools_chars": sum(per_tool[name]["total"] for name in PERSONA_TOOLS if name in per_tool)}


async def _measure(server) -> dict:
    from src.engine import config, protocol_text

    template = (REPO_ROOT / TEMPLATE).read_text(encoding="utf-8")
    instructions = server.mcp._mcp_server.create_initialization_options().instructions or ""
    report = {
        "client_profile": config.CLIENT_PROFILE,
        "template": {"path": TEMPLATE.as_posix(), "chars": len(template),
                     "utf8_bytes": len(template.encode("utf-8"))},
        "server_instructions": {"chars": len(instructions), "claude_code_window": CLAUDE_CODE_WINDOW,
                                "shown_chars": len(instructions[:CLAUDE_CODE_WINDOW]),
                                "beyond_window_chars": max(0, len(instructions) - CLAUDE_CODE_WINDOW)},
        "success_instruction": {"full_chars": len(protocol_text.bundle_instruction()),
                                "pointer_chars": len(protocol_text.bundle_pointer())},
    }
    # Before any activation: a SUCCESS would train the cache if its write were not stubbed.
    report["route_required"] = await _route_required(server)
    report["first_activation"] = {agent: await _first_activation(server, agent, query)
                                  for agent, query in ACTIVATIONS.items()}
    report["tools"] = await _tools(server)
    return report


def measure(*, embedder: bool = False) -> dict:
    """Every measurement for the client profile this process runs.

    The engine must already point at a throwaway data directory (``main`` in a child
    process, or the test suite's conftest). Patches are undone before returning.
    """
    from src import server
    from src.engine import embedder as embedding, enrichment, persona
    from src.engine.implants import ImplantRetriever
    from src.engine.skills import SkillRetriever

    guard = _EmbedderGuard()
    with contextlib.ExitStack() as stack:
        library = stack.enter_context(tempfile.TemporaryDirectory(prefix="agents-audit-library-"))
        stack.enter_context(patch.dict(os.environ, {"AGENTS_USER_FLOWS_DIR": library,
                                                     "AGENTS_USER_RULES_DIR": library}))
        stack.enter_context(patch.object(persona, "_repo_context", _no_repo_context))
        stack.enter_context(patch.object(server.client_instructions, "drift_warnings", lambda *a, **k: []))
        stack.enter_context(patch.object(server.router, "update_cache", _no_cache_write))
        if not embedder:
            stack.enter_context(patch.object(embedding, "_get_model", guard))
            stack.enter_context(patch.object(enrichment, "_skill_retriever", SimpleNamespace(
                retrieve=_declared_skills,
                format_skills_for_prompt=lambda items, **kw: SkillRetriever.format_skills_for_prompt(None, items, **kw))))
            stack.enter_context(patch.object(enrichment, "_implant_retriever", SimpleNamespace(
                retrieve=_declared_implants,
                format_implants_for_prompt=lambda items: ImplantRetriever.format_implants_for_prompt(None, items))))
        report = asyncio.run(_measure(server))
    if guard.calls:
        # A caller may swallow the guard's error; the numbers would then be wrong.
        raise RuntimeError(f"the audit tried to load the embedder {guard.calls} times without --embedder")
    report["skill_and_implant_selection"] = "retrieved" if embedder else "declared"
    return report


# --- The child process: isolate, import, measure ----------------------------------------------

def _child(profile: str, embedder: bool) -> int:
    # Everything config or the server reads at import is set before the first import.
    os.environ["AGENTS_CLIENT_PROFILE"] = profile
    os.environ.update(LANGFUSE_TRACING_ENABLED="false", AGENTS_DEBUG="0", AGENTS_AUTO_UPDATE="0",
                      HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1")
    os.environ.pop("AGENTS_TRANSPORT", None)
    service = tempfile.mkdtemp(prefix="agents-audit-service-")
    atexit.register(shutil.rmtree, service, ignore_errors=True)
    os.environ["AGENTS_SERVICE_DIR"] = service
    stdout = sys.stdout
    with contextlib.redirect_stdout(sys.stderr):  # stdout carries only the result
        from evals.scripts._isolated_data import isolate_data_dir

        isolate_data_dir()  # before any engine import: the router must not touch the live cache
        report = measure(embedder=embedder)
    json.dump(report, stdout)
    stdout.write("\n")
    return 0


# --- The parent process: one child per profile, then the report -------------------------------

def _revision() -> str | None:
    def git(*args: str) -> str:
        return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                              check=True).stdout.strip()
    try:
        head = git("rev-parse", "--short=8", "HEAD")
        return head + ("+modified" if git("status", "--porcelain", "--untracked-files=no") else "")
    except (OSError, subprocess.CalledProcessError):
        return None


def _run_child(profile: str, embedder: bool) -> dict:
    command = [sys.executable, str(Path(__file__).resolve()), "--child", "--profile", profile]
    if embedder:
        command.append("--embedder")
    completed = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, encoding="utf-8",
                               env={**os.environ, "AGENTS_CLIENT_PROFILE": profile})
    if completed.returncode != 0:
        sys.stderr.write(completed.stderr)
        raise SystemExit(f"measuring the {profile} profile failed (exit {completed.returncode})")
    report = json.loads(completed.stdout)
    if report.get("client_profile") != profile:
        raise SystemExit(f"the {profile} child ran the {report.get('client_profile')!r} profile")
    return report


def build_report(profiles: dict[str, dict], *, embedder: bool, revision: str | None) -> dict:
    return {"issues": ["#734", "#251"], "revision": revision, "embedder": embedder,
            "unit": "characters (Python len) unless a key says bytes", "profiles": profiles}


def _rows(measurement: dict) -> dict[str, int]:
    """One profile's measurements as ordered ``label -> number`` rows."""
    instructions = measurement["server_instructions"]
    route = measurement["route_required"]
    rows = {
        "managed template, characters": measurement["template"]["chars"],
        "managed template, UTF-8 bytes": measurement["template"]["utf8_bytes"],
        "server instructions, full": instructions["chars"],
        f"server instructions, first {instructions['claude_code_window']:,} Claude Code shows":
            instructions["shown_chars"],
        "server instructions, beyond that window": instructions["beyond_window_chars"],
        "SUCCESS instruction, full": measurement["success_instruction"]["full_chars"],
        "SUCCESS instruction, pointer": measurement["success_instruction"]["pointer_chars"],
        f"route_and_load {route['status']} ({route['candidates']} candidates), response": route["response_chars"],
        f"route_and_load {route['status']}, client text": route["client_chars"],
    }
    for agent, activation in measurement["first_activation"].items():
        prefix = f"{agent} first {activation['status']}"
        for block, size in activation["blocks"].items():
            rows[f"{prefix}, {block} block"] = size
        rows[f"{prefix}, response"] = activation["response_chars"]
        rows[f"{prefix}, response backslashes"] = activation["response_backslashes"]
        rows[f"{prefix}, client text"] = activation["client_chars"]
        rows[f"{prefix}, client text backslashes"] = activation["client_backslashes"]
    tools = measurement["tools"]
    rows["tools, count"] = tools["count"]
    rows["tools, description + compact inputSchema"] = tools["total_chars"]
    rows[" + ".join(tools["persona_tools"])] = tools["persona_tools_chars"]
    ranked = sorted(tools["per_tool"].items(), key=lambda item: (-item[1]["total"], item[0]))
    for name, sizes in ranked:
        rows[f"tool {name}"] = sizes["total"]
    return rows


def format_text(report: dict) -> str:
    profiles = report["profiles"]
    rows = {name: _rows(measurement) for name, measurement in profiles.items()}
    labels = list(dict.fromkeys(label for table in rows.values() for label in table))
    width = max(len(label) for label in labels)
    selection = ("retrieved with the embedder" if report["embedder"]
                 else "declared order, no embedder (--embedder retrieves them)")
    first = next(iter(profiles.values()))
    lines = [
        f"Agents-Core context size audit ({', '.join(report['issues'])}) at {report['revision'] or 'unknown revision'}",
        f"Unit: {report['unit']}. No number here is a budget.",
        f"Skills and implants: {selection}.",
        f"ROUTE_REQUIRED query: {first['route_required']['query']!r} on an empty router cache.",
        "",
        "".ljust(width) + "".join(f"{name:>14}" for name in profiles),
    ]
    for label in labels:
        cells = "".join(f"{table[label]:>14,}" if label in table else f"{'—':>14}" for table in rows.values())
        lines.append(label.ljust(width) + cells)
    lines.append("")
    for name, measurement in profiles.items():
        for agent, activation in measurement["first_activation"].items():
            lines.append(f"{name} {agent}: query {activation['query']!r}; skills "
                         f"{', '.join(activation['skills_loaded']) or '—'}; implants "
                         f"{', '.join(activation['implants_loaded']) or '—'}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="context_size_audit", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", action="store_true", help="print one machine-readable JSON object")
    parser.add_argument("--embedder", action="store_true",
                        help="retrieve skills and implants with the embedding model")
    parser.add_argument("--profile", choices=(*PROFILES, "both"), default="both",
                        help="AGENTS_CLIENT_PROFILE value to measure (default: both)")
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.child:
        if args.profile not in PROFILES:
            parser.error("--child measures one profile")
        return _child(args.profile, args.embedder)
    selected = PROFILES if args.profile == "both" else (args.profile,)
    report = build_report({profile: _run_child(profile, args.embedder) for profile in selected},
                          embedder=args.embedder, revision=_revision())
    print(json.dumps(report, ensure_ascii=False, indent=2) if args.json else format_text(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
