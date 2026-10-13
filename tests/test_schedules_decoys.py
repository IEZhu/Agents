"""Decoys of the autodev sandbox (#343): planted in a temporary home only, with a fake ``security``."""
import json
import os
import stat
import subprocess

import pytest

from src.schedules import config as autodev_config
from src.schedules import decoys
from src.schedules.lease import schedules_dir
from src.schedules.scan import HitKind, Scanner


@pytest.fixture
def home(tmp_path):
    home = tmp_path / "home"
    (home / ".ssh").mkdir(parents=True, mode=0o700)
    return home


class FakeSecurity:
    """Answers /usr/bin/security; never touches a keychain."""

    def __init__(self, present=False):
        self.calls = []
        self.present = present

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        if argv[1] == "find-generic-password":
            return subprocess.CompletedProcess(argv, 0 if self.present else 44)
        if argv[1] == "add-generic-password":
            self.present = True
        return subprocess.CompletedProcess(argv, 0)


def test_plants_private_decoys_with_long_unique_markers(service_state_dir, home):
    planted = decoys.ensure(service_state_dir, home)
    assert [decoy.label for decoy in planted] == ["ssh-key", "credentials", "token"]
    markers = [decoy.marker for decoy in planted]
    assert len(set(markers)) == 3
    for decoy in planted:
        assert len(decoy.marker) >= 40
        assert any(c.isdigit() for c in decoy.marker) and any(c.isupper() for c in decoy.marker)
        assert stat.S_IMODE(os.lstat(decoy.path).st_mode) == 0o600
        text = decoy.path.read_text()
        assert decoy.marker in text and decoys.DECOY_LINE in text
        assert decoy.known.label == f"decoy {decoy.label}" and decoy.marker not in repr(decoy)
    assert planted[0].path == home / ".ssh/agents_autodev_decoy_ed25519"
    assert stat.S_IMODE(os.stat(home / decoys.DECOY_DIR).st_mode) == 0o700
    registry = json.loads(decoys.registry_path(service_state_dir).read_text())
    assert stat.S_IMODE(os.stat(decoys.registry_path(service_state_dir)).st_mode) == 0o600
    assert [entry["marker"] for entry in registry["files"]] == markers


def test_planting_is_idempotent_and_repairs_a_changed_decoy(service_state_dir, home):
    first = decoys.ensure(service_state_dir, home)
    mtime = os.stat(first[1].path).st_mtime_ns
    assert decoys.ensure(service_state_dir, home) == first
    assert os.stat(first[1].path).st_mtime_ns == mtime
    first[2].path.write_text("changed")
    again = decoys.ensure(service_state_dir, home)
    assert again == first and first[2].marker in first[2].path.read_text()


def test_never_overwrites_a_file_that_is_not_a_decoy(service_state_dir, home):
    (home / decoys.DECOY_DIR).mkdir(mode=0o700)
    (home / decoys.DECOY_DIR / "credentials.json").write_text("the owner's own file")
    with pytest.raises(decoys.DecoyError):
        decoys.ensure(service_state_dir, home)
    assert (home / decoys.DECOY_DIR / "credentials.json").read_text() == "the owner's own file"


def test_adopts_a_decoy_whose_registry_was_lost(service_state_dir, home):
    first = decoys.ensure(service_state_dir, home)
    decoys.registry_path(service_state_dir).unlink()
    second = decoys.ensure(service_state_dir, home)
    assert [d.path for d in second] == [d.path for d in first]
    assert all(d.marker in d.path.read_text() for d in second)


def test_no_ssh_decoy_without_an_ssh_directory(service_state_dir, tmp_path):
    planted = decoys.ensure(service_state_dir, tmp_path / "bare-home")
    assert [decoy.label for decoy in planted] == ["credentials", "token"]
    assert not (tmp_path / "bare-home/.ssh").exists()


def test_keychain_decoy_is_opt_in(service_state_dir, home):
    security = FakeSecurity()
    decoys.ensure(service_state_dir, home, runner=security)
    assert security.calls == []
    planted = decoys.ensure(service_state_dir, home, keychain=True, runner=security)
    item = planted[-1]
    assert item.kind == "keychain" and item.path is None
    add = next(call for call in security.calls if call[1] == "add-generic-password")
    assert add[:9] == ["/usr/bin/security", "add-generic-password", "-U", "-s", "agents-autodev-decoy", "-a",
                       "autodev-decoy", "-T", "/usr/bin/security"] and add[-1] == item.marker
    security.calls.clear()
    assert decoys.ensure(service_state_dir, home, keychain=True, runner=security)[-1] == item
    assert [call[1] for call in security.calls] == ["find-generic-password"]
    assert decoys.registered(service_state_dir)[-1] == item


def test_keychain_failure_is_an_error_without_output(service_state_dir, home):
    def failing(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1 if argv[1] == "add-generic-password" else 44)

    with pytest.raises(decoys.DecoyError, match="exit code 1"):
        decoys.ensure(service_state_dir, home, keychain=True, runner=failing)


def test_remove_deletes_every_decoy_and_the_registry(service_state_dir, home):
    security = FakeSecurity()
    planted = decoys.ensure(service_state_dir, home, keychain=True, runner=security)
    (home / ".ssh" / "id_ed25519").write_text("the owner's key")
    removed = decoys.remove(service_state_dir, runner=security)
    assert all(not os.path.lexists(d.path) for d in planted if d.path)
    assert not (home / decoys.DECOY_DIR).exists() and (home / ".ssh/id_ed25519").exists()
    assert "keychain item agents-autodev-decoy" in removed
    assert any(call[1] == "delete-generic-password" for call in security.calls)
    assert not decoys.registry_path(service_state_dir).exists() and decoys.registered(service_state_dir) == []


def test_ephemeral_decoys(tmp_path):
    decoy = decoys.plant(tmp_path / "claude-501", "claude-temp")
    assert decoy.kind == "ephemeral" and decoy.marker in decoy.path.read_text()
    assert stat.S_IMODE(os.stat(tmp_path / "claude-501").st_mode) == 0o700
    decoys.discard(decoy)
    assert not decoy.path.exists()


def test_the_scan_finds_every_marker_in_every_form(service_state_dir, home):
    planted = decoys.ensure(service_state_dir, home)
    assert decoys.self_test(planted) == []
    scanner = Scanner(decoys.known(planted))
    hits = scanner.scan(f"leaked: {planted[1].marker}")
    assert [hit.name for hit in decoys.decoy_hits(hits)] == ["decoy credentials"]
    assert all(hit.kind == HitKind.KNOWN for hit in decoys.decoy_hits(hits))


def test_trip_turns_the_schedule_off_and_records_why(service_state_dir):
    autodev_config.save({**autodev_config.DEFAULTS, "enabled": True}, service_state_dir)
    entry = decoys.trip(service_state_dir, "decoy read: credentials", run_id="20261013T120000Z-abcdef")
    assert autodev_config.load(service_state_dir)["enabled"] is False
    assert entry["schedule"] == "disabled"
    stored = json.loads((schedules_dir(service_state_dir) / decoys.TRIPWIRE_NAME).read_text())
    assert stored["count"] == 1 and stored["last"]["reason"] == "decoy read: credentials"
    decoys.trip(service_state_dir, "again")
    assert json.loads((schedules_dir(service_state_dir) / decoys.TRIPWIRE_NAME).read_text())["count"] == 2


def test_trip_records_even_with_an_invalid_config(service_state_dir):
    path = autodev_config.config_path(service_state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": "auto"}))
    entry = decoys.trip(service_state_dir, "decoy leaked")
    assert entry["schedule"].startswith("not changed")
    assert (schedules_dir(service_state_dir) / decoys.TRIPWIRE_NAME).exists()
