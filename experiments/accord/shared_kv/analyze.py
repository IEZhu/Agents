"""Recheck saved real-model evidence and recompute resident-KV pilot metrics."""
import argparse
import json
from pathlib import Path
import statistics

from experiments.accord.inference.client import fingerprint, sha
from .run import ROOT, ORDER, SETTINGS, strict_json, save
from .workload import checker


def analyze(directory):
    freeze = json.loads((directory / "freeze.json").read_text())
    for path, expected in freeze["source_hashes"].items():
        assert sha((ROOT / path).read_bytes()) == expected, path
    package_data = json.loads((directory / "packages.json").read_text())
    packages = package_data["packages"]
    prefix = freeze["prefix_ids"]
    episodes, calls = [], []
    rerun_candidates = 0
    for index, (backend, shared) in enumerate(ORDER, 1):
        case = directory / f"episode-{index:02d}"
        episode = json.loads((case / "episode.json").read_text())
        assert (episode["backend"], episode["shared"]) == (backend, shared)
        assert all(t["remaining"] == 0 for t in episode["tasks"].values())
        previous_sequence = []
        for stage_index, stage in enumerate(episode["stages"]):
            role, call_id = stage["role"], stage["call_id"]
            row = json.loads((case / (role + "-response.json")).read_text())
            source = json.loads((case / (role + "-input.json")).read_text())
            package = packages[call_id]
            assert package["worker_context"] in source["prompt"]
            assert source["ids"][:len(prefix)] == prefix
            assert source["prompt"] == row["final"]["prompt"]
            assert not row["final"]["truncated"]
            assert row["final"]["id_slot"] == 0
            for key, expected in SETTINGS.items():
                if key != "id_slot":
                    assert row["final"]["generation_settings"][key] == expected, key
            timing = row["final"]["timings"]
            assert timing["cache_n"] + timing["prompt_n"] == len(source["ids"])
            assert row["final"]["tokens_evaluated"] == len(source["ids"])
            assert timing["predicted_n"] == len(row["tokens"])
            expected_reuse = shared and stage_index > 0
            assert row["shared_kv"]["requested_reuse"] == expected_reuse
            if expected_reuse:
                lcp = 0
                while lcp < min(len(previous_sequence), len(source["ids"])) and previous_sequence[lcp] == source["ids"][lcp]:
                    lcp += 1
                assert len(prefix) <= timing["cache_n"] <= lcp
            previous_sequence = source["ids"] + row["tokens"]
            if expected_reuse:
                assert timing["cache_n"] >= len(prefix)
            else:
                assert timing["cache_n"] == 0
            gate = json.loads((directory / "validations" / (call_id + ".json")).read_text())
            assert gate["raw_sha256"] == sha(row["content"])
            assert gate["contract_revision"] == package["contract_revision"]
            result = strict_json(row["content"])
            if gate["validation"]["valid"]:
                for key in ("protocol", "task_id", "attempt_id", "contract_revision", "bundle_revision"):
                    assert result[key] == package["contract"][key]
            if stage["accepted"]:
                assert gate["validation"]["valid"] and result["status"] == "completed"
            calls.append({"episode":index, "backend":backend, "shared":shared, "role":role,
                "valid":gate["validation"]["valid"], "accepted":stage["accepted"],
                "input_tokens":len(source["ids"]), "reused":timing["cache_n"], "evaluated":timing["prompt_n"],
                "generated":timing["predicted_n"], "ttft_ms":row["client_ttft_ms"], "total_ms":row["client_total_ms"],
                "prompt_ms":timing["prompt_ms"], "decode_ms":timing["predicted_ms"],
                "admission_ms":row["shared_kv"]["validation_ms"], "request_bytes":row["request_body_bytes"],
                "output_sha256":sha(row["content"]), "decision":stage.get("decision")})
        if (case / "candidate.py").exists():
            impl = strict_json(json.loads((case / "implement-response.json").read_text())["content"])
            patch = checker.patch_for_replacement(impl["result"]["items"][0])
            assert checker.materialize(patch) == (case / "candidate.py").read_bytes()
            report = checker.run_behavior(patch)
            assert report == json.loads((case / "behavior.json").read_text())
            rerun_candidates += 1
        episodes.append({k:v for k,v in episode.items() if k not in ("stages", "tasks")})
    summary = json.loads((directory / "summary.json").read_text())
    assert set(summary["routes"]) == {"/props", "/apply-template", "/tokenize", "/completion"}
    assert summary["routes"].count("/completion") == 24
    cells = []
    for backend, shared in ORDER[:4]:
        group = [e for e in episodes if e["backend"] == backend and e["shared"] == shared]
        rows = [c for c in calls if c["backend"] == backend and c["shared"] == shared]
        cells.append({"backend":backend, "shared":shared, "episodes":len(group),
            "completed":sum(e["completed"] for e in group),
            **{k:statistics.median(e[k] for e in group) for k in ("model_ms", "active_ms", "elapsed_ms")},
            **{k:sum(c[k] for c in rows) for k in ("input_tokens", "reused", "evaluated", "generated", "request_bytes")},
            "median_ttft_ms":statistics.median(c["ttft_ms"] for c in rows),
            "median_admission_ms":statistics.median(c["admission_ms"] for c in rows)})
    return {"verified":True, "distinct_tasks":1, "model_calls":len(calls), "prefix_tokens":len(prefix),
        "completed":sum(e["completed"] for e in episodes), "valid_envelopes":sum(c["valid"] for c in calls),
        "rerun_candidates":rerun_candidates, "cells":cells, "episodes":episodes, "calls":calls,
        "preparation_measured_ms":sum(package_data["preparation_ms"].values())}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = analyze(args.directory)
    save(args.directory / "analysis.json", result)
    print(json.dumps({k:v for k,v in result.items() if k not in ("calls", "episodes")}, indent=2))


if __name__ == "__main__":
    main()
