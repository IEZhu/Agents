"""Answer and judge an ablation run with a model served by OpenRouter.

    python evals/ablation/hosted.py answer RUN_DIR --model MODEL [--concurrency N]
    python evals/ablation/build_judges.py RUN_DIR
    python evals/ablation/hosted.py judge RUN_DIR --model MODEL [--concurrency N] [--reasoning low]
    python evals/ablation/aggregate.py RUN_DIR

The cloud runbook (README.md) answers and judges with Claude Code agents through
workflows/answers.js and workflows/judges.js. These two steps do the same for a
hosted model, from the same ctx/ and judge/ files, so one set of contexts can be
answered by several models and their verdicts aggregate the same way.

`answer` sends the context's operating instructions as the system prompt and the
case's conversation as chat turns. `judge` sends the criteria, verdict example and
allowed values of workflows/judges.js with the judge file inline, without tools, and
keeps a verdict only when aggregate.py would accept it. Both skip finished files, so
an interrupted run resumes. RUN_DIR/hosted.json records each step's model and
request settings; a step that already has outputs made with other settings is
refused, so one run never mixes them.

Environment: OPENROUTER_API_KEY; OPENROUTER_PROVIDER pins the endpoints, as for
prompt_ab (e.g. deepinfra/bf16,google-ai-studio); OPENROUTER_REASONING sets the
answers' reasoning effort; OPENROUTER_TEMPERATURE and OPENROUTER_SEED are described
in evals/runners/_providers.py. Judges run at temperature 0 with --reasoning.
"""
import argparse
import asyncio
import importlib.util
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[1]
HEADER = "# Operating context loaded for this conversation\n"
REASONING = ("off", "low", "medium", "high")


def _module(name: str):
    """A sibling script loaded by path: evals/ablation is not a package."""
    spec = importlib.util.spec_from_file_location(f"ablation_{name}", HERE / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aggregate = _module("aggregate")
build_contexts = _module("build_contexts")


def load_cases(run_dir: Path) -> dict:
    cases = {}
    for path in (run_dir / "cases").glob("*.json"):
        spec = json.loads(path.read_text())
        for case in spec.get("cases", []):
            cases[(spec["component"], case["id"])] = case
    return cases


def split_context(text: str, case: dict) -> tuple[str, list[dict]]:
    """The system prompt and chat turns that a ctx file was built from."""
    tail = "\n\n" + build_contexts.conversation_block(case)
    if not text.startswith(HEADER) or not text.endswith(tail):
        raise ValueError("context does not match its case; rebuild it with build_contexts.py")
    turns = [{"role": t["role"], "content": t["content"]} for t in case.get("history") or []]
    return text[len(HEADER):-len(tail)], turns + [{"role": "user", "content": case["user_message"]}]


def judge_instructions() -> str:
    """The criteria, example and allowed values of workflows/judges.js, for a tool-less judge."""
    js = (HERE / "workflows" / "judges.js").read_text(encoding="utf-8")
    criteria = re.search(r"^The file has a conversation.*?Judge the answers only as written\.$", js, re.M | re.S)
    example = re.search(r'^\{"winner".*\}$', js, re.M)
    allowed = re.search(r"^Allowed values:.*$", js, re.M)
    if not (criteria and example and allowed):
        raise SystemExit("workflows/judges.js changed shape; update judge_instructions() in hosted.py")
    return "\n\n".join([
        "You are a strict, impartial evaluator. The judge file is inside <file> tags below.",
        "Your own knowledge may end before recent laws, releases or prices. Never mark a fact "
        "wrong only because you do not recognise it.",
        criteria.group(0),
        "Reply with only one JSON object and no other text. It must be valid JSON shaped like this "
        "example (the values here are placeholders, not a verdict):\n" + example.group(0),
        allowed.group(0)])


def parse_verdict(text: str, rubric_size: int) -> dict:
    """A verdict object aggregate.py accepts, with each rubric item judged once; else ValueError."""
    body = re.sub(r"^```(?:json)?\s*", "", text.strip())
    start = body.find("{")
    if start < 0:
        raise ValueError("no JSON object in the reply")
    # The first object only: some models append a closing fence, notes or a second object.
    verdict, _ = json.JSONDecoder().raw_decode(body[start:])
    with tempfile.TemporaryDirectory() as tmp:
        probe = Path(tmp) / "verdict.json"
        probe.write_text(json.dumps(verdict))
        aggregate.read_verdict(probe)
    if sorted(r["item"] for r in verdict["rubric"]) != list(range(1, rubric_size + 1)):
        raise ValueError(f"rubric items do not cover items 1..{rubric_size} once each")
    return verdict


def write_atomic(path: Path, text: str) -> None:
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def record_step(run_dir: Path, step: str, settings: dict, has_outputs: bool) -> None:
    """Pin the step's settings in hosted.json; refuse a step whose outputs used others."""
    path = run_dir / "hosted.json"
    meta = json.loads(path.read_text()) if path.exists() else {}
    if has_outputs and meta.get(step) not in (None, settings):
        raise SystemExit(f"{run_dir} already has {step} outputs made with {meta[step]}; "
                         f"use a new run directory for {settings}")
    if has_outputs and step not in meta:
        raise SystemExit(f"{run_dir} has {step} outputs from another source; use a new run directory")
    meta[step] = settings
    path.write_text(json.dumps(meta, indent=1) + "\n")


async def _bounded(jobs, concurrency: int) -> list:
    gate = asyncio.Semaphore(concurrency)

    async def run(job):
        async with gate:
            return await job

    return await asyncio.gather(*(run(job) for job in jobs))


async def _attempt(label: str, attempts: int, call):
    """Run `call` up to `attempts` times; None after the last failure, which is logged."""
    for attempt in range(1, attempts + 1):
        try:
            return await call()
        except Exception as exc:  # one bad item must not stop the batch
            print(f"{label}: attempt {attempt}/{attempts} failed: {exc!r}", flush=True)
    return None


async def answer_run(run_dir: Path, chat, *, concurrency: int, attempts: int) -> int:
    """Write answers/<token>.md for every planned context without one; return the failure count."""
    plan = json.loads((run_dir / "plan.json").read_text())
    cases = load_cases(run_dir)
    (run_dir / "answers").mkdir(exist_ok=True)
    todo = [t for t in sorted(plan) if not (run_dir / "answers" / f"{t}.md").exists()]

    async def one(token: str) -> bool:
        p = plan[token]
        system, messages = split_context((run_dir / "ctx" / f"{token}.md").read_text(encoding="utf-8"),
                                         cases[(p["component"], p["case"])])

        async def call():
            text = await chat([{"role": "system", "content": system}, *messages], seed_key=token)
            if not text.strip():
                raise ValueError("empty answer")
            return text

        text = await _attempt(f"answer {token}", attempts, call)
        if text is not None:
            write_atomic(run_dir / "answers" / f"{token}.md", text.strip() + "\n")
        return text is not None

    done = await _bounded([one(t) for t in todo], concurrency)
    print(f"{sum(done)} answers written, {len(plan) - len(todo)} already there, {done.count(False)} failed")
    return done.count(False)


async def judge_run(run_dir: Path, chat, *, concurrency: int, attempts: int) -> int:
    """Write judge/<stem>.verdict.json for every planned judge file without one; return the failure count."""
    plan = json.loads((run_dir / "judge_plan.json").read_text())
    cases = load_cases(run_dir)
    instructions = judge_instructions()
    todo = [s for s in sorted(plan) if not (run_dir / "judge" / f"{s}.verdict.json").exists()]

    async def one(stem: str) -> bool:
        p = plan[stem]
        size = len(cases[(p["component"], p["case"])]["rubric"])
        body = (run_dir / "judge" / f"{stem}.md").read_text(encoding="utf-8")
        prompt = f"{instructions}\n\n<file>\n{body}</file>"

        async def call():
            return parse_verdict(await chat([{"role": "user", "content": prompt}], seed_key=stem), size)

        verdict = await _attempt(f"judge {stem}", attempts, call)
        if verdict is not None:
            write_atomic(run_dir / "judge" / f"{stem}.verdict.json", json.dumps(verdict, ensure_ascii=False) + "\n")
        return verdict is not None

    done = await _bounded([one(s) for s in todo], concurrency)
    print(f"{sum(done)} verdicts written, {len(plan) - len(todo)} already there, {done.count(False)} failed")
    return done.count(False)


def openrouter_chat(model: str, max_tokens: int, *, sample: bool, reasoning: str | None):
    """A chat(messages, seed_key) coroutine function on OpenRouter, and its settings."""
    sys.path.insert(0, str(ROOT))
    from evals.runners import _providers as p

    provider = p.get_provider("openrouter")
    if missing := p.missing_credentials(provider):
        raise SystemExit(f"OpenRouter needs {missing}")
    routing = p.openrouter_routing()
    if not routing.get("order"):
        raise SystemExit("OPENROUTER_PROVIDER must pin the endpoints (e.g. deepinfra/bf16), as for prompt_ab")
    client = provider.make_async_client()

    async def chat(messages: list[dict], seed_key: str) -> str:
        kwargs = p._openrouter_request(model, messages, max_tokens, sample, seed_key)
        if reasoning is not None:
            kwargs["extra_body"]["reasoning"] = (
                {"enabled": False} if reasoning == "off" else {"effort": reasoning, "exclude": True})
        text = p._openrouter_text(await p._openrouter_create(client, kwargs), "completion")
        if p.has_harness_artifacts(text):
            raise p.ContaminatedResponseError("reply leaked agentic-harness scaffolding")
        return text

    temperature = (os.getenv("OPENROUTER_TEMPERATURE", "0") if sample
                   else os.getenv("OPENROUTER_GRADER_TEMPERATURE", "0"))
    settings = {"model": model, "max_tokens": max_tokens, "temperature": temperature, "routing": routing,
                "request_settings": p.request_settings("openrouter")}
    if reasoning is not None:
        settings["reasoning"] = reasoning
    return chat, settings


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    parser.add_argument("step", choices=("answer", "judge"))
    parser.add_argument("run_dir", type=Path)
    parser.add_argument("--model", required=True)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--max-tokens", type=int, help="default 12000 for answers, 8000 for verdicts")
    parser.add_argument("--reasoning", choices=REASONING, default="low",
                        help="judge reasoning effort (answers use OPENROUTER_REASONING)")
    args = parser.parse_args(argv)
    run_dir = args.run_dir.resolve()
    if args.step == "answer":
        chat, settings = openrouter_chat(args.model, args.max_tokens or 12000, sample=True, reasoning=None)
        record_step(run_dir, "answer", settings, any((run_dir / "answers").glob("*.md")))
        failed = asyncio.run(answer_run(run_dir, chat, concurrency=args.concurrency, attempts=args.attempts))
    else:
        chat, settings = openrouter_chat(args.model, args.max_tokens or 8000, sample=False, reasoning=args.reasoning)
        record_step(run_dir, "judge", settings, any((run_dir / "judge").glob("*.verdict.json")))
        failed = asyncio.run(judge_run(run_dir, chat, concurrency=args.concurrency, attempts=args.attempts))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
