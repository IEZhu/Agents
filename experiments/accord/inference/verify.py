"""Verify committed raw experiment evidence offline, without querying a model."""

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path

from .analyze import summary
from .client import fingerprint, verify_artifact
from .run import hash_file
from .workload import WORKLOAD_PATH, load_workload, score


def verify(directory):
    directory = Path(directory)
    setup = json.loads((directory / "setup.json").read_text())
    rows = [json.loads(line) for line in (directory / "trials.jsonl").read_text().splitlines()]
    saved_summary = json.loads((directory / "summary.json").read_text())
    workload = load_workload()
    cases = {c["name"]: c for c in workload["cases"]}
    prepared = {p["case"]: p for p in setup["prepared"]}
    assert len(rows) == 144 and len(prepared) == len(cases) == 12
    assert setup["workload_sha256"] == hash_file(WORKLOAD_PATH)
    assert setup["protocol_sha256"] == hash_file(Path(__file__).with_name("protocol.md"))
    for name, expected in setup["harness_hashes"].items():
        assert hash_file(Path(__file__).with_name(name)) == expected, name
    root = Path(__file__).resolve().parents[3]
    for name, expected in workload["source_hashes"].items():
        assert hash_file(root / name) == expected, name
    assert len({(r["case"], r["repeat"], r["cell"]) for r in rows}) == 144
    positions = defaultdict(Counter)
    settings, outputs = set(), defaultdict(set)
    for i, row in enumerate(rows):
        assert row["trial_index"] == i
        assert {k:row[k] for k in ("case", "repeat", "cell")} == setup["order"][i]
        prompt = prepared[row["case"]]
        ids = verify_artifact(prompt["artifact"], setup["profile"], prompt["prompt"])
        assert ids[:setup["prefix_tokens"]] == setup["prefix_ids"]
        assert row["input_tokens"] == len(ids) == row["final"]["tokens_evaluated"]
        assert row["final"]["prompt"] == prompt["prompt"]
        assert row["cache_n"] == (setup["prefix_tokens"] if row["cell"].endswith("warm") else 0)
        assert row["cache_n"] + row["prompt_n"] == len(ids)
        assert len(row["tokens"]) == row["predicted_n"] == row["final"]["tokens_predicted"]
        assert not row["invariant_errors"] and not row["final"]["truncated"]
        assert row["score"] == score(row["content"], cases[row["case"]]["expected"])
        positions[row["cell"]][i % 4] += 1
        settings.add(fingerprint(row["final"]["generation_settings"]))
        outputs[row["case"]].add(row["content"])
    assert len(settings) == 1
    assert all(dict(counts) == {0:9, 1:9, 2:9, 3:9} for counts in positions.values())
    calculated = summary(rows, setup)
    assert {k:v for k,v in saved_summary.items() if k != "completed_at"} == calculated
    return {"status":"passed", "rows":len(rows), "distinct_tasks":len(cases),
            "effective_generation_profiles":len(settings), "prefix_tokens":setup["prefix_tokens"],
            "per_case_distinct_outputs":{k:len(v) for k,v in outputs.items()},
            "checks":["frozen workload/protocol/source/harness hashes", "complete balanced matrix and recorded order",
                      "full prepared IDs and engine-echoed text", "actual input/cache/output-token accounting",
                      "identical effective generation settings", "independently recomputed scoring and summary"],
            "artifact_sha256":{name:hash_file(directory/name) for name in ("prepared.json", "setup.json", "trials.jsonl", "summary.json")}}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    print(json.dumps(verify(args.directory), indent=2))
