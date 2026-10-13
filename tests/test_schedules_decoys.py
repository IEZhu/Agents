"""Decoys of the autodev sandbox (#343): planted in a temporary home only, with a fake ``security``."""
import json
import os
from pathlib import Path
import re
import stat
import subprocess

import pytest

from src.schedules import config as autodev_config
from src.schedules import decoys
from src.schedules.lease import schedules_dir
from src.schedules.scan import Hit, HitKind, Scanner


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
        assert decoy.marker in decoy.path.read_text()
        assert decoy.known.label == f"decoy {decoy.label}" and decoy.marker not in repr(decoy)
    assert planted[0].path.parent == home / ".ssh" and planted[0].path.name not in SSH_DEFAULT_NAMES
    assert re.search(r"[0-9a-f]{4}", planted[0].path.name)  # a random part keeps it clear of the owner's names
    folder = planted[1].path.parent
    assert folder.parent == home and decoys.DIRECTORY_NAME.fullmatch(folder.name) and planted[2].path.parent == folder
    assert stat.S_IMODE(os.stat(folder).st_mode) == 0o700
    assert decoys.decoy_directory(service_state_dir) == folder
    registry = json.loads(decoys.registry_path(service_state_dir).read_text())
    assert stat.S_IMODE(os.stat(decoys.registry_path(service_state_dir)).st_mode) == 0o600
    assert [entry["marker"] for entry in registry["files"]] == markers and registry["directory"] == str(folder)


def test_decoys_do_not_label_themselves_and_their_names_are_not_in_the_source(service_state_dir, home):
    planted = decoys.ensure(service_state_dir, home)
    item = decoys.ensure(service_state_dir, home, keychain=True, runner=FakeSecurity())[-1]
    source = "".join(path.read_text() for path in (Path(decoys.__file__).parent).glob("*.py"))
    for decoy in planted:
        text = decoy.path.read_text().lower()
        assert "decoy" not in text and "not a credential" not in text and "autodev" not in text
        relative = str(decoy.path.relative_to(home)).lower()
        assert "decoy" not in relative and "autodev" not in relative
        assert decoy.path.name not in source
        assert decoy.path.parent.name == ".ssh" or decoy.path.parent.name not in source
    assert "decoy" not in item.service and item.service not in source and item.account not in source
    other = decoys.ensure(service_state_dir.parent / "other-state", home)  # another installation: other names
    assert {d.path for d in other}.isdisjoint({d.path for d in planted})


def test_planting_is_idempotent_and_repairs_a_changed_decoy(service_state_dir, home):
    first = decoys.ensure(service_state_dir, home)
    mtime = os.stat(first[1].path).st_mtime_ns
    assert decoys.ensure(service_state_dir, home) == first
    assert os.stat(first[1].path).st_mtime_ns == mtime
    first[2].path.write_text("changed")
    again = decoys.ensure(service_state_dir, home)
    assert again == first and first[2].marker in first[2].path.read_text()


def test_never_overwrites_a_file_that_is_not_a_decoy(service_state_dir, home, monkeypatch):
    owned = home / ".ssh" / "id_ed25519_00000000"
    owned.write_text("the owner's own key")
    names = iter(["id_ed25519_00000000", "id_ed25519_11111111"])  # taken, then free
    files = tuple((label, where, form, (lambda: next(names)) if label == "ssh-key" else make)
                  for label, where, form, make in decoys.FILES)
    monkeypatch.setattr(decoys, "FILES", files)
    planted = decoys.ensure(service_state_dir, home)
    assert owned.read_text() == "the owner's own key"
    assert planted[0].path.name == "id_ed25519_11111111"


SSH_DEFAULT_NAMES = {"id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519", "id_ed25519_sk", "id_dsa", "id_xmss"}


def test_decoy_names_take_several_shapes_so_no_single_glob_excludes_them(tmp_path):
    """G4: the shapes are in the source, so each installation picks one of several at random."""
    import fnmatch

    from src import uninstall

    planted = []
    for index in range(40):
        home = tmp_path / f"home{index}"
        (home / ".ssh").mkdir(parents=True)
        planted.append({decoy.label: decoy.path for decoy in decoys.ensure(tmp_path / f"state{index}", home)})
    ssh = [names["ssh-key"].name for names in planted]
    folders = [names["credentials"].parent.name for names in planted]
    assert not set(ssh) & SSH_DEFAULT_NAMES  # ssh never tries a decoy on its own
    for pattern, names in (("id_ed25519_????????", ssh), (".*-????????", folders),
                           ("credentials-*.json", [names["credentials"].name for names in planted]),
                           ("token-*.txt", [names["token"].name for names in planted])):
        assert not all(fnmatch.fnmatchcase(name, pattern) for name in names), pattern
    assert all(decoys.DIRECTORY_NAME.fullmatch(name) and uninstall.DECOY_DIR_NAME.fullmatch(name) for name in folders)
    for _ in range(200):
        name = decoys._directory_name()
        assert decoys.DIRECTORY_NAME.fullmatch(name) and uninstall.DECOY_DIR_NAME.fullmatch(name), name


def test_a_lost_registry_plants_fresh_decoys_and_leaves_the_old_files(service_state_dir, home):
    first = decoys.ensure(service_state_dir, home)
    decoys.registry_path(service_state_dir).unlink()
    second = decoys.ensure(service_state_dir, home)
    assert {d.path for d in second}.isdisjoint({d.path for d in first})
    assert all(d.marker in d.path.read_text() for d in first + second)


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
    assert decoys.KEYCHAIN_SERVICE_NAME.fullmatch(item.service) and decoys.KEYCHAIN_ACCOUNT_NAME.fullmatch(item.account)
    add = next(call for call in security.calls if call[1] == "add-generic-password")
    assert add[:9] == ["/usr/bin/security", "add-generic-password", "-U", "-s", item.service, "-a",
                       item.account, "-T", "/usr/bin/security"] and add[-1] == item.marker
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
    folder = decoys.decoy_directory(service_state_dir)
    (home / ".ssh" / "id_ed25519").write_text("the owner's key")
    planted[0].path.write_text("replaced by the owner")  # no longer holds its marker: kept
    removed = decoys.remove(service_state_dir, runner=security)
    assert all(not os.path.lexists(d.path) for d in planted[1:] if d.path)
    assert planted[0].path.read_text() == "replaced by the owner"
    assert not folder.exists() and (home / ".ssh/id_ed25519").exists()
    assert f"keychain item {planted[-1].service}" in removed
    assert any(call[1] == "delete-generic-password" for call in security.calls)
    assert not decoys.registry_path(service_state_dir).exists() and decoys.registered(service_state_dir) == []


def test_ephemeral_decoys(tmp_path):
    decoy = decoys.plant(tmp_path / "claude-501", "claude-temp-decoy")
    assert decoy.kind == "ephemeral" and decoy.marker in decoy.path.read_text()
    assert "decoy" not in decoy.path.name and re.fullmatch(r"[0-9a-f]{12}\.txt", decoy.path.name)
    assert stat.S_IMODE(os.stat(tmp_path / "claude-501").st_mode) == 0o700
    decoys.discard(decoy)
    assert not decoy.path.exists()


class RawOnlyScanner:
    """A scan that knows the markers only raw: every encoded form is unscannable for it."""

    def __init__(self, known):
        self.known = list(known)

    def scan(self, text):
        return tuple(Hit(0, 1, HitKind.KNOWN, secret.label) for secret in self.known if secret.value in text)


def test_references_name_the_random_file_and_directory(service_state_dir, home):
    planted = decoys.ensure(service_state_dir, home, keychain=True, runner=FakeSecurity())
    references = decoys.references(planted)
    assert references["ssh-key"] == (planted[0].path.name,)
    assert references["credentials"] == (planted[1].path.name, planted[1].path.parent.name)
    assert references["keychain-item"] == (planted[-1].service,)


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
