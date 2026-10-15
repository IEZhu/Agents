"""Owner-only configuration and disabled import boundary."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest
from pydantic import ValidationError

from src.local_inference.config import OwnerConfig, RedisConfig, load_owner_config
from src.local_inference.models import LocalInferenceError


SPEC = Path(__file__).parents[1] / "docs/shared-kv-mcp/owner-config.example.json"


def test_config_fifo_without_writer_refuses_promptly_instead_of_blocking_daemon(tmp_path):
    fifo = tmp_path / "owner.json"
    os.mkfifo(fifo)
    code = """
import sys
from src.local_inference.config import load_owner_config
from src.local_inference.models import LocalInferenceError
try:
    load_owner_config(sys.argv[1])
except LocalInferenceError as error:
    assert error.code == 'configuration_invalid'
else:
    raise AssertionError('nonregular configuration accepted')
"""
    result = subprocess.run([sys.executable, "-c", code, str(fifo)], capture_output=True, text=True, timeout=5)
    assert result.returncode == 0


def test_owner_selected_regular_config_symlink_remains_supported(tmp_path):
    target = tmp_path / "regular.json"
    target.write_text('{"enabled":false}')
    link = tmp_path / "owner.json"
    link.symlink_to(target)
    assert load_owner_config(str(link)).enabled is False


def test_complete_owner_example_and_expanded_revision():
    data = json.loads(SPEC.read_text())
    config = OwnerConfig.model_validate(data)
    assert not config.enabled
    assert config.revision == OwnerConfig.model_validate(config.model_dump(mode="json", by_alias=True)).revision
    assert len(config.revision) == 64
    assert config.max_context_jobs == 200
    assert config.max_release_tombstones == 1024
    assert config.redis.max_pipeline_chunks == 8


def test_unset_configuration_is_disabled_without_file_read(monkeypatch):
    monkeypatch.setattr("builtins.open", lambda *a, **k: pytest.fail("unexpected configuration read"))
    assert not load_owner_config(environ={}).enabled


@pytest.mark.parametrize("field,value", [
    ("enabled", 1), ("port", True), ("port", 65536), ("threads", 0),
    ("llama_server_binary", "relative"), ("model_path", "relative"),
    ("max_queue", -1), ("max_context_jobs", 0), ("max_release_tombstones", 0),
    ("max_contexts", False), ("max_source_files", 33), ("max_source_file_bytes", 0),
    ("max_context_ttl_seconds", 29), ("max_context_requests", 101),
    ("max_task_bytes", 65537), ("max_output_tokens", 2049),
    ("shutdown_grace_seconds", float("nan")), ("shutdown_grace_seconds", True),
    ("kv_type_k", "unknown"), ("source_roots", {"../root": {"path": "/root", "workspace_root": "/root"}}),
])
def test_invalid_owner_fields_are_not_clamped(field, value):
    with pytest.raises(ValidationError):
        OwnerConfig.model_validate({field: value})


@pytest.mark.parametrize("data", [
    {"unexpected": 1}, {"schema": "wrong"}, {"enabled": True},
    {"source_roots": {"root": {"path": "relative", "workspace_root": "/root"}}},
    {"redis": {"enabled": "false"}}, {"redis": {"url": "redis://secret"}},
    {"redis": {"max_pipeline_chunks": 9}},
    {"redis": {"chunk_bytes": 1, "max_snapshot_bytes": 257}},
    {"redis": {"io_timeout_seconds": float("inf")}},
    {"redis": {"max_snapshot_bytes": 3_000_000_000}},
    {"redis": {"io_timeout_seconds": 31}},
])
def test_invalid_nested_configuration(data):
    with pytest.raises(ValidationError):
        OwnerConfig.model_validate(data)


def test_source_roots_and_config_are_immutable():
    config = OwnerConfig(source_roots={"root": {"path": "/root", "workspace_root": "/root"}})
    with pytest.raises(TypeError):
        config.source_roots["other"] = config.source_roots["root"]
    with pytest.raises(ValidationError):
        config.enabled = True


@pytest.mark.parametrize("payload", [b'{"enabled":false,"enabled":true}', b'{"threads":NaN}', b'[]', b'\xff'])
def test_invalid_file_has_bounded_public_error(tmp_path, payload):
    path = tmp_path / "config.json"
    path.write_bytes(payload)
    with pytest.raises(LocalInferenceError) as caught:
        load_owner_config(str(path))
    assert caught.value.code == "configuration_invalid"
    assert str(path) not in str(caught.value)


def test_load_absolute_file_and_config_credential_revision(tmp_path):
    path = tmp_path / "config.json"
    path.write_text('{"enabled":false}')
    assert not load_owner_config(str(path)).enabled
    with pytest.raises(LocalInferenceError, match="invalid"):
        load_owner_config("relative.json")
    first = load_owner_config(str(path), environ={"AGENTS_LOCAL_KV_REDIS_URL": "secret-one"}).revision
    assert first == load_owner_config(str(path), environ={"AGENTS_LOCAL_KV_REDIS_URL": "secret-two"}).revision


def test_local_package_import_has_no_optional_io():
    code = '''
import pydantic, pathlib, subprocess, socket, os

class DependencyWarmup(pydantic.BaseModel):
    pass

def denied(*a, **k):
    raise AssertionError("import attempted optional I/O")

subprocess.Popen = denied
socket.create_connection = denied
os.open = denied
pathlib.Path.open = denied
import src.local_inference.config
import src.local_inference.context
import src.local_inference.models
import src.local_inference.profile
print(src.local_inference.config.__file__)
'''
    result = subprocess.run([sys.executable, "-c", code], cwd=SPEC.parents[2],
                            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    assert str(SPEC.parents[2] / "src/local_inference/config.py") in result.stdout
