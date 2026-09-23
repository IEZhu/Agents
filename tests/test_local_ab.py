"""One-command local A/B runner (evals/scripts/local_ab.py). No network, no processes."""
from __future__ import annotations

import pytest

from evals.scripts import local_ab as la

MACOS_OUT = """The system has 38654705664 (2359296 pages with a page size of 16384).

Stats:
Pages free: 12345
System-wide memory free percentage: 14%
"""

MEMINFO = "MemTotal:       32000000 kB\nMemFree:         1000000 kB\nMemAvailable:    8000000 kB\n"


def test_parses_macos_and_linux_free_memory():
    assert la.parse_macos_free_pct(MACOS_OUT) == 14
    assert la.parse_macos_free_pct("no such line") is None
    assert la.parse_linux_free_pct(MEMINFO) == 25
    assert la.parse_linux_free_pct("garbage") is None
    # MemTotal without MemAvailable is "unknown", not 0% free, so the guard stays off
    assert la.parse_linux_free_pct("MemTotal:       32000000 kB\nMemFree:         1000000 kB\n") is None
    assert la.parse_linux_free_pct("MemTotal:              0 kB\nMemAvailable:    8000000 kB\n") is None


def test_runtime_prefers_installed_ollama_then_nix():
    assert la.runtime_command(lambda b: "/usr/bin/ollama" if b == "ollama" else "/bin/nix") == ["ollama", "serve"]
    assert la.runtime_command(lambda b: "/bin/nix" if b == "nix" else None) == ["nix", "run", "nixpkgs#ollama", "--", "serve"]
    assert la.runtime_command(lambda b: None) is None


def test_server_env_is_lean_and_bound_to_the_port():
    env = la.server_env(11435, 12288, base_env={"PATH": "/bin"})
    assert env["OLLAMA_HOST"] == "127.0.0.1:11435"
    assert env["OLLAMA_CONTEXT_LENGTH"] == "12288"
    assert env["OLLAMA_MAX_LOADED_MODELS"] == "1" and env["OLLAMA_KV_CACHE_TYPE"] == "q8_0"
    assert env["PATH"] == "/bin"


def test_missing_models_handles_latest_tag_and_duplicates():
    installed = {"qwen3:8b", "gemma4:31b-it-qat", "llama3:latest"}
    assert la.missing_models(["gemma4:31b-it-qat", "qwen3.8:27b", "qwen3.8:27b"], installed) == ["qwen3.8:27b"]
    assert la.missing_models(["llama3"], installed) == []


def test_guard_trips_only_after_consecutive_low_reads():
    reads = iter([4, 4, 9, 4, 4, 4, 1])
    tripped = []
    guard = la.MemoryGuard(min_pct=5, on_trip=tripped.append, read=lambda: next(reads))
    for _ in range(5):
        guard.check()
    assert not guard.tripped  # the 9% read reset the streak
    guard.check()
    assert guard.tripped and tripped == [4]
    guard.check()  # fires once
    assert tripped == [4] and guard.lowest == 4


def test_guard_ignores_unreadable_memory():
    guard = la.MemoryGuard(min_pct=5, on_trip=lambda f: pytest.fail("must not trip"), read=lambda: None)
    for _ in range(5):
        guard.check()
    assert not guard.tripped and guard.lowest is None


def test_command_and_env_wire_models_to_compare_rules():
    args = la.parse_args(["--answer-model", "a:1", "--judge-model", "j:2", "--samples", "3", "--temperature", "0.7"])
    cmd = la.ab_command(args)
    assert cmd[1:4] == ["-m", "evals.scripts.compare_rules", "--provider"] and "local" in cmd
    assert cmd[cmd.index("--samples-per-case") + 1] == "3"
    assert "--dataset" not in cmd
    env = la.ab_env(args, "http://127.0.0.1:11435", base_env={})
    assert env["LOCAL_LLM_BASE_URL"] == "http://127.0.0.1:11435/v1"
    assert (env["LOCAL_LLM_MODEL"], env["LOCAL_LLM_JUDGE_MODEL"]) == ("a:1", "j:2")
    assert env["LOCAL_LLM_TEMPERATURE"] == "0.7"
    assert args.out.endswith("local_ab_a-1_j-2.md")


def test_default_rule_fixtures_exist():
    args = la.parse_args([])
    assert all(__import__("pathlib").Path(p).is_file() for p in (args.baseline_rule, args.candidate_rule))


def test_repeated_greedy_samples_are_rejected():
    with pytest.raises(SystemExit):
        la.parse_args(["--samples", "3"])


def test_argument_validation():
    for bad in (["--samples", "0"], ["--temperature", "-1"], ["--samples", "2", "--temperature", "0"]):
        with pytest.raises(SystemExit):
            la.parse_args(bad)


def test_paths_resolve_against_the_callers_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    args = la.parse_args(["--dataset", "cases.jsonl", "--out", "r/report.md"])
    assert args.dataset == str(tmp_path / "cases.jsonl")
    assert args.out == str(tmp_path / "r" / "report.md")


def test_unreadable_memory_is_none_not_a_crash(monkeypatch):
    def boom(*a, **k):
        raise la.subprocess.TimeoutExpired("memory_pressure", 30)
    monkeypatch.setattr(la.sys, "platform", "darwin")
    monkeypatch.setattr(la.shutil, "which", lambda b: "/usr/bin/memory_pressure")
    monkeypatch.setattr(la.subprocess, "run", boom)
    assert la.read_free_pct() is None


def test_guard_keeps_watching_after_a_failing_check():
    import threading
    calls = []

    def read():
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("flaky")
        if len(calls) >= 3:
            stop.set()
        return 50

    stop = threading.Event()
    la.MemoryGuard(min_pct=5, on_trip=lambda f: None, read=read).run(stop, interval_s=0.01)
    assert len(calls) >= 3


class _FakeChild:
    def __init__(self, exits_on):
        self.exits_on, self.signals, self.done = exits_on, [], False

    def poll(self):
        return 0 if self.done else None

    def send_signal(self, sig):
        self.signals.append(sig)
        self.done = self.done or sig == self.exits_on

    def terminate(self):
        self.send_signal(la.signal.SIGTERM)

    def kill(self):
        self.send_signal(la.signal.SIGKILL)
        self.done = True

    def wait(self, timeout=None):
        if not self.done:
            raise la.subprocess.TimeoutExpired("child", timeout)
        return 0


def test_stop_child_asks_with_sigint_first_so_the_rule_file_is_restored():
    polite = _FakeChild(exits_on=la.signal.SIGINT)
    la.stop_child(polite, grace_s=0)
    assert polite.signals == [la.signal.SIGINT]
    stubborn = _FakeChild(exits_on=None)
    la.stop_child(stubborn, grace_s=0)
    assert stubborn.signals == [la.signal.SIGINT, la.signal.SIGTERM, la.signal.SIGKILL]



def test_preflight_checks_the_judge_too_and_leaves_the_answer_model_loaded():
    calls = []

    def check(base, model):
        calls.append(("check", model))
        return (model != "bad-judge", "boom")

    def drop(base, model):
        calls.append(("drop", model))

    assert la.preflight("b", "ans", "judge", check=check, drop=drop) is None
    assert calls == [("check", "judge"), ("drop", "judge"), ("check", "ans")]
    assert la.preflight("b", "ans", "bad-judge", check=check, drop=drop) == ("bad-judge", "boom")
    calls.clear()
    assert la.preflight("b", "same", "same", check=check, drop=drop) is None
    assert calls == [("check", "same")]


def test_main_runs_the_child_in_its_own_session(monkeypatch, tmp_path):
    """A terminal's SIGINT/SIGHUP must reach compare_rules only via stop_child."""
    seen = {}

    class _Child:
        def __init__(self, cmd, **kwargs):
            seen.update(kwargs)

        def wait(self, timeout=None):
            return 0

        def poll(self):
            return 0

    monkeypatch.setattr(la, "server_up", lambda base: True)
    monkeypatch.setattr(la, "installed_models", lambda base: {"a:1", "j:2"})
    monkeypatch.setattr(la, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(la, "read_free_pct", lambda: 50)
    monkeypatch.setattr(la, "unload", lambda base, model: None)
    monkeypatch.setattr(la.subprocess, "Popen", _Child)
    monkeypatch.setattr(la.signal, "signal", lambda *a: None)
    rc = la.main(["--answer-model", "a:1", "--judge-model", "j:2", "--out", str(tmp_path / "r.md")])
    assert rc == 0 and seen.get("start_new_session") is True
