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
or against hosted models through OpenRouter, with concurrent requests:
    OPENROUTER_PROVIDER=novita/bf16 python -m evals.scripts.prompt_ab implants \
        --provider openrouter --model google/gemma-4-31b-it --concurrency 8 --out-dir DIR
Hosts are not deterministic at temperature 0, so there the noise floors show
how far FAIL counts move by chance, and "answers changed" says nothing.

`--out-dir` holds the run's state. A manifest pins the model, grader,
temperature, dataset, each arm's resolved commit and a hash of the harness code
(HARNESS_FILES); a rerun with different settings, or after any edit to that
code, is refused, while new arms may be added to an existing run.
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
import re
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
# Code from this checkout, not the arms' revisions, that builds prompts, sends
# requests and grades answers, in a fixed order.
HARNESS_FILES = (Path(__file__).resolve(), BUILDER, Path(cr.__file__).resolve(),
                 REPO_ROOT / "evals/runners/_providers.py")
DEFAULT_DATASET = cr.DEFAULT_DATASET
MAX_TOKENS = 800  # default answer budget; a thinking model spends part of it reasoning
DEFAULT_EMBEDDING_MODEL = "intfloat/multilingual-e5-large"  # picks skills and implants in the builds
# Providers whose answers run at a temperature this script controls.
TEMPERATURE_ENV = {"local": "LOCAL_LLM_TEMPERATURE", "openrouter": "OPENROUTER_TEMPERATURE"}


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
    """Records of a JSONL state file.

    A kill during an append can leave a truncated last line with no newline; that
    fragment is dropped from the file so the run resumes. A complete last record
    that lost only its newline gets it back, or the next append would continue
    its line. Any other bad line is an error.
    """
    if not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    # Not splitlines(): it also splits at U+2028 and U+0085, which json.dumps
    # leaves unescaped inside an answer.
    lines = text.split("\n")
    rows = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            if number != len(lines) or text.endswith("\n"):
                raise
            log(f"dropping a truncated last record in {path}")
            path.write_text(text[:len(text) - len(line)], encoding="utf-8")
            return rows
    if text and not text.endswith("\n"):
        log(f"ending the last record in {path} with its missing newline")
        with path.open("a", encoding="utf-8") as fh:
            fh.write("\n")
    return rows


def append_jsonl(path: Path, record: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def write_json_atomic(path: Path, data: Any) -> None:
    """A killed write must never leave a truncated file that a resume trusts."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> Any | None:
    """Parsed content, or None when the file does not exist.

    A file that exists but cannot be parsed is refused rather than rebuilt:
    this script writes its state atomically, so such a file was damaged from
    outside. Rebuilding it would drop the fingerprint the answers and grades
    already in the directory were produced under.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        return json.loads(text)
    except ValueError as exc:
        raise SystemExit(f"{path} exists but is not valid JSON ({exc}); restore or delete it, "
                         "or use a new --out-dir") from exc


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def harness_sha256() -> str:
    """One hash over HARNESS_FILES, so a resume cannot mix records made by other code."""
    return hashlib.sha256("".join(sha256_file(p) for p in HARNESS_FILES).encode()).hexdigest()


def check_manifest(path: Path, current: dict[str, Any]) -> None:
    """Refuse to resume a run made with other settings; allow added arms."""
    old = read_json(path)
    if old is not None:
        # Runs made before --max-tokens existed used the fixed default. A missing
        # embedding model is checked per prompt file instead (build_prompts); request
        # settings, builder config and harness code of such a run cannot be
        # established, so it is not resumed.
        if ("request_settings" not in old and current.get("request_settings") is not None) or any(
                key not in old and key in current for key in ("builder_config", "harness_sha256")):
            raise SystemExit(f"{path.parent} was run before its settings were fully recorded; use a new --out-dir")
        for key in ("mode", "provider", "routing", "model", "judge_model", "temperature", "samples",
                    "max_tokens", "embedding_model", "request_settings", "builder_config", "harness_sha256",
                    "dataset_sha256", "agents_sha256"):
            if key == "embedding_model" and key not in old:
                continue
            default = MAX_TOKENS if key == "max_tokens" else None
            before, now = old.get(key, default), current.get(key, default)
            if before != now:
                raise SystemExit(f"{path.parent} was run with {key}={before!r}, now {now!r}; use a new --out-dir")
        old_arms = {a["label"]: a for a in old["arms"]}
        for arm in current["arms"]:
            if arm["label"] in old_arms and old_arms[arm["label"]] != arm:
                raise SystemExit(f"arm {arm['label']!r} changed since {path.parent} was run; use a new --out-dir")
        # Reports compare every arm with the first one, so the arms already run stay,
        # in their order, and the first of them stays first; new arms may come after
        # it, anywhere. The requested order is stored, so the same command resumes.
        kept = [a["label"] for a in current["arms"] if a["label"] in old_arms]
        if kept != list(old_arms):
            raise SystemExit(f"arms were removed or reordered since {path.parent} was run "
                             f"({list(old_arms)} -> {kept}); keep them in order or use a new --out-dir")
        baseline = next(iter(old_arms))
        if current["arms"][0]["label"] != baseline:
            raise SystemExit(f"arm {current['arms'][0]['label']!r} would replace {baseline!r} as the baseline of "
                             f"{path.parent}; add new arms after it or use a new --out-dir")
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


def builder_config() -> dict[str, str]:
    """Env settings the prompt builder's revision reads (src/engine/config.py), when set.

    The embedding model is recorded on its own; auto-update is forced off.
    """
    source = (REPO_ROOT / "src/engine/config.py").read_text(encoding="utf-8")
    names = set(re.findall(r'(?:getenv|environ\.get|_\w*env)\(\s*"([A-Z][A-Z0-9_]+)"', source))
    return {name: os.environ[name] for name in sorted(names)
            if name in os.environ and name != "EMBEDDING_MODEL" and not name.startswith("AGENTS_AUTO_UPDATE")}


def builder_env(extra: dict[str, str]) -> dict[str, str]:
    return {**os.environ, "LANGFUSE_TRACING_ENABLED": "false", "AGENTS_AUTO_UPDATE": "0",
            "EMBEDDING_MODEL": os.environ.get("EMBEDDING_MODEL") or DEFAULT_EMBEDDING_MODEL, **extra}


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
    env = builder_env(arm.env)
    if built is None:
        log(f"building prompts for {arm.label} ({arm.rev}, implants={arm.implants}, env={arm.env})")
        await run_builder(["--dataset", str(dataset), "--agents", str(agents), "--implants", arm.implants,
                           "--out", str(out)], root, env)
        built = read_json(out)
    # The embedding model picks skills and implants; the builder records the one it used.
    if built.get("embedding_model") != env["EMBEDDING_MODEL"]:
        raise SystemExit(f"{out} was built with embedding model {built.get('embedding_model')!r}, now "
                         f"{env['EMBEDDING_MODEL']!r}; use a new --out-dir")
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


def agents_pin(out: Path, agents_file: Path | None) -> str | None:
    """The agent map's hash for the manifest; None while a generated map is not pinned yet.

    run() pins a generated map right after writing it. A kill in between leaves a
    map the manifest does not know; it is adopted while nothing was built from it,
    since prompts are built only after the pin. Once prompts or answers exist the
    map's hash is returned, and check_manifest refuses it against the recorded None.
    """
    if agents_file is not None:
        return sha256_file(agents_file)
    generated = out / "agents.json"
    if not generated.exists():
        return None
    manifest = read_json(out / "manifest.json")
    if (manifest is not None and manifest.get("agents_sha256") is None
            and not any(out.glob("prompts_*.json")) and not (out / "answers.jsonl").exists()):
        return None
    return sha256_file(generated)


# --------------------------------------------------------------------------- #
# Answering and grading (resumable)
# --------------------------------------------------------------------------- #
async def bounded(jobs, limit: int) -> None:
    """Run zero-argument coroutine functions, at most `limit` at once.

    The first failure cancels the rest. Each job appends its own record when it
    finishes, so a failed or interrupted run keeps every completed record.
    """
    gate = asyncio.Semaphore(limit)

    async def one(job):
        async with gate:
            await job()

    tasks = [asyncio.ensure_future(one(job)) for job in jobs]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()


async def answer_all(cases, arms, prompts, provider, client, model, samples: int, path: Path,
                     concurrency: int = 1, max_tokens: int = MAX_TOKENS) -> None:
    done = {(r["arm"], r["id"], r["sample"]): r for r in read_jsonl(path)}

    def record(arm, cid, i, answer, reused):
        rec = {"arm": arm.label, "id": cid, "sample": i, "answer": answer, "reused_from": reused}
        append_jsonl(path, rec)
        done[(arm.label, cid, i)] = rec

    def answer(arm, case, i, prompt):
        async def job():
            # The seed follows the persisted identity, so a resumed run regenerates
            # a missing sample with its own seed, not a finished sample's.
            text = (await provider.complete(client, model, case["query"], prompt, max_tokens,
                                            seed_key=f"{arm.label}:{case['id']}:{i}"))[0]
            record(arm, case["id"], i, text, None)
        return job

    # Arm by arm: a later arm may reuse an earlier arm's answers, so each arm
    # starts once the previous one is complete. Within an arm the calls run
    # concurrently, in the arm's case order when `concurrency` is 1.
    for arm in arms:
        jobs = []
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
                    record(arm, cid, i, done[(twin.label, cid, i)]["answer"], twin.label)
                else:
                    jobs.append(answer(arm, case, i, prompt))
        await bounded(jobs, concurrency)
        log(f"answered {arm.label}")


async def grade_all(cases, answers_path: Path, grades_path: Path, provider, client, judge_model,
                    concurrency: int = 1) -> None:
    by_id = {c["id"]: c for c in cases}
    done = {(r["arm"], r["id"], r["sample"]) for r in read_jsonl(grades_path)}

    def grade(record):
        async def job():
            det, verdict, why = await cr.grade_sample(provider, client, judge_model, by_id[record["id"]],
                                                      record["answer"])
            append_jsonl(grades_path, {"arm": record["arm"], "id": record["id"], "sample": record["sample"],
                                       "deterministic": det, "verdict": verdict, "reason": why})
        return job

    await bounded([grade(r) for r in read_jsonl(answers_path) if (r["arm"], r["id"], r["sample"]) not in done],
                  concurrency)


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
             f"temperature {cfg['temperature']}"]
    if cfg.get("routing"):
        parts.append(f"- provider `{cfg['provider']}`, routing `{json.dumps(cfg['routing'])}`")
    parts.append("")
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
    from evals.runners._providers import (
        get_provider, judge_env_default, missing_credentials, openrouter_routing, request_settings,
    )

    # The builder runs with its cwd in a worktree, so every path it gets is absolute.
    out: Path = args.out_dir.expanduser().resolve()
    out.mkdir(parents=True, exist_ok=True)
    dataset = Path(args.dataset).expanduser().resolve()
    agents_file = Path(args.agents).expanduser().resolve() if args.agents else None
    cases = cr.load_cases(dataset)
    provider = get_provider(args.provider)
    if missing := missing_credentials(provider):
        raise SystemExit(f"--provider {provider.name} needs {missing}")
    if provider.name == "local" and args.concurrency > 1:
        # One model on one laptop answers one request at a time, and the noise
        # floors rely on a fixed request order.
        raise SystemExit("--concurrency > 1 is for hosted providers; a local server runs one request at a time")
    model = args.model or provider.default_model
    judge = args.judge_model or judge_env_default("JUDGE_MODEL", provider.name) or provider.default_judge_model
    temperature_env = TEMPERATURE_ENV.get(provider.name)
    temperature = os.environ.get(temperature_env, "0") if temperature_env else "provider default"
    greedy = temperature == "default" or (temperature_env is not None and float(temperature) == 0)
    if args.mode == "implants" and temperature_env and not greedy:
        raise SystemExit(f"implants mode needs temperature 0: set {temperature_env}=0")
    if provider.name == "local":
        # Only a local server repeats itself at temperature 0; hosted models vary
        # anyway, so repeated samples measure how often a case fails.
        if args.samples > 1 and greedy:
            raise SystemExit(f"--samples > 1 needs {temperature_env} > 0: greedy decoding repeats the same answer")
        if args.mode == "implants" and args.samples != 1:
            raise SystemExit("implants mode on a local server runs one greedy sample per case")
    routing = openrouter_routing() if provider.name == "openrouter" else None
    arms = ([parse_arm(s) for s in args.arm] if args.mode == "revisions"
            else implant_arms(args.rev, [n for n in args.implants.split(",") if n]))
    client = provider.make_async_client()
    log(f"mode={args.mode} provider={provider.name} model={model} grader={judge} cases={len(cases)} "
        f"arms={[a.label for a in arms]}" + (f" routing={routing}" if routing else ""))

    def free_answer_model():
        # Catalog and prompt builds load the embedding model; keep one model resident.
        if provider.name == "local":
            from evals.runners._providers import LOCAL_BASE_URL_ENV, LOCAL_DEFAULT_BASE_URL
            from evals.scripts.local_ab import unload
            unload(os.environ.get(LOCAL_BASE_URL_ENV, LOCAL_DEFAULT_BASE_URL).rstrip("/").removesuffix("/v1"), model)

    generated = out / "agents.json"
    with Worktrees(REPO_ROOT) as trees:
        # Resolve each revision once: a branch that moves mid-run must not build
        # prompts from a commit other than the one the manifest records.
        shas = {a.label: trees.sha(a.rev) for a in arms}
        check_manifest(out / "manifest.json", {
            "mode": args.mode, "provider": provider.name, "routing": routing, "model": model, "judge_model": judge,
            "temperature": temperature, "samples": args.samples, "max_tokens": args.max_tokens,
            "embedding_model": builder_env({})["EMBEDDING_MODEL"], "request_settings": request_settings(provider.name),
            "builder_config": builder_config(), "harness_sha256": harness_sha256(),
            "dataset_sha256": sha256_file(dataset),
            # A generated agent map is pinned too, once it exists (see below).
            "agents_sha256": agents_pin(out, agents_file),
            "arms": [{**asdict(a), "sha": shas[a.label]} for a in arms]})
        first = trees.get(shas[arms[0].label])
        catalog = out / "catalog.json"
        if agents_file is None and read_json(catalog) is None:
            free_answer_model()
            await run_builder(["--catalog-out", str(catalog)], first, builder_env({}))
        agents_path = agents_file or out / "agents.json"
        agents = await pick_agents(cases, provider, client, model, catalog, agents_path)
        if agents_file is None:
            manifest = read_json(out / "manifest.json")
            if manifest.get("agents_sha256") is None:
                write_json_atomic(out / "manifest.json", {**manifest, "agents_sha256": sha256_file(generated)})
        # A case without an agent would be routed by each revision on its own.
        if missing := [c["id"] for c in cases if c["id"] not in agents]:
            raise SystemExit(f"{agents_path} has no agent for {len(missing)} cases, e.g. {missing[:5]}")
        free_answer_model()
        prompts = {}
        for arm in arms:
            prompts[arm.label] = await build_prompts(trees.get(shas[arm.label]), dataset, agents_path,
                                                     out / f"prompts_{arm.label}.json", arm)

    answers_path, grades_path = out / "answers.jsonl", out / "grades.jsonl"
    await answer_all(cases, arms, prompts, provider, client, model, args.samples, answers_path, args.concurrency,
                     args.max_tokens)
    await grade_all(cases, answers_path, grades_path, provider, client, judge, args.concurrency)
    answers = read_jsonl(answers_path)
    results = arm_results(arms, read_jsonl(grades_path))
    cfg = {"dataset": str(dataset.relative_to(REPO_ROOT)) if dataset.is_relative_to(REPO_ROOT) else str(dataset),
           "provider": provider.name, "routing": routing, "model": model, "judge_model": judge,
           "samples": args.samples, "temperature": temperature}
    summary = write_reports(args.mode, cases, arms, prompts, answers, results, cfg, out)
    print((out / "report.md").read_text(encoding="utf-8"))
    log(f"done: {out / 'report.md'}; fails per arm: { {k: len(v) for k, v in summary['fails'].items()} }")
    return 0


def parse_args(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("mode", choices=["revisions", "implants"])
    p.add_argument("--out-dir", type=Path, required=True)
    p.add_argument("--dataset", default=str(DEFAULT_DATASET))
    p.add_argument("--provider", default="local", choices=["anthropic", "openai", "local", "openrouter"])
    p.add_argument("--model")
    p.add_argument("--judge-model")
    p.add_argument("--samples", type=int, default=1)
    p.add_argument("--concurrency", type=int, default=1, help="parallel requests; hosted providers only")
    p.add_argument("--max-tokens", type=int, default=MAX_TOKENS,
                   help="answer budget; raise it for thinking models, whose reasoning counts against it")
    p.add_argument("--agents", help="JSON map case id -> agent; default: picked by the answer model")
    p.add_argument("--arm", action="append", default=[], help="revisions mode: label=rev[:KEY=VAL,...] (repeat)")
    p.add_argument("--rev", default="HEAD", help="implants mode: revision whose prompts are varied")
    p.add_argument("--implants", default="RegressionFirst,CoV,IterBudget,StepBack,UncertaintyQ,LayerOfThoughts,LoT,VerifyAssumptions",
                   help="implants mode: comma-separated short names, each tested alone")
    args = p.parse_args(argv)
    if args.mode == "revisions" and len(args.arm) < 2:
        p.error("revisions mode needs at least two --arm")
    if args.concurrency < 1:
        p.error("--concurrency must be at least 1")
    if args.samples < 1:
        p.error("--samples must be at least 1")
    # Labels key the prompts, commits and resumable records: a repeat, or an implant
    # named like a built-in arm (none, production, ...), would overwrite another arm.
    try:
        labels = [a.label for a in ([parse_arm(s) for s in args.arm] if args.mode == "revisions"
                                    else implant_arms(args.rev, [n for n in args.implants.split(",") if n]))]
    except ValueError as error:
        p.error(str(error))
    if repeated := sorted({label for label in labels if labels.count(label) > 1}):
        p.error(f"arm labels must be unique; repeated or reserved: {repeated}")
    return args


def main(argv=None) -> int:
    cr.exit_on_termination_signals()
    return asyncio.run(run(parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
