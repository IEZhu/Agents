"""Metadata-only Codex stdio protocol and bounded child-process cleanup."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

import pytest

from src import codex_thread_titles as adapter


SESSION = "550e8400-e29b-41d4-a716-446655440000"
_SERVER = r'''
import json, os, signal, sys, time
from pathlib import Path
config = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
if config.get("ignore_terminate") and os.name == "posix":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
title = config.get("title", "🔄 Проверить API — #42")
reads = 0
with open(sys.argv[2], "a", encoding="utf-8") as log:
    for line in sys.stdin:
        message = json.loads(line)
        log.write(json.dumps(message) + "\n")
        log.flush()
        method = message["method"]
        if method == "initialized":
            continue
        if method == "thread/read":
            reads += 1
        stage = "readback" if method == "thread/read" and reads == 2 else method
        time.sleep(config.get("delay", 0))
        if config.get("hang_at") == stage:
            time.sleep(30)
        if config.get("exit_at") == stage:
            sys.exit(0)
        if config.get("bad_json_at") == stage:
            print("not JSON", flush=True)
            continue
        if config.get("oversized_at") == stage:
            print("x" * 65537, flush=True)
            continue
        response = {"id": message["id"], "result": {}}
        if config.get("wrong_response_id") == stage:
            response["id"] = 999
        if method == "thread/read":
            read_title = config.get("readback_title", title) if reads == 2 else title
            response["result"] = {"thread": {"id": config.get("thread_id", message["params"]["threadId"]), "name": read_title}}
        elif method == "thread/name/set":
            title = message["params"]["name"]
        elif method != "initialize":
            raise AssertionError("forbidden method: " + method)
        if config.get("error_at") == stage:
            response = {"id": message["id"], "error": {"code": -32601, "message": "unsupported"}}
        if config.get("missing_result_at") == stage:
            response.pop("result", None)
        if config.get("notifications"):
            print(json.dumps({"method": "thread/name/updated", "params": {}}), flush=True)
        print(json.dumps(response), flush=True)
    if config.get("ignore_eof"):
        time.sleep(30)
'''


@pytest.fixture
def server(tmp_path, monkeypatch):
    script = tmp_path / "fake_app_server.py"
    script.write_text(_SERVER, encoding="utf-8")
    config_path, log_path = tmp_path / "config.json", tmp_path / "messages.jsonl"
    config = {}
    processes = []
    real_popen = subprocess.Popen

    def popen(argv, **options):
        assert argv == ["fake-codex", "app-server", "--listen", "stdio://"]
        assert options.get("shell", False) is False
        assert options["stdin"] == options["stdout"] == subprocess.PIPE
        assert options["stderr"] == subprocess.DEVNULL
        config_path.write_text(json.dumps(config), encoding="utf-8")
        process = real_popen([sys.executable, "-B", "-u", str(script), str(config_path), str(log_path)], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(adapter.subprocess, "Popen", popen)
    monkeypatch.setattr(adapter, "_resolve_executable", lambda executable: executable)

    def messages():
        return [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()] if log_path.exists() else []

    yield config, processes, messages
    for process in processes:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)


def format_stopped(title, status):
    assert status == "stopped"
    return "🛑 " + title.removeprefix("🔄 ").removeprefix("🛑 ")


def rename(**kwargs):
    return adapter.rename_codex_thread(SESSION, "stopped", format_stopped, executable="fake-codex", **kwargs)


def test_supported_metadata_flow_preserves_body_and_confirms_readback(server):
    config, processes, messages = server
    config["notifications"] = True
    result = rename()
    assert result == {"client": "codex", "session_id": SESSION, "status": "stopped",
                      "title": "🛑 Проверить API — #42", "changed": True, "persisted": True,
                      "live_ui_refresh_verified": False}
    sent = messages()
    assert [message["method"] for message in sent] == [
        "initialize", "initialized", "thread/read", "thread/name/set", "thread/read"]
    assert sent[2]["params"] == sent[4]["params"] == {"threadId": SESSION, "includeTurns": False}
    assert sent[3]["params"] == {"threadId": SESSION, "name": result["title"]}
    assert processes[0].poll() is not None


def test_already_prefixed_title_skips_write_but_still_reads_back(server):
    config, _, messages = server
    config["title"] = "🛑 Keep my title"
    result = rename()
    assert result["changed"] is False and result["persisted"] is True
    assert [message["method"] for message in messages()] == ["initialize", "initialized", "thread/read", "thread/read"]


@pytest.mark.parametrize("session", ["../other", "", None, "550E8400-E29B-41D4-A716-446655440000", SESSION.replace("-", ""), "{" + SESSION + "}"])
def test_invalid_identity_is_rejected_before_starting_process(server, session):
    with pytest.raises(ValueError, match="canonical UUID"):
        adapter.rename_codex_thread(session, "stopped", format_stopped, executable="fake-codex")
    assert server[1] == []


@pytest.mark.parametrize("title", [None, "", "  ", 17])
def test_missing_known_name_never_invents_title_or_writes(server, title):
    config, _, messages = server
    config["title"] = title
    with pytest.raises(RuntimeError, match="known nonempty thread.name"):
        rename()
    assert "thread/name/set" not in [message["method"] for message in messages()]


def test_wrong_thread_identity_never_writes(server):
    server[0]["thread_id"] = "00000000-0000-0000-0000-000000000000"
    with pytest.raises(RuntimeError, match="thread identity"):
        rename()
    assert "thread/name/set" not in [message["method"] for message in server[2]()]


@pytest.mark.parametrize("stage", ["initialize", "thread/read", "thread/name/set", "readback"])
def test_protocol_error_is_visible_and_child_is_reaped(server, stage):
    server[0]["error_at"] = stage
    with pytest.raises(RuntimeError, match="failed.*-32601"):
        rename()
    assert server[1][0].poll() is not None


@pytest.mark.parametrize("config,match", [
    ({"bad_json_at": "initialize"}, "invalid JSON"),
    ({"oversized_at": "initialize"}, "oversized"),
    ({"missing_result_at": "thread/read"}, "no result"),
    ({"wrong_response_id": "thread/read"}, "response identity"),
    ({"exit_at": "initialize"}, "closed before"),
    ({"readback_title": "Old title"}, "readback"),
])
def test_malformed_or_unconfirmed_protocol_never_reports_success(server, config, match):
    server[0].update(config)
    with pytest.raises(RuntimeError, match=match):
        rename()
    assert server[1][0].poll() is not None


@pytest.mark.parametrize("title", [None, "", "line\nbreak", "x" * 1025])
def test_invalid_formatter_output_never_writes(server, title):
    with pytest.raises(ValueError, match="formatted Codex title"):
        adapter.rename_codex_thread(SESSION, "stopped", lambda *_: title, executable="fake-codex")
    assert "thread/name/set" not in [message["method"] for message in server[2]()]


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), True, "2.5"])
def test_invalid_timeout_never_launches_child(server, timeout):
    with pytest.raises(ValueError, match="timeout"):
        rename(timeout=timeout)
    assert server[1] == []


@pytest.mark.parametrize("stage", ["initialize", "thread/read", "thread/name/set", "readback"])
def test_timeout_includes_initialization_and_kills_blocked_child(server, stage):
    server[0].update(hang_at=stage, ignore_terminate=True)
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        rename(timeout=0.4)
    assert time.monotonic() - started < 0.55
    assert server[1][0].poll() is not None
    assert server[1][0].stdin.closed and server[1][0].stdout.closed


def test_deadline_is_shared_across_requests_instead_of_reset(server):
    server[0]["delay"] = 0.09
    started = time.monotonic()
    with pytest.raises(RuntimeError, match="timed out"):
        rename(timeout=0.4)
    assert time.monotonic() - started < 0.55
    assert server[1][0].poll() is not None


def test_success_closes_and_reaps_server_that_ignores_eof(server):
    server[0].update(ignore_eof=True, ignore_terminate=True)
    assert rename(timeout=0.6)["persisted"] is True
    assert server[1][0].poll() is not None


def test_module_can_load_as_installed_standalone_sibling(tmp_path):
    path = tmp_path / "agents_core_codex_titles.py"
    path.write_bytes(Path(adapter.__file__).read_bytes())
    spec = importlib.util.spec_from_file_location("installed_codex_titles", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.rename_codex_thread.__defaults__ is None
    assert module.rename_codex_thread.__kwdefaults__ == {"timeout": 2.5, "executable": "codex"}


@pytest.mark.parametrize("architecture,package,triple", [
    ("AMD64", "codex-win32-x64", "x86_64-pc-windows-msvc"),
    ("ARM64", "codex-win32-arm64", "aarch64-pc-windows-msvc"),
])
@pytest.mark.parametrize("layout", ["hoisted", "nested", "vendor"])
@pytest.mark.parametrize("installation", ["global", "local"])
@pytest.mark.parametrize("suffix", [".cmd", ".ps1"])
def test_windows_npm_shims_resolve_native_without_executing_wrapper(
        tmp_path, monkeypatch, architecture, package, triple, layout, installation, suffix):
    prefix = tmp_path / "npm with spaces"
    modules = prefix / "node_modules"
    shim = (prefix if installation == "global" else modules / ".bin") / ("codex" + suffix)
    shim.parent.mkdir(parents=True)
    # Deliberately invalid shell text: resolution must not parse or evaluate it.
    shim.write_text("WRAPPER MUST NEVER RUN & exit 99", encoding="utf-8")
    package_root = modules / "@openai" / "codex"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "bin" / "codex.js").write_text("throw 'launcher must not run';", encoding="utf-8")
    if layout == "vendor":
        vendor = package_root / "vendor"
    else:
        dependency_modules = package_root / "node_modules" if layout == "nested" else modules
        vendor = dependency_modules / "@openai" / package / "vendor"
    native = vendor / triple / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native executable placeholder")
    monkeypatch.setattr(adapter.platform, "machine", lambda: architecture)
    monkeypatch.setattr(adapter.shutil, "which", lambda *_args, **_kwargs: str(shim))
    assert adapter._windows_codex_executable("codex") == str(native.resolve())


def test_windows_direct_native_executable_is_resolved_to_absolute_path(tmp_path, monkeypatch):
    native = tmp_path / "Codex Native" / "codex.exe"
    native.parent.mkdir()
    native.write_bytes(b"native executable placeholder")
    monkeypatch.setattr(adapter.shutil, "which", lambda executable: str(native))
    assert adapter._windows_codex_executable("codex") == str(native.resolve())


@pytest.mark.parametrize("command", ["codex", "codex.ps1", "explicit-path"])
def test_windows_powershell_only_shim_is_discovered_without_pathext(tmp_path, monkeypatch, command):
    shim = tmp_path / "codex.ps1"
    shim.touch()
    package_root = tmp_path / "node_modules" / "@openai" / "codex"
    (package_root / "bin").mkdir(parents=True)
    (package_root / "bin" / "codex.js").touch()
    native = package_root / "vendor" / "x86_64-pc-windows-msvc" / "bin" / "codex.exe"
    native.parent.mkdir(parents=True)
    native.touch()
    calls = []

    def which(executable):
        calls.append(executable)
        return None

    monkeypatch.setattr(adapter.shutil, "which", which)
    monkeypatch.setattr(adapter.platform, "machine", lambda: "AMD64")
    monkeypatch.setenv("PATH", str(tmp_path))
    if command == "explicit-path":
        command = str(shim)
    assert adapter._windows_codex_executable(command) == str(native.resolve())
    assert calls == [command]


@pytest.mark.parametrize("suffix", [".cmd", ".ps1", ".bat", ".js", ""])
def test_windows_unsupported_wrapper_layout_fails_before_process_creation(tmp_path, monkeypatch, suffix):
    shim = tmp_path / ("codex" + suffix)
    shim.write_text("must never run", encoding="utf-8")
    monkeypatch.setattr(adapter.shutil, "which", lambda *_args, **_kwargs: str(shim))
    monkeypatch.setattr(adapter.platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(adapter, "_resolve_executable", adapter._windows_codex_executable)
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("wrapper was started"))
    with pytest.raises(RuntimeError, match="native codex.exe"):
        adapter.rename_codex_thread(SESSION, "stopped", format_stopped)


def test_windows_missing_command_fails_clearly(tmp_path, monkeypatch):
    monkeypatch.setattr(adapter.shutil, "which", lambda *_args, **_kwargs: None)
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(RuntimeError, match="not found"):
        adapter._windows_codex_executable("codex")


def test_windows_unsupported_architecture_fails_clearly(tmp_path, monkeypatch):
    shim = tmp_path / "codex.cmd"
    shim.touch()
    monkeypatch.setattr(adapter.shutil, "which", lambda *_args, **_kwargs: str(shim))
    monkeypatch.setattr(adapter.platform, "machine", lambda: "unknown-architecture")
    with pytest.raises(RuntimeError, match="architecture"):
        adapter._windows_codex_executable("codex")


def test_command_resolution_consumes_the_overall_deadline(monkeypatch):
    clock = [0.0]

    def resolve(executable):
        clock[0] = 1.0
        return executable

    monkeypatch.setattr(adapter.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(adapter, "_resolve_executable", resolve)
    monkeypatch.setattr(adapter.subprocess, "Popen", lambda *_args, **_kwargs: pytest.fail("deadline expired before launch"))
    with pytest.raises(RuntimeError, match="timed out"):
        adapter.rename_codex_thread(SESSION, "stopped", format_stopped, timeout=0.4)


@pytest.mark.skipif(os.name != "nt", reason="Requires Windows PATH and CreateProcess semantics")
@pytest.mark.parametrize("installation", ["native", "global_cmd", "global_ps1", "local_cmd"])
@pytest.mark.parametrize("hang", [False, True])
def test_windows_path_launches_and_reaps_native_server(tmp_path, monkeypatch, installation, hang):
    prefix = tmp_path / "npm with spaces"
    prefix.mkdir()
    if installation == "native":
        native = prefix / "codex.exe"
        path_directory = prefix
    else:
        modules = prefix / "node_modules"
        path_directory = modules / ".bin" if installation == "local_cmd" else prefix
        path_directory.mkdir(parents=True, exist_ok=True)
        suffix = ".ps1" if installation == "global_ps1" else ".cmd"
        marker = tmp_path / "wrapper-ran"
        (path_directory / ("codex" + suffix)).write_text(
            f'echo wrapper > "{marker}"', encoding="utf-8")
        architecture = adapter.platform.machine().lower()
        triples = {"amd64": "x86_64-pc-windows-msvc", "x86_64": "x86_64-pc-windows-msvc",
                   "arm64": "aarch64-pc-windows-msvc", "aarch64": "aarch64-pc-windows-msvc"}
        package_root = modules / "@openai" / "codex"
        (package_root / "bin").mkdir(parents=True)
        (package_root / "bin" / "codex.js").write_text("throw 'launcher must not run';", encoding="utf-8")
        native = package_root / "vendor" / triples[architecture] / "bin" / "codex.exe"
        native.parent.mkdir(parents=True)
    shutil.copyfile(sys.executable, native)
    script, config, log = (tmp_path / name for name in ("server.py", "config.json", "messages.jsonl"))
    script.write_text(_SERVER, encoding="utf-8")
    config.write_text(json.dumps({"hang_at": "initialize"} if hang else {}), encoding="utf-8")
    monkeypatch.setenv("PATH", str(path_directory) + os.pathsep + sys.base_prefix)
    monkeypatch.setenv("PATHEXT", ".EXE;.CMD")
    monkeypatch.setenv("PYTHONHOME", sys.base_prefix)
    processes = []
    real_popen = subprocess.Popen

    def popen(argv, **options):
        assert argv == [str(native.resolve()), "app-server", "--listen", "stdio://"]
        assert options.get("shell", False) is False
        process = real_popen([argv[0], "-B", "-u", str(script), str(config), str(log), *argv[1:]], **options)
        processes.append(process)
        return process

    monkeypatch.setattr(adapter.subprocess, "Popen", popen)
    try:
        if hang:
            with pytest.raises(RuntimeError, match="timed out"):
                adapter.rename_codex_thread(SESSION, "stopped", format_stopped, timeout=1.5)
        else:
            assert adapter.rename_codex_thread(SESSION, "stopped", format_stopped)["persisted"] is True
        assert len(processes) == 1 and processes[0].poll() is not None
        assert processes[0].stdin.closed and processes[0].stdout.closed
        assert not (tmp_path / "wrapper-ran").exists()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
