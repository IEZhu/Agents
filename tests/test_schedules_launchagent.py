"""The scheduler LaunchAgent with a fake launchctl: idempotent, and the daemon's agents untouched (#338)."""
import os
import plistlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import executables
from src.schedules import launchagent
from src.schedules.launchagent import Scheduler


class FakeLaunchd:
    def __init__(self):
        self.loaded, self.calls, self.fail = set(), [], set()

    def __call__(self, argv):
        assert argv[0] == "/bin/launchctl"
        self.calls.append(list(argv[1:]))
        verb, code = argv[1], 0
        if verb in self.fail:
            code = 5
        elif verb == "bootstrap":
            self.loaded.add(plistlib.loads(Path(argv[3]).read_bytes())["Label"])
        elif verb == "bootout":
            self.loaded.discard(argv[2].rsplit("/", 1)[1])
        elif verb == "print":
            code = 0 if argv[2].rsplit("/", 1)[1] in self.loaded else 113
        return SimpleNamespace(returncode=code, stdout="", stderr="")


@pytest.fixture
def host(tmp_path):
    state = tmp_path / "state" / "0123456789abcdef"
    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    daemon_label = "local.agents-core.0123456789abcdef"
    daemon_plists = {}
    for name in (daemon_label, daemon_label + ".updater"):
        path = agents / (name + ".plist")
        path.write_bytes(plistlib.dumps({"Label": name, "ProgramArguments": ["/bin/true"]}))
        daemon_plists[path] = path.read_bytes()
    tools = tmp_path / "bin"
    tools.mkdir()
    for name in ("claude", "gh"):
        (tools / name).write_text("#!/bin/sh\n")
        (tools / name).chmod(stat.S_IRWXU)
    fake = FakeLaunchd()
    fake.loaded.update({daemon_label, daemon_label + ".updater"})
    scheduler = Scheduler(tmp_path / "installation", state, "/opt/agents/.venv/bin/python", runner=fake,
                          agents_dir=agents, uid=501)
    return SimpleNamespace(scheduler=scheduler, fake=fake, state=state, agents=agents, tools=tools,
                           daemon_plists=daemon_plists, daemon_label=daemon_label, tmp=tmp_path)


def test_install_writes_the_scheduler_job(host):
    result = host.scheduler.install(str(host.tools))
    assert result["changed"] and result["installed"] and result["loaded"]
    assert result["label"] == "local.agents-core.0123456789abcdef.scheduler"
    payload = plistlib.loads(host.scheduler.plist.read_bytes())
    assert payload["ProgramArguments"] == ["/opt/agents/.venv/bin/python", "-P", "-m", "src.schedules", "tick"]
    assert payload["StartInterval"] == 300 and payload["RunAtLoad"] is False
    assert payload["WorkingDirectory"] == str((host.tmp / "installation").resolve())
    environment = payload["EnvironmentVariables"]
    assert environment["PYTHONPATH"] == str((host.tmp / "installation").resolve())
    assert environment["AGENTS_SERVICE_DIR"] == str(host.state)
    path = environment["PATH"].split(os.pathsep)
    assert path[0] == str(host.tools) and set(executables.system_directories()) <= set(path)
    tools = result["tools"]
    assert tools["claude"]["path"] == str(host.tools / "claude") and tools["claude"]["protected"] is False
    assert any(warning.startswith("claude:") for warning in result["warnings"])
    assert (host.state / "schedules" / "tools.json").is_file()


def test_install_and_uninstall_are_idempotent(host):
    host.scheduler.install(str(host.tools))
    calls = len(host.fake.calls)
    again = host.scheduler.install(str(host.tools))
    assert again["changed"] is False
    assert not any(call[0] in ("bootout", "bootstrap") for call in host.fake.calls[calls:])

    first = host.scheduler.uninstall()
    assert first["changed"] and not first["installed"] and not first["loaded"]
    second = host.scheduler.uninstall()
    assert second["changed"] is False and not second["installed"]


def test_changed_job_is_reloaded(host):
    host.scheduler.install(str(host.tools))
    host.scheduler.python = "/usr/local/bin/python3"
    calls = len(host.fake.calls)
    assert host.scheduler.install(str(host.tools))["changed"]
    verbs = [call[0] for call in host.fake.calls[calls:]]
    assert verbs.index("bootout") < verbs.index("bootstrap")


def test_the_daemons_own_agents_are_never_touched(host):
    host.scheduler.install(str(host.tools))
    host.scheduler.install(str(host.tools))
    host.scheduler.uninstall()
    host.scheduler.uninstall()
    for path, content in host.daemon_plists.items():
        assert path.read_bytes() == content
    assert {host.daemon_label, host.daemon_label + ".updater"} <= host.fake.loaded
    target = "gui/501/" + host.scheduler.label
    for call in host.fake.calls:
        assert call[1:] in ([target], ["gui/501", str(host.scheduler.plist)]), call


def test_failed_bootstrap_is_reported(host):
    host.fake.fail.add("bootstrap")
    with pytest.raises(RuntimeError, match="bootstrap"):
        host.scheduler.install(str(host.tools))


def test_a_job_launchd_cannot_stop_is_reported(host):
    host.scheduler.install(str(host.tools))
    host.fake.fail.add("bootout")  # launchd keeps the job loaded
    with pytest.raises(RuntimeError, match="still scheduled"):
        host.scheduler.uninstall()
    assert host.scheduler.plist.exists()


def test_label_names_the_state_directory():
    assert launchagent.label("/x/Agents-Core/0123456789abcdef") == "local.agents-core.0123456789abcdef.scheduler"
