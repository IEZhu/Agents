"""Summarise ablation verdicts per component.

    python evals/ablation/aggregate.py RUN_DIR [RUN_DIR ...] [--allow-partial]

Reads each run's judge_plan.json and judge/<stem>.verdict.json, maps A/B back to
with/without, and writes RUN_DIR/results.json and RUN_DIR/RESULTS.md for a single
run, or prints the combined table for several runs.
net = verdicts won with the component minus verdicts won without it; "robust"
counts only cases where the same arm won in both orders.
Missing verdicts, and answer pairs build_judges.py skipped, are listed as missing;
the script exits 1 when any are missing unless --allow-partial.
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
    skipped = run_dir / "judge_skipped.json"
    if skipped.exists():
        missing += [{"pair": pair, "error": "answer missing, not judged"} for pair in json.loads(skipped.read_text())]
    return rows, missing


def untestable(run_dir: Path) -> dict:
    out = {}
    for path in (run_dir / "cases").glob("*.json"):
        spec = json.loads(path.read_text())
        if not spec.get("cases"):
            out[spec["component"]] = spec.get("untestable", "no cases")
    return out


def table(rows: list[dict], skipped: dict) -> str:
    """Per-component counts. Judges favour position B, so the robust columns count
    only cases where the same arm won in both orders."""
    by = defaultdict(lambda: {"with": 0, "without": 0, "tie": 0, "clear_or_large": 0,
                              "orders": defaultdict(list)})
    for r in rows:
        s = by[r["component"]]
        s[r["winner_arm"]] += 1
        s["orders"][r["case"]].append(r["winner_arm"])
        if r["winner_arm"] != "tie" and r["margin"] in ("clear", "large"):
            s["clear_or_large"] += 1
    for s in by.values():
        both = [arms[0] for arms in s["orders"].values() if len(arms) == 2 and arms[0] == arms[1]]
        s["robust_with"], s["robust_without"] = both.count("with"), both.count("without")
    lines = ["| Component | Cases | with | without | tie | net | robust with | robust without | clear/large |",
             "|---|---|---|---|---|---|---|---|---|"]
    order = sorted(by.items(), key=lambda kv: (kv[1]["robust_with"] - kv[1]["robust_without"],
                                               kv[1]["with"] - kv[1]["without"]))
    for comp, s in order:
        lines.append(f"| {comp} | {len(s['orders'])} | {s['with']} | {s['without']} | {s['tie']} | "
                     f"{s['with'] - s['without']:+d} | {s['robust_with']} | {s['robust_without']} | "
                     f"{s['clear_or_large']} |")
    if skipped:
        lines += ["", "Untestable or no cases:"] + [f"- {c}: {why}" for c, why in sorted(skipped.items())]
    return "\n".join(lines) + "\n"


def main(run_dirs: list[Path], allow_partial: bool = False) -> int:
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
    return 1 if all_missing and not allow_partial else 0


if __name__ == "__main__":
    sys.exit(main([Path(p).resolve() for p in sys.argv[1:] if p != "--allow-partial"],
                  allow_partial="--allow-partial" in sys.argv[1:]))
