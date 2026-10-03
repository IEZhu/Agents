"""Build with/without-component contexts for one sweep run.

    python evals/ablation/build_contexts.py RUN_DIR [--arm with|without]

Reads RUN_DIR/cases/<component>.json ({"component": id, "cases": [...]}) and
writes RUN_DIR/ctx/<token>.md plus RUN_DIR/plan.json (token -> case, arm,
component, agent, context hash). Each context is the production enrichment for the
case's agent and latest message, with platform instructions stripped.

Refuses a run with no case files, a case file whose component differs from its
file name, that the checker did not mark, that repeats a case id or that has no
cases and no untestable reason,
and, when RUN_DIR/ids.txt exists, a listed
component without a case file or a case file for a component it does not list.
RUN_DIR/build_meta.json records the commit the contexts were built from, so they
can be rebuilt without committing ctx/. On a rebuild, an answer is kept only when
its context is known to be unchanged; otherwise it is deleted and answered again.

- rule-*:    with = production prompt;       without = that rule's section cut
- skill-*:   with = production skills + this skill (added if retrieval missed it);
             without = production skills minus this skill
- implant-*: with = exactly this implant;    without = no implant

`--arm` compares two revisions or two settings instead of one component: it builds
only the production context of this checkout and environment and records it as that
arm, keeping the other arm's entries. Run it once per arm, for example `--arm with`
in the candidate's checkout or with its `EMBEDDING_MODEL`, and `--arm without` in the
baseline's. The component name is then just the experiment's name. A case whose two
contexts are equal is dropped from both arms and listed in build_errors.json as
"arms identical", with the context's hash: the change does not reach it, and
rebuilding either arm with that context leaves the case out again. An arm written
while the other arm, already built, has no context for the case is listed as "other
arm not built" until that arm is rebuilt. build_meta.json records each arm.
"""
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Set for a run, not on import: tests import this module, and the settings would
# otherwise leak into every test that runs after them.
RUN_ENV = {"LANGFUSE_TRACING_ENABLED": "false", "AGENTS_AUTO_UPDATE": "0",
           "EMBEDDING_MODEL": "intfloat/multilingual-e5-large"}


PROMPT_SOURCES = ("agents", "skills", "implants", "rules", "src", "evals/ablation", "evals/runners")
ARMS = ("with", "without")


def token_of(component: str, case_id: str, arm: str) -> str:
    return hashlib.sha1(f"{component}:{case_id}:{arm}".encode()).hexdigest()[:12]


def other_arm(arm: str) -> str:
    return ARMS[1 - ARMS.index(arm)]


def ctx_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def drop_stale_answer(run_dir: Path, token: str, text: str, previous: dict) -> None:
    """Delete the answer to a context that changed since it was answered.

    The previous plan records each context's hash; runs built before that are
    compared with ctx/ when it is still there. An answer whose context cannot be
    compared at all (an old published run without either) is deleted too: nothing
    shows it answered this context. Only a context known to be unchanged keeps its
    answer. Published answers stay in git, so a deleted one can be restored.
    """
    before = previous.get(token, {}).get("ctx_sha256")
    ctx = run_dir / "ctx" / f"{token}.md"
    if before is None and ctx.exists():
        before = ctx_sha256(ctx.read_text(encoding="utf-8"))
    if before != ctx_sha256(text):
        (run_dir / "answers" / f"{token}.md").unlink(missing_ok=True)


def skill_arm(retrieved: list[dict], filename: str, arm: str, forced: list[dict]) -> list[dict]:
    """Production skills with the target skill forced in or out.

    with: production retrieval unchanged when it already has the skill (same order
    and position), otherwise the skill appended; without: the skill removed.
    """
    if arm == "without":
        return [s for s in retrieved if s["filename"] != filename]
    return retrieved if any(s["filename"] == filename for s in retrieved) else retrieved + forced


IDENTICAL = "arms identical"
UNPAIRED = "other arm not built"


def start_plan(previous: dict, previous_errors: list, arm: str | None) -> tuple[dict, list]:
    """The plan and errors a build starts from.

    A two-arm build starts empty. An --arm build keeps the other arm's entries and
    errors and every "arms identical" record, whatever arm found it, and rebuilds its
    own entries and errors.
    """
    if arm is None:
        return {}, []
    return ({t: p for t, p in previous.items() if p.get("arm") != arm},
            [e for e in previous_errors if e.get("arm") == other_arm(arm) or e.get("error") == IDENTICAL])


def place_arm(plan: dict, errors: list, component: str, case_id: str, arm: str, text: str,
              other_built: bool) -> bool:
    """Whether an --arm build writes `text` for this case; updates `plan` and `errors`.

    Arms with the same context are both left out under an "arms identical" record
    holding the context's hash, so rebuilding either arm with that context leaves
    the case out again. A changed context is written. The case is then "other arm
    not built" while the other arm, already built (`other_built`), has neither a
    context nor an error for it.
    """
    digest = ctx_sha256(text)

    def this_case(error: dict) -> bool:
        return error.get("component") == component and error.get("case") == case_id

    record = next((e for e in errors if this_case(e) and e.get("error") == IDENTICAL), None)
    if record is not None:
        if record.get("ctx_sha256") == digest:
            return False
        errors.remove(record)
    elif other := identical_other_arm(plan, component, case_id, arm, text):
        plan.pop(other)
        errors.append({"component": component, "case": case_id, "error": IDENTICAL, "arm": arm,
                       "ctx_sha256": digest})
        return False
    errors[:] = [e for e in errors if not (this_case(e) and e.get("error") == UNPAIRED)]
    reported = any(this_case(e) and e.get("arm") == other_arm(arm) for e in errors)
    if other_built and token_of(component, case_id, other_arm(arm)) not in plan and not reported:
        errors.append({"component": component, "case": case_id, "error": UNPAIRED, "arm": arm})
    return True


def identical_other_arm(plan: dict, component: str, case_id: str, arm: str, text: str) -> str | None:
    """The other arm's token when its context equals this arm's `text`, else None."""
    other = token_of(component, case_id, other_arm(arm))
    return other if plan.get(other, {}).get("ctx_sha256") == ctx_sha256(text) else None


def merge_build_meta(existing: dict, arm: str | None, meta: dict) -> dict:
    """build_meta.json content: the build's own meta, or one entry per arm for --arm builds.

    A two-arm build's meta stands for both arms, so an --arm build after it keeps that
    meta for the other arm.
    """
    if arm is None:
        return meta
    arms = existing.get("arms") or ({each: existing for each in ARMS} if "commit" in existing else {})
    return {"arms": {**arms, arm: meta}}


def build_meta() -> dict:
    """The commit the contexts come from, and whether the tree differed from it."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    # Untracked files count: a new, uncommitted skill changes the contexts as much as an edit.
    changed = git("status", "--porcelain", "--", *PROMPT_SOURCES, ":(exclude)evals/ablation/runs")
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(changed),
            "embedding_model": os.environ.get("EMBEDDING_MODEL", RUN_ENV["EMBEDDING_MODEL"])}


def cut_section(prompt: str, header: str) -> str:
    """Remove a `### ...` section: from its header line to the next ### / ## heading."""
    pattern = re.compile(rf"^{re.escape(header)}\n.*?(?=^#{{2,3}} |\Z)", re.S | re.M)
    out, n = pattern.subn("", prompt, count=1)
    if n != 1:
        raise ValueError(f"section not found: {header!r}")
    return out


def store_records(store, ids: list[str]) -> list[dict]:
    """Records in the retriever's own shape, looked up by filename id."""
    found = store.get(ids=ids)
    lookup = {cid: (found.metadatas[i] or {}, found.documents[i]) for i, cid in enumerate(found.ids)}
    missing = [i for i in ids if i not in lookup]
    if missing:
        raise ValueError(f"not in store: {missing}")
    return [{"filename": cid, "content": lookup[cid][0].get("body", lookup[cid][1]),
             "metadata": lookup[cid][0], "distance": 0.0, "tier": "forced"} for cid in ids]


def conversation_block(case: dict) -> str:
    parts = ["# Conversation", ""]
    for turn in case.get("history") or []:
        parts += [f"## {turn['role'].capitalize()}", "", turn["content"].strip(), ""]
    parts += ["## User (latest message, answer this)", "", case["user_message"].strip(), ""]
    return "\n".join(parts)


async def main(run_dir: Path, only_arm: str | None = None) -> None:
    # Checked before the imports below, which load the embedding model.
    case_files = sorted((run_dir / "cases").glob("*.json"))
    # A case file names its component twice; building from the wrong one would test
    # another component under this one's name.
    specs = {path: json.loads(path.read_text()) for path in case_files}
    if mismatched := [path.name for path, spec in specs.items() if spec.get("component") != path.stem]:
        raise SystemExit(f"case files whose component does not match the file name: {mismatched}")
    # An empty file is untestable only when it says why; otherwise the cases step failed.
    if unexplained := [path.name for path, spec in specs.items() if not spec.get("cases") and not (
            isinstance(spec.get("untestable"), str) and spec["untestable"].strip())]:
        raise SystemExit(f"case files with no cases and no untestable reason: {unexplained}; "
                         "rerun the cases step for them")
    # cases.js has a skeptic fix each file and mark it; an unmarked file skipped that step.
    if unchecked := [path.name for path, spec in specs.items() if spec.get("checked") is not True]:
        raise SystemExit(f"case files the checker did not mark (checked: true): {unchecked}; "
                         "rerun the cases step for them")
    # Tokens come from component, case id and arm, so a repeated id would overwrite a case.
    if repeated := [path.name for path, spec in specs.items()
                    if len({c["id"] for c in spec.get("cases", [])}) != len(spec.get("cases", []))]:
        raise SystemExit(f"case files with a repeated case id: {repeated}")
    ids_file = run_dir / "ids.txt"
    ids = ids_file.read_text().split() if ids_file.exists() else []
    if ids_file.exists() and (extra := [path.name for path in case_files if path.stem not in ids]):
        raise SystemExit(f"case files for components not in ids.txt: {extra}; remove them or list them there")
    # components.json is a snapshot: a component removed from the repository since
    # has no file to test, so it is recorded as a build error instead of stopping.
    files = {c["id"]: c["file"] for c in json.loads((ROOT / "evals/ablation/components.json").read_text())}
    removed = [i for i in ids if i in files and not (ROOT / files[i]).exists()]
    written = {path.stem for path in case_files}
    if absent := [i for i in ids if i not in written and i not in removed]:
        raise SystemExit(f"no cases file for {absent}; rerun the cases step for them")
    if not case_files and not removed:
        raise SystemExit(f"{run_dir}/cases has no case files; run the cases step first")
    removed_errors = [{"component": i, "case": "*",
                       "error": "not in store: removed from the repository since components.json"} for i in removed]
    if all(path.stem in removed or not specs[path].get("cases") for path in case_files):
        # Nothing left to build (removed or untestable components only): skip loading
        # the embedding model.
        (run_dir / "plan.json").write_text("{}\n")
        (run_dir / "build_errors.json").write_text(json.dumps(removed_errors, ensure_ascii=False, indent=1) + "\n")
        (run_dir / "build_meta.json").write_text(json.dumps(build_meta(), indent=1) + "\n")
        print(f"0 contexts, {len(removed_errors)} errors -> {run_dir}")
        return
    from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions
    from src import server
    from src.engine import enrichment

    skills = enrichment.get_skill_retriever()
    implants = enrichment.get_implant_retriever()
    orig_skill_retrieve = skills.retrieve
    orig_implant_retrieve = implants.retrieve
    orig_needed = enrichment.implants_needed

    def restore():
        skills.retrieve = orig_skill_retrieve
        implants.retrieve = orig_implant_retrieve
        enrichment.implants_needed = orig_needed

    async def build(agent, query, component, arm):
        restore()
        # An --arm build is the production context as this checkout and environment make it.
        kind = component.split("-", 1)[0] if only_arm is None else None
        if kind == "skill":
            forced = store_records(skills.store, [f"{component}.mdc"])

            def patched(*a, **k):
                return skill_arm(orig_skill_retrieve(*a, **k), f"{component}.mdc", arm, forced)
            skills.retrieve = patched
        elif kind == "implant":
            forced = store_records(implants.store, [f"{component}.mdc"]) if arm == "with" else []
            implants.retrieve = lambda *a, **k: list(forced)
            enrichment.implants_needed = (lambda *a, **k: True) if forced else orig_needed
        server.SESSION_CACHE.clear()
        prompt, loaded_skills, loaded_implants, rules, tier = await server._load_and_enrich(agent, query, [])
        prompt = _strip_platform_instructions(prompt)
        if kind == "rule" and arm == "without":
            prompt = cut_section(prompt, f"### Rule: {component.removeprefix('rule-')}")
        return prompt, {"skills": list(loaded_skills), "implants": list(loaded_implants),
                        "rules": list(rules), "tier": tier}

    (run_dir / "ctx").mkdir(exist_ok=True)
    meta_path = run_dir / "build_meta.json"
    existing_meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
    meta_path.write_text(json.dumps(merge_build_meta(existing_meta, only_arm, build_meta()), indent=1) + "\n")
    previous = json.loads((run_dir / "plan.json").read_text()) if (run_dir / "plan.json").exists() else {}
    errors_path = run_dir / "build_errors.json"
    previous_errors = json.loads(errors_path.read_text()) if errors_path.exists() else []
    plan, kept_errors = start_plan(previous, previous_errors, only_arm)
    errors = list(removed_errors) + kept_errors
    arm_note = {"arm": only_arm} if only_arm else {}
    # A two-arm build's meta has no "arms": it built both.
    other_built = only_arm is not None and (other_arm(only_arm) in existing_meta.get("arms", {})
                                            or "commit" in existing_meta)
    for path in case_files:
        if path.stem in removed:
            continue
        spec = json.loads(path.read_text())
        component = spec["component"]
        for case in spec["cases"]:
            try:
                built = {arm: await build(case["agent"], case["user_message"], component, arm)
                         for arm in ((only_arm,) if only_arm else ARMS)}
            except Exception as exc:  # one bad case must not stop the batch
                errors.append({"component": component, "case": case["id"], "error": repr(exc), **arm_note})
                print(f"ERROR {component}/{case['id']}: {exc!r}", flush=True)
                continue
            texts = {arm: f"# Operating context loaded for this conversation\n{prompt}\n\n{conversation_block(case)}"
                     for arm, (prompt, _) in built.items()}
            if only_arm is None and built["with"][0] == built["without"][0]:
                errors.append({"component": component, "case": case["id"], "error": IDENTICAL,
                               "ctx_sha256": ctx_sha256(texts["with"])})
                continue
            if only_arm and not place_arm(plan, errors, component, case["id"], only_arm, texts[only_arm], other_built):
                # Neither arm is worth answering: no context of this case stays.
                for arm in ARMS:
                    (run_dir / "ctx" / f"{token_of(component, case['id'], arm)}.md").unlink(missing_ok=True)
                continue
            for arm, (prompt, meta) in built.items():
                token = token_of(component, case["id"], arm)
                drop_stale_answer(run_dir, token, texts[arm], previous)
                (run_dir / "ctx" / f"{token}.md").write_text(texts[arm], encoding="utf-8")
                plan[token] = {"component": component, "case": case["id"], "arm": arm,
                               "agent": case["agent"], "chars": len(prompt), "ctx_sha256": ctx_sha256(texts[arm]), **meta}
            if only_arm:
                print(f"{component}/{case['id']}: {case['agent']} arm={only_arm} "
                      f"tier={built[only_arm][1]['tier']} {len(built[only_arm][0])} chars", flush=True)
                continue
            delta = len(built["with"][0]) - len(built["without"][0])
            print(f"{component}/{case['id']}: {case['agent']} tier={built['with'][1]['tier']} +{delta} chars", flush=True)
    restore()
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    (run_dir / "build_errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(plan)} contexts, {len(errors)} errors -> {run_dir}")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Build with/without contexts for one ablation run.")
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--arm", choices=ARMS, help="build only this arm's production context (see the module docstring)")
    args = parser.parse_args()
    for key, value in RUN_ENV.items():
        os.environ.setdefault(key, value)
    sys.path.insert(0, str(ROOT))
    asyncio.run(main(args.run_dir.resolve(), only_arm=args.arm))
