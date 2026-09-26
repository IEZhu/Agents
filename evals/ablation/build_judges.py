"""Build blind pairwise judge inputs from a run's answers.

    python evals/ablation/build_judges.py RUN_DIR [--allow-partial]

For every case whose two answers exist, writes RUN_DIR/judge/<stem>.md for both
orders and RUN_DIR/judge_plan.json (stem -> component, case, which arm is A/B).
Stems carry no arm names. Arm order in o1 is fixed per case by a hash; o2 swaps it.
Pairs with a missing answer go to RUN_DIR/judge_skipped.json, which aggregate.py
reports as missing; the script exits 1 on any skipped pair unless --allow-partial.
On a rebuild, a verdict whose judge input changed is deleted, so it is judged again
rather than counted for the new input; verdicts on unchanged input are kept.
"""
import hashlib
import json
import sys
from pathlib import Path


def main(run_dir: Path, allow_partial: bool = False) -> int:
    plan = json.loads((run_dir / "plan.json").read_text())
    cases = {}
    for path in (run_dir / "cases").glob("*.json"):
        spec = json.loads(path.read_text())
        for case in spec.get("cases", []):
            cases[(spec["component"], case["id"])] = case
    arms = {}
    for token, p in plan.items():
        arms.setdefault((p["component"], p["case"]), {})[p["arm"]] = token
    # plan.json comes from build_contexts.py; a case since removed or renamed in cases/
    # has no conversation or rubric to judge against.
    if stale := sorted("/".join(key) for key in arms if key not in cases):
        raise SystemExit(f"plan.json lists cases no longer in cases/: {stale}; rerun build_contexts.py")
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
            judge_input = run_dir / "judge" / f"{stem}.md"
            if not judge_input.exists() or judge_input.read_text(encoding="utf-8") != body:
                # A verdict on other input would be counted for this one: judge it again.
                (run_dir / "judge" / f"{stem}.verdict.json").unlink(missing_ok=True)
                judge_input.write_text(body, encoding="utf-8")
            judge_plan[stem] = {"component": component, "case": case_id, "A": a, "B": b}
    (run_dir / "judge_plan.json").write_text(json.dumps(judge_plan, indent=1) + "\n")
    (run_dir / "judge_skipped.json").write_text(json.dumps(skipped, indent=1) + "\n")
    print(f"{len(judge_plan)} judge files, {len(skipped)} pairs skipped (missing answers): {skipped}")
    return 1 if skipped and not allow_partial else 0


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if a != "--allow-partial"]
    sys.exit(main(Path(args[0]).resolve(), allow_partial="--allow-partial" in sys.argv[1:]))
