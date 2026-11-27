"""Secrets cleaned out of a turn before the turn-log hook writes it (#701, owner's decision 2026-11-27).

The Stop hook ``src.memory.turn_log_hook`` writes every interactive turn by default, so what it
writes must be clean (#701 review): ``redact`` masks what the repository's own detectors find and
what this module adds to them, round after round, and says whether a last scan found nothing more.

- Formats: the schedule records' (``records.mask``), which keep a token's prefix, and those listed
  in ``EXTRA_SECRET_PATTERNS``; private key blocks, PGP ones included, and key bodies whose BEGIN
  line is missing, to their END line or as consecutive lines of base64.
- Labels: the value after an identifier that names a secret (``secret_name``: API_TOKEN,
  JWT_SECRET, SECRET_KEY, db_password, accessToken, "SecretAccessKey", Authorization ...), in
  ``=``, ``:``, ``:=``, ``=>`` and ``==`` forms, quoted or not, and on a command line
  (``--password``, ``sshpass -p``, ``curl -u user:pass``), masked whole whatever its shape, unless it
  is a placeholder, an expression, a literal or type name, a plain identifier, or a plain word that
  prose or the end of an argument follows.
- Shapes: random-looking runs of token characters or of base64, 43-character tokens, 32-digit hex
  keys, AWS secret keys and Discord tokens, except the ids ``NOT_SECRET`` names.
- The scanner of self-development (``src.schedules.scan``): token formats, high-entropy strings and
  the caller's known secrets (``environment_secrets``), also split or encoded; and the credentials
  that shared memory and user sync refuse to serve or publish.

What still passes is listed in docs/memory.md (Secrets and private details).
"""
from __future__ import annotations

import functools
import os
import re
import time

# What a masked secret becomes. No detector reads it as a value, since it starts with "<": user
# sync and shared memory take <...> for a placeholder, so masking ends when nothing else is found.
MASK = "<secret>"
# Masking one secret can leave another detector's match, such as a label before the masked value;
# the scan runs again on the masked text this many times at most.
REDACTION_ROUNDS = 4
# How long the first scan of a text may take, per character and per known secret besides, as
# measured on log-like text (#701 review: 2.7 s for 1 MB, 10.5 s with 40 environment secrets).
SCAN_SECONDS_PER_CHARACTER = 3e-6
SCAN_SECONDS_PER_CHARACTER_AND_SECRET = 2.5e-7

# --- Names of secrets (#701 review) ---------------------------------------------------------------
# An identifier names a secret when one of its words, split at _ - . and at case and digit changes,
# is one of these, or ends like one: API_TOKEN, JWT_SECRET, X_API_KEY, accessToken, PGPASSWORD.
SECRET_WORDS = frozenset({
    "pass", "passwd", "password", "passwords", "passphrase", "pwd", "secret", "secrets", "token", "key",
    "apikey", "accesskey", "secretkey", "privatekey", "auth", "authorization", "credential", "credentials",
    "creds", "dsn", "cookie", "capability"})
SECRET_ENDINGS = ("password", "passwd", "passphrase", "secret", "token")
# Alone, these words are too common to name a secret: "PASS: test_x" in a test report, the shell's PWD.
LONE_WORDS = frozenset({"pass", "pwd"})
# A last word that makes the identifier a secret's property, not the secret: SSH_KEY_NAME, AUTH_MODE,
# TOKEN_TYPE, PASSWORD_FILE, KEY_DIGEST.
PROPERTY_WORDS = frozenset({
    "name", "names", "mode", "path", "paths", "file", "files", "dir", "dirs", "directory", "id", "ids",
    "type", "types", "kind", "length", "size", "count", "limit", "limits", "enabled", "disabled", "set",
    "version", "provider", "helper", "format", "prefix", "suffix", "label", "policy", "method", "scheme",
    "algorithm", "endpoint", "host", "port", "region", "url", "uri", "domain", "expiry", "expires", "ttl",
    "timeout", "user", "username", "login", "email", "digest", "hash", "fingerprint", "checksum", "stdin",
    "command", "cmd", "prompt"})
# A database or broker word before URL, URI or DSN: a connection string that often holds a password.
CONNECTION_WORDS = frozenset({
    "database", "db", "redis", "mongo", "mongodb", "postgres", "postgresql", "pg", "mysql", "mariadb",
    "amqp", "rabbitmq", "broker", "celery", "sqlalchemy"})
CONNECTION_ENDINGS = frozenset({"url", "uri", "dsn"})
_NAME_WORDS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
                         r"|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")

# --- Values after a label ---------------------------------------------------------------------------
# name, optionally quoted or set off by Markdown emphasis, then = : := => or a comparison's ==; not
# inside a longer name.
_LABEL = re.compile(r"(?<![A-Za-z0-9_$.\-])(?P<quote>[\"'`]?)(?P<name>[A-Za-z_][A-Za-z0-9_.\-]{0,79})(?P=quote)"
                    r"\**[ \t]*(?::=|=>|={1,3}(?!=)|:(?![:/\\]))\**[ \t]*")
# A command-line option: --password VALUE or --api-key=VALUE. With a blank before its value, an
# option whose first word asks a question or switches something takes none: --ask-pass, --no-password.
_OPTION = re.compile(r"(?<![\w-])--(?P<name>[A-Za-z][A-Za-z0-9-]{1,63})(?P<separator>=|[ \t]+)")
SWITCH_WORDS = frozenset({"no", "ask", "use", "show", "skip", "without", "enable", "disable", "require", "print"})
# Passwords given on a command line: sshpass -p PASSWORD, and curl -u USER:PASSWORD (--user too).
_SSHPASS = re.compile(r"\bsshpass\b(?:[ \t]+-[A-Za-z]\S*)*?[ \t]+-p[ \t]*")
_USER_PASSWORD = re.compile(r"\bcurl\b[^\r\n]*?(?<![\w-])(?:-u|--user)(?:[ \t]+|=)[\"']?[^\s:\"'@]+:")
# An unquoted value runs to whitespace, a quote or a separator of fields.
_UNQUOTED = re.compile(r"[^\s\"'`,;&]+")
_TRAILING = ".,:;!?)]}>"
# Placeholders and expressions are not values: <your token>, ${TOKEN}, $(cat key), {{ secret }},
# %(password)s, os.environ["KEY"], getpass(); nor are type names, literals and plain identifiers.
_PLACEHOLDER_START = "<$%{[(*-="
_LITERALS = frozenset({
    "none", "null", "nil", "true", "false", "yes", "no", "on", "off", "str", "string", "int", "integer",
    "float", "number", "bool", "boolean", "bytes", "object", "any", "optional", "undefined", "required",
    "secretstr"})
_SNAKE = re.compile(r"[a-z]+(?:_[a-z]+)+|[A-Z]+(?:_[A-Z]+)+")
_PLAIN_WORD = re.compile(r"[A-Za-z]+")
# After a plain word: more words (prose: "token: the unit a model reads") or the end of an argument.
_PROSE_OR_ARGUMENT = re.compile(r"[ \t]+[A-Za-z]|[ \t]*[),}]")
_REST_OF_LINE = re.compile(r"[^\r\n\"']*")
# The HTTP authentication schemes whose credentials follow them after an Authorization label.
_AUTH_SCHEME = re.compile(r"(?i)(?:basic|bearer|digest|token|negotiate|ntlm|hoba|mutual|apikey|api-key|key|"
                          r"sharedkey|hawk|oauth|jwt|ssws|dpop|aws4-hmac-sha256)[ \t]+(?=\S)")

# --- Formats and shapes -----------------------------------------------------------------------------
# Formats the schedule records' mask() does not know, masked here on top of it, keeping the prefix.
EXTRA_SECRET_PATTERNS = (
    re.compile(r"\b(sk-(?:proj-|svcacct-|admin-)?)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(xox[abposr]-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(xapp-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_-]{30,}"),
    re.compile(r"\b(eyJ)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
    re.compile(r"(?<![A-Z0-9])(ASIA)[A-Z0-9]{16}(?![A-Z0-9])"),
    re.compile(r"\b(SG\.)[A-Za-z0-9_-]{16,}\.[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(SK)[0-9a-fA-F]{32}\b"),
    re.compile(r"(?<![\w:])()\d{6,12}:AA[A-Za-z0-9_-]{30,}"),
    re.compile(r"(https://hooks\.slack\.com/(?:services|workflows|triggers)/)[A-Za-z0-9/_-]+"),
    re.compile(r"(https://(?:\w+\.)?discord(?:app)?\.com/api/webhooks/\d+/)[A-Za-z0-9_-]+"),
)
# A PGP private key block, to its END line (the schedule records' mask() takes the other private
# keys with their END line). A body whose BEGIN line is missing goes up to its END line, and a body
# without either as two or more lines of base64 (#701 review): the block of such lines is matched
# whole, and what follows it decides, so the search stays linear in the number of lines.
PRIVATE_KEY_BLOCK = re.compile(r"(-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY BLOCK-----)[\s\S]*?"
                               r"(?=-----END (?:[A-Z0-9]+ )*PRIVATE KEY BLOCK-----|\Z)")
BASE64_LINES = re.compile(r"(?m)^(?:[ \t]*[A-Za-z0-9+/=]{16,}[ \t]*(?:\r?\n|\Z))+")
KEY_END_LINE = re.compile(r"[ \t]*-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
KEY_LINE_LENGTH = 40
# A Discord bot token: three dotted parts of random-looking characters.
DISCORD_TOKEN = re.compile(r"(?<![A-Za-z0-9_.-])([A-Za-z0-9_-]{23,28})\.[A-Za-z0-9_-]{6,7}\.([A-Za-z0-9_-]{27,40})"
                           r"(?![A-Za-z0-9_.-])")
# An AWS secret access key: 40 characters of base64's alphabet, of both cases and digits.
AWS_SECRET_KEY = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{40}(?![A-Za-z0-9+/=])")
# A 43-character URL-safe token (secrets.token_urlsafe(32), the form of a cooperation capability)
# in prose, without a key (#734 review). Any mix of its characters is a valid encoding, so such a run
# is masked whatever it looks like, unless it is a plain snake_case, SCREAMING_CASE or kebab-case
# name (#701 review). Git object names (40) and SHA-256 digests (64) have other lengths and stay.
RANDOM_TOKEN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{43}(?![A-Za-z0-9_-])")
_NAME_RUN = re.compile(r"[a-z][a-z0-9]*(?:[_-][a-z0-9]+)+|[A-Z][A-Z0-9]*(?:_[A-Z0-9]+)+")
# A run of token characters long enough to be a secret, and 32 hexadecimal digits (an MD5-sized
# key; 40 and 64 are commit names and SHA-256 digests, and stay).
TOKEN_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
# A run of base64's alphabet with + or /, judged by its pieces between them (a path's pieces are words).
BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{20,}={0,2}(?![A-Za-z0-9+/=])")
HEX_KEY = re.compile(r"(?<![A-Za-z0-9_-])(?=[0-9a-fA-F]*[0-9])(?=[0-9a-fA-F]*[a-fA-F])"
                     r"(?:[0-9a-f]{32}|[0-9A-F]{32})(?![A-Za-z0-9_-])")
_SEGMENTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_PIECES = re.compile(r"[_-]+")
_BASE64_PIECES = re.compile(r"[+/=]+")
_HEX_RUN = re.compile(r"[0-9a-f]+|[0-9A-F]+")
_WORD_PIECE = re.compile(r"[A-Za-z]{3,}|[0-9]+")
_VOWELS = frozenset("aeiouyAEIOUY")
# Identifiers that look random and are no secret: Anthropic API ids (tool use, message, request,
# session), GitHub node ids, a Google document's id in its link, subresource integrity digests,
# and the payload of a data: URI. Neither the random-run rules nor the entropy check mask them.
NOT_SECRET = re.compile(
    r"(?<![A-Za-z0-9_-])(?:toolu|srvtoolu|msg|msgbatch|req|session|file|batch)_01[A-Za-z0-9]{14,40}(?![A-Za-z0-9_-])"
    r"|(?<![A-Za-z0-9_-])[A-Z]{1,5}_(?:kw|lA)[A-Za-z0-9_-]{8,}(?![A-Za-z0-9_-])"
    r"|(?<=docs\.google\.com/document/d/)[A-Za-z0-9_-]{20,}"
    r"|(?<=docs\.google\.com/spreadsheets/d/)[A-Za-z0-9_-]{20,}"
    r"|(?<=docs\.google\.com/presentation/d/)[A-Za-z0-9_-]{20,}"
    r"|(?<=drive\.google\.com/file/d/)[A-Za-z0-9_-]{20,}"
    r"|\bsha(?:256|384|512)-[A-Za-z0-9+/]{40,}={0,2}"
    r"|(?<=;base64,)[A-Za-z0-9+/]{16,}={0,2}")
# How far a match that only detects a credential (user sync's password patterns stop after the
# first character of the value) is widened to mask the whole value.
VALUE_TAIL = re.compile(r"[^\s\"'`<>,;)\]}]*")


@functools.cache
def _sync_patterns() -> tuple:
    """User sync's credential patterns, which match bytes, compiled for text."""
    from src.user_sync.scope import SECRET_PATTERNS
    return tuple((name, re.compile(pattern.pattern.decode("ascii"))) for name, pattern in SECRET_PATTERNS)


def name_words(name: str) -> list[str]:
    """The words of an identifier, lower-cased: split at ``_ - .``, at case changes and at digits."""
    return [word.lower() for word in _NAME_WORDS.split(name) if word]


def secret_name(name: str) -> bool:
    """Whether an identifier names a secret (#701 review): one of its words is a ``SECRET_WORDS``
    word or ends like one (PGPASSWORD, clientSecret), or a database or broker word comes before a
    last URL, URI or DSN (DATABASE_URL, REDIS_URL). A word inside another word does not count
    (KEYRING, KEYBOARD, AUTHOR), ``pass`` and ``pwd`` alone do not (``PASS:`` in a test report, the
    shell's PWD), and a last ``PROPERTY_WORDS`` word names a property of a secret, such as
    SSH_KEY_NAME or AUTH_MODE."""
    words = name_words(name)
    if not words or len(words) == 1 and words[0] in LONE_WORDS:
        return False
    if words[-1] in CONNECTION_ENDINGS and any(word in CONNECTION_WORDS for word in words[:-1]):
        return True
    if words[-1] in PROPERTY_WORDS:
        return False
    return any(word in SECRET_WORDS or word.endswith(SECRET_ENDINGS) for word in words)


def environment_secrets(environ=None) -> list:
    """The secret-looking values of the hook's environment, as the scanner's known secrets.

    A value counts when its variable's name names a secret (``secret_name``: by word, so KEYRING,
    KEYBOARD and AUTHOR do not count, and DB_PASS, MYSQL_PWD, SMTP_PASS, SENTRY_DSN, DATABASE_URL
    and REDIS_URL do), or when the value itself scans as one, as the schedule sandbox checks a
    session's environment. A value under ``KNOWN_MIN_LENGTH`` characters, or one naming an existing
    absolute path, is no secret to look for.
    """
    from src.schedules import records, scan

    probe, known = scan.Scanner(), []
    for name, value in (os.environ if environ is None else environ).items():
        if not isinstance(value, str) or len(value) < scan.KNOWN_MIN_LENGTH or "\x00" in value:
            continue
        if not (secret_name(name) or probe.scan(value)
                or any(pattern.search(value) for pattern in records.SECRET_PATTERNS)):
            continue
        if os.path.isabs(value) and os.path.exists(value):
            continue
        try:
            known.append(scan.KnownSecret(f"environment:{name}", value))
        except (TypeError, ValueError):  # a value inside its own label, or too short to fingerprint
            continue
    return known


def _quoted_end(text: str, start: int) -> int:
    """The index of the quote closing the value that opens at ``text[start]``, or the end of the
    text for one never closed: the other quote character and escaped quotes are part of the value,
    which may run across lines (#734 review)."""
    quote, index = text[start], start + 1
    while index < len(text):
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == quote:
            return index
        index += 1
    return len(text)


def _unquoted_value(text: str, start: int) -> tuple[int, int, bool]:
    """``(start, end, kept)``: the unquoted value at ``start`` and whether it stays as written: a
    placeholder or an expression (it starts with one of ``<$%{[(*-=`` or holds an opening bracket),
    a literal or a type name, a plain snake_case or SCREAMING_CASE name, or a plain word that more
    words or the end of an argument follow (#701 review)."""
    match = _UNQUOTED.match(text, start)
    if match is None:
        return start, start, True
    value = match.group().rstrip(_TRAILING)
    end = start + len(value)
    if not value:
        return start, start, True
    if (value[0] in _PLACEHOLDER_START or any(bracket in value for bracket in "([{")
            or value.lower() in _LITERALS or _SNAKE.fullmatch(value)):
        return start, end, True
    if _PLAIN_WORD.fullmatch(value) and _PROSE_OR_ARGUMENT.match(text, end):
        return start, end, True
    return start, end, False


def _value_spans(text: str, start: int, *, authorization: bool = False) -> list[tuple[int, int]]:
    """The spans to mask of the value that begins at ``start``, after a label naming a secret.

    A quoted value is masked whole up to its closing quote unless it is empty or a placeholder (it
    starts with one of ``<$%{[*``). After an ``Authorization`` label, what follows an auth scheme
    (``Basic``, ``Bearer``, ``Digest`` ...) goes to the end of the value or of the line.
    """
    if start >= len(text) or text[start] in "\r\n":
        return []
    if text[start] in "\"'`":
        end = _quoted_end(text, start)
        inner = start + 1
        if authorization and (scheme := _AUTH_SCHEME.match(text, inner, end)):
            inner = scheme.end()
        value = text[inner:end]
        if not value.strip() or value[0] in "<$%{[*" or value.strip().lower() in _LITERALS:
            return []
        return [(inner, end)]
    if authorization and (scheme := _AUTH_SCHEME.match(text, start)):
        rest = _REST_OF_LINE.match(text, scheme.end())
        value = rest.group().rstrip()
        if value and value[0] not in "<$":
            return [(scheme.end(), scheme.end() + len(value))]
        return []
    value_start, value_end, kept = _unquoted_value(text, start)
    return [] if kept or value_end <= value_start else [(value_start, value_end)]


def _labelled_spans(text: str) -> list[tuple[int, int]]:
    """Where a value after a label naming a secret is (#701 review): ``NAME=value``, ``NAME: value``,
    quoted and camelCase keys (``"SecretAccessKey": "..."``), Markdown labels (``**Password:**``),
    ``--password VALUE``, ``sshpass -p PASSWORD`` and ``curl -u USER:PASSWORD``."""
    spans: list[tuple[int, int]] = []
    for match in _LABEL.finditer(text):
        if secret_name(match.group("name")):
            authorization = "authorization" in name_words(match.group("name"))
            spans.extend(_value_spans(text, match.end(), authorization=authorization))
    for match in _OPTION.finditer(text):
        name = match.group("name")
        if secret_name(name) and (match.group("separator") == "=" or name_words(name)[0] not in SWITCH_WORDS):
            spans.extend(_value_spans(text, match.end()))
    for pattern in (_SSHPASS, _USER_PASSWORD):
        for match in pattern.finditer(text):
            start = match.end()
            if start < len(text) and text[start] in "\"'":
                end = _quoted_end(text, start)
                if end > start + 1 and text[start + 1] not in "<$":
                    spans.append((start + 1, end))
                continue
            value = _UNQUOTED.match(text, start)
            if value and value.group()[0] not in "<$":
                spans.append(value.span())
    return spans


def _not_secret_spans(text: str) -> list[tuple[int, int]]:
    return [match.span() for match in NOT_SECRET.finditer(text)]


def _inside(span: tuple[int, int], regions: list[tuple[int, int]]) -> bool:
    return any(start <= span[0] and span[1] <= end for start, end in regions)


def _outside(run: str, offset: int, regions: list[tuple[int, int]]) -> str:
    """``run``, found at ``offset``, with what ``regions`` cover turned into ``/``: a document's id
    in a link is judged as a separator of the path around it, never as a piece of it."""
    characters = list(run)
    for start, end in regions:
        for index in range(max(start, offset), min(end, offset + len(run))):
            characters[index - offset] = "/"
    return "".join(characters)


def _random_piece(piece: str) -> bool:
    """Whether a run without separators looks random rather than like a name: mixed case whose
    parts are short and poor in vowels, or lowercase spotted with digits; never hex alone or one
    case of capitals (a digest, a ULID, a constant)."""
    if _HEX_RUN.fullmatch(piece) or not any(character.islower() for character in piece):
        return False
    segments = _SEGMENTS.findall(piece)
    digit_runs = sum(1 for segment in segments if segment.isdigit())
    letters = [character for character in piece if character.isalpha()]
    vowels = sum(character in _VOWELS for character in letters) / max(1, len(letters))
    if not any(character.isupper() for character in piece):
        digits = sum(character.isdigit() for character in piece)
        beyond_hex = sum(character in "ghijklmnopqrstuvwxyz" for character in piece)
        return len(piece) >= 20 and digits >= 2 and digit_runs >= 2 and beyond_hex >= 3 and vowels < 0.40
    average = sum(map(len, segments)) / len(segments)
    short = sum(1 for segment in segments if len(segment) <= 2) / len(segments)
    score = ((average < 2.6) + (vowels < 0.28) + (short > 0.5)
             + (digit_runs >= 3 and digit_runs * 6 >= len(piece)))
    return score >= 2


def random_looking(run: str, separators: re.Pattern = _PIECES) -> bool:
    """Whether a run of token characters looks like a random key rather than a name (#701 review).

    Its pieces between ``separators`` (``_`` and ``-``; ``+``, ``/`` and ``=`` for base64) are judged
    one by one when long enough; a run of short pieces is a structured name
    (``x86_64-pc-windows-msvc``, a path) unless none of them is a word or a number."""
    pieces = [piece for piece in separators.split(run) if piece]
    if len(pieces) == 1:
        return _random_piece(pieces[0])
    if any(len(piece) >= 10 and _random_piece(piece) for piece in pieces):
        return True
    if any(_WORD_PIECE.fullmatch(piece) or _HEX_RUN.fullmatch(piece) for piece in pieces):
        return False
    joined = "".join(pieces)
    return any(character.isupper() for character in joined) and _random_piece(joined)


def _shape_spans(text: str, exempt: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Unlabelled secrets known by their shape, outside the ``exempt`` regions: key bodies, AWS
    secret keys, Discord tokens, 43-character tokens, random-looking runs of token characters or of
    base64, and 32-digit hex keys."""
    spans = []
    for match in BASE64_LINES.finditer(text):
        long_lines = sum(len(line.strip()) >= KEY_LINE_LENGTH for line in match.group().splitlines())
        if KEY_END_LINE.match(text, match.end()) or (long_lines >= 2 and _mixed(match.group())):
            spans.append(match.span())
    for match in AWS_SECRET_KEY.finditer(text):
        if _mixed(match.group()) and not _inside(match.span(), exempt):
            spans.append(match.span())
    for match in DISCORD_TOKEN.finditer(text):
        if _mixed(match.group(1)) and _mixed(match.group(2)):
            spans.append(match.span())
    for match in RANDOM_TOKEN.finditer(text):
        if not _NAME_RUN.fullmatch(match.group()) and not _inside(match.span(), exempt):
            spans.append(match.span())
    for match in TOKEN_RUN.finditer(text):
        if not _inside(match.span(), exempt) and random_looking(match.group()):
            spans.append(match.span())
    for match in BASE64_RUN.finditer(text):
        run = match.group()
        if ("+" in run or "/" in run) and random_looking(_outside(run, match.start(), exempt), _BASE64_PIECES):
            spans.append(match.span())
    spans.extend(match.span() for match in HEX_KEY.finditer(text) if not _inside(match.span(), exempt))
    return spans


def _mixed(value: str) -> bool:
    """Both letter cases and a digit: the mark of random key material, which hex and words lack."""
    return (any(character.isupper() for character in value) and any(character.islower() for character in value)
            and any(character.isdigit() for character in value))


def _secret_spans(text: str, scanner) -> list[tuple[int, int]] | None:
    """Where the detectors find a secret in ``text``; None when the scanner could not scan it
    whole (too large, too many hits). An entropy hit inside a region that ``NOT_SECRET`` names is
    no secret; token formats and known secrets always are."""
    from src.memory.shared import MEMORY_SECRETS
    from src.schedules import scan

    exempt = _not_secret_spans(text)
    spans = []
    for hit in scanner.scan(text):
        if hit.kind == scan.HitKind.LIMIT:
            return None
        if hit.kind == scan.HitKind.ENTROPY and _inside((hit.start, hit.end), exempt):
            continue
        spans.append((hit.start, hit.end))
    for name, pattern in MEMORY_SECRETS:
        for match in pattern.finditer(text):
            if name != "labelled_credential":
                spans.append(match.span())
                continue
            # Masked from where its value begins, unless the label rules keep that value whole.
            start = match.start("value")
            value_start, value_end, kept = _unquoted_value(text, start)
            if not (kept and not text[value_end:match.end()].strip(_TRAILING)):
                spans.append((start, match.end()))
    for name, pattern in _sync_patterns():
        for match in pattern.finditer(text):
            if name != "password":
                spans.append(match.span())
                continue
            # The value begins at the match's last character; a placeholder or a name stays.
            value_start, value_end, kept = _unquoted_value(text, match.end() - 1)
            if not kept:
                spans.append((value_start, max(value_end, VALUE_TAIL.match(text, match.end()).end())))
    spans.extend(_shape_spans(text, exempt))
    return spans


def _mask_spans(text: str, spans) -> str:
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces, kept = [], 0
    for start, end in merged:
        pieces += [text[kept:start], MASK]
        kept = end
    return "".join(pieces) + text[kept:]


def _scan_estimate(text: str, known) -> float:
    return len(text) * (SCAN_SECONDS_PER_CHARACTER + SCAN_SECONDS_PER_CHARACTER_AND_SECRET * len(known))


def redact(text: str, known=(), deadline: float | None = None) -> tuple[str, bool]:
    """``(text, clean)``: ``text`` with the secrets the repository's detectors find masked, and
    whether a last scan of the result found nothing more (#701, owner's decision 2026-11-27).

    The formats of the schedule records (``records.mask``) and this module keep a token's prefix
    or a key's name; private key blocks keep their BEGIN line; every other find becomes ``MASK``,
    round after round, until the scan comes back empty or ``REDACTION_ROUNDS`` pass; a value after a
    label naming a secret (``_labelled_spans``) is masked before each round, keeping the label.
    ``known`` are the scanner's known secrets (``environment_secrets``). A round that ``deadline`` (a
    ``time.monotonic`` value) would not leave time for is not started: the text is not clean.
    """
    from src.schedules import scan
    from src.schedules.records import mask

    text = mask(text)
    text = PRIVATE_KEY_BLOCK.sub(lambda match: match[1] + MASK, text)
    for pattern in EXTRA_SECRET_PATTERNS:
        text = pattern.sub(lambda match: match[1] + MASK, text)
    scanner = scan.Scanner(known)
    estimate = _scan_estimate(text, known)
    for _ in range(REDACTION_ROUNDS + 1):
        if deadline is not None and time.monotonic() + estimate > deadline:
            return text, False
        started = time.monotonic()
        # A label keeps its name: its value goes first, so no detector that matches the label with
        # its value takes the name too.
        text = _mask_spans(text, _labelled_spans(text))
        spans = _secret_spans(text, scanner)
        estimate = time.monotonic() - started
        if spans is None:
            return text, False
        if not spans:
            return text, True
        text = _mask_spans(text, spans)
    return text, False
