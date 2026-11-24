"""Reindex builds the agent catalog cards the cold router ranks with (#734)."""

import sys
import types

from src import reindex


def test_reindex_builds_the_agent_cards_after_the_stores(monkeypatch):
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
        def build_agent_cards(self):
            calls.append("build_agent_cards")

        def agent_card_count(self):
            return 46

    monkeypatch.setitem(sys.modules, "src.engine.skills",
                        types.SimpleNamespace(SkillRetriever=SkillRetriever))
    monkeypatch.setitem(sys.modules, "src.engine.implants",
                        types.SimpleNamespace(ImplantRetriever=ImplantRetriever))
    monkeypatch.setitem(sys.modules, "src.engine.router",
                        types.SimpleNamespace(SemanticRouter=SemanticRouter))

    assert reindex.main() == 0
    assert calls == ["SkillRetriever", "ImplantRetriever", "build_agent_cards"]
