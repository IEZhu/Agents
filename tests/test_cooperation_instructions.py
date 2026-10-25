"""Offline instruction propagation and authored behavior fixture checks; no model trials."""
from collections import defaultdict
import json
from pathlib import Path

import pytest

from src.engine import protocol_text

ROOT = Path(__file__).resolve().parents[1]


def _surfaces():
    return {
        "routing_template": (ROOT / "scripts/templates/routing-protocol-core.md").read_text(),
        "managed_claude": (ROOT / "CLAUDE.md").read_text(),
        "memory_reminder": (ROOT / "scripts/templates/memory-routing.md").read_text(),
        # Rendered from src/engine/protocol/ (#431); tests/test_protocol_text.py checks that
        # server.py and persona.py use these renders.
        "mcp_initialization": protocol_text.server_instructions(),
        "bundle_application": protocol_text.bundle_instruction(),
    }


@pytest.mark.parametrize("name", list(_surfaces()))
def test_cooperation_trigger_keeps_authority_and_fallback_on_all_surfaces(name):
    text = " ".join(_surfaces()[name].split()).lower()
    for phrase in ("when agent work intersects", "authorized", "priority source", "one integrator",
                   "resource claims", "prepared", "result validation", "independent work",
                   "silence", "timeout", "unrelated chats", "assignments"):
        assert phrase in text, (name, phrase)
    for phrase in ("cooperation_send", "cooperation_receive", "cooperation_ack", "mcp",
                   "delivery acknowledgement", "untrusted task data", "native host messaging is not required"):
        assert phrase in text, (name, phrase)
    for phrase in ("cooperation_wait", "notification cursor", "cooperation_request_status",
                   "cooperation_request_update", "request revision", "idle model"):
        assert phrase in text, (name, phrase)


def test_managed_section_matches_canonical_template_and_previous_memory_is_retained():
    template = (ROOT / "scripts/templates/routing-protocol-core.md").read_text().strip()
    claude = (ROOT / "CLAUDE.md").read_text()
    begin = "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
    end = "# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"
    assert claude.split(begin, 1)[1].split(end, 1)[0].strip() == template
    previous = (ROOT / "scripts/templates/legacy/memory-routing-v10.md").read_text()
    assert "When agent work intersects" not in previous
    assert "Every AI delegation" in previous
    intermediate = (ROOT / "scripts/templates/legacy/memory-routing-v12.md").read_text()
    assert "When agent work intersects" in intermediate
    assert "cooperation_send" not in intermediate
    previous_mailbox = (ROOT / "scripts/templates/legacy/memory-routing-v15.md").read_text()
    assert "cooperation_send" in previous_mailbox
    assert "cooperation_wait" not in previous_mailbox


def test_cooperation_fixtures_are_bilingual_bounded_and_cover_host_role_matrix():
    rows = [json.loads(line) for line in (ROOT / "evals/datasets/cooperation.jsonl").read_text(encoding="utf-8").splitlines()]
    required = {"id", "scenario_id", "language", "role", "host", "context", "query",
                "expected_pattern", "expected_actions", "forbidden_actions", "requires_worker_envelope"}
    languages = defaultdict(set)
    assert len({row["id"] for row in rows}) == len(rows)
    for row in rows:
        assert set(row) == required
        assert row["language"] in {"en", "ru"}
        assert row["role"] in {"root", "worker"}
        assert set(row["host"]) == {"peer_channel", "coordinator_available"}
        assert row["host"]["peer_channel"] in {"native_peer", "no_peer"}
        assert type(row["host"]["coordinator_available"]) is bool
        assert row["requires_worker_envelope"] is (row["role"] == "worker")
        assert all(isinstance(row[key], str) and 0 < len(row[key]) <= 4000
                   for key in ("context", "query", "expected_pattern"))
        assert row["expected_actions"] and row["forbidden_actions"]
        assert not set(row["expected_actions"]) & set(row["forbidden_actions"])
        languages[row["scenario_id"]].add(row["language"])
    assert len(languages) >= 12
    assert all(value == {"en", "ru"} for value in languages.values())
    assert {(row["role"], row["host"]["peer_channel"]) for row in rows} == {
        ("root", "native_peer"), ("root", "no_peer"),
        ("worker", "native_peer"), ("worker", "no_peer"),
    }
