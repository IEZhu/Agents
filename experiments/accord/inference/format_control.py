"""Post-hoc, prospectively specified warm format-order control; no primary reruns."""

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import random
import statistics

from .client import Client
from .run import hash_file, trial, write_json
from .workload import load_workload


def control_summary(rows):
    pairs = []
    for index in range(0, len(rows), 2):
        pair = rows[index:index+2]
        text = next(r for r in pair if r["cell"] == "text_warm")
        ids = next(r for r in pair if r["cell"] == "ids_warm")
        pairs.append({"case":text["case"], "text_first":pair[0]["cell"] == "text_warm",
                      "request_ttft_ratio":text["client_ttft_ms"]/ids["client_ttft_ms"],
                      "operational_total_ratio":text["operational_total_ms"]/ids["operational_total_ms"],
                      "request_ttft_saved_ms":text["client_ttft_ms"]-ids["client_ttft_ms"],
                      "exact_text_match":text["content"] == ids["content"],
                      "exact_token_match":text["tokens"] == ids["tokens"]})
    cells = {}
    for name in ("text_warm", "ids_warm"):
        group = [r for r in rows if r["cell"] == name]
        cells[name] = {"runs":len(group), "correct":sum(r["score"]["correct"] for r in group)}
        for field in ("client_ttft_ms", "client_total_ms", "operational_ttft_ms", "operational_total_ms", "restore_ms"):
            values = [r[field] for r in group]
            cells[name][field] = {"median":statistics.median(values), "min":min(values), "max":max(values)}
    contrasts = {}
    for name, selected in (("all", pairs), ("text_first", [p for p in pairs if p["text_first"]]),
                           ("ids_first", [p for p in pairs if not p["text_first"]])):
        contrasts[name] = {"pairs":len(selected),
            "request_ttft_ratio_median":statistics.median(p["request_ttft_ratio"] for p in selected),
            "request_ttft_saved_ms_median":statistics.median(p["request_ttft_saved_ms"] for p in selected),
            "operational_total_ratio_median":statistics.median(p["operational_total_ratio"] for p in selected),
            "exact_text_matches":sum(p["exact_text_match"] for p in selected),
            "exact_token_matches":sum(p["exact_token_match"] for p in selected)}
    return {"cells":cells, "contrasts":contrasts, "pairs":pairs}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--primary", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pid", type=int, required=True)
    args = parser.parse_args()
    # The completed primary summary is required; no overlap with primary calls.
    assert json.loads((args.primary / "summary.json").read_text())["runs"] == 144
    setup = json.loads((args.primary / "setup.json").read_text())
    prepared = {p["case"]:p for p in setup["prepared"]}
    cases = load_workload()["cases"]
    order = list(range(len(cases)))
    random.Random(20261015).shuffle(order)
    args.output.mkdir(parents=True, exist_ok=False)
    plan = {"started_at":datetime.now(timezone.utc).isoformat(), "seed":20261015, "case_indices":order,
            "protocol_sha256":hash_file(Path(__file__).with_name("format-control-protocol.md")),
            "primary_setup_sha256":hash_file(args.primary / "setup.json"),
            "harness_sha256":hash_file(Path(__file__)), "profile":setup["profile"]}
    write_json(args.output / "plan.json", plan)
    client, rows, preconditioning = Client(), [], []
    with (args.output / "trials.jsonl").open("x") as output:
        for block, case_index in enumerate(order):
            case = cases[case_index]
            cells = ("text_warm", "ids_warm", "ids_warm", "text_warm") if block % 2 == 0 else ("ids_warm", "text_warm", "text_warm", "ids_warm")
            for position, cell in enumerate((cells[0], *cells)):
                item = {"case":case["name"], "cell":cell, "repeat":(position-1)//2,
                        "block":block, "position":position-1, "preconditioning":position == 0}
                try:
                    row = trial(client, item, prepared[case["name"]], case, setup["profile"],
                                setup["prefix_ids"], setup["cache"], args.pid)
                except Exception as error:
                    write_json(args.output / "failure.json", {**item, "error":repr(error), "partial":getattr(error, "partial", None)})
                    raise
                output.write(json.dumps(row, separators=(",",":")) + "\n")
                output.flush()
                if row["invariant_errors"]:
                    raise RuntimeError(row["invariant_errors"])
                (preconditioning if position == 0 else rows).append(row)
            print(json.dumps({"control_blocks":block+1, "of":12}), flush=True)
    result = control_summary(rows)
    result["preconditioning_requests"] = len(preconditioning)
    result["preconditioning_operational_total_ms"] = sum(r["operational_total_ms"] for r in preconditioning)
    result["completed_at"] = datetime.now(timezone.utc).isoformat()
    write_json(args.output / "summary.json", result)
    client.json("/slots/0?action=erase", {})


if __name__ == "__main__":
    main()
