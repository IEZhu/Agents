"""Run the rule A/B on local models with one command.

Starts a memory-lean Ollama server if none is listening, pulls any missing
model, checks the server can actually run both models, runs
``evals.scripts.compare_rules --provider local`` with the answer and judge
models while watching free memory, then stops the server it started (or
unloads the models from a server it reused).

    python -m evals.scripts.local_ab                     # gemma answers, qwen judges
    python -m evals.scripts.local_ab --plan              # print what would happen
    python -m evals.scripts.local_ab --samples 3 --temperature 0.7

Needs Ollama: `ollama` on PATH, or else `nix run nixpkgs#ollama`, so nothing
has to be installed on a machine with nix. For another OpenAI-compatible
server, run compare_rules by hand (evals/LOCAL_MODELS.md).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

REPO_ROOT = Path(__file__).resolve().parents[2]
REPORTS = REPO_ROOT / "evals" / "reports"
FIXTURES = REPO_ROOT / "evals" / "fixtures"

DEFAULT_ANSWER_MODEL = "gemma4:31b-it-qat"
DEFAULT_JUDGE_MODEL = "qwen3.8:27b"
DEFAULT_PORT = 11435
DEFAULT_CONTEXT = 12288
GUARD_EXIT_CODE = 3
GUARD_INTERVAL_S = 10
GUARD_STRIKES = 3
# compare_rules swaps a fixture over the tracked rule file while it builds
# prompts; it needs a moment after SIGINT to put the original back.
CHILD_GRACE_S = 30

# One model in memory at a time, one request at a time, and a quantized KV cache:
# on a 36 GB machine this kept free memory at or above 11% through two runs
# (46 and 93 min) with a 19 GB answer model and an 18 GB judge.
LEAN_SERVER_ENV = {
    "OLLAMA_KEEP_ALIVE": "10m",
    "OLLAMA_MAX_LOADED_MODELS": "1",
    "OLLAMA_NUM_PARALLEL": "1",
    "OLLAMA_FLASH_ATTENTION": "1",
    "OLLAMA_KV_CACHE_TYPE": "q8_0",
}


def log(msg: str) -> None:
    print(f"[local_ab {time.strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# Ollama HTTP helpers
# --------------------------------------------------------------------------- #


def _request(url: str, payload: dict | None = None, timeout: float | None = 10.0):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


def server_up(base: str) -> bool:
    try:
        with _request(f"{base}/api/version", timeout=2) as r:
            return r.status == 200
    except (urllib.error.URLError, OSError):
        return False


def installed_models(base: str) -> set[str]:
    with _request(f"{base}/api/tags") as r:
        return {m["name"] for m in json.load(r).get("models", [])}


def missing_models(wanted: list[str], installed: set[str]) -> list[str]:
    """Models in *wanted* that are not installed; a bare name matches its `:latest` tag."""
    have = installed | {n.removesuffix(":latest") for n in installed}
    return [m for m in dict.fromkeys(wanted) if m not in have]


def pull(base: str, model: str) -> None:
    log(f"pulling {model} (this can take a while)")
    last = -10
    with _request(f"{base}/api/pull", {"model": model, "stream": True}, timeout=None) as r:
        for line in r:
            event = json.loads(line or b"{}")
            if "error" in event:
                raise RuntimeError(f"pull {model} failed: {event['error']}")
            total, done = event.get("total"), event.get("completed")
            if total and done is not None:
                pct = int(100 * done / total)
                if pct >= last + 10:
                    log(f"  {model}: {pct}%")
                    last = pct
    log(f"pulled {model}")


def can_generate(base: str, model: str) -> tuple[bool, str]:
    """Load *model* and produce one token.

    A server can answer /api/version and still be unable to run anything — an
    `ollama serve` left running after its install was removed fails every
    generation with "llama-server binary not found".
    """
    try:
        with _request(
            f"{base}/api/generate",
            {"model": model, "prompt": "hi", "stream": False, "options": {"num_predict": 1}},
            timeout=600,
        ) as r:
            json.load(r)
        return True, ""
    except urllib.error.HTTPError as exc:
        return False, exc.read().decode(errors="replace")[:300]
    except (urllib.error.URLError, OSError) as exc:
        return False, str(exc)


def preflight(
    base: str,
    answer_model: str,
    judge_model: str,
    check: Callable[[str, str], tuple[bool, str]] | None = None,
    drop: Callable[[str, str], None] | None = None,
) -> tuple[str, str] | None:
    """Check that both models generate; return (model, error) for the first that can't.

    The local A/B generates every answer before its first grade, so a judge
    that can't load would otherwise fail only after the whole answer phase.
    The judge goes first and is unloaded, leaving the answer model in memory
    for the run.
    """
    check = check or can_generate
    drop = drop or unload
    for model in dict.fromkeys([judge_model, answer_model]):
        ok, why = check(base, model)
        if not ok:
            return model, why
        if model != answer_model:
            drop(base, model)
    return None


def unload(base: str, model: str) -> None:
    try:
        with _request(f"{base}/api/generate", {"model": model, "keep_alive": 0}, timeout=60):
            pass
    except (urllib.error.URLError, OSError) as exc:
        log(f"could not unload {model}: {exc}")


# --------------------------------------------------------------------------- #
# Process lifecycle
# --------------------------------------------------------------------------- #


def runtime_command(which: Callable[[str], str | None] = shutil.which) -> list[str] | None:
    """Command that runs `ollama serve`: an installed binary, else an ephemeral nix one."""
    if which("ollama"):
        return ["ollama", "serve"]
    if which("nix"):
        return ["nix", "run", "nixpkgs#ollama", "--", "serve"]
    return None


def server_env(port: int, context: int, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.update(LEAN_SERVER_ENV)
    env["OLLAMA_HOST"] = f"127.0.0.1:{port}"
    env["OLLAMA_CONTEXT_LENGTH"] = str(context)
    return env


def launch_server(cmd: list[str], env: dict[str, str], log_path: Path) -> subprocess.Popen:
    log(f"starting {' '.join(cmd)} (log: {log_path})")
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("ab") as out:  # the child keeps its own copy of the descriptor
        return subprocess.Popen(
            cmd, env=env, stdout=out, stderr=subprocess.STDOUT,
            start_new_session=True,  # own process group, so the runner it spawns is stopped too
        )


def wait_for_server(proc: subprocess.Popen, base: str, log_path: Path, wait_s: float = 600) -> None:
    deadline = time.monotonic() + wait_s  # a first `nix run` downloads Ollama
    while time.monotonic() < deadline:
        if server_up(base):
            log("server is up")
            return
        if proc.poll() is not None:
            raise RuntimeError(f"ollama exited with code {proc.returncode}; see {log_path}")
        time.sleep(1)
    raise RuntimeError(f"ollama did not answer within {wait_s:.0f}s; see {log_path}")


def stop_process_group(proc: subprocess.Popen, grace_s: float = 15) -> None:
    """Stop *proc* and everything in its process group (nix wrapper, server, runners)."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        pass  # group gone, or (macOS) only zombies left, where killpg fails with EPERM
    try:
        proc.wait(timeout=grace_s)
    except subprocess.TimeoutExpired:
        pass
    try:  # the leader can exit before its children; take the rest of the group down
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


# The memory guard's thread and the main thread's interrupt path can both stop
# the child. A second SIGINT could interrupt compare_rules while it restores the
# live rule file, so the stop runs once and a later caller waits for it.
_STOP_CHILD_LOCK = threading.Lock()


def stop_child(child: subprocess.Popen, grace_s: float = CHILD_GRACE_S) -> None:
    """Ask compare_rules to stop the way Ctrl+C does, so it restores the rule file first."""
    with _STOP_CHILD_LOCK:
        if child.poll() is not None:
            return
        child.send_signal(signal.SIGINT)
        try:
            child.wait(timeout=grace_s)
            return
        except subprocess.TimeoutExpired:
            pass
        child.terminate()
        try:
            child.wait(timeout=10)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()


def _raise_interrupt(signum, _frame):
    raise KeyboardInterrupt(f"signal {signum}")


# --------------------------------------------------------------------------- #
# Memory guard
# --------------------------------------------------------------------------- #

_MACOS_FREE = re.compile(r"System-wide memory free percentage:\s*(\d+)%")


def parse_macos_free_pct(text: str) -> int | None:
    m = _MACOS_FREE.search(text)
    return int(m.group(1)) if m else None


def parse_linux_free_pct(meminfo: str) -> int | None:
    values = {}
    for line in meminfo.splitlines():
        key, _, rest = line.partition(":")
        if key in ("MemTotal", "MemAvailable") and rest.split():
            values[key] = int(rest.split()[0])
    total, available = values.get("MemTotal"), values.get("MemAvailable")
    if not total or available is None:
        return None  # no MemAvailable (old kernels, some containers): unknown, not 0% free
    return int(100 * available / total)


def read_free_pct() -> int | None:
    """Share of memory the system reports as free/available, or None if unknown."""
    try:
        if sys.platform == "darwin" and shutil.which("memory_pressure"):
            out = subprocess.run(["memory_pressure"], capture_output=True, text=True, timeout=30).stdout
            return parse_macos_free_pct(out)
        return parse_linux_free_pct(Path("/proc/meminfo").read_text())
    except (OSError, subprocess.SubprocessError, ValueError):
        return None


@dataclass
class MemoryGuard:
    """Calls *on_trip* once free memory is below *min_pct* on *strikes* reads in a row."""

    min_pct: int
    on_trip: Callable[[int], None]
    read: Callable[[], int | None] = read_free_pct
    strikes: int = GUARD_STRIKES
    tripped: bool = False
    lowest: int | None = None
    _low: int = field(default=0, repr=False)

    def check(self) -> None:
        free = self.read()
        if free is None or self.tripped:
            return
        self.lowest = free if self.lowest is None else min(self.lowest, free)
        self._low = self._low + 1 if free < self.min_pct else 0
        if self._low >= self.strikes:
            self.tripped = True
            self.on_trip(free)

    def run(self, stop: threading.Event, interval_s: float = GUARD_INTERVAL_S) -> None:
        while not stop.wait(interval_s):
            try:
                self.check()
            except Exception as exc:  # keep guarding; one bad read must not end the watch
                log(f"memory guard: check failed ({exc!r}); still watching")


# --------------------------------------------------------------------------- #
# A/B run
# --------------------------------------------------------------------------- #


def ab_command(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable, "-m", "evals.scripts.compare_rules", "--provider", "local",
        "--baseline-rule", args.baseline_rule, "--candidate-rule", args.candidate_rule,
        "--samples-per-case", str(args.samples), "--out", args.out,
    ]
    if args.dataset:
        cmd += ["--dataset", args.dataset]
    return cmd


def ab_env(args: argparse.Namespace, base: str, base_env: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    env.update({
        "LOCAL_LLM_BASE_URL": f"{base}/v1",
        "LOCAL_LLM_MODEL": args.answer_model,
        "LOCAL_LLM_JUDGE_MODEL": args.judge_model,
        "LOCAL_LLM_TEMPERATURE": str(args.temperature),
        "LANGFUSE_TRACING_ENABLED": env.get("LANGFUSE_TRACING_ENABLED", "false"),
    })
    return env


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="local_ab", description=__doc__.splitlines()[0])
    p.add_argument("--answer-model", default=DEFAULT_ANSWER_MODEL)
    p.add_argument("--judge-model", default=DEFAULT_JUDGE_MODEL)
    p.add_argument("--port", type=int, default=DEFAULT_PORT)
    p.add_argument("--context", type=int, default=DEFAULT_CONTEXT, help="OLLAMA_CONTEXT_LENGTH for a server this script starts")
    p.add_argument("--samples", type=int, default=1, help="samples per case")
    p.add_argument("--temperature", type=float, default=0.0, help="use > 0 with --samples > 1")
    p.add_argument("--baseline-rule", default=str(FIXTURES / "rule-no-fabrication.pre-compression.mdc"))
    p.add_argument("--candidate-rule", default=str(FIXTURES / "rule-no-fabrication.compressed.mdc"))
    p.add_argument("--dataset", default=None, help="default: compare_rules' no-fabrication set")
    p.add_argument("--out", default=None, help="report path (default: evals/reports/local_ab_<answer>_<judge>.md)")
    p.add_argument("--min-free-pct", type=int, default=5,
                   help=f"stop the run if free memory is below this on {GUARD_STRIKES} checks "
                        f"{GUARD_INTERVAL_S} s apart (%%)")
    p.add_argument("--keep-server", action="store_true", help="leave a server this script started running")
    p.add_argument("--plan", action="store_true", help="print the plan and exit without starting anything")
    args = p.parse_args(argv)
    if args.samples < 1:
        p.error("--samples must be at least 1")
    if args.temperature < 0:
        p.error("--temperature must not be negative")
    if args.samples > 1 and args.temperature <= 0:
        p.error("--samples > 1 needs --temperature > 0: greedy decoding repeats the same answer")
    if args.out is None:
        slug = "_".join(re.sub(r"[^A-Za-z0-9.]+", "-", m) for m in (args.answer_model, args.judge_model))
        args.out = str(REPORTS / f"local_ab_{slug}.md")
    # The child runs from the repo root, so pin paths to where the caller is.
    for name in ("baseline_rule", "candidate_rule", "dataset", "out"):
        value = getattr(args, name)
        if value:
            setattr(args, name, str(Path(value).expanduser().resolve()))
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if sys.platform == "win32":
        # SIGHUP, os.killpg and start_new_session have no Windows equivalent here.
        log("local_ab needs POSIX signals and process groups; run it on Linux or macOS")
        return 2
    base = f"http://127.0.0.1:{args.port}"
    models = [args.answer_model, args.judge_model]
    running = server_up(base)
    runtime = None if running else runtime_command()

    if args.plan:
        print(f"server:  {'reuse ' + base if running else ' '.join(runtime or ['<no ollama or nix found>'])}")
        print(f"models:  answer={args.answer_model} judge={args.judge_model} (pulled if missing)")
        print(f"run:     {' '.join(ab_command(args))}")
        print(f"guard:   stop if free memory < {args.min_free_pct}% on {GUARD_STRIKES} checks {GUARD_INTERVAL_S} s apart")
        return 0
    if not running and runtime is None:
        log("no Ollama server on this port and neither `ollama` nor `nix` is available. "
            "local_ab needs Ollama; for another OpenAI-compatible server run compare_rules "
            "by hand (evals/LOCAL_MODELS.md, 'Run an eval').")
        return 2

    # kill, a timeout wrapper or a closed terminal take the same cleanup path as Ctrl+C.
    for sig in (signal.SIGTERM, signal.SIGHUP):
        signal.signal(sig, _raise_interrupt)

    server: subprocess.Popen | None = None
    child: subprocess.Popen | None = None
    stop = threading.Event()
    log_path = REPORTS / "local_ab_ollama.log"

    def trip(free: int) -> None:
        log(f"MEMORY GUARD: free memory {free}% < {args.min_free_pct}% on {GUARD_STRIKES} checks in a row — stopping the run")
        if child is not None:
            stop_child(child)

    guard = MemoryGuard(min_pct=args.min_free_pct, on_trip=trip)
    code = 1
    try:
        if running:
            log(f"reusing the server already on {base} (it keeps its own context/memory settings)")
        else:
            server = launch_server(runtime, server_env(args.port, args.context), log_path)
            wait_for_server(server, base, log_path)
        for model in missing_models(models, installed_models(base)):
            pull(base, model)
        failed = preflight(base, args.answer_model, args.judge_model)
        if failed is not None:
            model, why = failed
            log(f"the server on {base} can't run {model}: {why}")
            log("if it is a stale server, stop it or pass --port to start a fresh one")
            return 2
        if read_free_pct() is None:
            log("free memory can't be read on this system; the memory guard is off")
        threading.Thread(target=guard.run, args=(stop,), daemon=True).start()
        log(f"running A/B: answer={args.answer_model} judge={args.judge_model} samples={args.samples}")
        # Own session: a terminal's SIGINT/SIGHUP must not reach compare_rules
        # directly (a second interrupt could land during its rule-file restore);
        # only stop_child's single SIGINT does.
        child = subprocess.Popen(
            ab_command(args), cwd=REPO_ROOT, env=ab_env(args, base), start_new_session=True,
        )
        code = child.wait()
    except KeyboardInterrupt:
        log("interrupted — stopping")
        code = 130
        if child is not None:
            stop_child(child)
    finally:
        stop.set()
        try:
            if server is not None:
                if args.keep_server:
                    for model in models:
                        unload(base, model)
                else:
                    log("stopping the server this script started")
                    stop_process_group(server)
            else:
                for model in models:
                    unload(base, model)
        finally:
            if server is not None and not args.keep_server:
                # Safe to repeat; a second Ctrl+C/SIGTERM/SIGHUP during the first
                # stop's wait lands here and still gets the group killed.
                stop_process_group(server)
    if guard.lowest is not None:
        log(f"lowest free memory seen: {guard.lowest}%")
    if guard.tripped:
        return GUARD_EXIT_CODE
    if code == 0:
        log(f"report: {args.out}  answers: {Path(args.out).with_suffix('.answers.jsonl')}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
