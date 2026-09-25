"""Build blind pairwise judge inputs from a run's answers.

    python evals/ablation/build_judges.py RUN_DIR

For every case whose two answers exist, writes RUN_DIR/judge/<stem>.md for both
orders and RUN_DIR/judge_plan.json (stem -> component, case, which arm is A/B).
Stems carry no arm names. Arm order in o1 is fixed per case by a hash; o2 swaps it.
"""
import hashlib
import json
import sys
from pathlib import Path


def main(run_dir: Path) -> None:
    plan = json.loads((run_dir / "plan.json").read_text())
    cases = {}
    for path in (run_dir / "cases").glob("*.json"):
        spec = json.loads(path.read_text())
        for case in spec.get("cases", []):
            cases[(spec["component"], case["id"])] = case
    arms = {}
    for token, p in plan.items():
        arms.setdefault((p["component"], p["case"]), {})[p["arm"]] = token
    (run_dir / "judge").mkdir(exist_ok=True)
    judge_plan, skipped = {}, []
    for key, pair in sorted(arms.items()):
        answers = {arm: run_dir / "answers" / f"{token}.md" for arm, token in pair.items()}
        if set(answers) != {"with", "without"} or not all(p.exists() and p.stat().st_size for p in answers.values()):
            skipped.append("/".join(key))
            continue
        component, case_id = key
        case = cases[key]
        hist = "".join(f"### {t['role'].capitalize()}\n{t['content']}\n\n" for t in case.get("history") or []) or "(none)\n\n"
        rubric = "\n".join(f"{i}. {r}" for i, r in enumerate(case["rubric"], 1))
        first = "with" if int(hashlib.sha1(case_id.encode()).hexdigest(), 16) % 2 else "without"
        second = "without" if first == "with" else "with"
        base = hashlib.sha1(f"{component}:{case_id}".encode()).hexdigest()[:10]
        for i, (a, b) in enumerate([(first, second), (second, first)], 1):
            stem = f"{base}__o{i}"
            body = (f"# Case {case_id}\n\n## Conversation so far\n{hist}## Latest user message\n{case['user_message']}\n\n"
                    f"## Rubric (what a strong answer does)\n{rubric}\n\n## Answer A\n{answers[a].read_text()}\n\n"
                    f"## Answer B\n{answers[b].read_text()}\n")
            (run_dir / "judge" / f"{stem}.md").write_text(body, encoding="utf-8")
            judge_plan[stem] = {"component": component, "case": case_id, "A": a, "B": b}
    (run_dir / "judge_plan.json").write_text(json.dumps(judge_plan, indent=1) + "\n")
    print(f"{len(judge_plan)} judge files, {len(skipped)} pairs skipped (missing answers): {skipped}")


if __name__ == "__main__":
    main(Path(sys.argv[1]).resolve())
