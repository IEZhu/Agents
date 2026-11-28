"""Secrets cleaned out of a turn before the turn-log hook writes it (#701, owner's decisions 2026-11-27/28).

The Stop hook ``src.memory.turn_log_hook`` writes every interactive turn by default, so what it
writes must be clean (#701 review): ``redact`` masks what the rules below and the repository's own
detectors find, round after round, and says whether a last scan found nothing more.

- The line rule (owner's decision 2026-11-28, ``line_spans``): a line that names a secret, by a
  word in an identifier or a label (``DB_PASSWORD``, ``"token"``, ``$authToken``, ``-Dapi.key``,
  ``api key:``, ``пароль``) or by a client's password option (``mysql -p``, ``curl -u``), loses
  everything after that word to the end of the line, separators and symbols included. A value
  that a label line leaves for the lines below (a YAML block, a table's header) goes with them.
  A word used as an ordinary word in a sentence keeps its line when the rule can tell nothing
  after it is a value; in doubt the line is masked.
- Formats: the schedule records' token formats (``records.SECRET_PATTERNS``) and
  ``EXTRA_SECRET_PATTERNS``, keeping a token's prefix; private key blocks, PGP ones included, and
  key bodies whose BEGIN line is missing; credentials in URLs and DSNs; pgpass lines.
- Shapes: random-looking runs of token characters or of base64, 43-character tokens, hex keys of
  32 digits or of a length no commit name or digest has, base32 secrets, hex dumps of key
  material, AWS secret keys and Discord tokens, except the ids ``NOT_SECRET`` names.
- The scanner of self-development (``src.schedules.scan``): token formats, high-entropy strings and
  the caller's known secrets (``environment_secrets``), also split or encoded; and the credentials
  that shared memory and user sync refuse to serve or publish.

Every rule works line by line or on bounded windows, so a scan stays linear in the text's length
(#701 review: one pattern took 70 s on a 160,000-character line). What still passes is listed in
docs/memory.md (Secrets and private details).
"""
from __future__ import annotations

from bisect import bisect_left
import functools
import os
import re
import time

# What a masked secret becomes. No detector reads it as a value, since it starts with "<": user
# sync and shared memory take <...> for a placeholder, so masking ends when nothing else is found.
MASK = "<secret>"
# Masking one secret can leave another detector's match; the scan runs again on the masked text
# this many times at most.
REDACTION_ROUNDS = 4
# How long the first scan of a text may take, per character and per known secret besides, as
# measured on log-like text (#701 review: 2.7 s for 1 MB, 10.5 s with 40 environment secrets).
SCAN_SECONDS_PER_CHARACTER = 3e-6
SCAN_SECONDS_PER_CHARACTER_AND_SECRET = 2.5e-7

# --- Words that name a secret (owner's decision 2026-11-28) -----------------------------------------
# A word names a secret on its own and in any identifier: in a sentence it keeps its line only when
# nothing after it can be a value. Words that end like one count too (PGPASSWORD, clientSecret).
STRONG_WORDS = frozenset({
    "password", "passwd", "passphrase", "secret", "token", "credential", "credentials", "creds", "apikey",
    "accesskey", "secretkey", "privatekey", "authtoken"})
STRONG_ENDINGS = ("password", "passwd", "passphrase", "secret", "token")
STRONG_STEMS = ("парол", "токен", "секрет")
# Words common in prose: they name a secret only inside an identifier (api_key, authToken), quoted,
# after a sigil or a dash ($key, -key), or before a separator (key: V, key=V, key 'V').
WEAK_WORDS = frozenset({
    "key", "keys", "apikeys", "pass", "pwd", "pw", "passcode", "auth", "authorization", "cookie", "cookies",
    "passwords", "secrets", "tokens", "otp", "hmac", "bearer", "oauth", "dsn", "mnemonic", "sessionid",
    "capability"})
WEAK_STEMS = ("ключ",)
# A joined name in capitals that ends in one of these (MASTERKEY, SSHPASS, DBPASS).
CAPS_ENDINGS = ("key", "keys", "pass")
# Words that name a secret only as the last word of a name right before "=": pin=, salt=, sig=.
EQUALS_WORDS = frozenset({"private", "seed", "salt", "pin", "session", "sig", "signature"})
# Calls whose arguments hold a password: conn.login("deploy", "V").
CALL_WORDS = frozenset({"login", "logon", "signin", "authenticate"})
# Two words that name a secret together, side by side in a name or a blank apart.
STRONG_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key"), ("signing", "key"),
                          ("encryption", "key"), ("master", "key")})
WEAK_PAIRS = frozenset({("session", "id"), ("connection", "string"), ("seed", "phrase"), ("recovery", "phrase")})
# A last word that makes the name a secret's property, not the secret: SSH_KEY_NAME, AUTH_MODE,
# TOKEN_TYPE, PASSWORD_FILE, ACCESS_TOKEN_EXPIRE_MINUTES.
PROPERTY_WORDS = frozenset({
    "name", "names", "mode", "path", "paths", "file", "files", "dir", "dirs", "directory", "id", "ids",
    "type", "types", "kind", "length", "size", "count", "limit", "limits", "enabled", "disabled", "set",
    "version", "provider", "helper", "format", "prefix", "suffix", "label", "policy", "method", "scheme",
    "algorithm", "endpoint", "host", "port", "region", "url", "uri", "domain", "expiry", "expires", "ttl",
    "timeout", "user", "username", "login", "email", "digest", "hash", "fingerprint", "checksum", "stdin",
    "command", "cmd", "prompt", "header", "headers", "field", "fields", "param", "params", "env", "var",
    "minutes", "seconds", "hours", "days", "age", "lifetime", "model", "backend", "store", "cache"})
# A word before token(s) that makes the name a count: max_tokens, input_tokens, MAX_TOKENS.
QUANTITY_WORDS = frozenset({
    "max", "min", "num", "number", "total", "input", "output", "prompt", "completion", "cache", "count",
    "used", "remaining", "reasoning", "thinking", "budget", "per"})
# After a word used in prose, these introduce a value: "the password is ...", "the token was ...".
INTRODUCERS = frozenset({
    "is", "are", "was", "were", "be", "been", "being", "equals", "equal", "becomes", "became", "reads", "set",
    "sets", "это", "был", "была", "было", "были", "будет", "равен", "равна", "равно", "является"})
# "to" introduces one only after a verb that sets it, earlier on the line: "set the password to ...".
SETTING_VERBS = frozenset({"set", "sets", "change", "changes", "changed", "reset", "resets", "update", "updates",
                           "updated", "поменяй", "смени", "установи"})
VOWELS = frozenset("aeiouyаеёиоуыэюя")

# --- Names of environment variables (#701 review) ---------------------------------------------------
# A variable's name alone decides whether its value is a known secret: plural and joined names count
# (API_KEYS, SERVICE_TOKENS, MASTERKEY, SSHPASS, ADMIN_PW, GITHUB_OAUTH, PRIVATE_PEM).
ENVIRONMENT_WORDS = STRONG_WORDS | WEAK_WORDS | frozenset({"secrets", "jwt", "pem", "salt", "signature"})
# Alone, these words are too common to name a secret: "PASS: test_x" in a test report, the shell's PWD.
LONE_WORDS = frozenset({"pass", "pwd", "pw"})
# A database or broker word before URL, URI or DSN: a connection string that often holds a password.
CONNECTION_WORDS = frozenset({
    "database", "db", "redis", "mongo", "mongodb", "postgres", "postgresql", "pg", "mysql", "mariadb",
    "amqp", "rabbitmq", "broker", "celery", "sqlalchemy"})
CONNECTION_ENDINGS = frozenset({"url", "uri", "dsn"})
_NAME_WORDS = re.compile(r"[^A-Za-z0-9]+|(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])"
                         r"|(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])")

# --- How the line rule reads a line -----------------------------------------------------------------
_LETTERS = "A-Za-zЀ-ӿ"
# A name: letters, digits and _, joined by - or . between them (DB_PASSWORD, api-key, spring.datasource).
_IDENT = re.compile(rf"[0-9{_LETTERS}_]+(?:[-.][0-9{_LETTERS}_]+)*")
_LETTER_RUN = re.compile(rf"[{_LETTERS}]+")
_CAMEL = re.compile(r"(?<=[a-zа-џ])(?=[A-ZЀ-Я])|(?<=[A-ZЀ-Я])(?=[A-ZЀ-Я][a-zа-џ])")
# A line without one of these holds no word of the rule and is skipped at once.
_HINT = re.compile(r"(?i)pass|pw|key|token|secret|auth|cred|cookie|dsn|otp|hmac|bearer|mnemonic|session|"
                   r"connection|seed|recovery|private|salt|pin|sig|capab|log[io]n|signin|api|access|encryption|"
                   r"master|парол|токен|ключ|секрет")
_PLAIN_KEBAB = re.compile(rf"[a-zа-џ]+(?:-[a-zа-џ]+)+")
# What a label line may end with and still hold no value: quotes, separators, brackets, a YAML
# block's indicator and sentence punctuation (password: |, "password": {, name="password").
_NO_VALUE = " \t\"'`:=;,.!?{[()]}|>-+*\\"
_PLAIN_TOKEN = re.compile(rf"\(?[{_LETTERS}]+(?:['’-][{_LETTERS}]+)*[.,;!?)]{{0,3}}")
_APOSTROPHES = re.compile(r"['’-]")
_NAME_TAIL = re.compile(rf"[{_LETTERS}_.\-]*")
# The armour lines of a key block; the block's own rule masks what lies between them.
_ARMOUR_LINE = re.compile(r"[ \t]*-----(?:BEGIN|END) [A-Z0-9 ]+-----[ \t]*")
# Password options of common clients: the client's name, then the option, anywhere after it on the line.
CLIENT_OPTIONS = tuple((re.compile(client), re.compile(option) if option else None) for client, option in (
    (r"\b(?:mysql|mysqldump|mysqladmin|mysqlimport|mysqlshow|mysqlcheck|mysqlpump|mysqlsh|mariadb(?:-dump|-admin)?)\b",
     r"(?<![\w-])-p"),
    (r"\bdocker[ \t]+login\b", r"(?<![\w-])-p\b"),
    (r"\bsqlcmd\b", r"(?<![\w-])-P"),
    (r"\bredis-cli\b", r"(?<![\w-])-a\b"),
    (r"\bcurl\b", r"(?<![\w-])(?:-u|-U|--user|--proxy-user)"),
    (r"\bsshpass\b", r"(?<![\w-])-p"),
    (r"(?:^|[\s;&|(])https?(?=[ \t])", r"(?<![\w-])-a\b"),
    (r"\bldap(?:search|modify|add|delete|passwd|whoami|compare)\b", r"(?<![\w-])-w\b"),
    (r"\bsmbclient\b", r"(?<![\w-])-U"),
    (r"\b7za?\b", r"(?<![\w-])-p"),
    (r"\b(?:zip|unzip)\b", r"(?<![\w-])-P"),
    (r"\bopenssl\b", r"(?<![\w-])-k\b"),
    (r"\bmongo(?:sh|dump|restore|export|import)?\b", r"(?<![\w-])-p"),
    (r"::add-mask::", None),
))
_CLIENT_HINT = re.compile(r"mysql|mariadb|docker|sqlcmd|redis-cli|curl|sshpass|http|ldap|smbclient|7z|zip|openssl|"
                          r"mongo|::add-mask::")
STRONG, WEAK, EQUALS, CALL = "strong", "weak", "equals", "call"

# --- Formats and shapes -----------------------------------------------------------------------------
# Formats the schedule records do not know, masked here on top of theirs, keeping the prefix.
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
    re.compile(r"\b(do[opr]_v1_)[0-9a-f]{64}\b"),                  # DigitalOcean
    re.compile(r"\b(PMAK-)[0-9a-f]{24}-[0-9a-f]{34}\b"),           # Postman
    re.compile(r"\b(NRAK-)[A-Z0-9]{27}\b"),                         # New Relic
    re.compile(r"\b(pat)[A-Za-z0-9]{14}\.[0-9a-f]{64}\b"),          # Airtable
)
# A private key block: the armour lines stay, what lies between them goes when it is key material
# (base64, the headers of an encrypted or PGP key, JSON's \n escapes); a BEGIN line without its END
# line takes the key-material lines that follow it, and a BEGIN line in prose takes nothing.
KEY_ARMOUR = re.compile(r"-----(BEGIN|END) (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
_KEY_MATERIAL = re.compile(r"[A-Za-z0-9+/=]+")
_KEY_HEADER = re.compile(r"(?:Proc-Type|DEK-Info|Version|Comment|Hash|Charset|MessageID):[ \t].*")
_ESCAPED_NEWLINE = re.compile(r"\\r\\n|\\n|\\r")
_SHORT_KEY_PIECE = re.compile(r"[0-9+/=]")
# A key body without either armour line: two or more lines of base64 (#701 review); what follows
# the block decides, so the search stays linear in the number of lines.
BASE64_LINES = re.compile(r"(?m)^(?:[ \t]*[A-Za-z0-9+/=]{16,}[ \t]*(?:\r?\n|\Z))+")
KEY_END_LINE = re.compile(r"[ \t]*-----END (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----")
KEY_LINE_LENGTH = 40
# Key material printed as hex pairs (openssl -text): two or more lines of eight pairs or more.
HEX_PAIR_LINES = re.compile(r"(?m)(?:^[ \t]*(?:[0-9a-fA-F]{2}:){7,}[0-9a-fA-F]{0,2}[ \t]*(?:\r?\n|\Z)){2,}")
# A credential in a URL: scheme://user:password@ (the user may be empty, the password may hold an
# at sign), //user:password@ without a scheme, and user:password@host in a DSN (app:pw@tcp(db)).
URL_CREDENTIAL = re.compile(r"(?i)(?:(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,20}:)?//[^\s/:@]{0,128}:"
                            r"(?P<value>[^\s/]{1,512})@")
URL_USER_TOKEN = re.compile(r"(?i)(?<![a-z0-9+.-])[a-z][a-z0-9+.-]{0,20}://(?P<value>[^\s/:@]{8,256})@")
DSN_CREDENTIAL = re.compile(r"(?<![^\s\"'(<=,;])(?P<user>[A-Za-z0-9_.+-]{1,64}):"
                            r"(?P<value>[^\s/@\"']{1,256})@(?=[A-Za-z0-9-])")
# A pgpass line: host:port:database:user:password.
PGPASS_LINE = re.compile(r"(?m)^[ \t]*[A-Za-z0-9_.*-]{1,253}:(?:\d{1,5}|\*):[^:\s]{1,63}:[^:\s]{1,63}:"
                         r"(?P<value>[^\r\n]+)")
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
# A run of token characters long enough to be a secret.
TOKEN_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])")
# A run of base64's alphabet with + or /, judged by its pieces between them (a path's pieces are words).
BASE64_RUN = re.compile(r"(?<![A-Za-z0-9+/=])[A-Za-z0-9+/]{20,}={0,2}(?![A-Za-z0-9+/=])")
# Hexadecimal digits of one case, as long a run as they go, glued to a prefix or a suffix or not
# (shpat_H, key-H, dapiH, H-us21): 32 digits (an MD5-sized key) and any length from 33 on that no
# commit name or digest has; 40, 56, 64, 96 and 128 digits are SHA-1, SHA-224, SHA-256, SHA-384 and
# SHA-512 digests and commit names, and stay, as shorter runs do (short commits, object ids).
HEX_RUN = re.compile(r"(?<![0-9a-fA-F])(?:[0-9a-f]{32,}|[0-9A-F]{32,})(?![0-9a-fA-F])")
DIGEST_LENGTHS = frozenset({40, 56, 64, 96, 128})
# Base32 of RFC 4648 (A-Z and 2-7): a TOTP seed or an encoded secret. ULIDs and Crockford ids hold
# 0, 1, 8 or 9 and do not match.
BASE32_RUN = re.compile(r"(?<![A-Za-z0-9_-])[A-Z2-7]{16,}={0,6}(?![A-Za-z0-9_-])")
_SEGMENTS = re.compile(r"[A-Z]+(?=[A-Z][a-z])|[A-Z]?[a-z]+|[A-Z]+|[0-9]+")
_PIECES = re.compile(r"[_-]+")
_BASE64_PIECES = re.compile(r"[+/=]+")
_HEX_PIECE = re.compile(r"[0-9a-f]+|[0-9A-F]+")
_WORD_PIECE = re.compile(r"[A-Za-z]{3,}|[0-9]+")
_ASCII_VOWELS = frozenset("aeiouyAEIOUY")
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
    r"|(?<=;base64,)[A-Za-z0-9+/]{16,}={0,2}"
    r"|\b00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}\b")  # a W3C traceparent: trace and span ids
_LINE_END = re.compile(r"[^\r\n]*")


@functools.cache
def _sync_patterns() -> tuple:
    """User sync's credential patterns, which match bytes, compiled for text."""
    from src.user_sync.scope import SECRET_PATTERNS
    return tuple((name, re.compile(pattern.pattern.decode("ascii"))) for name, pattern in SECRET_PATTERNS)


@functools.cache
def _record_patterns() -> tuple:
    """The schedule records' token formats, without their private key pattern, which masks to the
    end of the text when a BEGIN line has no END line: ``KEY_ARMOUR`` takes key blocks here."""
    from src.schedules import records
    return tuple(pattern for pattern in records.SECRET_PATTERNS if "PRIVATE KEY" not in pattern.pattern)


# --- Environment names --------------------------------------------------------------------------------


def name_words(name: str) -> list[str]:
    """The words of an identifier, lower-cased: split at ``_ - .``, at case changes and at digits."""
    return [word.lower() for word in _NAME_WORDS.split(name) if word]


def secret_name(name: str) -> bool:
    """Whether an environment variable's name names a secret (#701 review): one of its words is a
    word of the line rule or ``ENVIRONMENT_WORDS``, ends like a strong one (PGPASSWORD,
    clientSecret) or, in a name of capitals, like a key or a password (MASTERKEY, SSHPASS); or a
    database or broker word comes before a last URL, URI or DSN (DATABASE_URL, REDIS_URL). A word
    inside another word does not count (KEYRING, KEYBOARD, AUTHOR), ``pass``, ``pwd`` and ``pw``
    alone do not (``PASS:`` in a test report, the shell's PWD), a last ``PROPERTY_WORDS`` word names
    a property (SSH_KEY_NAME, AUTH_MODE), and a quantity before tokens a count (MAX_TOKENS)."""
    words = name_words(name)
    if not words or len(words) == 1 and words[0] in LONE_WORDS:
        return False
    if words[-1] in CONNECTION_ENDINGS and any(word in CONNECTION_WORDS for word in words[:-1]):
        return True
    if words[-1] in PROPERTY_WORDS:
        return False
    if len(words) > 1 and words[-1] in ("token", "tokens") and words[-2] in QUANTITY_WORDS:
        return False
    capitals = name.upper() == name
    return any(word in ENVIRONMENT_WORDS or word.endswith(STRONG_ENDINGS) or word.startswith(STRONG_STEMS + WEAK_STEMS)
               or capitals and len(word) > 3 and word.endswith(CAPS_ENDINGS) for word in words)


def environment_secrets(environ=None) -> list:
    """The secret-looking values of the hook's environment, as the scanner's known secrets.

    A value counts when its variable's name names a secret (``secret_name``: by word, so KEYRING,
    KEYBOARD and AUTHOR do not count, and DB_PASS, MYSQL_PWD, API_KEYS, SSHPASS, ADMIN_PW,
    SENTRY_DSN and DATABASE_URL do), or when the value itself scans as one, as the schedule sandbox
    checks a session's environment. A value under ``KNOWN_MIN_LENGTH`` characters, or one naming an
    existing absolute path, is no secret to look for.
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


# --- The line rule (owner's decision 2026-11-28) -------------------------------------------------------


def _word_class(word: str) -> str | None:
    lower = word.lower()
    if (lower in STRONG_WORDS or lower.startswith(STRONG_STEMS)
            or len(lower) > 5 and lower.endswith(STRONG_ENDINGS)):
        return STRONG
    if (lower in WEAK_WORDS or lower.startswith(WEAK_STEMS)
            or word.isupper() and len(lower) > 3 and lower.endswith(CAPS_ENDINGS)):
        return WEAK
    if lower in EQUALS_WORDS:
        return EQUALS
    if lower in CALL_WORDS:
        return CALL
    return None


def _words(name: str, offset: int) -> list[tuple[int, int, str]]:
    """``(start, end, word)`` of a name's words, at ``offset`` in its line: letter runs, split at
    case changes unless the whole run is a word of the rule (OAuth, APIKey stay whole). A piece of
    two letters split off by a case change is no word (``wPw7X`` in a random id is not ``pw``)."""
    out = []
    for run in _LETTER_RUN.finditer(name):
        text, start = run.group(), run.start()
        if _word_class(text) is not None or text.lower() in {"oauth"}:
            out.append((offset + start, offset + run.end(), text))
            continue
        cuts = [0] + [cut.start() for cut in _CAMEL.finditer(text)] + [len(text)]
        out.extend((offset + start + a, offset + start + b, text[a:b]) for a, b in zip(cuts, cuts[1:])
                   if b - a > 2 or b - a == len(text))
    return out


PAIR_FIRSTS = frozenset(first for first, _ in STRONG_PAIRS | WEAK_PAIRS)


@functools.lru_cache(maxsize=16384)
def _name_info(name: str) -> tuple:
    """``(words, lowered, labels, random, pairable)`` of a name, offsets within it: its words, its
    labels in order (pairs first where two words form one: api_key, session_id, then single words),
    whether it is a random-looking run, and whether its last word may begin a pair with the next
    name a blank away ("API key")."""
    words = _words(name, 0)
    lowered = [word.lower() for _, _, word in words]
    labels, paired = [], set()
    for index, (start, end, word) in enumerate(words):
        if index in paired:
            continue
        if index + 1 < len(words):
            pair = (lowered[index], lowered[index + 1])
            if pair in STRONG_PAIRS or pair in WEAK_PAIRS:
                labels.append((start, words[index + 1][1], STRONG if pair in STRONG_PAIRS else WEAK, pair))
                paired.add(index + 1)
                continue
        kind = _word_class(word)
        if kind is not None:
            labels.append((start, end, kind, (lowered[index],)))
    random_run = bool(TOKEN_RUN.fullmatch(name)) and random_looking(name)
    pairable = bool(lowered) and lowered[-1] in PAIR_FIRSTS and len(words) - 1 not in paired
    return tuple(words), tuple(lowered), tuple(labels), random_run, pairable


def _labels(line: str, name: re.Match) -> list[tuple[int, int, str, tuple[str, ...]]]:
    """``(start, end, class, words)`` of the labels in a name, at their place in ``line``; a last
    word and the next name's first word a blank away may form a pair ("API key", "session id")."""
    words, lowered, labels, _random, pairable = _name_info(name.group())
    offset = name.start()
    out = [(offset + start, offset + end, kind, label) for start, end, kind, label in labels]
    if pairable and line[name.end():name.end() + 1] == " ":
        following = _IDENT.match(line, name.end() + 1)
        first = _name_info(following.group())[0][:1] if following is not None else ()
        if first and first[0][0] == 0:
            pair = (lowered[-1], first[0][2].lower())
            if pair in STRONG_PAIRS or pair in WEAK_PAIRS:
                last = offset + words[-1][0]
                out = [label for label in out if label[0] != last]
                out.append((last, following.start() + first[0][1], STRONG if pair in STRONG_PAIRS else WEAK, pair))
                out.sort()
    return out


def _separator_after(line: str, index: int) -> str | None:
    """What follows a name, past its closing quotes, brackets and Markdown emphasis and blanks:
    ``=`` (also ``:=``, ``=>``, ``?=``, ``+=``, ``==``), ``:`` (also ``->``; never ``::`` or ``://``),
    ``quote`` for an opening quote a blank away (``key 'V'``), ``call`` for ``(``, else None."""
    i, length = index, len(line)
    while i < length and line[i] in "\"'`)]*\\" and i - index < 8:
        i += 1
    closed = i
    while i < length and line[i] in " \t ":
        i += 1
    rest = line[i:i + 3]
    if rest.startswith(("=", ":=", "?=", "+=")):
        return "="
    if rest.startswith("->") or rest.startswith(":") and not rest.startswith(("::", ":/")):
        return ":"
    if rest[:1] in ("\"", "'") and i > closed:
        return "quote"
    if rest[:1] == "(" and i == index:
        return "call"
    return None


def _field(line: str, start: int, end: int) -> str | None:
    """The delimiter of a table's header that the name is a field of (``user,password``,
    ``| user | password |``), else None."""
    before, after = line[start - 1:start] if start else "", line[end:end + 1]
    for delimiter in (",", ";", "\t"):
        if before in (delimiter, "") and after in (delimiter, "") and delimiter in line:
            return delimiter
    if line[:start].rstrip(" ").endswith("|") and line[end:].lstrip(" ").startswith("|"):
        return "|"
    return None


def _prose_token(token: str, introducers: frozenset) -> bool:
    """A word of ordinary prose: letters with an apostrophe or a hyphen inside, sentence punctuation
    around, lower case, capitalised or a short acronym, a vowel in a word of four letters or more,
    and no verb that introduces a value."""
    if not _PLAIN_TOKEN.fullmatch(token):
        return False
    word = token.strip("(.,;!?)")
    letters = _APOSTROPHES.sub("", word)
    if not letters or not (letters.islower() or letters[0].isupper() and letters[1:].islower()
                           or letters.isupper() and len(letters) <= 5):
        return False
    lower = letters.lower()
    return lower not in introducers and (len(lower) < 4 or any(character in VOWELS for character in lower))


class _Prose:
    """Whether what follows a word to the end of its line is a sentence of ordinary words, at least
    two of them; checked once per line, since a later word's rest is part of an earlier one's."""

    def __init__(self, line: str):
        self.line, self.since, self.starts = line, None, []

    def after(self, index: int) -> bool:
        if self.since is not None and index >= self.since:
            return len(self.starts) - bisect_left(self.starts, index) >= 2
        # An earlier round's placeholder is no word, so a later round decides as the first one did.
        rest = self.line[index:].replace(MASK, " masked ")
        tokens = [(match.start(), match.group()) for match in re.finditer(r"\S+", rest)]
        before = {word.lower() for word in _LETTER_RUN.findall(self.line[:index])}
        introducers = INTRODUCERS | {"to"} if before & SETTING_VERBS else INTRODUCERS
        if len(tokens) < 2 or not all(_prose_token(token, introducers) for _, token in tokens):
            return False
        self.since, self.starts = index, [index + start for start, _ in tokens]
        return True


def _trigger(line: str) -> tuple[int, str | None] | None:
    """Where the line rule masks ``line`` from and what kind of label it is (``bare``, ``table:<d>``,
    ``quoted``, ``label``, or None for a word of prose), for the first label or client option
    that names a secret; None when no label does."""
    found = _word_trigger(line) if _HINT.search(line) and not _ARMOUR_LINE.fullmatch(line) else None
    if _CLIENT_HINT.search(line):
        for client, option in CLIENT_OPTIONS:
            match = client.search(line)
            if match is None:
                continue
            if option is not None:
                match = option.search(line, match.end())
                if match is None:
                    continue
            if found is None or match.end() < found[0]:
                found = (match.end(), "label")
    return found


def _word_trigger(line: str) -> tuple[int, str | None] | None:
    prose, exempt, documents = _Prose(line), None, "docs.google" in line
    for name in _IDENT.finditer(line.replace(MASK, " " * len(MASK)) if MASK in line else line):
        if not _HINT.search(name.group()):
            continue
        start, end = name.span()
        _words_, words, _labels_, random_run, _pairable = _name_info(name.group())
        # A random-looking run is a value, never a label: the shape rules judge it, and ids that
        # look random and are no secret (toolu_01..., session_01...) stay.
        if random_run:
            continue
        if documents or "_01" in name.group() or "_kw" in name.group() or "_lA" in name.group():
            if exempt is None:
                exempt = _not_secret_spans(line)
            if _inside((start, end), exempt):
                continue
        for label_start, label_end, kind, label in _labels(line, name):
            label_name_end = max(end, label_end)  # a pair a blank apart ends in the next name
            if words and words[-1] in PROPERTY_WORDS and label[-1] != words[-1]:
                continue
            index = words.index(label[0]) if label[0] in words else -1
            if label[-1] in ("token", "tokens") and index > 0 and words[index - 1] in QUANTITY_WORDS:
                continue
            before = line[start - 1:start] if start else ""
            # A name in a Markdown code span (`key`) is prose about it, not a label.
            quoted = before in ("\"", "'", "<") or line[max(0, start - 2):start] in ("\\\"", "\\'")
            sigil = before in ("$", "@", "%", "-")
            joined = (line[start:label_name_end] != line[label_start:label_end]
                      and not _PLAIN_KEBAB.fullmatch(name.group()))
            separator = _separator_after(line, label_name_end)
            stripped = line.strip().rstrip(":=").strip()
            bare = stripped == line[label_start:label_end] or stripped == line[start:label_name_end]
            field = _field(line, start, label_name_end)
            if kind == EQUALS:
                hit = label_end == label_name_end and separator == "="
            elif kind == CALL:
                hit = label_end == label_name_end and separator == "call"
            elif kind == WEAK:
                # A dotted name without a separator is a package path or a file (oauth2.client, server.key),
                # and a short word that only case or digits set off is chance in a random id (KeyNoe4w).
                dotted = "." in name.group() and not (quoted or sigil)
                chance = label_end - label_start < 4 and not _set_off(line, label_start, label_end)
                hit = (joined and not dotted and not chance) or quoted or sigil or separator in ("=", ":", "quote")
            else:
                hit = (joined or quoted or sigil or separator in ("=", ":", "quote")
                       or not prose.after(label_name_end))
            # From the end of the name when the rest of it is words (SECRET_KEY, authToken), else
            # from the end of the word, so a value glued to it goes too (key-<hex>).
            rest = line[label_end:label_name_end]
            mask_from = label_name_end if _NAME_TAIL.fullmatch(rest) else label_end
            if not line[mask_from:].strip(_NO_VALUE):
                # Nothing to mask on this line: a label line leaves its value for the lines below. A
                # word alone on its line or heading a table's column is such a label when it names a
                # secret by itself (Password, username,password), a weaker word only with a separator.
                strong = kind == STRONG or kind == WEAK and separator in ("=", ":")
                if bare and strong:
                    return mask_from, "bare"
                if field is not None and strong:
                    return mask_from, "table:" + field
                if hit and (joined or quoted or sigil or separator in ("=", ":")):
                    return mask_from, "quoted" if quoted and not separator else "label"
                continue
            if hit:
                return mask_from, "label"
    return None


def _set_off(line: str, start: int, end: int) -> bool:
    """Whether a word stands between separators of a name or its ends (db_pw, ADMIN_PW, x-pw)."""
    before, after = line[start - 1:start], line[end:end + 1]
    return not (before.isalnum() or after.isalnum())


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" \t"))


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


# The quote that opens a value after a label's separator (password="..., "token": '..., \"key\": \"...).
_OPENING_QUOTE = re.compile(r"[\"'`)\]*\\]{0,8}[ \t]*(?::=|=>|->|\?=|\+=|==?|:)[ \t]*(\\?)([\"'`])")


def line_spans(text: str) -> list[tuple[int, int]]:
    """Where the line rule masks ``text`` (owner's decision 2026-11-28): after the first label on a
    line that names a secret, to the end of the line (``_trigger``); a quoted value that opens there
    and closes on a later line, to the end of that line; and the value a label line without one leaves
    below: the lines indented under it (a YAML block, a Terraform block), the next line after a
    label alone on its line or a quoted name (a form field), and the rows under a table's header."""
    spans: list[tuple[int, int]] = []
    lines, offset = [], 0
    for piece in text.split("\n"):
        end = offset + len(piece)
        lines.append((offset, end - 1 if piece.endswith("\r") else end))
        offset = end + 1
    index, covered = 0, 0
    while index < len(lines):
        start, end = lines[index]
        if start < covered:
            index += 1
            continue
        line = text[start:end]
        found = _trigger(line)
        if found is None:
            index += 1
            continue
        mask_from, form = found
        tail = line[mask_from:]
        if tail.strip(_NO_VALUE):
            if tail != MASK:
                stop = end
                opening = _OPENING_QUOTE.match(tail)
                if opening is not None:
                    quote_at = start + mask_from + opening.start(2)
                    if opening.group(1):  # an escaped quote, inside a JSON string: it closes escaped too
                        close = text.find("\\" + opening.group(2), quote_at + 1)
                        close = len(text) if close < 0 else close
                    else:
                        close = _quoted_end(text, quote_at)
                    if close > end:  # to the end of the line the value closes on
                        stop = _LINE_END.match(text, close).end()
                spans.append((start + mask_from, stop))
                covered = stop
            index += 1
            continue
        if form is None:
            index += 1
            continue
        # A label line without its value: the value is below it.
        indent, below, masked = _indent(line), index + 1, False
        while below < len(lines):
            row_start, row_end = lines[below]
            row = text[row_start:row_end]
            if not row.strip():
                below += 1
                continue
            if _indent(row) <= indent:
                break
            spans.append((row_start + _indent(row), row_end))
            masked = True
            below += 1
        if not masked and below < len(lines):
            if form.startswith("table:"):
                delimiter = form.split(":", 1)[1]
                while below < len(lines):
                    row_start, row_end = lines[below]
                    row = text[row_start:row_end]
                    if not row.strip() or delimiter not in row:
                        break
                    spans.append((row_start + _indent(row), row_end))
                    below += 1
            elif form in ("bare", "quoted"):
                row_start, row_end = lines[below]
                spans.append((row_start + _indent(text[row_start:row_end]), row_end))
                below += 1
        index = below
    return [span for span in spans if text[span[0]:span[1]] != MASK]


# --- Formats and shapes --------------------------------------------------------------------------------


def _key_line(line: str) -> bool:
    """A line of key material: base64 pieces (long ones, and a short last one with a digit or
    padding), an encrypted or PGP key's header, or blank; never a line of words."""
    line = line.strip()
    if not line or _KEY_HEADER.fullmatch(line):
        return True
    pieces = line.split()
    if not all(_KEY_MATERIAL.fullmatch(piece) for piece in pieces) or any(len(piece) < 16 for piece in pieces[:-1]):
        return False
    return len(pieces[-1]) >= 16 or bool(_SHORT_KEY_PIECE.search(pieces[-1]))


def _key_body(body: str) -> bool:
    """Whether ``body`` is a key's material, JSON's ``\\n`` escapes read as line breaks."""
    lines = _ESCAPED_NEWLINE.sub("\n", body).split("\n")
    return all(_key_line(line) for line in lines) and any(len(line.strip()) >= 16 for line in lines)


def key_block_spans(text: str) -> list[tuple[int, int]]:
    """The bodies of private key blocks (#701 review): between a BEGIN line and its END line when
    what lies between is key material, else the key-material lines right after a BEGIN line; the
    armour lines stay lines of their own."""
    spans = []
    armour = list(KEY_ARMOUR.finditer(text))
    for index, match in enumerate(armour):
        if match.group(1) != "BEGIN":
            continue
        following = armour[index + 1] if index + 1 < len(armour) else None
        if following is not None and following.group(1) == "END" and _key_body(text[match.end():following.start()]):
            start, end = match.end(), following.start()
            start += 2 if text.startswith("\r\n", start) else 1 if text.startswith("\n", start) else 0
            end -= 2 if text[max(start, end - 2):end] == "\r\n" else 1 if text[max(start, end - 1):end] == "\n" else 0
            spans.append((start, max(start, end)))
            continue
        # No END line, or prose before it: the lines of key material right after the BEGIN line.
        first = text.find("\n", match.end())
        position, stop = first, None
        while position >= 0:
            line_end = text.find("\n", position + 1)
            line = text[position + 1:line_end if line_end >= 0 else len(text)]
            if not line.strip():
                position = line_end
                continue
            if not _key_line(line) or len(line.strip()) < 16 and stop is None or KEY_ARMOUR.search(line):
                break
            stop = line_end if line_end >= 0 else len(text)
            position = line_end
        if stop is not None:
            spans.append((first + 1, stop - 1 if text[stop - 1:stop] == "\r" else stop))
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
    if _HEX_PIECE.fullmatch(piece) or not any(character.islower() for character in piece):
        return False
    segments = _SEGMENTS.findall(piece)
    digit_runs = sum(1 for segment in segments if segment.isdigit())
    letters = [character for character in piece if character.isalpha()]
    vowels = sum(character in _ASCII_VOWELS for character in letters) / max(1, len(letters))
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
    if any(_WORD_PIECE.fullmatch(piece) or _HEX_PIECE.fullmatch(piece) for piece in pieces):
        return False
    joined = "".join(pieces)
    return any(character.isupper() for character in joined) and _random_piece(joined)


def _base32_secret(run: str) -> bool:
    """A base32 run that is no word in capitals: several digits in several places, few vowels."""
    body = run.rstrip("=")
    digits = [character for character in body if character.isdigit()]
    letters = [character for character in body if character.isalpha()]
    runs = len(re.findall(r"[2-7]+", body))
    vowels = sum(character in "AEIOU" for character in letters) / max(1, len(letters))
    return len(digits) >= 3 and runs >= 2 and vowels < 0.35


def _shape_spans(text: str, exempt: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Unlabelled secrets known by their shape, outside the ``exempt`` regions: key bodies and hex
    dumps, credentials in URLs, DSNs and pgpass lines, AWS secret keys, Discord tokens, 43-character
    tokens, random-looking runs of token characters or of base64, hex keys and base32 secrets."""
    spans = []
    for match in BASE64_LINES.finditer(text):
        long_lines = sum(len(line.strip()) >= KEY_LINE_LENGTH for line in match.group().splitlines())
        if KEY_END_LINE.match(text, match.end()) or (long_lines >= 2 and _mixed(match.group())):
            spans.append(match.span())
    spans.extend(match.span() for match in HEX_PAIR_LINES.finditer(text))
    for pattern in (URL_CREDENTIAL, URL_USER_TOKEN, DSN_CREDENTIAL, PGPASS_LINE):
        for match in pattern.finditer(text):
            value = match.group("value")
            if pattern is URL_USER_TOKEN and not (_mixed(value) or not value.isalnum()):
                continue  # https://deploy@host: a user's name, not a token
            if pattern is DSN_CREDENTIAL and match.group("user").lower() == "mailto":
                continue
            spans.append(match.span("value"))
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
    for match in HEX_RUN.finditer(text):
        run = match.group()
        if (len(run) == 32 or len(run) not in DIGEST_LENGTHS) and not _inside(match.span(), exempt) \
                and any(character.isdigit() for character in run) and any(character.isalpha() for character in run):
            spans.append(match.span())
    spans.extend(match.span() for match in BASE32_RUN.finditer(text) if _base32_secret(match.group()))
    return spans


def _mixed(value: str) -> bool:
    """Both letter cases and a digit: the mark of random key material, which hex and words lack."""
    return (any(character.isupper() for character in value) and any(character.islower() for character in value)
            and any(character.isdigit() for character in value))


def _secret_spans(text: str, scanner) -> list[tuple[int, int]] | None:
    """Where the detectors find a secret in ``text``; None when the scanner could not scan it
    whole (too large, too many hits). An entropy hit inside a region that ``NOT_SECRET`` names is
    no secret; token formats and known secrets always are. A labelled credential that shared memory
    or user sync finds is masked to the end of its line, as the line rule masks it."""
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
            if name == "labelled_credential":
                start = match.start("value")
                spans.append((start, _LINE_END.match(text, start).end()))
            else:
                spans.append(match.span())
    for name, pattern in _sync_patterns():
        for match in pattern.finditer(text):
            if name == "password":  # it stops after the first character of the value
                start = match.end() - 1
                spans.append((start, _LINE_END.match(text, start).end()))
            else:
                spans.append(match.span())
    spans.extend(_shape_spans(text, exempt))
    return [span for span in spans if text[span[0]:span[1]] != MASK]


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


def _formats(text: str) -> str:
    """Token formats, keeping their prefix, and the bodies of private key blocks."""
    from src.schedules.records import MASK as RECORDS_MASK

    for pattern in _record_patterns():
        text = pattern.sub(lambda match: match[1] + RECORDS_MASK, text)
    for pattern in EXTRA_SECRET_PATTERNS:
        text = pattern.sub(lambda match: match[1] + MASK, text)
    return _mask_spans(text, key_block_spans(text))


def _scan_estimate(text: str, known) -> float:
    return len(text) * (SCAN_SECONDS_PER_CHARACTER + SCAN_SECONDS_PER_CHARACTER_AND_SECRET * len(known))


def redact(text: str, known=(), deadline: float | None = None) -> tuple[str, bool]:
    """``(text, clean)``: ``text`` with the secrets the rules and the repository's detectors find
    masked, and whether a last scan of the result found nothing more (#701, owner's decisions
    2026-11-27 and 2026-11-28).

    Token formats keep their prefix and private key blocks their armour lines; then, round after
    round, the line rule (``line_spans``) masks after each label that names a secret, and every
    other find becomes ``MASK``, until the scan comes back empty or ``REDACTION_ROUNDS`` pass.
    ``known`` are the scanner's known secrets (``environment_secrets``). A round that ``deadline``
    (a ``time.monotonic`` value) would not leave time for is not started: the text is not clean.
    """
    from src.schedules import scan

    text = _formats(text)
    scanner = scan.Scanner(known)
    estimate = _scan_estimate(text, known)
    for _ in range(REDACTION_ROUNDS + 1):
        if deadline is not None and time.monotonic() + estimate > deadline:
            return text, False
        started = time.monotonic()
        # The detectors read the same text as the line rule, so a known secret is still whole
        # when they look for it, even where the line rule masks part of it.
        spans = _secret_spans(text, scanner)
        estimate = time.monotonic() - started
        if spans is None:
            return text, False
        spans += line_spans(text)
        if not spans:
            return text, True
        text = _mask_spans(text, spans)
    return text, False
