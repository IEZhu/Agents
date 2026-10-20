"""Source-bound daemon overrides retain the installed content and stdio identity.

Fresh interpreters exercise import ordering without changing pytest's configured
installation. No service, network listener, embedding or native model is started.
"""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import textwrap

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _child(code, *args, cwd=ROOT, input=None):
    env = os.environ.copy()
    for name in ("PYTHONPATH", "AGENTS_USER_FLOWS_DIR", "AGENTS_LOCAL_INFERENCE_CONFIG",
                 "AGENTS_STDIO_STANDALONE", "RUNTIME_IDENTITY_TEST_CANONICAL"):
        env.pop(name, None)
    env.update(PYTHONDONTWRITEBYTECODE="1", LANGFUSE_TRACING_ENABLED="false",
               HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", AGENTS_AUTO_UPDATE="0")
    return subprocess.run([sys.executable, "-c", textwrap.dedent(code), *map(str, args)],
                          cwd=cwd, env=env, input=input, capture_output=True, text=True, timeout=30)


def _answer(process):
    assert process.returncode == 0, process.stderr or process.stdout
    return json.loads(process.stdout)


def test_default_paths_remain_source_derived():
    answer = _answer(_child("""
        import json, sys
        from src import runtime_paths
        assert not any(name.startswith('src.engine') for name in sys.modules)
        from src.engine import config
        print(json.dumps({'installation': config.INSTALL_ROOT,
                          'runtime': str(runtime_paths.runtime_root()),
                          'agents': config.AGENTS_DIR, 'flows': config.FLOWS_DIR}))
    """))
    assert answer == {"installation": str(ROOT), "runtime": str(ROOT),
                      "agents": str(ROOT / "agents"), "flows": str(ROOT / "flows")}


def test_matching_runtime_reads_canonical_components_and_personal_flows(tmp_path):
    canonical = tmp_path / "installation"
    agent = canonical / "agents" / "canonical" / "system_prompt.mdc"
    agent.parent.mkdir(parents=True)
    agent.write_text("---\nidentity: {name: canonical, role: Canonical fixture}\n---\nInstalled agent.\n")
    flows = canonical / "flows"
    flows.mkdir()
    (flows / "canonical.md").write_text("# Installed flow\n\nCanonical content.\n")
    answer = _answer(_child("""
        import json, sys
        from pathlib import Path
        from src.runtime_paths import configure_runtime_paths, runtime_root
        root = Path(sys.argv[1])
        configure_runtime_paths(root, runtime_root=runtime_root())
        from src.engine import config
        from src.component_catalog import list_agents
        from src.flows import FlowCatalog
        from src.user_flows import FlowLibrary
        from src.user_sync import engine as sync_engine
        from src.user_sync.schedule import scheduler
        from src.daemon import state, overview, autodev_ui
        from src.schedules import config as schedule_config, cli as schedule_cli
        schedule = scheduler(platform='darwin', runner=lambda *args, **kwargs: None,
                             launch_agents_dir=root / 'launch-agents', lock_dir=root / 'locks')
        assert 'fastembed' not in sys.modules and 'src.server' not in sys.modules
        print(json.dumps({'paths': {name: getattr(config, name) for name in
                          ('INSTALL_ROOT', 'INSTALL_DATA_DIR', 'AGENTS_DIR', 'SKILLS_DIR',
                           'IMPLANTS_DIR', 'RULES_DIR', 'FLOWS_DIR')},
                          'agents': [agent['id'] for agent in list_agents()],
                          'flow': FlowCatalog().load('canonical').content,
                          'personal': str(FlowLibrary().user_dir),
                          'sync_personal': str(sync_engine.default_library()),
                          'state_id': state.default_state_dir().name,
                          'sync_id': sync_engine.installation_id(),
                          'identities': [str(value) for value in (
                              sync_engine.installation_root(), overview.INSTALL_ROOT,
                              autodev_ui.INSTALLATION, schedule_config.INSTALLATION,
                              schedule_cli.INSTALLATION, schedule.job.installation)]}))
    """, canonical))
    assert answer["paths"] == {name: str(canonical / relative) for name, relative in (
        ("INSTALL_ROOT", ""), ("INSTALL_DATA_DIR", "data"), ("AGENTS_DIR", "agents"),
        ("SKILLS_DIR", "skills"), ("IMPLANTS_DIR", "implants"), ("RULES_DIR", "rules"),
        ("FLOWS_DIR", "flows"))}
    assert answer["agents"] == ["canonical"]
    assert "Canonical content." in answer["flow"]
    assert answer["personal"] == answer["sync_personal"] == str(flows / ".user")
    identity = hashlib.sha256(str(canonical).encode()).hexdigest()[:16]
    assert answer["state_id"] == answer["sync_id"] == identity
    assert answer["identities"] == [str(canonical)] * 6


@pytest.mark.parametrize("runtime", ["missing", "mismatch"])
def test_unbound_or_mismatching_runtime_fails_before_application_imports(tmp_path, runtime):
    directory = tmp_path / "private"
    directory.mkdir()
    config = {"installation": str(tmp_path / "canonical")}
    if runtime == "mismatch":
        config["runtime_root"] = str(tmp_path / "different-source")
    (directory / "service.json").write_text(json.dumps(config))
    answer = _answer(_child("""
        import json, sys
        from pathlib import Path
        from src.daemon.bootstrap import serve
        try:
            serve(sys.argv[1])
        except RuntimeError as error:
            assert 'different runtime source' in str(error)
        else:
            raise AssertionError('mismatched runtime was accepted')
        assert not any(name.startswith('src.engine') for name in sys.modules)
        assert 'src.daemon.app' not in sys.modules
        print(json.dumps({'log_created': (Path(sys.argv[1]) / 'service.log').exists()}))
    """, directory))
    assert answer == {"log_created": False}


@pytest.mark.parametrize("source", ["", ".", [], 12])
def test_declared_runtime_requires_an_absolute_path(tmp_path, source):
    process = _child("""
        import json, sys
        from src.runtime_paths import configure_runtime_paths
        try:
            configure_runtime_paths(sys.argv[1], runtime_root=json.loads(sys.argv[2]))
        except RuntimeError as error:
            assert 'absolute path' in str(error)
        else:
            raise AssertionError('invalid runtime root was accepted')
    """, tmp_path, json.dumps(source))
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize("loaded", ["src.engine.config", "src.daemon.app", "src.daemon.overview",
                                     "src.schedules.config"])
def test_runtime_paths_cannot_be_reconfigured_after_application_imports(loaded):
    process = _child("""
        import importlib, sys
        from src.runtime_paths import configure_runtime_paths, runtime_root
        importlib.import_module(sys.argv[1])
        try:
            configure_runtime_paths(runtime_root())
        except RuntimeError as error:
            assert 'before engine/application imports' in str(error)
        else:
            raise AssertionError('late runtime configuration was accepted')
    """, loaded)
    assert process.returncode == 0, process.stderr


def test_runtime_paths_refuse_conflicting_installations(tmp_path):
    process = _child("""
        import sys
        from pathlib import Path
        from src.runtime_paths import configure_runtime_paths, installation_root, runtime_root
        root = Path(sys.argv[1])
        configure_runtime_paths(root, runtime_root=runtime_root())
        try:
            configure_runtime_paths(root / 'other', runtime_root=runtime_root())
        except RuntimeError as error:
            assert 'different installation' in str(error)
        else:
            raise AssertionError('conflicting identity was accepted')
        assert installation_root() == root
    """, tmp_path)
    assert process.returncode == 0, process.stderr


@pytest.mark.parametrize("candidate", [ROOT / "runner-fixture", ROOT.parent])
def test_runner_excludes_runtime_children_and_ancestors_with_canonical_binding(tmp_path, candidate):
    process = _child("""
        import sys
        from pathlib import Path
        from unittest.mock import patch
        from src.runtime_paths import configure_runtime_paths, runtime_root
        configure_runtime_paths(sys.argv[1], runtime_root=runtime_root())
        from src.schedules.config import runner_location
        try:
            # Isolate this guard from the earlier ~/Documents guard, even when
            # this test's source checkout itself lives under ~/Documents.
            with patch.object(Path, 'home', return_value=Path(sys.argv[1]).parent / 'home'):
                runner_location(sys.argv[2])
        except ValueError as error:
            assert 'runtime source' in str(error)
        else:
            raise AssertionError('runner path overlaps executing runtime source')
    """, tmp_path / "canonical", candidate)
    assert process.returncode == 0, process.stderr


def test_bootstrap_keeps_content_dotenv_leases_health_and_state_identity(tmp_path):
    canonical = tmp_path / "canonical"
    (canonical / "data").mkdir(parents=True)
    (canonical / ".env").write_text("RUNTIME_IDENTITY_TEST_CANONICAL=loaded\nWARMUP_WAIT_SECONDS=37\n")
    directory = tmp_path / "private"
    directory.mkdir()
    token = "test-token-" + "x" * 48
    (directory / "token").write_text(token)
    (directory / "token").chmod(0o600)
    config = {"installation": str(canonical), "runtime_root": str(ROOT), "port": 8765,
              "model": "fixture-no-model", "model_artifact": "fixture", "model_path": "/unused",
              "model_cache": "/unused"}
    (directory / "service.json").write_text(json.dumps(config))
    answer = _answer(_child("""
        from contextlib import contextmanager
        import json, os, sys
        from pathlib import Path
        import uvicorn
        from src.daemon import bootstrap
        output, errors = sys.stdout, sys.stderr
        leases, answer = [], {}
        original_lock = bootstrap.file_lock
        @contextmanager
        def recording_lock(path, **options):
            leases.append(str(path))
            with original_lock(path, **options):
                yield
        bootstrap.file_lock = recording_lock
        bootstrap.stdio_bridge = lambda directory: None
        bootstrap.keep_registrations_current = lambda directory: None
        bootstrap._keep_alive = lambda directory: None
        bootstrap.executables.configure = lambda config: None
        bootstrap.executables.enter_service_environment = lambda: None
        def capture(app, **options):
            from src.engine import config
            import src.daemon.app as app_module
            service = app.state.service
            app_module.agents_core_version = lambda: 'fixture'
            answer.update(health=service.health(), registry=str(service.registry.path), token=service.token,
                          root=config.INSTALL_ROOT, warmup=config.WARMUP_WAIT_SECONDS,
                          router=os.environ['AGENTS_ROUTER_DATA_DIR'],
                          dotenv=os.environ.get('RUNTIME_IDENTITY_TEST_CANONICAL'),
                          source=str(Path(app_module.__file__).resolve().parents[2]),
                          options=options, handoff=service.user_sync.hand_off_schedule)
            assert 'fastembed' not in sys.modules and 'src.server' not in sys.modules
        uvicorn.run = capture
        try:
            bootstrap.serve(sys.argv[1])
        finally:
            sys.stdout, sys.stderr = output, errors
        answer['leases'] = leases
        print(json.dumps(answer))
    """, directory))
    assert answer["health"]["install_root"] == answer["root"] == str(canonical)
    assert answer["health"]["runtime_root"] == answer["source"] == str(ROOT)
    assert answer["registry"] == str(directory / "workspaces.json") and answer["token"] == token
    assert answer["router"] == str(directory / "router")
    assert str(canonical / "data/.sessions.lock") in answer["leases"]
    assert answer["dotenv"] == "loaded" and answer["warmup"] == 37
    assert answer["options"]["port"] == 8765 and answer["handoff"] is True


def test_original_raw_stdio_entry_still_bridges_the_canonical_installation(tmp_path):
    """The real startup guard executes its bridge with an extra runtime_root field.

    Python stands in for Node and the bridge script, so this checks the original
    raw-stdio dispatch and stream handoff without opening a network connection.
    """
    canonical = tmp_path / "canonical"
    source = canonical / "src"
    source.mkdir(parents=True)
    (source / "__init__.py").touch()
    shutil.copyfile(ROOT / "src/startup.py", source / "startup.py")
    (source / "daemon").mkdir()
    (source / "daemon/__init__.py").touch()
    shutil.copyfile(ROOT / "src/daemon/bridge.py", source / "daemon/bridge.py")
    shutil.copyfile(ROOT / "src/windows_job.py", source / "windows_job.py")
    prefix = (ROOT / "src/server.py").read_text().split("import atexit", 1)[0]
    server = source / "server.py"
    server.write_text(prefix + "raise AssertionError('a second engine would start')\n")
    bridge = canonical / "bridge" / "stdio.mjs"
    bridge.parent.mkdir()
    bridge.write_text("import sys\nprint('BRIDGE ' + sys.argv[1], flush=True)\n"
                      "print('ECHO ' + sys.stdin.readline().strip(), flush=True)\nraise SystemExit(5)\n")
    directory = tmp_path / "private"
    (directory / "bridges").mkdir(parents=True)
    (directory / "service.json").write_text(json.dumps({"installation": str(canonical),
        "runtime_root": str(ROOT), "node": sys.executable, "port": 8765}))
    bridge_config = directory / "bridges/stdio-auto.json"
    bridge_config.write_text("{}")
    bridge_config.chmod(0o600)
    (canonical / "data").mkdir()
    (canonical / "data/.shared-service.json").write_text(json.dumps({"directory": str(directory)}))
    process = _child("""
        import runpy, sys
        from src import startup
        startup._answers = lambda port: True
        runpy.run_path(sys.argv[1], run_name='__main__')
    """, server, cwd=canonical, input="request\n")
    assert process.returncode == 5, process.stderr
    assert process.stdout.splitlines() == [f"BRIDGE {bridge_config}", "ECHO request"]
    assert "serving standalone" not in process.stderr
