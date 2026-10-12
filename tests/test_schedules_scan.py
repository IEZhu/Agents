"""The outgoing-text scan of self-development (#341).

Every secret here is generated at run time from ``secrets``; none is a real credential. The
repository test reads the live checkout's tracked files and changes nothing.
"""
from __future__ import annotations

import base64
from collections import Counter
import json
import math
import os
from pathlib import Path
import random
import secrets
import string
import subprocess
import time

import pytest

from src.schedules import scan as scan_module
from src.schedules.scan import (ENTROPY_MIN_LENGTH, ENTROPY_NAME, ENTROPY_THRESHOLD, KNOWN_LINE_MIN_LENGTH, MAX_HITS,
                                MAX_TEXT_LENGTH, TOKEN_PATTERNS, TOO_LARGE_NAME, TOO_MANY_HITS_NAME, Hit, HitKind,
                                KnownSecret, Scanner, SecretFound, require_clean, scan)

ROOT = Path(__file__).resolve().parents[1]
ALNUM = string.ascii_letters + string.digits
URLSAFE = ALNUM + "-_"
BASE64_STANDARD = ALNUM + "+/"
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


def shannon(text: str) -> float:
    return -sum(count / len(text) * math.log2(count / len(text)) for count in Counter(text).values())


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
def test_random_base64url_string_is_an_entropy_hit(length):
    value = base64.urlsafe_b64encode(random.Random(length).randbytes(length)).decode()[:length]

    assert found(scan(f"value = {value}"), HitKind.ENTROPY, ENTROPY_NAME)


@pytest.mark.parametrize("alphabet,length,low,high", [
    (ALNUM, ENTROPY_MIN_LENGTH - 1, 0, 0), (ALNUM, 24, 0.01, 0.12), (ALNUM, 32, 0.55, 0.80),
    (ALNUM, 40, 0.93, 1), (ALNUM, 48, 0.99, 1), (BASE64_STANDARD, 40, 0.55, 0.80), (BASE64_STANDARD, 64, 0.85, 0.97),
])
def test_entropy_coverage_by_length_is_as_documented(alphabet, length, low, high):
    """docs/autodev-guards.md: about 5% at 24 characters, two thirds at 32, nearly all from 40
    (letters and digits); standard base64 counts between its slashes. Seeded, so stable."""
    generator = random.Random(f"{alphabet}:{length}")
    samples = [f"value = {''.join(generator.choice(alphabet) for _ in range(length))} end" for _ in range(400)]

    caught = sum(found(scan(sample), HitKind.ENTROPY, ENTROPY_NAME) for sample in samples) / len(samples)

    assert low <= caught <= high


# Commits of this repository and the sessions that made them: permalinks to them are ordinary PR text.
REAL_SHAS = ["ccaa3bb0f489e61bc41a8d2e1759971c92072f6d", "9c71f7d8f1d86ba4819871dc8f2afa253d71bfa2",
             "45c296f4e311c26b52cee6b704ad11ba7bc6c045", "88ebdee639386287077ab1036ab32d7574b0302f",
             "7b539d3c4304ef6fe0cc963ceecd1810f637f7ea", "cc304261bd19eec30c04bb2d29be90d868d4bdae"]
SESSION_IDS = ["session_012KUMEqWSF5sGpXctC6u8Dz", "session_016FZY2TyFfrhMev14CApAvL",
               "session_018FT9wabzEQhVAn5pMpmbdA", "session_01BwPwo3cN7mwiatHK46qF4M"]
PERMALINKS = [
    "See https://github.com/IEZhu/Agents/blob/{sha}/src/schedules/scan.py#L10-L20 for the check.",
    "https://github.com/IEZhu/Agents/blob/{sha}/tests/fixtures/autodev_gate_recorded.json#L602",
    "https://github.com/IEZhu/Agents/blob/{sha}/docs/autodev-plan.md?plain=1#L5",
    "Fixed in https://github.com/IEZhu/Agents/commit/{sha}.",
    "https://github.com/IEZhu/Agents/tree/{sha}/src/schedules",
    "https://github.com/IEZhu/Agents/compare/{sha}...{sha}",
    "https://api.github.com/repos/IEZhu/Agents/commits/{sha}/check-runs?per_page=100",
    "https://api.github.com/repos/IEZhu/Agents/git/trees/{sha}?recursive=1",
]


@pytest.mark.parametrize("template", PERMALINKS)
@pytest.mark.parametrize("sha", REAL_SHAS)
def test_github_permalinks_with_real_shas_are_not_hits(template, sha):
    assert scan(template.format(sha=sha)) == ()


def test_permalinks_to_this_repository_are_not_hits():
    """The reviewer measured 983 of 1000 such permalinks as entropy hits before `/` ended a run."""
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    commits = subprocess.run(["git", "-C", str(ROOT), "rev-list", "--max-count=300", "HEAD"], env=environment,
                             capture_output=True, text=True)
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files"], env=environment, capture_output=True, text=True)
    if commits.returncode != 0 or listing.returncode != 0:
        pytest.skip("not a git checkout")
    paths = listing.stdout.split()
    generator = random.Random(341)

    links = [f"https://github.com/IEZhu/Agents/blob/{sha}/{generator.choice(paths)}#L10-L20"
             for sha in commits.stdout.split()]

    assert links and [link for link in links if scan(link)] == []


@pytest.mark.parametrize("session", SESSION_IDS)
def test_claude_session_link_is_exempt_from_the_entropy_check(session):
    trailer = f"Co-Authored-By: Claude <noreply@anthropic.com>\nClaude-Session: https://claude.ai/code/{session}\n"

    assert scan(trailer) == ()


@pytest.mark.parametrize("session", SESSION_IDS)
@pytest.mark.parametrize("text", ["token {session} end", "https://claude.ai.example.com/code/{session}",
                                  "https://claude.ai/code/{session}X", "https://claude.ai/code/{session}_more",
                                  "http://claude.ai/code/{session}", "https://claude.ai/other/{session}"])
def test_session_link_exemption_is_narrow(session, text):
    assert found(scan(text.format(session=session)), HitKind.ENTROPY, ENTROPY_NAME)


def test_random_token_in_a_url_query_is_still_an_entropy_hit():
    token = "".join(random.Random(341).sample(ALNUM, 40))  # 40 distinct characters: 5.3 bits each
    text = f"https://example.com/callback?code={token}&state=1"

    hits = scan(text)

    start = text.index(token)
    assert Hit(start, start + len(token), HitKind.ENTROPY, ENTROPY_NAME) in hits


@pytest.mark.parametrize("sha", REAL_SHAS)
def test_hex_runs_inside_a_run_do_not_raise_its_entropy(sha):
    run = f"IEZhu_Agents_blob_{sha}_src_schedules_scan_Py3"
    assert shannon(run) >= ENTROPY_THRESHOLD  # the SHA's digits would make it a hit

    assert scan(f"artifact {run} uploaded") == ()


@pytest.mark.parametrize("text,names", [
    ("eyJ" * 60000, set()), ("ghp_" * 50000, set()), ("x" * 200000, set()), ("QUFB" * 50000, set()),
    ("sk-ant-" * 30000, {"anthropic-key"}), ("github_pat_" * 20000, {"github-fine-grained-token"}),
    ("ghp_" + "a" * 200000 + " b", {"github-token"}), ("%41" * 70000, set()), ("\\x41" * 50000, set()),
    ("aa " * 70000 + "zz", set()), ("0x41, " * 40000, set()), (" " * 200000 + "x", set()),
    ("\u200b" * 200000, set()), ("\n@@ -1 +1 @@\n" + "\n+" * 100000, set()),
], ids=lambda value: f"{value[:12]!r}x{len(value)}" if isinstance(value, str) else str(sorted(value)))
def test_long_adversarial_runs_are_scanned_in_linear_time(text, names):
    started = time.perf_counter()

    hits = scan(text)

    assert time.perf_counter() - started < 3
    assert {hit.name for hit in hits} == names and all(hit.kind is HitKind.PATTERN for hit in hits)


def test_hits_and_errors_never_contain_the_secret():
    token = TOKEN_FACTORIES["github-token"]()
    known = KnownSecret("broker token", KNOWN)
    decoy = KnownSecret("decoy: env file", DECOY)
    config = KnownSecret("decoy: config", LOW_ENTROPY_DECOY)
    planted = [token, KNOWN, DECOY, *DECOY.splitlines(), LOW_ENTROPY_DECOY, *LOW_ENTROPY_DECOY.splitlines()]
    texts = [encoded for value in (token, KNOWN, DECOY) for encoded in encodings(value).values()]
    texts += [form(LOW_ENTROPY_DECOY) for form in DECOY_FORMS.values()]
    texts += [split(value, form) for value in (token, KNOWN) for form in SPLITS]
    texts += [form(value.encode()) for value in (token, KNOWN) for form in ESCAPES.values()]
    text = "\n".join(texts)

    hits = Scanner([known, decoy, config]).scan(text)
    with pytest.raises(SecretFound) as raised:
        require_clean(text, [known, decoy, config])

    assert hits and raised.value.hits == hits
    shown = [repr(hits), str(hits), repr(known), str(decoy), repr(config), str(raised.value), repr(raised.value)]
    shown += [f"{hit}" for hit in hits]
    for value in planted:
        assert all(value not in text_shown for text_shown in shown)
    for hit in hits:
        assert set(vars(hit)) == {"start", "end", "kind", "name"}
        assert hit.name in (*TOKEN_PATTERNS, ENTROPY_NAME, "broker token", "decoy: env file", "decoy: config")


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


# Tracked files that are hits by design (docs/autodev-guards.md): a session diff whose context
# reaches these lines is refused. Any other tracked file under the scanned directories must stay clean.
KNOWN_FIXTURE_FILES = {
    "evals/templates/report.html.j2": "an SRI integrity hash (sha384-...)",
    "tests/fixtures/autodev_gate_recorded.json": "a recorded GitHub URL with a token-like query value",
    "tests/test_library_history.py": "fake ghp_ tokens",
    "tests/test_schedules_runner.py": "a fake private key header",
    "tests/test_schedules_scan.py": "this scan's own fixtures: a private key header",
    "tests/test_sync_ui.py": "a fake OpenSSH private key",
    "tests/test_user_sync.py": "fake tokens, keys and an SSH public key",
    "tests/test_user_sync_github.py": "a fake gho_ token",
    "tests/test_user_sync_github_setup.py": "an SSH host key",
}


def test_tracked_files_are_clean_except_known_fixtures():
    """The thresholds keep this repository's own text clean: a false hit would block every run."""
    environment = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    environment.update(GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull, GIT_OPTIONAL_LOCKS="0")
    listing = subprocess.run(["git", "-C", str(ROOT), "ls-files", "-z", "--", "docs", "src", "flows", "agents",
                              "tests", "evals"], env=environment, capture_output=True)
    if listing.returncode != 0:
        pytest.skip("not a git checkout")
    files = [name.decode() for name in listing.stdout.split(b"\0") if name]
    assert len(files) > 300
    assert set(KNOWN_FIXTURE_FILES) <= set(files)
    scanner = Scanner([KnownSecret("broker token", KNOWN), KnownSecret("decoy: env file", DECOY)])

    dirty = {}
    for name in files:
        if name in KNOWN_FIXTURE_FILES:
            continue
        path = ROOT / name
        if path.is_file():
            hits = scanner.scan(path.read_bytes().decode("utf-8", "replace"))
            if hits:
                dirty[name] = [(hit.kind, hit.name, hit.start) for hit in hits]

    assert dirty == {}


# --- known secrets and tokens in normalized, split and escaped forms (review round 1, F2, F7, F9) ---

LOW_ENTROPY_DECOY = "[default]\nnote = this is the decoy config\nowner = runner\n"


def unified_diff(lines: list[str], *, newline: str = "\n") -> str:
    header = ["diff --git a/decoy.ini b/decoy.ini", "new file mode 100644", "--- /dev/null", "+++ b/decoy.ini",
              f"@@ -0,0 +1,{len(lines)} @@"]
    return newline.join([*header, *(f"+{line}" for line in lines)]) + newline


DECOY_FORMS = {
    "new-file diff": lambda value: unified_diff(value.splitlines()),
    "new-file diff with CRLF": lambda value: unified_diff(value.splitlines(), newline="\r\n"),
    "CRLF line endings": lambda value: value.replace("\n", "\r\n"),
    "Markdown code block": lambda value: "".join(f"    {line}\n" for line in value.splitlines()),
    "no trailing newline": lambda value: value.rstrip("\n"),
    "Markdown quote": lambda value: "".join(f"> {line}\n" for line in value.splitlines()),
    "lines reordered": lambda value: "\n".join(reversed(value.splitlines())),
    "zero-width spaces": lambda value: value.replace(" ", "\u200b "),
}


@pytest.mark.parametrize("form", DECOY_FORMS)
def test_low_entropy_multi_line_decoy_is_caught_in_each_form(form):
    assert scan(LOW_ENTROPY_DECOY) == ()  # neither a pattern nor entropy: only the known value catches it
    scanner = Scanner([KnownSecret("decoy: config", LOW_ENTROPY_DECOY)])

    # Not `embedded`: its newline after the value would complete the raw form without a final newline.
    hits = scanner.scan(f"result: {DECOY_FORMS[form](LOW_ENTROPY_DECOY)} end")

    assert found(hits, HitKind.KNOWN, "decoy: config")


def test_each_long_line_of_a_known_value_is_a_needle_of_its_own():
    scanner = Scanner([KnownSecret("decoy: config", LOW_ENTROPY_DECOY)])
    assert KNOWN_LINE_MIN_LENGTH == 16

    assert found(scanner.scan("copied: note = this is the decoy config"), HitKind.KNOWN, "decoy: config")
    assert scanner.scan("owner = runner, [default]") == ()  # 12 and 9 characters: too short alone


SPLITS = {
    "newline": lambda first, second: f"{first}\n{second}",
    "CRLF": lambda first, second: f"{first}\r\n{second}",
    "space": lambda first, second: f"{first} {second}",
    "NBSP": lambda first, second: f"{first}\u00a0{second}",
    "zero-width space": lambda first, second: f"{first}\u200b{second}",
    "two added diff lines": lambda first, second: f"\n@@ -1,0 +1,2 @@\n+{first}\n+{second}\n",
    "indented lines": lambda first, second: f"    {first}\n    {second}\n",
}


def split(value: str, form: str) -> str:
    half = len(value) // 2
    return embedded(SPLITS[form](value[:half], value[half:]))


@pytest.mark.parametrize("form", SPLITS)
@pytest.mark.parametrize("kind", ["known", "ghs_ token"])
def test_split_single_line_known_secret_is_caught(kind, form):
    value = KNOWN if kind == "known" else "ghs_" + random_text(ALNUM, 36)
    scanner = Scanner([KnownSecret("broker token", value)])

    assert found(scanner.scan(split(value, form)), HitKind.KNOWN, "broker token")


@pytest.mark.parametrize("form", SPLITS)
@pytest.mark.parametrize("prefix", ["ghp_", "ghs_"])
def test_split_token_is_caught_by_its_pattern(prefix, form):
    token = prefix + random_text(ALNUM, 36)

    assert found(scan(split(token, form)), HitKind.PATTERN, "github-token")


@pytest.mark.parametrize("text", [
    "A GitHub token starts with ghp_\n    followed by thirty six letters and digits.\n",
    "@@ -1,0 +1,3 @@\n+The prefix is ghs_\n+for app tokens and sk-ant-\n+for Anthropic keys.\n",
    "Fine-grained tokens start with github_pat_\nand are longer.\n",
    "Use ghp_abcdefgh as a placeholder and sk-ant-api03 in examples.\n",
    "Zero\u200bwidth spaces in prose\u200b stay clean, as do ghp_\u200b and AKIA\u200b.\n",
])
def test_joined_lines_and_split_tokens_keep_prose_clean(text):
    assert scan(text) == ()


ESCAPES = {
    "percent-encoded": lambda data: "".join(f"%{byte:02X}" for byte in data),
    "percent-encoded, lower case": lambda data: "".join(f"%{byte:02x}" for byte in data),
    "\\xNN": lambda data: "".join(f"\\x{byte:02x}" for byte in data),
    "\\uNNNN": lambda data: "".join(f"\\u{byte:04x}" for byte in data),
    "\\\\uNNNN, escaped twice": lambda data: "".join(f"\\\\u{byte:04X}" for byte in data),
    "octal \\NNN": lambda data: "".join(f"\\{byte:03o}" for byte in data),
    "0x pairs with comma and space": lambda data: "{" + ", ".join(f"0x{byte:02x}" for byte in data) + "}",
    "0X pairs with comma": lambda data: ",".join(f"0X{byte:02X}" for byte in data),
    "pairs with colons": lambda data: ":".join(f"{byte:02x}" for byte in data),
    "pairs with dashes": lambda data: "-".join(f"{byte:02X}" for byte in data),
    "pairs with spaces": lambda data: " ".join(f"{byte:02x}" for byte in data),
}


@pytest.mark.parametrize("form", ESCAPES)
def test_escaped_token_and_known_secret_are_caught(form):
    token = TOKEN_FACTORIES["github-token"]()
    scanner = Scanner([KnownSecret("broker token", KNOWN)])
    escaped_token, escaped_known = ESCAPES[form](token.encode()), ESCAPES[form](KNOWN.encode())
    text = f"first: {escaped_token}\nsecond: {escaped_known}\n"

    hits = scanner.scan(text)

    token_start, known_start = text.index(escaped_token), text.index(escaped_known)
    assert any(hit.kind is HitKind.PATTERN and hit.name == "github-token"
               and token_start <= hit.start < hit.end <= token_start + len(escaped_token) for hit in hits)
    assert any(hit.kind is HitKind.KNOWN and hit.name == "broker token"
               and known_start <= hit.start < hit.end <= known_start + len(escaped_known) for hit in hits)


def test_one_escaped_character_does_not_hide_a_token_or_known_secret():
    token = TOKEN_FACTORIES["github-token"]()
    scanner = Scanner([KnownSecret("broker token", KNOWN)])

    hits = scanner.scan(f"url?t={token.replace('_', '%5F')}&k={KNOWN.replace('-', '%2d')}")

    assert found(hits, HitKind.PATTERN, "github-token")
    assert found(hits, HitKind.KNOWN, "broker token")


@pytest.mark.parametrize("text", [
    "Search: https://example.com/?q=hello%20world%21&lang=en and C:\\Users\\x\\012.txt",
    "Bytes 0x00, 0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x07, 0x08 and 10:20:30:40:50:60:70:80.",
    "A date 2026-10-12, a time 10:12:30, a UUID 123e4567-e89b-12d3-a456-426614174000.",
])
def test_ordinary_escapes_and_numbers_are_not_hits(text):
    assert scan(text) == ()


# --- limits (review round 1, F10) ---

def test_text_above_the_size_limit_is_one_limit_hit_without_a_scan(monkeypatch):
    monkeypatch.setattr(scan_module, "MAX_TEXT_LENGTH", 1000)
    token = TOKEN_FACTORIES["github-token"]()

    assert scan("x" * 1000) == ()
    assert scan(token + "x" * (1001 - len(token))) == (Hit(0, 1001, HitKind.LIMIT, TOO_LARGE_NAME),)
    with pytest.raises(SecretFound, match="limit 'text-too-large'"):
        require_clean("x" * 1001)


def test_size_limit_is_a_few_megabytes_and_refused_at_once():
    assert 1_000_000 <= MAX_TEXT_LENGTH <= 8_000_000
    text = "x" * (MAX_TEXT_LENGTH + 1)
    started = time.perf_counter()

    assert scan(text) == (Hit(0, len(text), HitKind.LIMIT, TOO_LARGE_NAME),)
    assert time.perf_counter() - started < 1


def test_hits_stop_at_the_cap_and_the_text_stays_not_clean():
    text = " ".join([KNOWN] * (3 * MAX_HITS))
    scanner = Scanner([KnownSecret("broker token", KNOWN)])
    started = time.perf_counter()

    hits = scanner.scan(text)

    assert time.perf_counter() - started < 5
    assert len(hits) == MAX_HITS + 1
    assert Hit(0, len(text), HitKind.LIMIT, TOO_MANY_HITS_NAME) in hits
    with pytest.raises(SecretFound, match=f"and {MAX_HITS + 1 - 10} more"):
        scanner.require_clean(text)


def test_hit_cap_applies_to_every_kind(monkeypatch):
    monkeypatch.setattr(scan_module, "MAX_HITS", 5)
    text = " ".join(TOKEN_FACTORIES["aws-access-key-id"]() for _ in range(20))

    hits = scan(text)

    assert len(hits) == 6 and Hit(0, len(text), HitKind.LIMIT, TOO_MANY_HITS_NAME) in hits
