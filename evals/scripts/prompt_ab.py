"""Prompt A/B across git revisions, flags and implant sets on one case set.

Both experiments share one pipeline:

1. Every case gets one agent, picked once by the answer model from the agent
   catalog (the production ROUTE_REQUIRED path), or taken from --agents. All
   arms enrich for that agent, so arms differ only in what is under test.
2. Each arm builds its system prompts with its revision's own code and content,
   in a throwaway git worktree (evals/scripts/_prompt_builder.py).
3. The answer model answers every arm, then the grader grades every answer
   (hybrid grading from compare_rules). Both steps append to JSONL files and
   skip what is already there, so an interrupted run resumes.

Modes:
  revisions  Arms are revisions plus env flags, `label=rev[:KEY=VAL,...]`, e.g.
             old=3a4fc5f new=HEAD gate=HEAD:IMPLANT_NEED_GATE=intent. Each arm is
             reported against the first one. Cases whose prompt is identical to
             one already answered reuse those answers.
  implants   One revision. Arms: `none` (no implant), each implant alone,
             `production` (the revision's own selection), and two noise floors.
             `none_repeat` repeats `none` in the same order; `none_reversed`
             repeats it last and in reverse case order, so the server's prompt
             cache holds different neighbours. Run it at temperature 0: an answer
             that differs from `none` by more than the floors do was changed by
             the implant.

Run it under evals/scripts/local_ab.py for local models, e.g.
    python -m evals.scripts.local_ab -- prompt_ab implants --out-dir DIR

`--out-dir` holds the run's state. A manifest pins the model, grader,
temperature, dataset and each arm's resolved commit; a rerun with different
settings is refused, while new arms may be added to an existing run.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from dataclasses import asdict, dataclass, field
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts import compare_rules as cr  # noqa: E402

BUILDER = Path(__file__).resolve().parent / "_prompt_builder.py"
DEFAULT_DATASET = cr.DEFAULT_DATASET
MAX_TOKENS = 800


def log(msg: str) -> None:
    print(f"[prompt_ab] {msg}", file=sys.stderr, flush=True)


@dataclass
class Arm:
    label: str
    rev: str
    env: dict[str, str] = field(default_factory=dict)
    implants: str = "production"
    reverse: bool = False  # answer this arm's cases in reverse order


def parse_arm(spec: str) -> Arm:
    """`label=rev[:KEY=VAL,KEY=VAL]` -> Arm."""
    label, sep, rest = spec.partition("=")
    if not sep or not label or not rest:
        raise ValueError(f"arm must look like label=rev[:KEY=VAL,...], got {spec!r}")
    rev, _, flags = rest.partition(":")
    env = {}
    for item in filter(None, flags.split(",")):
        key, eq, value = item.partition("=")
        if not eq or not key:
            raise ValueError(f"flag must look like KEY=VAL, got {item!r} in {spec!r}")
        env[key] = value
    return Arm(label, rev, env)


NOISE_FLOORS = ("none_repeat", "none_reversed")


def implant_arms(rev: str, implants: list[str]) -> list[Arm]:
    arms = [Arm("none", rev, implants="none"), Arm("none_repeat", rev, implants="none")]
    arms += [Arm(name, rev, implants=name) for name in implants]
    return arms + [Arm("production", rev, implants="production"),
                   Arm("none_reversed", rev, implants="none", reverse=True)]


def mcnemar(base: dict[str, bool], other: dict[str, bool], ids) -> dict[str, Any]:
    """Exact two-sided McNemar on per-case FAIL flags (True = failed)."""
    fixed = sum(1 for i in ids if base.get(i) and not other.get(i))
    broken = sum(1 for i in ids if not base.get(i) and other.get(i))
    n = fixed + broken
    p = 1.0 if n == 0 else min(1.0, 2 * sum(math.comb(n, k) for k in range(min(fixed, broken) + 1)) / 2 ** n)
    return {"fixed": fixed, "broken": broken, "p_exact": round(p, 4)}


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_json_atomic(path: Path, data: Any) -> None:
    """A killed write must never leave a truncated file that a resume trusts."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any | None:
    """Parsed content, or None when the file is missing or unreadable."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def check_manifest(path: Path, current: dict[str, Any]) -> None:
    """Refuse to resume a run made with other settings; allow added arms."""
    old = read_json(path)
    if old is not None:
        for key in ("mode", "provider", "model", "judge_model", "temperature", "samples", "dataset_sha256", "agents_sha256"):
            if old.get(key) != current.get(key):
                raise SystemExit(f"{path.parent} was run with {key}={old.get(key)!r}, now {current.get(key)!r}; "
                                 f"use a new --out-dir")
        old_arms = {a["label"]: a for a in old["arms"]}
        for arm in current["arms"]:
            if arm["label"] in old_arms and old_arms[arm["label"]] != arm:
                raise SystemExit(f"arm {arm['label']!r} changed since {path.parent} was run; use a new --out-dir")
        current = {**current, "arms": list(old_arms.values()) + [a for a in current["arms"] if a["label"] not in old_arms]}
    write_json_atomic(path, current)


# --------------------------------------------------------------------------- #
# Prompt building
# --------------------------------------------------------------------------- #
class Worktrees:
    """One detached worktree per revision, removed on exit."""

    def __init__(self, repo: Path):
        self.repo, self.parent, self.paths = repo, Path(tempfile.mkdtemp(prefix="prompt-ab-")), {}

    def __enter__(self):
        return self

    def get(self, rev: str) -> Path:
        if rev not in self.paths:
            path = self.parent / f"wt{len(self.paths)}"
            subprocess.run(["git", "-C", str(self.repo), "worktree", "add", "--quiet", "--detach", str(path), rev],
                           check=True)
            self.paths[rev] = path
        return self.paths[rev]

    def sha(self, rev: str) -> str:
        return subprocess.run(["git", "-C", str(self.repo), "rev-parse", f"{rev}^{{commit}}"],
                              check=True, capture_output=True, text=True).stdout.strip()

    def __exit__(self, *exc):
        for path in self.paths.values():
            subprocess.run(["git", "-C", str(self.repo), "worktree", "remove", "--force", str(path)], check=False)


def builder_env(extra: dict[str, str]) -> dict[str, str]:
    return {**os.environ, "LANGFUSE_TRACING_ENABLED": "false", "AGENTS_AUTO_UPDATE": "0",
            "EMBEDDING_MODEL": os.environ.get("EMBEDDING_MODEL") or "intfloat/multilingual-e5-large", **extra}


async def run_builder(args: list[str], cwd: Path, env: dict[str, str]) -> None:
    """Run the builder without blocking the event loop, so a SIGINT stops it promptly."""
    proc = await asyncio.create_subprocess_exec(sys.executable, str(BUILDER), *args, cwd=cwd, env=env)
    try:
        code = await proc.wait()
    except BaseException:
        if proc.returncode is None:
            proc.kill()
            await proc.wait()
        raise
    if code:
        raise SystemExit(f"prompt builder failed with exit code {code} in {cwd}")


async def build_prompts(root: Path, dataset: Path, agents: Path, out: Path, arm: Arm) -> dict[str, Any]:
    built = read_json(out)
    if built is None:
        log(f"building prompts for {arm.label} ({arm.rev}, implants={arm.implants}, env={arm.env})")
        await run_builder(["--dataset", str(dataset), "--agents", str(agents), "--implants", arm.implants,
                           "--out", str(out)], root, builder_env(arm.env))
        built = read_json(out)
    return built["prompts"]


async def pick_agents(cases, provider, client, model, catalog_path: Path, out: Path) -> dict[str, str]:
    agents = read_json(out)
    if agents is not None:
        return agents
    from evals.runners import run_mcp_vs_vanilla as rmv
    catalog = read_json(catalog_path)
    rmv._get_router = lambda: SimpleNamespace(get_agent_catalog=lambda: catalog)
    agents = {}
    for case in cases:
        agents[case["id"]] = await rmv._llm_pick_agent(provider, client, model, case["query"])
    write_json_atomic(out, agents)
    log(f"agents: {dict(Counter(agents.values()))}")
    return agents


# --------------------------------------------------------------------------- #
# Answering and grading (resumable)
# --------------------------------------------------------------------------- #
async def answer_all(cases, arms, prompts, provider, client, model, samples: int, path: Path) -> None:
    done = {(r["arm"], r["id"], r["sample"]): r for r in read_jsonl(path)}
    for arm in arms:
        for case in (reversed(cases) if arm.reverse else cases):
            cid = case["id"]
            prompt = prompts[arm.label][cid]["system_prompt"]
            for i in range(samples):
                if (arm.label, cid, i) in done:
                    continue
                # Identical prompt already answered in an earlier arm: reuse it.
                twin = next((a for a in arms[:arms.index(arm)]
                             if arm.implants != "none" and prompts[a.label][cid]["system_prompt"] == prompt
                             and (a.label, cid, i) in done), None)
                if twin is not None:
                    answer, reused = done[(twin.label, cid, i)]["answer"], twin.label
                else:
                    answer, reused = (await provider.complete(client, model, case["query"], prompt, MAX_TOKENS))[0], None
                record = {"arm": arm.label, "id": cid, "sample": i, "answer": answer, "reused_from": reused}
                append_jsonl(path, record)
                done[(arm.label, cid, i)] = record
        log(f"answered {arm.label}")


async def grade_all(cases, answers_path: Path, grades_path: Path, provider, client, judge_model) -> None:
    by_id = {c["id"]: c for c in cases}
    done = {(r["arm"], r["id"], r["sample"]) for r in read_jsonl(grades_path)}
    for record in read_jsonl(answers_path):
        key = (record["arm"], record["id"], record["sample"])
        if key in done:
            continue
        det, verdict, why = await cr.grade_sample(provider, client, judge_model, by_id[record["id"]], record["answer"])
        append_jsonl(grades_path, {"arm": key[0], "id": key[1], "sample": key[2], "deterministic": det,
                                   "verdict": verdict, "reason": why})
        done.add(key)


def arm_results(arms, grades: list[dict[str, Any]]) -> dict[str, cr.ArmResult]:
    results = {arm.label: cr.ArmResult(label=arm.label) for arm in arms}
    for g in sorted(grades, key=lambda g: (g["arm"], g["id"], g["sample"])):
        res = results.get(g["arm"])
        if res is None:
            continue
        failed = cr.case_fails(g["deterministic"], g["verdict"])
        res.per_case[g["id"]] = res.per_case.get(g["id"], False) or failed
        if failed and g["id"] not in res.reasons:
            res.reasons[g["id"]] = g["reason"]
    return results


# --------------------------------------------------------------------------- #
# Reports
# --------------------------------------------------------------------------- #
def implant_table(cases, arms, prompts, answers, results) -> str:
    """One row per arm against `none`: how often and how the answers moved."""
    ids = [c["id"] for c in cases]
    cats = sorted({c["category"] for c in cases})
    first = {(r["arm"], r["id"]): r["answer"] for r in answers if r["sample"] == 0}
    none = results["none"].per_case
    head = ("| arm | prompt +chars | answers changed | " + " | ".join(f"{c} FAIL" for c in cats)
            + " | fixed / broken vs none (p) | hedge markers | asks for input | mean chars |")
    lines = [head, "|" + "---|" * (7 + len(cats))]
    for arm in arms:
        added = sum(len(prompts[arm.label][i]["system_prompt"]) - len(prompts["none"][i]["system_prompt"]) for i in ids) / len(ids)
        texts = [first.get((arm.label, i), "") for i in ids]
        changed = sum(first.get((arm.label, i)) != first.get(("none", i)) for i in ids)
        per = results[arm.label].per_case
        fails = [f"{sum(per.get(c['id'], False) for c in cases if c['category'] == cat)}"
                 f"/{sum(c['category'] == cat for c in cases)}" for cat in cats]
        test = mcnemar(none, per, ids)
        hedges = sum(bool(cr._HEDGE_MARKERS.search(t)) for t in texts)
        asks = sum(bool(cr._REFUSAL.search(t)) for t in texts)
        lines.append(f"| {arm.label} | {added:+.0f} | {changed}/{len(ids)} | " + " | ".join(fails)
                     + f" | {test['fixed']} / {test['broken']} ({test['p_exact']}) | {hedges} | {asks}"
                     f" | {sum(map(len, texts)) / len(ids):.0f} |")
    return "\n".join(lines) + "\n"


def write_reports(mode, cases, arms, prompts, answers, results, cfg, out: Path) -> dict[str, Any]:
    ids = [c["id"] for c in cases]
    base = arms[0]
    summary = {"mode": mode, **cfg, "arms": [asdict(a) for a in arms], "fails": {}, "vs_first": {}}
    for arm in arms:
        summary["fails"][arm.label] = sorted(i for i, f in results[arm.label].per_case.items() if f)
        if arm is not base:
            summary["vs_first"][arm.label] = mcnemar(results[base.label].per_case, results[arm.label].per_case, ids)
    parts = [f"# prompt_ab {mode}", "",
             f"- dataset: `{cfg['dataset']}` ({len(cases)} cases); model `{cfg['model']}`, grader `{cfg['judge_model']}`",
             f"- samples per case: {cfg['samples']} (a case fails if any sample fails); "
             f"temperature {cfg['temperature']}", ""]
    if mode == "implants":
        parts += ["Every arm against `none`. The noise floors repeat `none`: `none_repeat` in the same "
                  "order, `none_reversed` last and in reverse order, so the server's prompt cache holds "
                  "different neighbours. An implant's effect is what exceeds both floors.", "",
                  implant_table(cases, arms, prompts, answers, results)]
    for arm in arms[1:] if mode == "revisions" else []:
        report = cr.render_report(cases, results[base.label], results[arm.label],
                                  {**cfg, "candidate": f"{arm.label} vs {base.label}",
                                   "samples_per_case": cfg["samples"]})
        parts += [f"## {arm.label} vs {base.label}", "", report.split("\n", 2)[2]]
    (out / "report.md").write_text("\n".join(parts), encoding="utf-8")
    write_json_atomic(out / "summary.json", summary)
    return summary


# --------------------------------------------------------------------------- #
# Entry point
# --------------------------------------------------------------------------- #
async def run(args) -> int:
    from evals.runners._providers import get_provider, judge_env_default

    # The builder runs with its cwd in a worktree, so every path it gets is absolute.
    out: Path = args.out_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    dataset = Path(args.dataset).expanduser().resolve()
    agents_file = Path(args.agents).expanduser().resolve() if args.agents else None
    cases = cr.load_cases(dataset)
    provider = get_provider(args.provider)
    model = args.model or provider.default_model
    judge = args.judge_model or judge_env_default("JUDGE_MODEL", provider.name) or provider.default_judge_model
    temperature = os.environ.get("LOCAL_LLM_TEMPERATURE", "0") if provider.name == "local" else "provider default"
    if args.mode == "implants" and provider.name == "local" and float(temperature) != 0:
        raise SystemExit("implants mode needs greedy decoding: set LOCAL_LLM_TEMPERATURE=0")
    if args.samples > 1 and provider.name == "local" and float(temperature) == 0:
        raise SystemExit("--samples > 1 needs LOCAL_LLM_TEMPERATURE > 0: greedy decoding repeats the same answer")
    arms = ([parse_arm(s) for s in args.arm] if args.mode == "revisions"
            else implant_arms(args.rev, [n for n in args.implants.split(",") if n]))
    client = provider.make_async_client()
    log(f"mode={args.mode} provider={provider.name} model={model} grader={judge} cases={len(cases)} "
        f"arms={[a.label for a in arms]}")

    def free_answer_model():
        # Catalog and prompt builds load the embedding model; keep one model resident.
        if provider.name == "local":
            from evals.runners._providers import LOCAL_BASE_URL_ENV, LOCAL_DEFAULT_BASE_URL
            from evals.scripts.local_ab import unload
            unload(os.environ.get(LOCAL_BASE_URL_ENV, LOCAL_DEFAULT_BASE_URL).rstrip("/").removesuffix("/v1"), model)

    with Worktrees(REPO_ROOT) as trees:
        check_manifest(out / "manifest.json", {
            "mode": args.mode, "provider": provider.name, "model": model, "judge_model": judge,
            "temperature": temperature, "samples": args.samples, "dataset_sha256": sha256_file(dataset),
            "agents_sha256": sha256_file(agents_file) if agents_file else None,
            "arms": [{**asdict(a), "sha": trees.sha(a.rev)} for a in arms]})
        first = trees.get(arms[0].rev)
        catalog = out / "catalog.json"
        if agents_file is None and read_json(catalog) is None:
            free_answer_model()
            await run_builder(["--catalog-out", str(catalog)], first, builder_env({}))
        agents_path = agents_file or out / "agents.json"
        await pick_agents(cases, provider, client, model, catalog, agents_path)
        free_answer_model()
        prompts = {}
        for arm in arms:
            prompts[arm.label] = await build_prompts(trees.get(arm.rev), dataset, agents_path,
                                                     out / f"prompts_{arm.label}.json", arm)

    answers_path, grades_path = out / "answers.jsonl", out / "grades.jsonl"
    await answer_all(cases, arms, prompts, provider, client, model, args.samples, answers_path)
    await grade_all(cases, answers_path, grades_path, provider, client, judge)
    answers = read_jsonl(answers_path)
    results = arm_results(arms, read_jsonl(grades_path))
    cfg = {"dataset": str(dataset.relative_to(REPO_ROOT)) if dataset.is_relative_to(REPO_ROOT) else str(dataset),
           "provider": provider.name, "model": model, "judge_model": judge, "samples": args.samples,
           "temperature": temperature}
    summary = write_reports(args.mode, cases, arms, prompts, answers, results, cfg, out)
    print((out / "report.md").read_text(encoding="utf-8"))
    log(f"done: {out / 'report.md'}; fails per arm: { {k: len(v) for k, v in summary['fails'].items()} }")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("mode", choices=["revisions", "implants"])
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--dataset", default=str(DEFAULT_DATASET))
    p.add_argument("--provider", default="local", choices=["anthropic", "openai", "local"])
    p.add_argument("--model")
    p.add_argument("--judge-model")
    p.add_argument("--samples", type=int, default=1)
    p.add_argument("--agents", help="JSON map case id -> agent; default: picked by the answer model")
    p.add_argument("--arm", action="append", default=[], help="revisions mode: label=rev[:KEY=VAL,...] (repeat)")
    p.add_argument("--rev", default="HEAD", help="implants mode: revision whose prompts are varied")
    p.add_argument("--implants", default="RegressionFirst,CoV,IterBudget,StepBack,UncertaintyQ,LayerOfThoughts,LoT,VerifyAssumptions",
                   help="implants mode: comma-separated short names, each tested alone")
    args = p.parse_args(argv)
    if args.mode == "revisions" and len(args.arm) < 2:
        p.error("revisions mode needs at least two --arm")
    if args.mode == "implants" and args.samples != 1:
        p.error("implants mode runs one greedy sample per case")
    return args


def main(argv=None) -> int:
    cr.exit_on_termination_signals()
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
