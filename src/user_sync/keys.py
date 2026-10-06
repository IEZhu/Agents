"""This machine's SSH key and the host keys sync trusts.

The key is an ed25519 pair made with ``ssh-keygen`` in the private state directory. Its comment
is ``agents-core-sync:<label>``, never the hostname, because the public key ends up in the
repository's deploy keys. The key is meant for one repository only, as a deploy key with write
access.

Host keys go to sync's own ``known_hosts`` and are checked strictly. github.com's keys come from
``https://api.github.com/meta``; they are also written for ``ssh.github.com`` port 443, the
fallback for networks that block port 22. Other hosts are scanned with ``ssh-keyscan`` and
trusted only after the owner confirms a fingerprint.
"""
from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from urllib.request import Request, urlopen

from src.user_sync.gitcmd import clean_environment, no_window

KEY_NAME = "id_ed25519"
GITHUB_META = "https://api.github.com/meta"
_KEY_TYPES = ("ssh-ed25519", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521",
              "ssh-rsa", "sk-ssh-ed25519@openssh.com", "sk-ecdsa-sha2-nistp256@openssh.com")


class SSHKeyError(RuntimeError):
    """A key or host key could not be made, read or checked."""


class PublicKeyNotWritten(SSHKeyError):
    """A new private key is in place, but its public half could not be written next to it."""


def key_path(state: Path) -> Path:
    return Path(state) / KEY_NAME


def public_key(state: Path) -> str | None:
    """``ssh-ed25519 AAAA… agents-core-sync:<label>``, or None before the key exists."""
    return _read_public(key_path(state))


def _public_of(private: Path) -> Path:
    return private.with_name(private.name + ".pub")


def _read_public(private: Path) -> str | None:
    try:
        return _public_of(private).read_text(encoding="ascii").strip() or None
    except (OSError, UnicodeDecodeError):
        return None


def ensure_key(state: Path, label: str) -> str:
    """Create this machine's key once; returns its public line.

    A private key without its public file (one that could not be written when the key was replaced)
    stays this machine's key: the public line is derived from it and written again.
    """
    private = key_path(state)
    existing = public_key(state)
    if private.is_file() and existing:
        return existing
    if private.is_file():
        derived = derive_public(private, label)
        if derived:
            try:
                write_private(_public_of(private), (derived + "\n").encode("ascii"))
            except OSError as error:
                raise SSHKeyError(f"{_public_of(private).name} could not be written "
                                  f"({error.strerror or error})") from None
            return derived
    return generate_key(private, label)


def current_public(state: Path) -> str | None:
    """This machine's public key: its public file, or, when that is missing, derived from the private key."""
    line = public_key(state)
    if line or not key_path(state).is_file():
        return line
    return derive_public(key_path(state))


def derive_public(private: Path, label: str | None = None) -> str | None:
    """The public line of the private key at ``private`` (``ssh-keygen -y``), or None when it cannot be read.

    ``-P ""`` makes a key with a passphrase fail at once instead of asking for it.
    """
    try:
        result = subprocess.run(["ssh-keygen", "-y", "-P", "", "-f", str(private)], capture_output=True,
                                text=True, timeout=30, env=clean_environment(), stdin=subprocess.DEVNULL,
                                **no_window())
    except (OSError, subprocess.SubprocessError):
        return None
    material = key_material(result.stdout) if result.returncode == 0 else None
    if not material:
        return None
    return " ".join(material) + (f" agents-core-sync:{label}" if label else "")


def generate_key(private: Path, label: str) -> str:
    """A new ed25519 pair at ``private`` and ``<private>.pub``, replacing any; returns its public line."""
    discard_key(private)
    try:
        result = subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C",
                                 f"agents-core-sync:{label}", "-f", str(private)],
                                capture_output=True, text=True, timeout=30, env=clean_environment(),
                                stdin=subprocess.DEVNULL, **no_window())
    except (OSError, subprocess.SubprocessError) as error:
        raise SSHKeyError(f"ssh-keygen could not run ({error}); install OpenSSH") from None
    if result.returncode != 0 or not _read_public(private):
        raise SSHKeyError(f"ssh-keygen failed: {result.stderr.strip() or result.returncode}")
    if os.name == "posix":
        private.chmod(0o600)
    return _read_public(private)


def install_key(private: Path, state: Path, public: str, *, moved=None) -> None:
    """Make the pair at ``private`` (``public`` is its public line) this machine's key.

    The private file moves first. An ``OSError`` from that move changed nothing, and the caller may
    discard the pair. ``moved()`` is called as soon as it has moved: from then on the new key is
    this machine's and is never discarded, whatever interrupts the rest. The public file follows
    (``install_public``).
    """
    os.replace(private, key_path(state))
    if moved is not None:
        moved()
    install_public(private, state, public)


def install_public(private: Path, state: Path, public: str) -> None:
    """Put the public half of the pair at ``private`` next to this machine's key, which is already in place.

    The public file moves, or is written from ``public`` when it cannot. When neither works, the
    old public file is removed, so that ssh derives the public key from the new private key instead
    of offering the old one, and ``PublicKeyNotWritten`` says so. Calling it again is harmless.
    """
    target = _public_of(key_path(state))
    leftover = _public_of(private)
    try:
        os.replace(leftover, target)
        return
    except OSError:
        pass
    try:
        write_private(target, (public.strip() + "\n").encode("ascii"))
    except OSError as error:
        problem = f"{target.name} could not be written ({error.strerror or error})"
        try:
            target.unlink(missing_ok=True)
        except OSError as stale:
            problem += (f", and the old one could not be removed ({stale.strerror or stale}): remove {target}, "
                        "or ssh offers the old key")
        raise PublicKeyNotWritten(problem) from None
    finally:  # only a leftover copy: no failure here may read as "nothing changed"
        try:
            leftover.unlink(missing_ok=True)
        except OSError:
            pass


def write_private(path: Path, data: bytes) -> None:
    """Replace ``path`` with ``data`` through a temporary file only this user can read."""
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
        try:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            stream.close()
            os.unlink(stream.name)
            raise
    try:
        os.replace(stream.name, path)
    except BaseException:
        Path(stream.name).unlink(missing_ok=True)
        raise


def discard_key(private: Path) -> None:
    for path in (private, _public_of(private)):
        path.unlink(missing_ok=True)


def fingerprint(line: str) -> str:
    """``SHA256:…`` of a public key or ``known_hosts`` line, as ``ssh-keygen -l`` prints it."""
    parts = line.split()
    for index, part in enumerate(parts):
        if part in _KEY_TYPES and index + 1 < len(parts):
            blob = base64.b64decode(parts[index + 1])
            return "SHA256:" + base64.b64encode(hashlib.sha256(blob).digest()).decode().rstrip("=")
    raise SSHKeyError("not an SSH public key")


def key_material(line: str) -> tuple[str, str] | None:
    """``(type, base64)`` of a public key line, ignoring its comment."""
    parts = line.split()
    for index, part in enumerate(parts):
        if part in _KEY_TYPES and index + 1 < len(parts):
            return part, parts[index + 1]
    return None


def host_pattern(host: str, port: int | None) -> str:
    return host if port in (None, 22) else f"[{host}]:{port}"


def github_host_keys(fetch=None) -> list[str]:
    """github.com's SSH host keys (``type base64``) from the API's ``meta`` endpoint."""
    if fetch is None:
        def fetch():
            request = Request(GITHUB_META, headers={"Accept": "application/vnd.github+json",
                                                    "User-Agent": "Agents-Core"})
            with urlopen(request, timeout=20) as response:
                return json.load(response)
    try:
        keys = fetch().get("ssh_keys")
    except (OSError, ValueError, AttributeError) as error:
        raise SSHKeyError(f"could not read github.com host keys from {GITHUB_META} ({error})") from None
    keys = [key for key in keys or [] if isinstance(key, str) and key_material(key)]
    if not keys:
        raise SSHKeyError(f"{GITHUB_META} returned no SSH host keys")
    return keys


def scan_host_keys(host: str, port: int | None) -> list[str]:
    """Keys offered by ``host`` (``type base64``), for the owner to confirm by fingerprint."""
    command = ["ssh-keyscan", "-T", "10", "-t", "ed25519,ecdsa,rsa"]
    if port not in (None, 22):
        command += ["-p", str(port)]
    try:
        result = subprocess.run(command + ["--", host], capture_output=True, text=True, timeout=30,
                                env=clean_environment(), stdin=subprocess.DEVNULL, **no_window())
    except (OSError, subprocess.SubprocessError) as error:
        raise SSHKeyError(f"ssh-keyscan could not run ({error})") from None
    keys = []
    for line in result.stdout.splitlines():
        if line.startswith("#"):
            continue
        material = key_material(line)
        if material:
            keys.append(" ".join(material))
    if not keys:
        raise SSHKeyError(f"{host} offered no SSH host key ({result.stderr.strip() or 'no answer'})")
    return keys


def known_hosts_lines(host: str, port: int | None, keys: list[str]) -> list[str]:
    lines = [f"{host_pattern(host, port)} {key}" for key in keys]
    if host == "github.com":
        lines += [f"{host_pattern('ssh.github.com', 443)} {key}" for key in keys]
    return lines


def trusted_keys(known_hosts: Path, host: str, port: int | None) -> list[str]:
    pattern = host_pattern(host, port)
    try:
        lines = Path(known_hosts).read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    return [line.split(None, 1)[1] for line in lines
            if line.split(None, 1)[:1] == [pattern] and len(line.split()) >= 3]


def write_known_hosts(known_hosts: Path, host: str, port: int | None, keys: list[str]) -> None:
    """Replace the entries for ``host`` (and ssh.github.com for GitHub) and keep the others."""
    replaced = {host_pattern(host, port)} | ({host_pattern("ssh.github.com", 443)} if host == "github.com" else set())
    try:
        kept = [line for line in Path(known_hosts).read_text(encoding="utf-8").splitlines()
                if line.split(None, 1)[:1] and line.split(None, 1)[0] not in replaced]
    except OSError:
        kept = []
    content = "\n".join(kept + known_hosts_lines(host, port, keys)) + "\n"
    directory = Path(known_hosts).parent
    with tempfile.NamedTemporaryFile("w", dir=directory, prefix=".tmp-", delete=False, encoding="utf-8") as stream:
        stream.write(content)
    os.replace(stream.name, known_hosts)
