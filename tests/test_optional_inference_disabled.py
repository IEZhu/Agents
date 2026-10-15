"""Portable import/disabled boundary for the optional POSIX engine."""
from types import SimpleNamespace

import pytest

from src.daemon.workspaces import ClientContext
from src.local_inference.config import OwnerConfig
from src.local_inference.models import LocalInferenceError
from src.local_inference.process import OwnedProcess
from src.local_inference.service import LocalInferenceService


@pytest.mark.asyncio
async def test_disabled_http_status_and_stdio_refusal_do_not_start_optional_resources(tmp_path, monkeypatch):
    monkeypatch.delenv("AGENTS_LOCAL_INFERENCE_CONFIG", raising=False)
    service = LocalInferenceService(tmp_path / "not-created")
    try:
        client = ClientContext("portable", "http", "portable-workspace", tmp_path)
        value = await service.call("local_inference_status", client, {})
        assert value["data"]["enabled"] is False
        assert value["data"]["capabilities"] == {"resident": "unknown", "snapshot": "unknown"}
        assert service.backend is service.snapshots is service._files is None
        assert not service.directory.exists()
        stdio = ClientContext("portable-stdio", "stdio", "portable-workspace", tmp_path)
        assert (await service.call("local_inference_status", stdio, {}))["error"]["code"] == "transport_unsupported"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_enabled_unsupported_platform_refuses_before_native_io(tmp_path, monkeypatch):
    import src.local_inference.process as native
    config = OwnerConfig(enabled=True, llama_server_binary=str(tmp_path / "engine"), model_path=str(tmp_path / "model"))
    backend = OwnedProcess(config, tmp_path / "state")
    monkeypatch.setattr(native.sys, "platform", "unsupported-test-platform")
    try:
        with pytest.raises(LocalInferenceError) as failure:
            await backend.ensure_started()
        assert failure.value.code == "unsupported"
        assert backend._files_executor is backend._child is None
        assert not backend.directory.exists()
    finally:
        await backend.close()


def test_posix_fixture_collection_filter_is_narrow(monkeypatch):
    from tests import conftest
    with monkeypatch.context() as changed:
        changed.setattr(conftest.os, "name", "nt")
        assert conftest.pytest_ignore_collect(SimpleNamespace(name="test_local_inference_process.py"), None)
        assert not conftest.pytest_ignore_collect(SimpleNamespace(name="test_optional_inference_disabled.py"), None)
        assert not conftest.pytest_ignore_collect(SimpleNamespace(name="test_daemon.py"), None)
