"""Offline consistency replay; does not create new MCP validation receipts."""
import argparse
import json
import math
from pathlib import Path
import statistics

from experiments.accord.inference.client import sha
from experiments.accord.shared_kv.run import strict_json, save
from experiments.accord.shared_kv.workload import checker
from .run import ROOT, summarize


def wilson(successes, total):
    z = 1.959963984540054
    p = successes / total
    denominator = 1 + z*z/total
    center = (p + z*z/(2*total)) / denominator
    half = z*math.sqrt(p*(1-p)/total + z*z/(4*total*total)) / denominator
    return [max(0, center-half), min(1, center+half)]


def analyze(directory):
    directory = Path(directory)
    freeze = json.loads((directory / "freeze.json").read_text())
    summary = json.loads((directory / "summary.json").read_text())
    for path, expected in freeze["source_hashes"].items():
        assert sha((ROOT / path).read_bytes()) == expected, f"source changed: {path}"
    rows = summary["episodes"]
    assert len(rows) == len(freeze["order"]) == 8, "incomplete pilot"
    totals = {"model_calls":0, "valid_envelopes":0, "behavior_replays":0,
              "input_tokens":0, "output_tokens":0, "reused_tokens":0,
              "prepare_ms":0, "validation_ms":0, "model_ms":0}
    observed = []
    for index, (frontend, seed) in enumerate(freeze["order"], 1):
        row = rows[index-1]
        assert (row["index"], row["backend"], row["seed"]) == (index, frontend, seed)
        case = directory / f"episode-{index:02d}"
        episode = json.loads((case / "episode.json").read_text())
        for key, value in row.items():
            if key != "full_elapsed_ms":
                assert episode[key] == value, f"summary mismatch: {key}"
        stages = episode["stages"]
        impl = [stage for stage in stages if stage["role"] == "implement"]
        assert 1 <= len(impl) <= 3
        assert [stage["attempt"] for stage in impl] == list(range(1, len(impl)+1))
        assert not any(stage["accepted"] for stage in impl[:-1]), "continued after success"
        expected_roles = ["implement"] * len(impl)
        if impl[-1]["accepted"]:
            expected_roles.append("review")
            if stages[len(impl)]["accepted"]:
                expected_roles.append("adopt")
        assert [stage["role"] for stage in stages] == expected_roles
        assert episode["completed"] == (stages[-1]["role"] == "adopt" and stages[-1]["accepted"])
        assert sum(task["remaining"] for task in episode["tasks"].values()) + episode["request_units_consumed"] == 5
        assert row["full_elapsed_ms"] >= episode["active_elapsed_ms"]
        assert episode["active_elapsed_ms"] >= sum(stage["elapsed_ms"] for stage in stages)
        for position, stage in enumerate(stages):
            folder = case / f"{stage['role']}-{stage['attempt']}"
            assert json.loads((folder / "observation.json").read_text()) == stage
            package = json.loads((folder / "package.json").read_text())
            inputs = json.loads((folder / "input.json").read_text())
            response = json.loads((folder / "response.json").read_text())
            validation = json.loads((folder / "validation.json").read_text())
            assert package["status"] == "prepared"
            assert package["worker_context"] in inputs["prompt"]
            assert response["final"]["prompt"] == inputs["prompt"]
            assert response["final"]["truncated"] is False
            assert sha(inputs["prompt"]) == stage["prompt_sha256"]
            assert stage["validation"] == validation
            tokens = response["final"]["timings"]
            assert tokens["cache_n"] + tokens["prompt_n"] == len(inputs["ids"])
            assert response["shared_kv"]["actual_reused_tokens"] == tokens["cache_n"]
            assert response["shared_kv"]["requested_reuse"] == (position > 0)
            if position == 0:
                assert tokens["cache_n"] == 0
            assert len(response["tokens"]) <= freeze["settings"]["n_predict"]
            for key in ("prepare_ms", "validation_ms", "model_ms"):
                totals[key] += stage.get(key, 0)
            totals["input_tokens"] += len(inputs["ids"])
            totals["output_tokens"] += len(response["tokens"])
            totals["reused_tokens"] += tokens["cache_n"]
            totals["model_calls"] += 1
            if validation.get("valid") is True:
                result = strict_json(response["content"])
                assert json.loads((folder / "result.json").read_text()) == result == stage["result"]
                for key in ("protocol", "task_id", "attempt_id", "bundle_revision", "contract_revision"):
                    assert result[key] == package["contract"][key]
                totals["valid_envelopes"] += 1
            if "behavior" in stage:
                patch = checker.patch_for_replacement(stage["result"]["result"]["items"][0])
                candidate = checker.materialize(patch)
                assert candidate == (folder / "candidate.py").read_bytes()
                replay = checker.run_behavior(patch)
                original = stage["behavior"]
                # The probe's elapsed time is expected to change on replay.
                for key in ("passed", "changed", "checks", "count"):
                    if key in original:
                        assert replay[key] == original[key], f"behavior replay: {key}"
                assert stage["accepted"] == (replay["passed"] and replay["changed"])
                totals["behavior_replays"] += 1
            if stage["accepted"]:
                assert validation["valid"] is True and stage["result"]["status"] == "completed"
                if stage["role"] != "implement":
                    assert stage["decision"] == "approve" and all(s["accepted"] for s in stages[len(impl)-1:position])
        observed.append(row)
    assert summary["groups"] == summarize(observed)
    groups = summary["groups"]
    for name, group in groups.items():
        group["wilson_95_interval"] = wilson(group["successes"], group["episodes"])
        group["full_ms_range"] = [min(row["full_elapsed_ms"] for row in rows if row["backend"] == name),
                                  max(row["full_elapsed_ms"] for row in rows if row["backend"] == name)]
    paired = []
    for seed in sorted({row["seed"] for row in rows}):
        pair = {row["backend"]:row for row in rows if row["seed"] == seed}
        paired.append({"seed":seed, "accord_minus_python_ms":pair["accord"]["full_elapsed_ms"]-pair["python"]["full_elapsed_ms"],
                       "accord_success":pair["accord"]["completed"], "python_success":pair["python"]["completed"]})
    return {"verified":True, "groups":groups, "paired":paired, "totals":totals,
            "median_paired_delta_ms":statistics.median(pair["accord_minus_python_ms"] for pair in paired),
            "batch_elapsed_ms":summary["batch_elapsed_ms"], "setup_ms":summary["setup_ms"],
            "reuse_fraction":totals["reused_tokens"]/totals["input_tokens"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    result = analyze(args.directory)
    save(args.directory / "analysis.json", result)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
