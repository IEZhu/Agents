"""The outgoing-text scan of self-development (#341).

Every secret here is generated at run time from ``secrets``; none is a real credential. The
repository test reads the live checkout's tracked files and changes nothing.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import secrets
import string
import subprocess
import time

import pytest

from src.schedules.scan import (ENTROPY_NAME, TOKEN_PATTERNS, HitKind, KnownSecret, Scanner, SecretFound,
                                require_clean, scan)

ROOT = Path(__file__).resolve().parents[1]
ALNUM = string.ascii_letters + string.digits
URLSAFE = ALNUM + "-_"
UPPER_DIGITS = string.ascii_uppercase + string.digits


def random_text(alphabet: str, length: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(length))


def b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def fake_jwt() -> str:
    header = b64url(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64url(json.dumps({"sub": random_text(ALNUM, 12), "iat": 1700000000}).encode())
    return f"{header}.{payload}.{b64url(secrets.token_bytes(32))}"


def fake_private_key() -> str:
    body = base64.encodebytes(secrets.token_bytes(96)).decode()
    return f"-----BEGIN OPENSSH PRIVATE KEY-----\n{body}-----END OPENSSH PRIVATE KEY-----\n"


TOKEN_FACTORIES = {
    "github-token": lambda: secrets.choice(["ghp_", "gho_", "ghs_", "ghu_", "ghr_"]) + random_text(ALNUM, 36),
    "github-fine-grained-token": lambda: f"github_pat_{random_text(ALNUM, 22)}_{random_text(ALNUM, 59)}",
    "anthropic-key": lambda: f"sk-ant-api03-{random_text(URLSAFE, 93)}AA",
    "openrouter-key": lambda: f"sk-or-v1-{secrets.token_hex(32)}",
    "openai-project-key": lambda: f"sk-proj-{random_text(URLSAFE, 48)}",
    "aws-access-key-id": lambda: "AKIA" + random_text(UPPER_DIGITS, 16),
    "private-key": fake_private_key,
    "jwt": fake_jwt,
}

KNOWN = "known-" + secrets.token_hex(12)
DECOY = f"AWS_SECRET_ACCESS_KEY={random_text(ALNUM, 40)}\nDECOY_MARKER={secrets.token_hex(8)}\n"


def encodings(value: str) -> dict[str, str]:
    """``value`` as a session might leak it."""
    data = value.encode()
    plain = base64.b64encode(data).decode()
    return {
        "raw": value,
        "base64": plain,
        "base64 unpadded": plain.rstrip("="),
        "base64url": base64.urlsafe_b64encode(data).decode(),
        "base64url unpadded": b64url(data),
        "hex": data.hex(),
        "HEX": data.hex().upper(),
        "base64 in a blob, offset 1": base64.b64encode(b"\x01" + data + b"\x02tail").decode(),
        "base64 in a blob, offset 2": base64.b64encode(b"\x01\x02" + data + b"tail").decode(),
        "base64url in a blob, offset 3": b64url(b"\xff\xfe\xfd" + data + b"\x00"),
        "base64 wrapped at 76": base64.encodebytes(b"leading bytes " + data).decode(),
        "hex in pairs": " ".join(data.hex()[i:i + 4] for i in range(0, len(data.hex()), 4)),
        "base64 of base64": base64.b64encode(plain.encode()).decode(),
        "hex of base64": plain.encode().hex(),
    }


def embedded(encoded: str) -> str:
    return f"Here is the run output.\nresult: {encoded}\nThat is all.\n"


def found(hits, kind: HitKind, name: str) -> bool:
    return any(hit.kind is kind and hit.name == name for hit in hits)


@pytest.mark.parametrize("name", TOKEN_PATTERNS)
def test_token_shaped_secret_is_caught_raw(name):
    token = TOKEN_FACTORIES[name]()

    hits = scan(embedded(token))

    assert found(hits, HitKind.PATTERN, name)


@pytest.mark.parametrize("encoding", encodings("x" * 8))
@pytest.mark.parametrize("name", TOKEN_PATTERNS)
def test_token_shaped_secret_is_caught_in_each_encoding(name, encoding):
    token = TOKEN_FACTORIES[name]()
    text = embedded(encodings(token)[encoding])

    hits = scan(text)

    assert found(hits, HitKind.PATTERN, name), encoding


@pytest.mark.parametrize("encoding", encodings("x" * 8))
@pytest.mark.parametrize("label,value", [("broker token", KNOWN), ("decoy: env file", DECOY)])
def test_known_secret_and_decoy_are_caught_in_each_encoding(label, value, encoding):
    scanner = Scanner([KnownSecret("broker token", KNOWN), KnownSecret("decoy: env file", DECOY)])

    hits = scanner.scan(embedded(encodings(value)[encoding]))

    assert found(hits, HitKind.KNOWN, label), encoding


@pytest.mark.parametrize("shift", range(6))
def test_known_secret_is_caught_at_any_byte_offset_of_a_larger_blob(shift):
    blob = secrets.token_bytes(shift) + KNOWN.encode() + secrets.token_bytes(5)
    scanner = Scanner([KnownSecret("broker token", KNOWN)])

    for encoded in (base64.b64encode(blob).decode(), b64url(blob)):
        assert found(scanner.scan(f"data:{encoded}"), HitKind.KNOWN, "broker token")


def test_bytes_known_secret_is_caught_in_base64_and_hex():
    value = secrets.token_bytes(24)
    scanner = Scanner([KnownSecret("runner key", value)])

    for encoded in (base64.b64encode(value).decode(), value.hex(), value.hex().upper()):
        assert found(scanner.scan(embedded(encoded)), HitKind.KNOWN, "runner key")


def test_secret_wrapped_inside_a_unified_diff_is_caught():
    token = TOKEN_FACTORIES["github-token"]()
    lines = base64.encodebytes(secrets.token_bytes(31) + token.encode() + secrets.token_bytes(40)).decode().splitlines()
    diff = ("diff --git a/data.txt b/data.txt\n--- a/data.txt\n+++ b/data.txt\n"
            f"@@ -1,0 +1,{len(lines)} @@\n" + "".join(f"+{line}\n" for line in lines))
    scanner = Scanner([KnownSecret("broker token", token)])

    hits = scanner.scan(diff)

    assert found(hits, HitKind.PATTERN, "github-token")
    assert found(hits, HitKind.KNOWN, "broker token")


def test_hit_offsets_point_at_the_secret_or_its_encoded_form():
    token = TOKEN_FACTORIES["github-token"]()
    encoded = base64.b64encode(token.encode()).decode()
    text = f"raw {token} encoded {encoded} end"

    hits = [hit for hit in scan(text) if hit.kind is HitKind.PATTERN]

    raw_start, encoded_start = text.index(token), text.index(encoded)
    assert any((hit.start, hit.end) == (raw_start, raw_start + len(token)) for hit in hits)
    assert any(encoded_start <= hit.start < hit.end <= encoded_start + len(encoded) for hit in hits)


@pytest.mark.parametrize("text", [
    "Secret patterns: `gho_`, `ghp_`, `ghs_`, `github_pat_`, `sk-ant-`, `sk-or-`, `sk-proj-`, `AKIA`, "
    "`PRIVATE KEY`, JWT.",
    "Tokens look like ghp_ followed by 36 characters; AKIA starts an access key ID.",
    "Set GITHUB_TOKEN=ghp_... and ANTHROPIC_API_KEY=sk-ant-... in the environment.",
    "A JWT starts with eyJ and has three dot-separated parts.",
    "The BEGIN PRIVATE KEY header must never appear in a comment.",
])
def test_bare_prefixes_are_not_hits(text):
    assert scan(text) == ()


@pytest.mark.parametrize("value", [
    secrets.token_hex(20), secrets.token_hex(32), secrets.token_hex(20).upper(), secrets.token_hex(32).upper(),
    f"sha256:{secrets.token_hex(32)}", "0" * 40,
    "9c71f7d3a1e0b2c4d5f6a7b8c9d0e1f2a3b4c5d6",
])
def test_hex_strings_such_as_commit_shas_are_not_entropy_hits(value):
    assert scan(f"commit {value} landed") == ()


def test_uuid_and_identifiers_are_not_entropy_hits():
    text = ("id 123e4567-e89b-12d3-a456-426614174000 in "
            "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2 and "
            "test_scan_finds_base64_encoded_known_secret_inside_a_larger_blob")

    assert scan(text) == ()


@pytest.mark.parametrize("length", [48, 64, 100])
def test_random_base64_string_is_an_entropy_hit(length):
    value = base64.b64encode(secrets.token_bytes(length)).decode()[:length]

    assert found(scan(f"value = {value}"), HitKind.ENTROPY, ENTROPY_NAME)


@pytest.mark.parametrize("text", ["eyJ" * 60000, "ghp_" * 50000, "x" * 200000, "QUFB" * 50000])
def test_long_adversarial_runs_are_scanned_in_linear_time(text):
    started = time.perf_counter()

    assert scan(text) == ()
    assert time.perf_counter() - started < 3


def test_hits_and_errors_never_contain_the_secret():
    token = TOKEN_FACTORIES["github-token"]()
    known = KnownSecret("broker token", KNOWN)
    decoy = KnownSecret("decoy: env file", DECOY)
    planted = [token, KNOWN, DECOY, *DECOY.splitlines()]
    text = "\n".join(encoded for value in (token, KNOWN, DECOY) for encoded in encodings(value).values())

    hits = Scanner([known, decoy]).scan(text)
    with pytest.raises(SecretFound) as raised:
        require_clean(text, [known, decoy])

    assert hits and raised.value.hits == hits
    shown = [repr(hits), str(hits), repr(known), str(decoy), str(raised.value), repr(raised.value)]
    shown += [f"{hit}" for hit in hits]
    for value in planted:
        assert all(value not in text_shown for text_shown in shown)
    for hit in hits:
        assert set(vars(hit)) == {"start", "end", "kind", "name"}
        assert hit.name in (*TOKEN_PATTERNS, ENTROPY_NAME, "broker token", "decoy: env file")


def test_require_clean_passes_clean_text_and_raises_on_a_hit():
    require_clean("A comment about the change, with commit 9c71f7d and no secrets.\n")

    with pytest.raises(SecretFound, match="pattern 'aws-access-key-id'"):
        require_clean(embedded(TOKEN_FACTORIES["aws-access-key-id"]()))


@pytest.mark.parametrize("value", ["", "x7Q", "1234567", b"\x00" * 7])
def test_known_secret_shorter_than_eight_characters_is_refused(value):
    with pytest.raises(ValueError, match="shorter than 8") as raised:
        KnownSecret("label", value)

    assert not value or str(value) not in str(raised.value)


def test_known_secret_needs_a_label_that_does_not_contain_it():
    with pytest.raises(ValueError):
        KnownSecret("", KNOWN)
    with pytest.raises(ValueError, match="must not contain"):
        KnownSecret(f"token {KNOWN}", KNOWN)


def test_tracked_documentation_and_code_are_clean():
    """The thresholds keep this repository's own text clean: a false hit would block every run."""
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "docs", "src", "flows", "agents"],
                             env=environment, capture_output=True)
    if listing.returncode != 0:
        pytest.skip("not a git checkout")
    files = [name.decode() for name in listing.stdout.split(b"\0") if name]
    assert len(files) > 100
    scanner = Scanner([KnownSecret("broker token", KNOWN), KnownSecret("decoy: env file", DECOY)])

    dirty = {}
    for name in files:
        path = ROOT / name
        if path.is_file():
            hits = scanner.scan(path.read_bytes().decode("utf-8", "replace"))
            if hits:
                dirty[name] = [(hit.kind, hit.name, hit.start) for hit in hits]

    assert dirty == {}
