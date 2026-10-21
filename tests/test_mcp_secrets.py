"""Native-store facade tests; these never use a real OS secret store."""
import json

import pytest

from src.mcp_secrets import Secrets, SecretsError
from src.user_sync.github import FileStore


class FakeStore:
    backend = "fake-native"

    def __init__(self):
        self.value = None
        self.saves = 0

    def load(self):
        return self.value

    def save(self, value):
        assert 8 <= len(value) <= 1024
        assert all(33 <= ord(c) <= 126 for c in value)
        self.value = value
        self.saves += 1


@pytest.fixture
def fixture(tmp_path):
    stores = {}
    secrets = Secrets(tmp_path / "state", lambda name: stores.setdefault(name, FakeStore()))
    return secrets, stores


def test_round_trip_complete_headers_and_status_never_contains_values(fixture):
    secrets, stores = fixture
    assert secrets.load("example") == {}
    status = secrets.set("example", "Authorization", "Bearer actual-secret")
    assert secrets.load("example") == {"Authorization": "Bearer actual-secret"}
    assert status == {"backend": "fake-native", "names": ["Authorization"], "configured": {"Authorization": True}}
    assert secrets.status("example") == status
    assert "actual-secret" not in json.dumps(status)
    assert "actual-secret" not in next(iter(stores.values())).value
    assert [path.name for path in secrets.state_dir.rglob("*") if path.is_file()] == [".secrets.lock"]


def test_header_updates_are_case_insensitive_and_other_secrets_survive(fixture):
    secrets, _ = fixture
    secrets.set("example", "Authorization", "Bearer old")
    secrets.set("example", "X-API-Key", "api-value")
    secrets.set("example", "authorization", "Bearer new")
    assert secrets.load("example") == {"authorization": "Bearer new", "X-API-Key": "api-value"}


def test_store_names_are_per_installation_and_alias(tmp_path):
    names = []
    factory = lambda name: names.append(name) or FakeStore()
    Secrets(tmp_path / "first", factory).load("example")
    Secrets(tmp_path / "first", factory).load("other")
    Secrets(tmp_path / "second", factory).load("example")
    assert len(set(names)) == 3
    assert all(name.startswith("agents-core-mcp-") for name in names)
    assert not (tmp_path / "first").exists()


def test_file_store_is_rejected_even_when_injected(tmp_path):
    fallback = FileStore(tmp_path)
    secrets = Secrets(tmp_path / "state", lambda name: fallback)
    with pytest.raises(SecretsError) as error:
        secrets.set("example", "Authorization", "Bearer private")
    assert error.value.code == "no_secret_store"
    assert secrets.status("example")["error"] == "no_secret_store"
    assert not fallback.path.exists()


def test_default_file_fallback_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr("src.mcp_secrets.default_store", lambda directory, name: FileStore(directory))
    assert Secrets(tmp_path).status("example")["error"] == "no_secret_store"
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize("name,value", [("Authorization", "x\r\nInjected: yes"), ("Authorization", ""), ("Authorization", "unicodé"), ("Bad Header", "private"), ("Host", "example.test"), ("X-Agents-Client", "codex")])
def test_invalid_headers_rejected_before_writes(fixture, name, value):
    secrets, stores = fixture
    with pytest.raises(SecretsError):
        secrets.set("example", name, value)
    assert stores == {}
    assert not secrets.state_dir.exists()


def test_invalid_alias_does_not_open_store(fixture):
    secrets, stores = fixture
    with pytest.raises(SecretsError):
        secrets.load("../escape")
    assert stores == {}


def test_oversized_document_is_rejected_without_losing_existing_value(fixture):
    secrets, stores = fixture
    secrets.set("example", "Authorization", "Bearer private")
    before = next(iter(stores.values())).value
    with pytest.raises(SecretsError) as error:
        secrets.set("example", "X-API-Key", "A" * 1024)
    assert error.value.code == "invalid"
    assert next(iter(stores.values())).value == before
    assert secrets.load("example") == {"Authorization": "Bearer private"}


def test_store_errors_are_sanitized_and_never_fall_back_to_files(tmp_path):
    class BrokenStore(FakeStore):
        def save(self, value):
            raise RuntimeError(f"failure including secret {value}")
    secrets = Secrets(tmp_path / "state", lambda name: BrokenStore())
    with pytest.raises(SecretsError) as error:
        secrets.set("example", "Authorization", "Bearer private")
    assert "private" not in str(error.value)
    assert "including" not in str(error.value)
    assert [path.name for path in secrets.state_dir.rglob("*") if path.is_file()] == [".secrets.lock"]


def test_corrupt_store_data_is_not_returned(fixture):
    secrets, stores = fixture
    secrets.load("example")
    next(iter(stores.values())).value = "not JSON containing private"
    with pytest.raises(SecretsError) as error:
        secrets.load("example")
    assert "private" not in str(error.value)
    assert secrets.status("example") == {"backend": None, "names": [], "configured": {}, "error": "storage"}
