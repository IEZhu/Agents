"""Compare the verdicts of ablation runs per run, per case group and across runs.

    python evals/ablation/compare.py RUN_DIR [RUN_DIR ...] [--json]

Each RUN_DIR needs aggregate.py's inputs: cases/, judge_plan.json and
judge/<stem>.verdict.json. A case's optional `group` field groups the tables; a
case without one is in "all". Group "control" marks cases the change under test
should not affect, so their result is the noise floor.

Per run: verdicts won by the arm with the change and by the arm without it, ties,
net, and robust cases, where the same arm won in both orders. Across the runs given,
every verdict scores +1 when the arm with the change wins, -1 when it loses and 0 for
a tie. A case's score sums its verdicts, and an exact two-sided sign test compares
cases with a positive and a negative score, separately for controls; even cases
drop out. Give runs of one answer model per call: the test assumes one model.
Missing or malformed verdicts are counted and left out.
"""
import argparse
import importlib.util
import json
import sys
from collections import defaultdict
from math import comb
from pathlib import Path

HERE = Path(__file__).resolve().parent
CONTROL = "control"
POINTS = {"with": 1, "without": -1, "tie": 0}


def _aggregate():
    spec = importlib.util.spec_from_file_location("ablation_aggregate", HERE / "aggregate.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aggregate = _aggregate()


def sign_test(positive: int, negative: int) -> float:
    """Exact two-sided sign test p-value."""
    n = positive + negative
    if not n:
        return 1.0
    k = min(positive, negative)
    return min(1.0, 2 * sum(comb(n, i) for i in range(k + 1)) / 2 ** n)


def load_run(run_dir: Path) -> dict:
    """Cases with their groups, and each verdict's winning arm per case."""
    groups = {}
    for path in (run_dir / "cases").glob("*.json"):
        for case in json.loads(path.read_text()).get("cases", []):
            groups[case["id"]] = case.get("group", "all")
    plan_path = run_dir / "judge_plan.json"
    if not plan_path.exists():
        raise SystemExit(f"{run_dir}: no judge_plan.json; run build_judges.py first")
    winners, missing = defaultdict(list), 0
    for stem, p in sorted(json.loads(plan_path.read_text()).items()):
        try:
            verdict, _errors = aggregate.read_verdict(run_dir / "judge" / f"{stem}.verdict.json")
        except (OSError, ValueError):
            missing += 1
            continue
        winners[p["case"]].append({"A": p["A"], "B": p["B"], "tie": "tie"}[verdict["winner"]])
    return {"name": run_dir.name, "groups": groups, "winners": dict(winners), "missing": missing}


def run_summary(run: dict, group: str | None = None) -> dict:
    cases = [c for c in run["winners"] if group is None or run["groups"].get(c, "all") == group]
    arms = [a for c in cases for a in run["winners"][c]]
    robust = {arm: sum(1 for c in cases if len(run["winners"][c]) == 2 and set(run["winners"][c]) == {arm})
              for arm in ("with", "without")}
    return {"cases": len(cases), "with": arms.count("with"), "without": arms.count("without"),
            "tie": arms.count("tie"), "net": arms.count("with") - arms.count("without"),
            "robust_with": robust["with"], "robust_without": robust["without"]}


def across_runs(runs: list[dict]) -> dict:
    score, group_of = defaultdict(int), {}
    for run in runs:
        group_of.update(run["groups"])
        for case, arms in run["winners"].items():
            score[case] += sum(POINTS[a] for a in arms)
    out = {}
    for name, keep in (("change applies", lambda g: g != CONTROL), ("controls", lambda g: g == CONTROL)):
        cases = {c: s for c, s in score.items() if keep(group_of.get(c, "all"))}
        better = sum(s > 0 for s in cases.values())
        worse = sum(s < 0 for s in cases.values())
        out[name] = {"cases": len(cases), "better": better, "worse": worse, "even": len(cases) - better - worse,
                     "score": sum(cases.values()), "p": sign_test(better, worse)}
    return out


def compare(run_dirs: list[Path]) -> dict:
    runs = [load_run(d) for d in run_dirs]
    groups = sorted({g for run in runs for g in run["groups"].values()}, key=lambda g: (g == CONTROL, g))
    return {"runs": [{"name": run["name"], "missing": run["missing"], "all": run_summary(run),
                      "groups": {g: run_summary(run, g) for g in groups}} for run in runs],
            "groups": groups, "across": across_runs(runs)}


def to_markdown(result: dict) -> str:
    lines = ["| Run | with | without | tie | net | robust with / without | missing |", "|---|---:|---:|---:|---:|---:|---:|"]
    for run in result["runs"]:
        s = run["all"]
        lines.append(f"| {run['name']} | {s['with']} | {s['without']} | {s['tie']} | {s['net']:+d} | "
                     f"{s['robust_with']} / {s['robust_without']} | {run['missing']} |")
    lines += ["", "Net verdicts by group (robust with / without):", "",
              "| Group | " + " | ".join(run["name"] for run in result["runs"]) + " |",
              "|---|" + "---|" * len(result["runs"])]
    for group in result["groups"]:
        cells = [run["groups"][group] for run in result["runs"]]
        lines.append(f"| {group} ({cells[0]['cases']}) | " + " | ".join(
            f"{c['net']:+d} ({c['robust_with']} / {c['robust_without']})" for c in cells) + " |")
    lines += ["", "| Across runs | cases | change better | worse | even | score | sign test p |",
              "|---|---:|---:|---:|---:|---:|---:|"]
    for name, s in result["across"].items():
        lines.append(f"| {name} | {s['cases']} | {s['better']} | {s['worse']} | {s['even']} | {s['score']:+d} | {s['p']:.3f} |")
    return "\n".join(lines)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("run_dirs", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="print the result as JSON")
    args = parser.parse_args(argv)
    result = compare([d.resolve() for d in args.run_dirs])
    print(json.dumps(result, indent=1) if args.json else to_markdown(result))
    return 0


if __name__ == "__main__":
    sys.exit(main())
