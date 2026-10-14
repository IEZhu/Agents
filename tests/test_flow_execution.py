"""The execution capability table (src/flow_execution.py, #347): effort lists from the installed CLIs.

No CLI runs here: a fake command runner answers ``--help`` and ``--version`` with stored text.
"""
import subprocess

import pytest

from src import flow_execution as fe
from src.schedules import codex_sandbox, config, workers

# `claude --help` of 2.1.296, the entries around --effort as printed (a two-line entry and an
# entry whose description starts on the next line).
CLAUDE_HELP = """\
  --disable-slash-commands              Disable all skills
  --disallowedTools, --disallowed-tools <tools...>
      Comma or space-separated list of tool names to deny (e.g. "Bash(git *)
      Edit")
  --effort <level>                      Effort level for the current session
                                        (low, medium, high, xhigh, max)
  --environment <environment_id>        Create a new cloud session that runs on
                                        the given self-hosted environment
                                        (ccpool_...).
  --model <model>                       Model for the current session. Provide
                                        an alias for the latest model (e.g.
                                        'fable', 'opus', or 'sonnet') or a
                                        model's full name.
"""
PATH = "/usr/bin:/bin"


class Runner:
    """Answers `--help` and `--version` per executable; records each argv and environment."""

    def __init__(self, answers):
        self.answers = answers  # (executable, flag) -> stdout, or an exception to raise
        self.calls = []

    def __call__(self, argv, **options):
        self.calls.append((list(argv), options))
        answer = self.answers.get((argv[0], argv[-1]))
        if isinstance(answer, BaseException):
            raise answer
        if answer is None:
            return subprocess.CompletedProcess(argv, 1, "", "unknown")
        return subprocess.CompletedProcess(argv, 0, answer, "")


def test_parse_claude_efforts_reads_the_continuation_line():
    assert fe.parse_claude_efforts(CLAUDE_HELP) == ("low", "medium", "high", "xhigh", "max")
    assert fe.parse_claude_efforts("  --effort <level>   Effort level (low, high)\n  --model <m>  (a, b)\n") == \
        ("low", "high")


@pytest.mark.parametrize("text", [
    "", "  --model <model>  Model\n",
    "  --effort <level>   Effort level for the session\n  --model <model>  (low, high)\n",  # list of another entry
    "  --effort <level>   Effort level (choices: low, high)\n",
    "  --effort <level>   Effort level (low, low)\n",
])
def test_parse_claude_efforts_without_a_usable_entry(text):
    assert fe.parse_claude_efforts(text) is None


def test_the_effort_lists_live_here_and_the_runner_imports_them():
    assert fe.CLAUDE_EFFORTS == ("low", "medium", "high", "xhigh", "max")
    assert config.CODEX_EFFORTS is fe.CODEX_EFFORTS and codex_sandbox.EFFORTS is fe.CODEX_EFFORTS
    assert workers.CLAUDE_EFFORTS is fe.CLAUDE_EFFORTS and config.CLIENTS == fe.CLIENTS


def test_installed_reads_the_claude_help_and_the_codex_version():
    runner = Runner({("/opt/claude", "--help"): CLAUDE_HELP, ("/opt/claude", "--version"): "2.1.296 (Claude Code)\n",
                     ("/opt/codex", "--version"): "codex-cli 0.162.1\n"})
    table = fe.installed("/opt/claude", "/opt/codex", path=PATH, runner=runner)
    assert table["claude"] == fe.Capability("claude", fe.CLAUDE_EFFORTS, "2.1.296", "installed")
    assert table["codex"] == fe.Capability("codex", fe.CODEX_EFFORTS, "0.162.1", "installed")
    assert table["claude"].to_dict() == {"client": "claude", "efforts": list(fe.CLAUDE_EFFORTS),
                                         "version": "2.1.296", "source": "installed"}
    for argv, options in runner.calls:
        assert argv[-1] in ("--help", "--version") and len(argv) == 2
        assert options["env"]["PATH"] == PATH and options["env"]["HOME"] != str(__import__("pathlib").Path.home())
        assert options["stdin"] == subprocess.DEVNULL


def test_installed_reads_a_changed_claude_list():
    help_text = CLAUDE_HELP.replace("(low, medium, high, xhigh, max)", "(low, medium, high, xhigh, max, ultra)")
    runner = Runner({("/opt/claude", "--help"): help_text, ("/opt/claude", "--version"): "2.2.0 (Claude Code)\n"})
    table = fe.installed("/opt/claude", None, path=PATH, runner=runner)
    assert table["claude"].efforts[-1] == "ultra" and table["claude"].source == "installed"
    fe.check_effort("claude", "ultra", table)
    with pytest.raises(fe.FlowInvalid):
        fe.check_effort("claude", "ultra")  # the static table does not know it


@pytest.mark.parametrize("answers", [{}, {("/opt/claude", "--help"): OSError("gone")},
                                     {("/opt/claude", "--help"): subprocess.TimeoutExpired("claude", 30)},
                                     {("/opt/claude", "--help"): "no effort entry\n"}])
def test_installed_falls_back_to_the_static_lists(answers):
    table = fe.installed("/opt/claude", None, path=PATH, runner=Runner(answers))
    assert table["claude"].efforts == fe.CLAUDE_EFFORTS and table["claude"].source == "static"
    assert table["codex"] == fe.Capability("codex", fe.CODEX_EFFORTS, None, "static")


def test_installed_without_the_clis_runs_nothing():
    runner = Runner({})
    assert fe.installed(None, None, path=PATH, runner=runner) == fe.static()
    assert fe.installed("claude", "codex", path=PATH, runner=runner) == fe.static()  # not absolute paths
    assert runner.calls == []


def test_an_unverified_codex_version_keeps_the_list_static():
    runner = Runner({("/opt/codex", "--version"): "codex-cli 0.170.0\n"})
    assert fe.installed(None, "/opt/codex", path=PATH, runner=runner)["codex"] == \
        fe.Capability("codex", fe.CODEX_EFFORTS, "0.170.0", "static")


@pytest.mark.parametrize("client, effort, allowed", [("claude", "ultra", fe.CLAUDE_EFFORTS),
                                                     ("claude", "minimal", fe.CLAUDE_EFFORTS),
                                                     ("codex", "extreme", fe.CODEX_EFFORTS),
                                                     ("codex", "", fe.CODEX_EFFORTS)])
def test_check_effort_names_the_allowed_values(client, effort, allowed):
    with pytest.raises(fe.FlowInvalid) as raised:
        fe.check_effort(client, effort)
    assert raised.value.code == "flow_invalid" and raised.value.allowed == list(allowed)
    assert str(raised.value).startswith("flow_invalid: ")


def test_check_effort_accepts_the_clients_values_and_the_default():
    for client, efforts in fe.STATIC_EFFORTS.items():
        for effort in efforts:
            fe.check_effort(client, effort)
        fe.check_effort(client, None)
    with pytest.raises(fe.FlowInvalid, match="unknown client") as raised:
        fe.check_effort("gemini", "high")
    assert raised.value.allowed == ["claude", "codex"]


def test_check_model_warns_about_an_unknown_alias():
    assert fe.check_model("opus") == [] and fe.check_model("claude-opus-4-5") == [] and fe.check_model(None) == []
    assert fe.check_model("gpt-6.1-sol", "codex") == []
    [warning] = fe.check_model("opsu")
    assert "'opsu'" in warning and "known Claude alias" in warning
    for bad in ("", "opus --dangerously", "-m", "a" * 65, "op us", 7):
        with pytest.raises(fe.FlowInvalid, match="not a plain model name"):
            fe.check_model(bad)
