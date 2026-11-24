"""Reindex builds the agent catalog cards the cold router ranks with (#734)."""

import sys
import types

from src import reindex


def test_reindex_builds_stale_agent_cards_after_the_stores_and_skips_current_ones(monkeypatch):
    calls = []

    class _Store:
        def count(self):
            return 0

    class _Retriever:
        def __init__(self):
            calls.append(type(self).__name__)
            self.store = _Store()

    class SkillRetriever(_Retriever):
        pass

    class ImplantRetriever(_Retriever):
        pass

    class SemanticRouter:
        current = False

        def persisted_cards_current(self):
            return self.current

        def build_agent_cards(self, *, persist=False):
            calls.append(("build_agent_cards", persist))

        def agent_card_count(self):
            return 46

    monkeypatch.setitem(sys.modules, "src.engine.skills",
                        types.SimpleNamespace(SkillRetriever=SkillRetriever))
    monkeypatch.setitem(sys.modules, "src.engine.implants",
                        types.SimpleNamespace(ImplantRetriever=ImplantRetriever))
    monkeypatch.setitem(sys.modules, "src.engine.router",
                        types.SimpleNamespace(SemanticRouter=SemanticRouter))

    monkeypatch.setenv("AGENTS_ROUTER_DATA_DIR", "/a/server/slot/router")
    assert reindex.main() == 0
    assert calls == ["SkillRetriever", "ImplantRetriever", ("build_agent_cards", True)]
    # A server's router directory must not leak into the router reindex builds with (#734 review).
    import os
    assert "AGENTS_ROUTER_DATA_DIR" not in os.environ

    calls.clear()
    SemanticRouter.current = True
    assert reindex.main() == 0
    assert calls == ["SkillRetriever", "ImplantRetriever"]
