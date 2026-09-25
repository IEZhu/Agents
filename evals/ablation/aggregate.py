"""Summarise ablation verdicts per component.

    python evals/ablation/aggregate.py RUN_DIR [RUN_DIR ...]

Reads each run's judge_plan.json and judge/<stem>.verdict.json, maps A/B back to
with/without, and writes RUN_DIR/results.json and RUN_DIR/RESULTS.md for a single
run, or prints the combined table for several runs.
net = verdicts won with the component minus verdicts won without it.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path


def load(run_dir: Path) -> tuple[list[dict], list[dict]]:
    judge_plan = json.loads((run_dir / "judge_plan.json").read_text())
    rows, missing = [], []
    for stem, p in sorted(judge_plan.items()):
        path = run_dir / "judge" / f"{stem}.verdict.json"
        try:
            v = json.loads(path.read_text())
            winner = {"A": p["A"], "B": p["B"], "tie": "tie"}[v["winner"]]
        except (OSError, ValueError, KeyError) as exc:
            missing.append({"stem": stem, "error": repr(exc)})
            continue
        rows.append({**p, "stem": stem, "winner_arm": winner, "margin": v.get("margin"),
                     "reasons": v.get("reasons", ""),
                     "errors_with": v.get("factual_errors", {}).get("A" if p["A"] == "with" else "B", []),
                     "errors_without": v.get("factual_errors", {}).get("A" if p["A"] == "without" else "B", [])})
    return rows, missing


def untestable(run_dir: Path) -> dict:
    out = {}
    for path in (run_dir / "cases").glob("*.json"):
        spec = json.loads(path.read_text())
        if not spec.get("cases"):
            out[spec["component"]] = spec.get("untestable", "no cases")
    return out


def table(rows: list[dict], skipped: dict) -> str:
    by = defaultdict(lambda: {"with": 0, "without": 0, "tie": 0, "clear_or_large": 0, "cases": set()})
    for r in rows:
        s = by[r["component"]]
        s[r["winner_arm"]] += 1
        s["cases"].add(r["case"])
        if r["winner_arm"] != "tie" and r["margin"] in ("clear", "large"):
            s["clear_or_large"] += 1
    lines = ["| Component | Cases | with | without | tie | net | clear/large |", "|---|---|---|---|---|---|---|"]
    for comp, s in sorted(by.items(), key=lambda kv: kv[1]["with"] - kv[1]["without"]):
        lines.append(f"| {comp} | {len(s['cases'])} | {s['with']} | {s['without']} | {s['tie']} | "
                     f"{s['with'] - s['without']:+d} | {s['clear_or_large']} |")
    if skipped:
        lines += ["", "Untestable or no cases:"] + [f"- {c}: {why}" for c, why in sorted(skipped.items())]
    return "\n".join(lines) + "\n"


def main(run_dirs: list[Path]) -> None:
    all_rows, all_skipped, all_missing = [], {}, []
    for run_dir in run_dirs:
        rows, missing = load(run_dir)
        all_rows += rows
        all_missing += missing
        all_skipped.update(untestable(run_dir))
        if len(run_dirs) == 1:
            (run_dir / "results.json").write_text(json.dumps({"verdicts": rows, "missing": missing}, ensure_ascii=False, indent=1) + "\n")
            (run_dir / "RESULTS.md").write_text(f"# Ablation results: {run_dir.name}\n\n" + table(rows, all_skipped))
    print(table(all_rows, all_skipped))
    print(f"{len(all_rows)} verdicts, {len(all_missing)} missing")


if __name__ == "__main__":
    main([Path(p).resolve() for p in sys.argv[1:]])
