"""Build with/without-component contexts for one sweep run.

    python evals/ablation/build_contexts.py RUN_DIR

Reads RUN_DIR/cases/<component>.json ({"component": id, "cases": [...]}) and
writes RUN_DIR/ctx/<token>.md plus RUN_DIR/plan.json (token -> case, arm,
component, agent). Refuses a run with no case files, or with a component listed in
RUN_DIR/ids.txt that has none. RUN_DIR/build_meta.json records the commit the
contexts were built from, so they can be rebuilt without committing ctx/. Each context is the production enrichment for the case's
agent and latest message, with platform instructions stripped:

- rule-*:    with = production prompt;       without = that rule's section cut
- skill-*:   with = production skills + this skill (added if retrieval missed it);
             without = production skills minus this skill
- implant-*: with = exactly this implant;    without = no implant
"""
import asyncio
import hashlib
import json
import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")
os.environ.setdefault("AGENTS_AUTO_UPDATE", "0")
os.environ.setdefault("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")


def build_meta() -> dict:
    """The commit the contexts come from, and whether the tree differed from it."""
    import subprocess

    def git(*args):
        return subprocess.run(["git", *args], cwd=ROOT, capture_output=True, text=True).stdout.strip()
    return {"commit": git("rev-parse", "HEAD"), "dirty": bool(git("status", "--porcelain", "--untracked-files=no")),
            "embedding_model": os.environ["EMBEDDING_MODEL"]}


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


async def main(run_dir: Path) -> None:
    # Checked before the imports below, which load the embedding model.
    case_files = sorted((run_dir / "cases").glob("*.json"))
    if not case_files:
        raise SystemExit(f"{run_dir}/cases has no case files; run the cases step first")
    ids_file = run_dir / "ids.txt"
    if ids_file.exists():
        written = {path.stem for path in case_files}
        if absent := [i for i in ids_file.read_text().split() if i not in written]:
            raise SystemExit(f"no cases file for {absent}; rerun the cases step for them")
    from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions
    from src import server
    from src.engine import enrichment

    skills = enrichment.skill_retriever
    implants = enrichment.implant_retriever
    orig_skill_retrieve = skills.retrieve
    orig_implant_retrieve = implants.retrieve
    orig_needed = enrichment.implants_needed

    def restore():
        skills.retrieve = orig_skill_retrieve
        implants.retrieve = orig_implant_retrieve
        enrichment.implants_needed = orig_needed

    async def build(agent, query, component, arm):
        restore()
        kind = component.split("-", 1)[0]
        if kind == "skill":
            forced = store_records(skills.store, [f"{component}.mdc"])

            def patched(*a, **k):
                got = [s for s in orig_skill_retrieve(*a, **k) if s["filename"] != f"{component}.mdc"]
                return got + forced if arm == "with" else got
            skills.retrieve = patched
        elif kind == "implant":
            forced = store_records(implants.store, [f"{component}.mdc"]) if arm == "with" else []
            implants.retrieve = lambda *a, **k: list(forced)
            enrichment.implants_needed = (lambda *a, **k: True) if forced else orig_needed
        server.SESSION_CACHE.clear()
        prompt, _h, loaded_skills, loaded_implants, rules, tier = await server._load_and_enrich(agent, query, [])
        prompt = _strip_platform_instructions(prompt)
        if kind == "rule" and arm == "without":
            prompt = cut_section(prompt, f"### Rule: {component.removeprefix('rule-')}")
        return prompt, {"skills": list(loaded_skills), "implants": list(loaded_implants),
                        "rules": list(rules), "tier": tier}

    (run_dir / "ctx").mkdir(exist_ok=True)
    (run_dir / "build_meta.json").write_text(json.dumps(build_meta(), indent=1) + "\n")
    plan, errors = {}, []
    for path in case_files:
        spec = json.loads(path.read_text())
        component = spec["component"]
        for case in spec["cases"]:
            try:
                built = {arm: await build(case["agent"], case["user_message"], component, arm)
                         for arm in ("with", "without")}
            except Exception as exc:  # one bad case must not stop the batch
                errors.append({"component": component, "case": case["id"], "error": repr(exc)})
                print(f"ERROR {component}/{case['id']}: {exc!r}", flush=True)
                continue
            if built["with"][0] == built["without"][0]:
                errors.append({"component": component, "case": case["id"], "error": "arms identical"})
                continue
            for arm, (prompt, meta) in built.items():
                token = hashlib.sha1(f"{component}:{case['id']}:{arm}".encode()).hexdigest()[:12]
                text = f"# Operating context loaded for this conversation\n{prompt}\n\n{conversation_block(case)}"
                (run_dir / "ctx" / f"{token}.md").write_text(text, encoding="utf-8")
                plan[token] = {"component": component, "case": case["id"], "arm": arm,
                               "agent": case["agent"], "chars": len(prompt), **meta}
            delta = len(built["with"][0]) - len(built["without"][0])
            print(f"{component}/{case['id']}: {case['agent']} tier={built['with'][1]['tier']} +{delta} chars", flush=True)
    restore()
    (run_dir / "plan.json").write_text(json.dumps(plan, ensure_ascii=False, indent=1) + "\n")
    (run_dir / "build_errors.json").write_text(json.dumps(errors, ensure_ascii=False, indent=1) + "\n")
    print(f"{len(plan)} contexts, {len(errors)} errors -> {run_dir}")


if __name__ == "__main__":
    asyncio.run(main(Path(sys.argv[1]).resolve()))
