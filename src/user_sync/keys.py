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
    """Create this machine's key once; returns its public line."""
    private = key_path(state)
    existing = public_key(state)
    if private.is_file() and existing:
        return existing
    return generate_key(private, label)


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


def install_key(private: Path, state: Path) -> None:
    """Make the pair at ``private`` this machine's key: the private file first, then the public one."""
    os.replace(private, key_path(state))
    os.replace(_public_of(private), _public_of(key_path(state)))


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
