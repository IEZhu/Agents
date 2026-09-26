"""evals/telemetry turns a Langfuse export into text-free tables and statistics.

The scripts are loaded from their paths (evals/telemetry is not a package) and run on
a tiny synthetic export, so no network and no real telemetry are involved.
"""
from __future__ import annotations

import csv
import importlib.util
import json
from pathlib import Path

TELEMETRY = Path(__file__).resolve().parents[1] / "evals" / "telemetry"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"telemetry_{name}", TELEMETRY / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


extract = _module("extract")
stats = _module("stats")
export = _module("export_langfuse")


def _trace(name, ts, inp=None, out=None, **extra):
    return {"name": name, "timestamp": ts, "latency": 0.1, "input": inp, "output": out, **extra}


def _export(tmp_path: Path) -> Path:
    traces = [
        _trace("route_and_load", "2026-09-01T10:00:00Z", {"kwargs": {"query": "Как настроить nginx?", "protocol_version": 1}},
               json.dumps({"status": "ROUTE_REQUIRED", "tier": "standard", "candidates": [{"name": "sysadmin"}]})),
        _trace("get_agent_context", "2026-09-01T10:00:05Z", {"kwargs": {"agent_name": "sysadmin", "query": "Как настроить nginx?"}},
               {"status": "SUCCESS", "agent": "sysadmin", "system_prompt": "x" * 1200,
                "skills_loaded": ["skill-content-structure"], "implants_loaded": [], "rules_loaded": ["no-fabrication"]}),
        _trace("retrieve_skills", "2026-09-01T10:00:04Z", {"args": ["q"], "kwargs": {"n_results": 2, "mandatory": ["a"]}},
               [{"filename": "skill-content-structure.mdc", "tier": "mandatory", "distance": 0}]),
        _trace("retrieve_implants", "2026-09-01T10:00:04Z", {"args": ["q"], "kwargs": {"n_results": 2, "role": "sysadmin"}},
               [{"filename": "implant-regression-first.mdc", "distance": 0}]),
    ]
    observations = [{"type": "GENERATION", "startTime": "2026-09-01T10:01:00Z", "metadata": {"agent": "sysadmin"},
                     "input": "Как настроить nginx?",
                     "output": "Вот конфигурация сервера.\n\n**Agent**: sysadmin · **Skills**: x · **Implants**: — · **Rules**: y"},
                    {"type": "SPAN", "startTime": "2026-09-01T10:00:00Z"}]
    tmp_path.mkdir(exist_ok=True)
    with open(tmp_path / "traces.jsonl", "w") as f:
        f.writelines(json.dumps(t, ensure_ascii=False) + "\n" for t in traces)
    with open(tmp_path / "observations.jsonl", "w") as f:
        f.writelines(json.dumps(o, ensure_ascii=False) + "\n" for o in observations)
    return tmp_path


def test_language_heuristic_ignores_code_blocks():
    assert extract.lang("Как настроить nginx?") == "ru"
    assert extract.lang("How do I configure nginx?") == "en"
    assert extract.lang("Готово.\n```\nserver { listen 80; server_name example.com; }\n```") == "ru"
    assert extract.lang("") == "" and extract.lang("12345") == "none"
    # The English footer, inline code and URLs do not make a Russian answer English.
    assert extract.lang("Готово, см. `kubectl get pods -A` и https://example.com/docs/setup\n\n"
                        "**Agent**: sysadmin · **Skills**: skill-a · **Implants**: — · **Rules**: rule-b") == "ru"


def test_extract_writes_text_free_tables(tmp_path, capsys):
    data = _export(tmp_path / "data")
    extract.main(data)
    route = list(csv.DictReader(open(data / "route.csv")))
    assert route[0]["status"] == "ROUTE_REQUIRED" and route[0]["q_lang"] == "ru" and route[0]["n_candidates"] == "1"
    inter = list(csv.DictReader(open(data / "interactions.csv")))
    assert inter[0]["footer"] == "True" and inter[0]["footer_agent"] == "sysadmin" and inter[0]["r_lang"] == "ru"
    # No table carries query or answer text.
    for table in data.glob("*.csv"):
        assert "nginx" not in table.read_text() and "конфигурация" not in table.read_text()


def test_stats_summarise_the_tables(tmp_path, capsys):
    data = _export(tmp_path / "data")
    extract.main(data)
    result = stats.main(data)
    assert result["counts"]["route_and_load"] == 1
    assert result["route_required_followed_by_gac_180s_upper_bound"] == [1, 1, 100.0]
    assert result["footer_present_pct"] == 100.0 and result["language_match_pct"] == 100.0
    assert json.loads((data / "stats.json").read_text())["top_implants"] == [["implant-regression-first", 1]]


def test_one_log_is_not_counted_for_two_routes():
    from datetime import datetime, timedelta
    t0 = datetime(2026, 9, 1, 10, 0)
    routes = [t0, t0 + timedelta(seconds=10)]
    assert stats.match_one_to_one(routes, [t0 + timedelta(seconds=20)], timedelta(minutes=30)) == 1
    assert stats.match_one_to_one(routes, [t0 + timedelta(seconds=5), t0 + timedelta(seconds=20)], timedelta(minutes=30)) == 2
    assert stats.match_one_to_one(routes, [t0 + timedelta(hours=1)], timedelta(minutes=30)) == 0


def test_an_empty_export_gives_empty_stats_and_clears_stale_tables(tmp_path, capsys):
    data = tmp_path / "data"
    data.mkdir()
    (data / "traces.jsonl").write_text("")
    (data / "observations.jsonl").write_text("")
    (data / "route.csv").write_text("ts,status\n2026-09-01T10:00:00Z,SUCCESS\n")  # left by an earlier export
    extract.main(data)
    assert not (data / "route.csv").exists()
    result = stats.main(data)
    assert result["period"] is None and result["counts"]["route_and_load"] == 0
    assert result["response_chars"]["max"] is None


def test_router_reasoning_text_is_not_exported(tmp_path, capsys):
    data = _export(tmp_path / "data")
    line = json.dumps(_trace("route_and_load", "2026-09-01T11:00:00Z", {"kwargs": {"query": "q"}},
                             {"status": "SUCCESS", "agent": "sysadmin", "reasoning": "private rationale text"}))
    with open(data / "traces.jsonl", "a") as f:
        f.write(line + "\n")
    extract.main(data)
    table = (data / "route.csv").read_text()
    assert "private rationale" not in table and "reasoning_len" in table


def test_v2_observations_become_legacy_shaped_traces():
    obs = [
        {"id": "c", "traceId": "t1", "startTime": "2026-09-01T10:00:01Z", "parentObservationId": "p", "name": "child"},
        {"id": "p", "traceId": "t1", "startTime": "2026-09-01T10:00:02Z", "parentObservationId": None, "name": "route_and_load",
         "traceName": "route_and_load", "input": export.parsed('{"kwargs": {"query": "q"}}'), "output": export.parsed("plain text")},
        # log_interaction: the response generation can start before its agent_interaction span.
        {"id": "gen", "traceId": "t2", "startTime": "2026-09-01T10:00:05Z", "parentObservationId": "span", "name": "response"},
        {"id": "span", "traceId": "t2", "startTime": "2026-09-01T10:00:06Z", "parentObservationId": "x", "name": "agent_interaction"},
    ]
    traces = {t["id"]: t for t in map(export.as_trace, export.trace_roots(obs))}
    assert traces["t1"]["name"] == "route_and_load" and traces["t1"]["input"] == {"kwargs": {"query": "q"}}
    assert traces["t1"]["output"] == "plain text"
    # No parentless observation: the one with children stands in, even if it started later.
    assert traces["t2"]["name"] == "agent_interaction"


def test_matching_is_the_greedy_one_to_one_pairing():
    import random
    from datetime import datetime, timedelta
    rng = random.Random(7)
    t0, window = datetime(2026, 9, 1), timedelta(seconds=30)

    def brute(starts, ends):
        used, n = set(), 0
        for s in sorted(starts):
            later = [i for i, e in sorted(enumerate(ends), key=lambda x: x[1]) if e >= s and i not in used]
            if later and ends[later[0]] - s <= window:
                used.add(later[0]); n += 1
        return n

    for _ in range(200):
        starts = [t0 + timedelta(seconds=rng.randint(0, 120)) for _ in range(rng.randint(0, 8))]
        ends = [t0 + timedelta(seconds=rng.randint(0, 150)) for _ in range(rng.randint(0, 8))]
        assert stats.match_one_to_one(starts, ends, window) == brute(starts, ends)


class _Response:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, *args):
        return self.payload


def test_export_stops_when_pagination_makes_no_progress(tmp_path, monkeypatch):
    import pytest
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk")
    monkeypatch.setenv("LANGFUSE_HOST", "https://example.invalid")
    obs = {"id": "o1", "traceId": "t1", "startTime": "2026-09-01T10:00:00Z", "parentObservationId": None, "name": "x"}
    pages = iter([{"data": [obs], "meta": {"cursor": "c1"}}, {"data": [obs], "meta": {"cursor": "c1"}}])
    monkeypatch.setattr(export.urllib.request, "urlopen", lambda request, timeout: _Response(next(pages)))
    monkeypatch.setattr(export.time, "sleep", lambda s: None)
    with pytest.raises(SystemExit, match="no progress"):
        export.main(tmp_path / "out", "2026-09-01")
    assert not (tmp_path / "out" / "traces.jsonl").exists()
    # A last page without a cursor ends the export normally.
    pages = iter([{"data": [obs], "meta": {"cursor": "c1"}}, {"data": [obs], "meta": {}}])
    export.main(tmp_path / "out", "2026-09-01")
    assert len((tmp_path / "out" / "observations.jsonl").read_text().splitlines()) == 2

