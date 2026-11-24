"""The offline context-size audit (#734, #251): every cheap part, without the embedder."""
from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.scripts import context_size_audit as audit

REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def empty_router_cache(monkeypatch, tmp_path):
    """The suite shares one router directory, which earlier tests may have trained."""
    from src import server
    from src.engine.vector_store import NumpyVectorStore
    monkeypatch.setattr(server.router, "store", NumpyVectorStore(name="router_cache", data_dir=str(tmp_path)))


@pytest.fixture
def embedding_calls(monkeypatch):
    """Records every embedding request; the audit guards the model loader, not these."""
    from src.engine import embedder
    calls = []

    def refuse(kind):
        def record(*args, **kwargs):
            calls.append(kind)
            raise AssertionError(f"the cheap audit called {kind}")
        return record

    monkeypatch.setattr(embedder, "_embed_query", refuse("_embed_query"))
    monkeypatch.setattr(embedder, "_embed_texts", refuse("_embed_texts"))
    return calls


def test_cheap_measurement_covers_every_listed_part(empty_router_cache, embedding_calls):
    from src import server
    from src.engine import config, protocol_text

    report = audit.measure(embedder=False)
    assert embedding_calls == []
    assert report["client_profile"] == config.CLIENT_PROFILE
    assert report["skill_and_implant_selection"] == "declared"

    template = (REPO_ROOT / audit.TEMPLATE).read_text(encoding="utf-8")
    assert report["template"] == {"path": audit.TEMPLATE.as_posix(), "chars": len(template),
                                  "utf8_bytes": len(template.encode("utf-8"))}

    instructions = report["server_instructions"]
    assert instructions["chars"] == len(server.mcp.instructions)
    assert instructions["shown_chars"] == min(instructions["chars"], audit.CLAUDE_CODE_WINDOW)
    assert instructions["beyond_window_chars"] == instructions["chars"] - instructions["shown_chars"]
    assert report["success_instruction"] == {"full_chars": len(protocol_text.bundle_instruction()),
                                             "pointer_chars": len(protocol_text.bundle_pointer())}

    route = report["route_required"]
    assert route["status"] == "ROUTE_REQUIRED"
    assert route["router_cache_entries"] == 0
    assert route["candidates"] > 0
    assert route["response_chars"] > 0 and route["client_chars"] > 0
    if route["wrapped"]:
        assert route["client_chars"] >= route["response_chars"]

    assert set(report["first_activation"]) == set(audit.ACTIVATIONS)
    for agent, activation in report["first_activation"].items():
        assert activation["status"] == "SUCCESS", agent
        assert set(activation["blocks"]) == set(audit.BLOCKS)
        assert activation["blocks"]["persona"] > 0
        # The response holds every block, JSON-escaped, plus the descriptor and footer.
        assert activation["response_chars"] > sum(activation["blocks"].values())
        assert activation["response_backslashes"] > 0
        if activation["wrapped"]:
            # A string result wrapped as {"result": text} reaches the client escaped twice.
            assert activation["client_chars"] >= activation["response_chars"]
            assert activation["client_backslashes"] >= activation["response_backslashes"]
        else:
            # The object itself, compact: nothing is escaped a second time (#734).
            assert activation["client_backslashes"] <= activation["response_backslashes"]


def test_tool_sizes_add_up(empty_router_cache, embedding_calls):
    from src import server

    tools = audit.measure(embedder=False)["tools"]
    names = {tool.name for tool in asyncio.run(server.mcp.list_tools())}
    assert set(tools["per_tool"]) == names
    assert tools["count"] == len(names)
    for name, sizes in tools["per_tool"].items():
        assert sizes["total"] == sizes["description"] + sizes["input_schema"] > 0, name
    assert tools["total_chars"] == sum(sizes["total"] for sizes in tools["per_tool"].values())
    assert set(audit.PERSONA_TOOLS) <= names
    assert tools["persona_tools_chars"] == sum(tools["per_tool"][name]["total"] for name in audit.PERSONA_TOOLS)


def _patched_state():
    from src import server
    from src.engine import embedder, enrichment, persona
    return (embedder._get_model, enrichment._skill_retriever, enrichment._implant_retriever,
            persona._repo_context, server.client_instructions.drift_warnings,
            "update_cache" in vars(server.router),
            os.environ.get("AGENTS_USER_FLOWS_DIR"), os.environ.get("AGENTS_USER_RULES_DIR"))


def test_measurement_restores_what_it_patched(empty_router_cache):
    before = _patched_state()
    audit.measure(embedder=False)
    assert _patched_state() == before


def test_a_swallowed_embedder_load_fails_the_cheap_audit(monkeypatch, empty_router_cache):
    """A caller that hides the guard's error must not let wrong numbers through."""
    from src.engine import embedder

    declared = audit._declared_skills

    def loads_the_model(*args, **kwargs):
        try:
            embedder._get_model()
        except RuntimeError:
            pass
        return declared(*args, **kwargs)

    monkeypatch.setattr(audit, "_declared_skills", loads_the_model)
    before = _patched_state()
    with pytest.raises(RuntimeError, match="tried to load the embedder"):
        audit.measure(embedder=False)
    assert _patched_state() == before


def test_text_report_names_every_measurement_and_marks_missing_rows(empty_router_cache):
    interactive = audit.measure(embedder=False)
    headless = copy.deepcopy(interactive)
    headless["client_profile"] = "headless"
    del headless["tools"]["per_tool"]["load_implants"]
    report = audit.build_report({"interactive": interactive, "headless": headless},
                                embedder=False, revision="abc12345")
    text = audit.format_text(report)

    assert "at abc12345" in text
    for expected in ("managed template, characters", "managed template, UTF-8 bytes",
                     "server instructions, full", "first 4,096 Claude Code shows",
                     "SUCCESS instruction, full", "SUCCESS instruction, pointer",
                     "route_and_load ROUTE_REQUIRED", "client text backslashes",
                     "tools, description + compact inputSchema",
                     "route_and_load + get_agent_context + log_interaction", "tool log_interaction"):
        assert expected in text, expected
    for agent in audit.ACTIVATIONS:
        for block in audit.BLOCKS:
            assert f"{agent} first SUCCESS, {block} block" in text
    missing = next(line for line in text.splitlines() if line.startswith("tool load_implants "))
    assert missing.rstrip().endswith("—")
    assert json.loads(json.dumps(report)) == report


def test_module_import_leaves_the_engine_unloaded():
    """The child isolates the data directory before the first engine import."""
    probe = ("import json, sys; import evals.scripts.context_size_audit; "
             "print(json.dumps(sorted(m for m in sys.modules if m == 'src' or m.startswith('src.'))))")
    completed = subprocess.run([sys.executable, "-c", probe], cwd=REPO_ROOT, capture_output=True,
                               text=True, check=True)
    assert json.loads(completed.stdout) == []


def test_script_runs_offline_and_never_uses_the_inherited_router_directory(tmp_path):
    live_router = tmp_path / "live-router"
    env = {**os.environ, "AGENTS_ROUTER_DATA_DIR": str(live_router), "HF_HUB_OFFLINE": "1",
           "LANGFUSE_TRACING_ENABLED": "false"}
    completed = subprocess.run(
        [sys.executable, str(REPO_ROOT / "evals" / "scripts" / "context_size_audit.py"),
         "--json", "--profile", "headless"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, encoding="utf-8", timeout=300)
    assert completed.returncode == 0, completed.stderr[-4000:]
    report = json.loads(completed.stdout)
    assert report["embedder"] is False
    assert list(report["profiles"]) == ["headless"]
    headless = report["profiles"]["headless"]
    assert headless["client_profile"] == "headless"
    assert headless["skill_and_implant_selection"] == "declared"
    assert headless["route_required"]["router_cache_entries"] == 0
    assert headless["route_required"]["status"] == "ROUTE_REQUIRED"
    assert not live_router.exists()
