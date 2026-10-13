"""Decoys of the autodev sandbox (#343; design 5.6, 5.7): fake secrets that trip the scan.

The runner plants decoy files in the owner's home directory and registers their markers as
known secrets (`src.schedules.scan.KnownSecret`), so a marker in a probe's output, in the broker's
outgoing text or anywhere the scan looks is a ``known`` hit naming only the decoy's label. A
decoy is never used for anything; reading one, or sending its marker out, turns the schedule off
(`trip`) and is recorded.

* **Files** (`ensure`): ``~/.ssh/agents_autodev_decoy_ed25519`` (a fake key block under a name
  ``ssh`` never tries on its own; planted only when ``~/.ssh`` exists), and
  ``~/.agents-autodev-decoys/credentials.json`` and ``github-token.txt``. Each file is 0600, holds
  `DECOY_LINE` (a plain line the Edit probe targets) and one marker of `MARKER_LENGTH` random
  letters and digits with both cases and a digit: unique per install, at or above 40 characters
  so the entropy check is a reliable second detector, and never written to a prompt.
* **Keychain item** (only with ``autodev.json`` ``sandbox.decoy_keychain: true``): a generic
  password ``agents-autodev-decoy`` (account ``autodev-decoy``) whose password is a marker,
  created by ``/usr/bin/security`` with ``-T /usr/bin/security``, so a session that can reach
  securityd reads it without a prompt, as it would the broker's token. It is opt-in because it
  writes to the owner's login keychain.
* **Ephemeral decoys** (`plant`): a probe plants one in the owner's temporary directory and one in
  Claude Code's per-user temporary root and removes them afterwards (`discard`).

Creation is idempotent: a decoy whose file still matches its registered marker is kept, and a
path that exists but is not a registered decoy is never overwritten (`DecoyError`). The registry
``<state>/schedules/decoys.json`` (0600, in the private state directory, which the sandbox
denies) holds the paths and markers; `remove` deletes every decoy and the registry, and the
uninstaller (`src.uninstall`) lists the registered paths too.
"""
from __future__ import annotations

import base64
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import secrets
import stat
import string
import subprocess

from src.daemon.state import private_dir, read_json, write_json

from . import config as autodev_config
from .github import SECURITY
from .lease import schedules_dir
from .scan import HitKind, KnownSecret, Scanner

REGISTRY_NAME = "decoys.json"
TRIPWIRE_NAME = "tripwire.json"
DECOY_DIR = ".agents-autodev-decoys"
MARKER_LENGTH = 48
MARKER_ALPHABET = string.ascii_letters + string.digits
DECOY_LINE = "agents-autodev decoy: not a credential, never used"
LABEL_PREFIX = "decoy "
KEYCHAIN_SERVICE = "agents-autodev-decoy"
KEYCHAIN_ACCOUNT = "autodev-decoy"
KEYCHAIN_TIMEOUT = 30
EPHEMERAL_PREFIX = "agents-autodev-decoy-"
# (label, path relative to the home directory, content form)
FILES = (
    ("ssh-key", ".ssh/agents_autodev_decoy_ed25519", "ssh"),
    ("credentials", f"{DECOY_DIR}/credentials.json", "json"),
    ("token", f"{DECOY_DIR}/github-token.txt", "plain"),
)

Runner = Callable[..., subprocess.CompletedProcess]


class DecoyError(RuntimeError):
    """A decoy cannot be planted or removed safely."""


@dataclass(frozen=True)
class Decoy:
    """One decoy: a file (``path``) or the keychain item (``path`` is None)."""

    label: str
    path: Path | None
    marker: str = field(repr=False)
    kind: str = "file"  # file, keychain or ephemeral

    @property
    def known(self) -> KnownSecret:
        return KnownSecret(LABEL_PREFIX + self.label, self.marker)


def new_marker() -> str:
    """`MARKER_LENGTH` random letters and digits with a digit and both letter cases."""
    while True:
        marker = "".join(secrets.choice(MARKER_ALPHABET) for _ in range(MARKER_LENGTH))
        if any(c.isdigit() for c in marker) and any(c.islower() for c in marker) and any(c.isupper() for c in marker):
            return marker


def _valid_marker(value: object) -> bool:
    return isinstance(value, str) and len(value) >= MARKER_LENGTH and all(c in MARKER_ALPHABET for c in value)


# A key block's armour, split so this source file is no private-key hit of the scan.
_KEY_ARMOUR = ("-----BEGIN OPENSSH " + "PRIVATE KEY-----", "-----END OPENSSH " + "PRIVATE KEY-----")


def content(form: str, marker: str) -> str:
    if form == "ssh":
        return f"{_KEY_ARMOUR[0]}\n{DECOY_LINE}\n{marker}\n{_KEY_ARMOUR[1]}\n"
    if form == "json":
        return json.dumps({"note": DECOY_LINE, "token": marker}, indent=2) + "\n"
    return f"{DECOY_LINE}\n{marker}\n"


def registry_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / REGISTRY_NAME


def _load(directory: str | Path | None) -> dict:
    try:
        data = read_json(registry_path(directory), None)
    except (OSError, ValueError):
        data = None
    if not isinstance(data, dict):
        return {"version": 1, "files": [], "keychain": None}
    files = [entry for entry in data.get("files") or [] if isinstance(entry, dict)
             and isinstance(entry.get("label"), str) and isinstance(entry.get("path"), str)
             and _valid_marker(entry.get("marker"))]
    keychain = data.get("keychain") if isinstance(data.get("keychain"), dict) and \
        _valid_marker(data["keychain"].get("marker")) else None
    return {"version": 1, "files": files, "keychain": keychain}


def _save(directory: str | Path | None, registry: dict) -> None:
    private_dir(schedules_dir(directory))
    write_json(registry_path(directory), registry)


def _matches(path: Path, expected: str) -> bool:
    try:
        found = os.lstat(path)
    except OSError:
        return False
    if not stat.S_ISREG(found.st_mode) or found.st_uid != os.getuid() or stat.S_IMODE(found.st_mode) != 0o600 \
            or found.st_nlink != 1:
        return False
    try:
        return path.read_text(encoding="utf-8") == expected
    except (OSError, UnicodeDecodeError):
        return False


def _is_decoy_file(path: Path) -> bool:
    """A regular file of this user holding `DECOY_LINE`: a decoy whose registry entry was lost."""
    try:
        found = os.lstat(path)
        return stat.S_ISREG(found.st_mode) and found.st_uid == os.getuid() and found.st_size < 4096 \
            and DECOY_LINE in path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return False


def _write_new(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(text)
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(path, 0o600, follow_symlinks=False)


def _unlink_entry(path: Path) -> bool:
    """Remove a regular file or link at ``path`` (never a directory); False when nothing was there."""
    try:
        found = os.lstat(path)
    except FileNotFoundError:
        return False
    if stat.S_ISDIR(found.st_mode):
        raise DecoyError(f"{path} is a directory, not a decoy file")
    os.unlink(path)
    return True


def ensure(directory: str | Path | None, home: Path, *, keychain: bool = False,
           runner: Runner = subprocess.run) -> list[Decoy]:
    """Plant the decoys that are missing or changed and return them all (idempotent)."""
    home = Path(home)
    registry = _load(directory)
    registered = {entry["path"]: entry for entry in registry["files"]}
    planted: list[Decoy] = []
    entries = []
    for label, relative, form in FILES:
        path = home / relative
        parent = path.parent
        if label == "ssh-key":
            if not parent.is_dir() or parent.is_symlink():
                continue  # no ~/.ssh: nothing would look for a key there
        else:
            private_dir(parent)
        entry = registered.get(str(path))
        if entry is None and os.path.lexists(path) and not _is_decoy_file(path):
            raise DecoyError(f"{path} exists and is not a decoy of this runner; it is never overwritten")
        marker = entry["marker"] if entry else new_marker()
        text = content(form, marker)
        if not _matches(path, text):
            _unlink_entry(path)  # a registered decoy that changed: ours to replace
            _write_new(path, text)
        entries.append({"label": label, "path": str(path), "marker": marker})
        planted.append(Decoy(label, path, marker))
    registry["files"] = entries
    if keychain:
        item = registry["keychain"] or {"service": KEYCHAIN_SERVICE, "account": KEYCHAIN_ACCOUNT, "marker": new_marker()}
        present = _run(runner, [SECURITY, "find-generic-password", "-s", item["service"], "-a", item["account"]])
        if present.returncode != 0 or registry["keychain"] is None:
            added = _run(runner, [SECURITY, "add-generic-password", "-U", "-s", item["service"], "-a", item["account"],
                                  "-T", SECURITY, "-w", item["marker"]])
            if added.returncode != 0:
                raise DecoyError(f"the decoy keychain item could not be created (security exit code {added.returncode})")
        registry["keychain"] = item
        planted.append(Decoy("keychain-item", None, item["marker"], "keychain"))
    _save(directory, registry)
    return planted


def _run(runner: Runner, argv: list[str]) -> subprocess.CompletedProcess:
    """Run ``security`` with its output discarded; an error is an exit code, never text."""
    try:
        return runner(argv, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                      timeout=KEYCHAIN_TIMEOUT, check=False)
    except (OSError, subprocess.SubprocessError):
        return subprocess.CompletedProcess(argv, 127)


def registered(directory: str | Path | None = None) -> list[Decoy]:
    """The registered decoys (files and the keychain item), without touching them."""
    registry = _load(directory)
    found = [Decoy(entry["label"], Path(entry["path"]), entry["marker"]) for entry in registry["files"]]
    if registry["keychain"]:
        found.append(Decoy("keychain-item", None, registry["keychain"]["marker"], "keychain"))
    return found


def remove(directory: str | Path | None, *, runner: Runner = subprocess.run) -> list[str]:
    """Delete every registered decoy, the decoy directory when empty, the keychain item and the
    registry; return what was removed."""
    registry = _load(directory)
    removed = []
    parents = set()
    for entry in registry["files"]:
        path = Path(entry["path"])
        if _unlink_entry(path):
            removed.append(str(path))
        if path.parent.name == DECOY_DIR:
            parents.add(path.parent)
    for parent in parents:
        try:
            parent.rmdir()
            removed.append(str(parent))
        except OSError:
            pass  # not empty or already gone: left for the owner
    if registry["keychain"]:
        item = registry["keychain"]
        result = _run(runner, [SECURITY, "delete-generic-password", "-s", item["service"], "-a", item["account"]])
        if result.returncode == 0:
            removed.append(f"keychain item {item['service']}")
    try:
        registry_path(directory).unlink()
        removed.append(str(registry_path(directory)))
    except FileNotFoundError:
        pass
    return removed


def plant(directory: Path, label: str) -> Decoy:
    """An ephemeral decoy file in ``directory`` (created 0700 when missing); `discard` removes it."""
    directory = Path(directory)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    found = os.lstat(directory)
    if not stat.S_ISDIR(found.st_mode) or found.st_uid != os.getuid():
        raise DecoyError(f"{directory} is not a directory of this user")
    marker = new_marker()
    path = directory / f"{EPHEMERAL_PREFIX}{secrets.token_hex(6)}.txt"
    _write_new(path, content("plain", marker))
    return Decoy(label, path, marker, "ephemeral")


def discard(decoy: Decoy) -> None:
    if decoy.kind == "ephemeral" and decoy.path is not None:
        _unlink_entry(decoy.path)


def known(decoys: Iterable[Decoy]) -> list[KnownSecret]:
    return [decoy.known for decoy in decoys]


def is_decoy_label(name: str) -> bool:
    return name.startswith(LABEL_PREFIX)


def decoy_hits(hits: Iterable) -> list:
    """The ``known`` hits that name a decoy."""
    return [hit for hit in hits if hit.kind == HitKind.KNOWN and is_decoy_label(hit.name)]


def self_test(decoys: Iterable[Decoy]) -> list[str]:
    """Check that the scan finds every decoy marker raw, base64, hex, split over lines and inside
    a JSON string (probe C19); return the labels and forms it missed."""
    decoys = list(decoys)
    scanner = Scanner(known(decoys))
    missed = []
    for decoy in decoys:
        marker = decoy.marker
        half = len(marker) // 2
        forms = {
            "raw": f"output: {marker} end",
            "base64": base64.b64encode(f"x{marker}y".encode()).decode(),
            "hex": marker.encode().hex(),
            "split": f"{marker[:half]}\n  {marker[half:]}",
            "json": json.dumps({"text": f"a\n{marker}\tb"}),
        }
        for form, text in forms.items():
            if not any(hit.kind == HitKind.KNOWN and hit.name == LABEL_PREFIX + decoy.label for hit in scanner.scan(text)):
                missed.append(f"{decoy.label}: {form}")
    return missed


def trip(directory: str | Path | None, reason: str, *, run_id: str | None = None,
         now: datetime | None = None) -> dict:
    """Turn the schedule off (``enabled: false`` in ``autodev.json``) and record why in
    ``<state>/schedules/tripwire.json``; ``reason`` names labels and checks, never a marker."""
    now = now or datetime.now(timezone.utc)
    entry = {"time": now.astimezone(timezone.utc).isoformat(timespec="seconds"), "reason": reason, "run_id": run_id}
    try:
        config = autodev_config.load(directory)
        config["enabled"] = False
        autodev_config.save(config, directory)
        entry["schedule"] = "disabled"
    except (autodev_config.ConfigError, OSError) as error:
        entry["schedule"] = f"not changed: {type(error).__name__}"
    path = schedules_dir(directory) / TRIPWIRE_NAME
    try:
        previous = read_json(path, None)
    except (OSError, ValueError):
        previous = None
    count = previous.get("count", 0) if isinstance(previous, dict) and isinstance(previous.get("count"), int) else 0
    private_dir(schedules_dir(directory))
    write_json(path, {"count": count + 1, "last": entry})
    return entry
