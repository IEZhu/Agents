"""Secrets cleaned out of a turn before the turn-log hook writes it (#701, owner's decisions 2026-11-27/28).

The Stop hook ``src.memory.turn_log_hook`` writes every interactive turn by default, so what it
writes must be clean (#701 review): ``redact`` masks what the rules below and the repository's own
detectors find, round after round, and says whether a last scan found nothing more.

- The line rule (owner's decision 2026-11-28, ``line_spans``): a line that names a secret, by a
  word in an identifier or a label (``DB_PASSWORD``, ``"token"``, ``$authToken``, ``-Dapi.key``,
  ``api key:``, ``пароль``) or by a client's password option (``mysql -p``, ``curl -u``), loses
  everything after that word to the end of the line, separators and symbols included. A value
  that a label line leaves for the lines below goes with them, whatever the form (a YAML block, a
  sentence or heading ending in the label, a fenced block, a heredoc, a continuation line, a
  name-value pair, a table's header in any delimited form), and so does a value written before
  its label in the forms the owner chose (#701 review, attempt 5). A word used as an ordinary word
  in a sentence keeps its line when the rule can tell nothing after it is a value; in doubt the
  line is masked.
- Formats: the schedule records' token formats (``records.SECRET_PATTERNS``) and
  ``EXTRA_SECRET_PATTERNS``, keeping a token's prefix; the bodies of private key blocks, PGP ones
  included (the scanner then masks a block's BEGIN line, and its END line stays), and key bodies
  whose BEGIN line is missing; credentials in URLs and DSNs; pgpass lines.
- Shapes: random-looking runs of token characters or of base64, 43-character tokens, hex keys of
  32 digits or of a length no commit name or digest has, base32 secrets, hex dumps of key
  material, AWS secret keys and Discord tokens, except the ids ``NOT_SECRET`` names.
- The scanner of self-development (``src.schedules.scan``): token formats, high-entropy strings and
  the caller's known secrets (``environment_secrets``), also split or encoded; and the credentials
  that shared memory and user sync refuse to serve or publish.

Every rule works line by line or on bounded windows: a match that masks to its line's end is
read once per line, lookaheads and quoted values are bounded, no pattern restarts inside a run,
and a line's tokens are read once for all its words (#701 reviews: one pattern took 70 s on a
160,000-character line, and ten shapes of the final verification 5 to over 120 s at 1 MB). This
is measured, not proven: docs/memory.md gives the times. What still passes is listed there too
(Secrets and private details).
"""
from __future__ import annotations

from bisect import bisect_left, bisect_right
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
# nothing after it can be a value. Words that end like one count too (PGPASSWORD, clientSecret), and
# "assword", which expect scripts match a prompt by.
STRONG_WORDS = frozenset({
    "password", "passwd", "passphrase", "secret", "token", "credential", "credentials", "creds", "apikey",
    "accesskey", "secretkey", "privatekey", "authtoken", "pswd", "assword"})
STRONG_ENDINGS = ("password", "passwd", "passphrase", "secret", "token")
STRONG_STEMS = ("парол", "токен", "секрет")
# Russian words without a stem of their own: кред- begins кредит too, and пороль is a common misspelling.
STRONG_RUSSIAN = frozenset({"креды", "кредов", "кредам", "кредами", "кредах", "кредс", "пороль", "пороля",
                            "поролем", "поролю"})
# Words common in prose: they name a secret inside an identifier (api_key, authToken), quoted, after
# a sigil or a dash ($key, -key), before a separator (key: V, key=V, key 'V', 'key', 'V'), as a table
# cell before a value, before a call in a dotted name (req.SetBasicAuth(, hmac.New(), and in a
# sentence when a value follows within three words (pwd V, the key is V, ключ от апи: V).
WEAK_WORDS = frozenset({
    "key", "keys", "apikeys", "pass", "pwd", "pw", "passcode", "auth", "authtok", "authorization", "cookie",
    "cookies", "passwords", "secrets", "tokens", "otp", "hmac", "bearer", "oauth", "dsn", "mnemonic", "sessionid",
    "capability", "psk", "пасс"})
WEAK_STEMS = ("ключ",)
# A word of letters in any case glued to one of these endings names a secret as well (dbpass, rootpw,
# bindpw, DBPWD, masterauth, requirepass, privkey, wpapsk, authtok, JSESSIONID, PHPSESSID), unless it
# is an everyday word that happens to end so.
JOINED_ENDINGS = ("pass", "passwd", "pwd", "pw", "psk", "key", "auth", "authtok", "secret", "token",
                  "sessionid", "sessid")
COMMON_JOINED = frozenset({
    "bypass", "compass", "surpass", "trespass", "encompass", "overpass", "underpass", "multipass", "impass",
    "monkey", "turkey", "hockey", "donkey", "jockey", "whiskey", "lackey", "turnkey", "hotkey", "passkey",
    "latchkey", "malarkey"})
# A joined name in capitals that ends in one of these (MASTERKEY, SSHPASS, DBPASS), for environment names.
CAPS_ENDINGS = ("key", "keys", "pass")
# Words that name a secret only as the last word of a name right before "=": pin=, salt=, sig=. Of
# them pin and salt also before ":", a quoted value or a pair's value, and in a sentence before a
# value (the PIN is 4821, NONCE_SALT', 'V').
EQUALS_WORDS = frozenset({"private", "seed", "salt", "pin", "session", "sig", "signature"})
COLON_WORDS = frozenset({"pin", "salt"})
# Calls whose arguments hold a password: conn.login("deploy", "V").
CALL_WORDS = frozenset({"login", "logon", "signin", "authenticate"})
# The last component of a dotted name that builds a keyed object out of the component before it: hmac.New(.
FACTORY_NAMES = frozenset({"new", "create"})
# Two words that name a secret together, side by side in a name or a blank apart.
STRONG_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key"), ("signing", "key"),
                          ("encryption", "key"), ("master", "key"), ("seed", "phrase"), ("recovery", "phrase"),
                          ("identified", "by")})
WEAK_PAIRS = frozenset({("session", "id"), ("connection", "string")})
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
# Of those, the ones that name what carries a secret as often as a property of it: they make a name a
# property only when its value is a path, a number or one plain word (VAULT_SECRET_ID=<uuid>,
# AUTH_HEADER="Basic ...", PASSWORD_COMMAND="echo ..." are masked; api_key_header = "X-API-Key" stays).
VALUE_PROPERTY_WORDS = frozenset({"id", "ids", "header", "headers", "command", "cmd", "param", "params"})
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
# "be" introduces none after a modal or an auxiliary verb: "the seed phrase must be written down".
MODALS = frozenset({"must", "should", "can", "could", "will", "would", "may", "might", "shall", "to", "cannot",
                    "has", "have", "had", "not", "never", "also", "always", "need", "needs"})
# What ties a value written before its label to it: "use V as the password", "typed V into the
# password field".
VALUE_LINKS = frozenset({"as", "into", "for", "in", "как", "в"})
VOWELS = frozenset("aeiouyаеёиоуыэюя")

# --- Names of environment variables (#701 review) ---------------------------------------------------
# A variable's name alone decides whether its value is a known secret: plural and joined names count
# (API_KEYS, SERVICE_TOKENS, MASTERKEY, SSHPASS, ADMIN_PW, GITHUB_OAUTH, PRIVATE_PEM).
ENVIRONMENT_WORDS = STRONG_WORDS | WEAK_WORDS | frozenset({"secrets", "jwt", "pem", "salt", "signature"})
# Alone, these words are too common to name a secret: "PASS: test_x" in a test report, the shell's PWD.
LONE_WORDS = frozenset({"pass", "pwd", "pw"})
# The last words that make a variable's name a secret's property: the line rule's, without the ones
# it added for names in code (#701 review: AUTH_HEADER, TOKEN_STORE, SECRET_ENV and API_KEY_PARAM hold
# secrets as often as not, and their values were no longer looked for).
ENVIRONMENT_PROPERTY_WORDS = frozenset({
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

# --- How the line rule reads a line -----------------------------------------------------------------
_LETTERS = "A-Za-zЀ-ӿ"
# A name: letters, digits and _, joined by - or . between them (DB_PASSWORD, api-key, spring.datasource).
_IDENT = re.compile(rf"[0-9{_LETTERS}_]+(?:[-.][0-9{_LETTERS}_]+)*")
_LETTER_RUN = re.compile(rf"[{_LETTERS}]+")
_CAMEL = re.compile(r"(?<=[a-zа-џ])(?=[A-ZЀ-Я])|(?<=[A-ZЀ-Я])(?=[A-ZЀ-Я][a-zа-џ])")
# A line without one of these holds no word of the rule and is skipped at once.
_HINT = re.compile(r"(?i)pass|pw|key|token|secret|auth|cred|cookie|dsn|otp|hmac|bearer|mnemonic|session|sessid|"
                   r"connection|seed|recovery|private|salt|pin|sig|capab|log[io]n|signin|api|access|encryption|"
                   r"master|psk|pswd|identified|assword|парол|порол|токен|ключ|секрет|кред|пасс")
_PLAIN_KEBAB = re.compile(rf"[a-zа-џ]+(?:-[a-zа-џ]+)+")
# What a label line may end with and still hold no value: quotes, separators, brackets, a YAML
# block's indicator and sentence punctuation (password: |, "password": {, name="password").
_NO_VALUE = " \t\"'`:=;,.!?{[()]}|>-+*\\"
# After a separator, what still is no value: a block's indicator, an opening bracket, an opening quote
# alone; any other rest is the value, punctuation alone included (password: !?*-+).
_AFTER_SEPARATOR = re.compile(r"[\"'`)\]*\\]{0,8}[ \t]*(?::=|=>|->|\?=|\+=|==?|:)[ \t]*(?P<rest>.*)", re.S)
_OPENING_ONLY = re.compile(r"(?:[|>][-+]?[0-9]?|[{\[(]|\"\"\"|'''|[\"'`]|\\)?[ \t,;]*")
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
    # Options with no user option beside them, which the generic rule below cannot see.
    (r"\bssh-keygen\b", r"(?<![\w-])-[NP]\b"),
    (r"\bun?rar\b", r"(?<![\w-])-p"),
    (r"::add-mask::", None),
))
_CLIENT_HINT = re.compile(r"mysql|mariadb|docker|sqlcmd|redis-cli|curl|sshpass|http|ldap|smbclient|7z|zip|openssl|"
                          r"mongo|ssh-keygen|rar|::add-mask::")
# Any other client: a password option after a user option on the same line (podman login -u U -p V,
# cqlsh -u U -p V, ipmitool -U U -P V, xfreerdp /u:U /p:V), and the value a user option carries after
# its user (lftp -u U,V; etcdctl --user U:V). A uid:gid pair is no password.
USER_OPTION = re.compile(r"(?<![\w/-])(?:-u|-U|--user(?:name)?|-user|/u(?:ser)?:)(?:[ \t]*=[ \t]*|[ \t]+|(?<=:))"
                         r"(?P<user>[^\s\"'`;|&<>]{1,128})")
PASSWORD_OPTION = re.compile(r"(?<![\w/-])(?:(?:-p|-P|-w|-pw|--pass)(?=[ \t=\"']|$)|/p(?:ass(?:word)?)?:)")
_ID_PAIR = re.compile(r"[0-9]+[:,][0-9]+")
STRONG, WEAK, EQUALS, CALL = "strong", "weak", "equals", "call"

# How far below a label line its value is looked for, and how far a quoted value may run (#701 review:
# a misread quote masked to the end of the text).
QUOTE_LINES = 12
BELOW_LINES = 8
BELOW_BLANKS = 2
FENCE_LINES = 40
HEREDOC_LINES = 50
TABLE_ROWS = 1000
CONTINUATION_LINES = 50
PAIR_LINES = 2
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
_HEADING = re.compile(r"[ \t]{0,3}#{1,6}[ \t]")
# A shell heredoc's opener: a terminator in capitals or quoted (EOF, 'EOF'), not C++'s << or <<<.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(?:(['\"])([A-Za-z_][A-Za-z0-9_]{0,31})\1|()([A-Z_][A-Z0-9_]{0,31})\b)")
_TABLE_SEPARATOR = re.compile(r"[ \t]*\|?[ \t]*:?-{2,}:?[ \t]*(?:\|[ \t]*:?-{2,}:?[ \t]*)*\|?[ \t]*")
# A YAML or INI key with nothing after it: a sibling key below it is no value of it.
_KEY_LINE = re.compile(r"[ \t]*(?:-[ \t]+)?[\"']?[\w.\-]+[\"']?[ \t]*[:=]")
_IDENTIFIER_KEY = re.compile(r"[ \t]*[\"']?[a-z_][a-z0-9_.\-]*[\"']?[ \t]*[:=][ \t]*")
# A field that names what a nearby field holds, in JSON, YAML or XML: "Name": "/prod/db/password".
_NAME_FIELD = re.compile(
    r"[ \t]*(?:-[ \t]+)?[\"']?(?i:name|key|parameter(?:name|key)?|variable|var|field|setting|secretname|secretid)"
    r"[\"']?[ \t]*[:=][ \t]*[\"']?(?P<value>[^\"',}\]\s][^\"',}\]]{0,255}?)[\"']?[ \t]*,?[ \t]*"
    r"|[ \t]*<(?i:name|key)>(?P<xml>[^<]{1,256})</(?i:name|key)>[ \t]*")
_VALUE_FIELD = re.compile(
    r"[ \t]*(?:-[ \t]+)?[\"']?(?i:value|val|parametervalue|default|data|content|secretvalue|secretstring)"
    r"[\"']?[ \t]*[:=][ \t]*(?P<value>\S.{0,1024}?)[ \t]*"
    r"|[ \t]*<(?i:value)>(?P<xml>.{0,1024}?)</(?i:value)>[ \t]*")
# A value piped into a command that reads a password on its standard input.
PIPED_VALUE = re.compile(r"(?<![\w-])(?:echo|printf)(?:[ \t]+-[neE]{1,3})*[ \t]+(?P<value>[^|;&\r\n]{1,512}?)[ \t]*"
                         r"\|(?P<rest>[^|;&\r\n]{0,512})")
STDIN_PASSWORD = re.compile(r"--password-stdin|--passwd-stdin|-password-stdin|\bchpasswd\b|\bpasswd\b.*--stdin|"
                            r"\bsudo\b(?:[ \t]+-\w+)*[ \t]+-S\b|\bkinit\b|\bsshpass\b")
# Tokens a value can follow a prose-common word with: a dash, and the closing marks of a table cell.
_DASHES = frozenset({"-", "--", "—", "–", "=", "=>", "->"})
_TOKEN_TRIM = "\"'`([{<>)]}.,;:!?*"
_VALUE_SYMBOLS = frozenset("!@#$%^&*+=~|\\/<>{}[]?")
_TOKEN_CHARACTERS = re.compile(r"[A-Za-z0-9_-]+")
_LINK_HINT = re.compile(r"(?i)(?<!\w)(?:as|into|for|in|как|в)(?!\w)")
_IMPOSSIBLE_PAIRS = frozenset(
    "bx cj cv cx dx fq fx gq gx hx jc jf jg jq js jv jw jx jz kq kx mx px pz qb qc qd qf qg qh qj qk ql qm qn qp "
    "qs qt qv qw qx qy qz sx vb vf vh vj vm vp vq vt vw vx wx xj xx zj zq zx".split())
_CELL_TAGS = re.compile(r"[ \t]*</t[dh]>[ \t]*<t[dh][^>]{0,64}>")

# --- Formats and shapes -----------------------------------------------------------------------------
# Formats the schedule records do not know, masked here on top of theirs, keeping the prefix.
EXTRA_SECRET_PATTERNS = (
    re.compile(r"\b(sk-(?:proj-|svcacct-|admin-)?)[A-Za-z0-9_-]{16,}"),
    re.compile(r"\b(xox[abposr]-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(xapp-)[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(AIza)[0-9A-Za-z_-]{30,}"),
    # The scanner's run-start lookbehind: without it every "eyJ" inside a run restarted the match
    # (#701 review: 72 s for 300 kB of "-eyJ").
    re.compile(r"(?<![A-Za-z0-9_-])(eyJ)[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
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
# A token in a URL's user position: https://<token>@github.com. A run of 20 characters or more counts
# in any case (#701 review: 40 hex digits were taken for a user's name).
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
    alone do not (``PASS:`` in a test report, the shell's PWD), a last ``ENVIRONMENT_PROPERTY_WORDS``
    word names a property (SSH_KEY_NAME, AUTH_MODE), and a quantity before a plural tokens a count
    (MAX_TOKENS; INPUT_TOKEN and CACHE_TOKEN are tokens, #701 review)."""
    words = name_words(name)
    if not words or len(words) == 1 and words[0] in LONE_WORDS:
        return False
    if words[-1] in CONNECTION_ENDINGS and any(word in CONNECTION_WORDS for word in words[:-1]):
        return True
    if words[-1] in ENVIRONMENT_PROPERTY_WORDS:
        return False
    if len(words) > 1 and words[-1] == "tokens" and words[-2] in QUANTITY_WORDS:
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


def _joined_weak(lower: str) -> bool:
    """A word of letters glued to an ending that names a secret (dbpass, rootpw, DBPWD, masterauth,
    requirepass, privkey, wpapsk, jsessionid), in any case; not an everyday word (bypass, monkey)."""
    if lower in COMMON_JOINED or not lower.isascii():
        return False
    return any(lower.endswith(ending) and len(lower) >= len(ending) + (1 if len(ending) >= 5 else 2)
               for ending in JOINED_ENDINGS)


def _word_class(word: str) -> str | None:
    lower = word.lower()
    if (lower in STRONG_WORDS or lower in STRONG_RUSSIAN or lower.startswith(STRONG_STEMS)
            or len(lower) > 5 and lower.endswith(STRONG_ENDINGS)):
        return STRONG
    # A glued ending counts in a word of one case or a capitalised one (dbpass, DBPWD, Requirepass);
    # camelCase is split at its case changes instead (HTTPBasicAuth: Auth).
    one_case = word.islower() or word.isupper() or word[:1].isupper() and word[1:].islower()
    if lower in WEAK_WORDS or lower.startswith(WEAK_STEMS) or one_case and _joined_weak(lower):
        return WEAK
    if lower in EQUALS_WORDS:
        return EQUALS
    if lower in CALL_WORDS:
        return CALL
    return None


def _words(name: str, offset: int) -> list[tuple[int, int, str]]:
    """``(start, end, word)`` of a name's words, at ``offset`` in its line: letter runs, split at
    case changes unless the whole run is a word of the rule (OAuth, APIKey stay whole). A piece of
    two letters split off by a case change is no word (``wPw7X`` in a random id is not ``pw``),
    except a last ``Pw`` or ``PW`` after a word (adminPw, olcRootPW)."""
    out = []
    for run in _LETTER_RUN.finditer(name):
        text, start = run.group(), run.start()
        if _word_class(text) is not None or text.lower() in {"oauth"}:
            out.append((offset + start, offset + run.end(), text))
            continue
        cuts = [0] + [cut.start() for cut in _CAMEL.finditer(text)] + [len(text)]
        pieces = list(zip(cuts, cuts[1:]))
        for number, (a, b) in enumerate(pieces):
            last_pw = (number == len(pieces) - 1 and number > 0 and text[a:b].lower() == "pw"
                       and pieces[number - 1][1] - pieces[number - 1][0] >= 3)
            if b - a > 2 or b - a == len(text) or last_pw:
                out.append((offset + start + a, offset + start + b, text[a:b]))
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
    word and the next name's first word a blank away may form a pair ("API key", "IDENTIFIED BY")."""
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
    ``quote`` for an opening quote a blank away (``key 'V'``), ``pair`` for a quoted name's comma and
    a quoted value after it (``'NONCE_SALT', 'V'``), ``call`` for ``(``, else None."""
    i, length = index, len(line)
    while i < length and line[i] in "\"'`)]*\\" and i - index < 8:
        i += 1
    closed = i
    while i < length and line[i] in " \t ":
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
    if rest[:1] == "," and closed > index and line[index] in "\"'":
        j = i + 1
        while j < length and line[j] in " \t" and j - i < 8:
            j += 1
        if line[j:j + 1] in ("\"", "'"):
            return "pair"
    return None


def _field(line: str, start: int, end: int, delimiters=None) -> str | None:
    """The delimiter of a table's header that the name is a field of (``user,password``,
    ``| user | password |``), else None. ``delimiters``, those the line holds, saves a search of the
    line per name; the blanks around a pipe are read a bounded way, never by slicing the line."""
    delimiters = {mark for mark in ",;\t|" if mark in line} if delimiters is None else delimiters
    before, after = line[start - 1:start] if start else "", line[end:end + 1]
    for delimiter in (",", ";", "\t"):
        if before in (delimiter, "") and after in (delimiter, "") and delimiter in delimiters:
            return delimiter
    if "|" in delimiters:
        left, right = start - 1, end
        while left >= 0 and line[left] == " " and start - left <= 64:
            left -= 1
        while right < len(line) and line[right] == " " and right - end <= 64:
            right += 1
        if left >= 0 and line[left] == "|" and right < len(line) and line[right] == "|":
            return "|"
    return None


def _cell_value(line: str, start: int, end: int, delimiters) -> str | None:
    """The next cell's text when the word at ``start:end`` is a table cell of its own (``| pass | V |``,
    ``<td>pass</td><td>V</td>``), else None; read a bounded way."""
    tags = _CELL_TAGS.match(line, end)
    if tags is not None:
        close = line.find("<", tags.end(), tags.end() + 512)
        return line[tags.end():close if close >= 0 else tags.end() + 512]
    if "|" not in delimiters:
        return None
    right = end
    while right < len(line) and line[right] in " \t*`" and right - end <= 64:
        right += 1
    if right >= len(line) or line[right] != "|":
        return None
    left = start - 1
    while left >= 0 and line[left] in " \t*`" and start - left <= 64:
        left -= 1
    if left >= 0 and line[left] != "|":
        return None
    close = line.find("|", right + 1, right + 513)
    return line[right + 1:close if close >= 0 else right + 513]


def _pronounceable(word: str) -> bool:
    """Whether a run of lowercase Latin letters reads like a word: no five consonants in a row (y
    counted as a vowel) and no pair of letters English never puts together (qx, zj). Measured on
    this machine's word list and on random runs: 0.12% of 181,374 words of six letters or more fail,
    and 76% of random runs of ten letters do."""
    run = 0
    for index, character in enumerate(word):
        run = 0 if character in "aeiouy" else run + 1
        if run >= 5 or word[index:index + 2] in _IMPOSSIBLE_PAIRS:
            return False
    return True


def _value_like(token: str) -> bool:
    """Whether a token is shaped like a value rather than a word: a digit, a symbol inside it, a
    random-looking run, or lowercase letters no word is made of."""
    core = token.strip(_TOKEN_TRIM)
    if len(core) < 4:
        return False
    if any(character.isdigit() for character in core) or any(character in _VALUE_SYMBOLS for character in core):
        return True
    if len(core) >= 8 and _TOKEN_CHARACTERS.fullmatch(core) and random_looking(core):
        return True
    return core.isascii() and core.isalpha() and core.islower() and len(core) >= 6 and not _pronounceable(core)


def _prose_token(token: str, introducers: frozenset) -> bool:
    """A word of ordinary prose: letters with an apostrophe or one hyphen inside, sentence
    punctuation around, lower case, capitalised or a short acronym, a vowel in a word of four letters
    or more, a lowercase word of six letters or more that reads like one, and no verb that
    introduces a value. A chain of hyphenated words is no word of prose (#701 review: a passphrase of
    words joined by hyphens)."""
    if not _PLAIN_TOKEN.fullmatch(token):
        return False
    word = token.strip("(.,;!?)")
    letters = _APOSTROPHES.sub("", word)
    if not letters or word.count("-") > 1 or not (letters.islower() or letters[0].isupper() and letters[1:].islower()
                                                   or letters.isupper() and len(letters) <= 5):
        return False
    lower = letters.lower()
    if lower.isascii() and letters.islower() and len(lower) >= 6 and not _pronounceable(lower):
        return False
    return lower not in introducers and (len(lower) < 4 or any(character in VOWELS for character in lower))


class _Prose:
    """Whether what follows a word to the end of its line is a sentence of ordinary words, at least
    two of them, the first in lower case, and whether a value follows a prose-common word. The
    line's tokens are read once and judged once, so every word's question is a bisection (#701
    review: reading the rest of the line per word took 5 to 18 s for one 1 MB line)."""

    def __init__(self, line: str):
        self.line, self.ready = line, False

    def _read(self) -> None:
        # An earlier round's placeholder is no word, so a later round decides as the first one did;
        # " masked " has the placeholder's length, so every offset stays.
        words = self.line.replace(MASK, " masked ") if MASK in self.line else self.line
        tokens = [(match.start(), match.end()) for match in re.finditer(r"\S+", words)]
        self.words, self.starts, self.ends = words, [start for start, _ in tokens], [end for _, end in tokens]
        # Where the first verb that sets a value ends: after it, "to" introduces one ("set it to V").
        self.setting = min((run.end() for run in _LETTER_RUN.finditer(words) if run.group().lower() in SETTING_VERBS),
                           default=len(words) + 1)
        self.texts, self.ready, self.suffix = [words[start:end] for start, end in tokens], True, None

    def _judge(self) -> None:
        """Which tokens to the end of the line are all ordinary prose, from each one on, with "to"
        read as a word and as a verb that introduces a value; judged once, when a word first asks."""
        texts = self.texts
        plain = [self._plain(texts, index, INTRODUCERS) for index in range(len(texts))]
        plain_to = [judged and texts[index].strip("(.,;!?)").lower() != "to" for index, judged in enumerate(plain)]
        self.suffix, self.suffix_to = [True] * (len(texts) + 1), [True] * (len(texts) + 1)
        for index in range(len(texts) - 1, -1, -1):
            self.suffix[index] = plain[index] and self.suffix[index + 1]
            self.suffix_to[index] = plain_to[index] and self.suffix_to[index + 1]

    @staticmethod
    def _plain(texts, index: int, introducers: frozenset) -> bool:
        token = texts[index]
        if token.strip("(.,;!?)").lower() in ("be", "been", "being") and index and \
                texts[index - 1].strip("(.,;!?)").lower() in MODALS:
            return _prose_token(token, introducers - {"be", "been", "being"})
        return _prose_token(token, introducers)

    def after(self, index: int) -> bool:
        if not self.ready:
            self._read()
        if self.suffix is None:
            self._judge()
        setting = self.setting <= index
        introducers = INTRODUCERS | {"to"} if setting else INTRODUCERS
        first = bisect_left(self.starts, index)
        count = len(self.starts) - first
        holder = first - 1  # a token the word ends inside: its rest is the first token after the word
        if holder >= 0 and self.ends[holder] > index:
            count += 1
            leading = self.words[index:self.ends[holder]]
            if not _prose_token(leading, introducers):
                return False
        elif first < len(self.texts):
            leading = self.texts[first]
        else:
            return False
        # The word right after it is where a value would stand: a capitalised word or an acronym
        # there may be one ("wifi password Purple Monkey"), so prose begins in lower case.
        letters = leading.strip("(.,;!?)")
        return count >= 2 and letters[:1].islower() and (self.suffix_to if setting else self.suffix)[first]

    def value_follows(self, index: int) -> bool:
        """Whether a value stands within the next three tokens after a prose-common word, with no
        sentence ending before it: ``pwd V``, ``the key is V``, ``ключ от апи: V``, ``* pass — V``."""
        if not self.ready:
            self._read()
        first = bisect_left(self.starts, index)
        following = self.texts[first:first + 3]
        if first and self.ends[first - 1] > index:  # the word ends inside a token: its rest comes first
            following = [self.words[index:self.ends[first - 1]]] + following[:2]
        if following and following[0].strip(_TOKEN_TRIM).lower() in PROPERTY_WORDS:
            return False  # a property's value follows: "key digest <hex>", "the key file id_ed25519"
        for token in following:
            if _value_like(token):
                return True
            if token.rstrip("\"')*`").endswith((".", "!", "?")) and token not in _DASHES:
                return False
        return False

    def value_before(self, link: int) -> int | None:
        """The token index of a value written right before ``link`` (as, into, for) when a word naming
        a secret follows within four tokens: "use V as the password", "typed V into the password
        field"; else None."""
        if not self.ready:
            self._read()
        if link == 0 or not _value_like(self.texts[link - 1]):
            return None
        for token in self.texts[link + 1:link + 5]:
            word = token.strip(_TOKEN_TRIM)
            if word and (_word_class(word) == STRONG or word.lower() in ("pass", "pwd", "pw", "passcode", "pin")):
                return link - 1
        return None


def _plain_value(line: str, index: int) -> bool:
    """Whether the value after a name at ``index`` is a path, a number or one plain word (letters,
    joined by - or _ at most), which a property name holds (TOKEN_ID = 12, api_key_header =
    "X-API-Key"); read up to 256 characters."""
    rest = line[index:index + 256].lstrip("\"'`)]*\\ \t")
    rest = rest[2:] if rest.startswith((":=", "=>", "->", "?=", "+=", "==")) else rest[1:] if rest[:1] in "=:" else rest
    value = rest.strip().strip("\"'`,;")
    if not value:
        return True
    return bool(re.fullmatch(r"[A-Za-z]+(?:[-_][A-Za-z]+)*|[0-9][0-9_.,]*|(?:~|\.{1,2})?/[\w./-]*|[\w.-]+(?:/[\w.-]+)+/?",
                             value))


def _call_label(name: str, label_start: int, label_end: int) -> bool:
    """Whether a word in a dotted name before ``(`` names what the call takes: the last word of the
    last component (req.SetBasicAuth(, crypto.createHmac(, r.auth(), or the component before a
    factory (hmac.New(, hmac.new()."""
    last_dot = name.rfind(".")
    if label_end == len(name) and label_start > last_dot:
        return True
    head = name[:last_dot]
    return name[last_dot + 1:].lower() in FACTORY_NAMES and label_end == last_dot and label_start > head.rfind(".")


def _trigger(line: str, prose=None) -> tuple[int, str | None, int] | None:
    """Where the line rule masks ``line`` from, what kind of label it is (``bare``, ``table:<d>``,
    ``quoted``, ``label``, ``end`` for a word that ends the line without a separator, ``option``),
    and where the label ends, for the first label or password option that names a secret; None when
    none does."""
    found = _word_trigger(line, prose) if _HINT.search(line) and not _ARMOUR_LINE.fullmatch(line) else None
    option = _option_trigger(line)
    if option is not None and (found is None or option < found[0]):
        found = (option, "option", option)
    return found


def _option_trigger(line: str) -> int | None:
    """Where a client's password option leaves its value on ``line``: a known client's option, or a
    password option after a user option on the same line, or the value after a user option's user."""
    found = None
    if _CLIENT_HINT.search(line):
        for client, option in CLIENT_OPTIONS:
            match = client.search(line)
            if match is None:
                continue
            if option is not None:
                match = option.search(line, match.end())
                if match is None:
                    continue
            if found is None or match.end() < found:
                found = match.end()
    user = USER_OPTION.search(line)
    if user is not None:
        separator = next((user.start("user") + offset for offset, character in enumerate(user.group("user"))
                          if character in ":,"), None)
        if separator is not None and not _ID_PAIR.fullmatch(user.group("user")):
            found = separator + 1 if found is None else min(found, separator + 1)
        password = PASSWORD_OPTION.search(line, user.end())
        if password is not None:
            found = password.end() if found is None else min(found, password.end())
    return found


def _word_trigger(line: str, prose=None) -> tuple[int, str | None, int] | None:
    prose, exempt, documents = prose or _Prose(line), None, "docs.google" in line
    # Read once per line, not per word (#701 review: slicing the line per word was quadratic).
    stripped_line = line.strip().rstrip(":=").strip()
    delimiters = {mark for mark in ",;\t|" if mark in line}
    # The last character that can be a value: a label after it has nothing to mask on this line.
    last_value = len(line) - 1
    while last_value >= 0 and line[last_value] in _NO_VALUE:
        last_value -= 1
    for name in _IDENT.finditer(line.replace(MASK, " " * len(MASK)) if MASK in line else line):
        text = name.group()
        if not _HINT.search(text):
            continue
        start, end = name.span()
        _words_, words, _labels_, random_run, _pairable = _name_info(text)
        # A random-looking run is a value, never a label: the shape rules judge it, and ids that
        # look random and are no secret (toolu_01..., session_01...) stay.
        if random_run:
            continue
        if documents or "_01" in text or "_kw" in text or "_lA" in text:
            if exempt is None:
                exempt = _not_secret_spans(line)
            if _inside((start, end), exempt):
                continue
        for label_start, label_end, kind, label in _labels(line, name):
            label_name_end = max(end, label_end)  # a pair a blank apart ends in the next name
            if words and words[-1] in PROPERTY_WORDS and label[-1] != words[-1]:
                if words[-1] not in VALUE_PROPERTY_WORDS or _plain_value(line, label_name_end):
                    continue
            index = words.index(label[0]) if label[0] in words else -1
            if label[-1] in ("token", "tokens") and index > 0 and words[index - 1] in QUANTITY_WORDS:
                continue
            before = line[start - 1:start] if start else ""
            # A name in a Markdown code span (`key`) is prose about it, not a label.
            quoted = before in ("\"", "'", "<") or line[max(0, start - 2):start] in ("\\\"", "\\'")
            sigil = before in ("$", "@", "%", "-")
            joined = (line[start:label_name_end] != line[label_start:label_end]
                      and not _PLAIN_KEBAB.fullmatch(text))
            separator = _separator_after(line, label_name_end)
            last = label_end == label_name_end
            if kind == EQUALS:
                hit = last and (separator == "=" or label[0] in COLON_WORDS and (
                    separator in (":", "quote", "pair") or not (joined or quoted or sigil) and separator is None
                    and prose.value_follows(label_name_end)))
            elif kind == CALL:
                hit = last and separator == "call"
            elif kind == WEAK:
                # A dotted name without a separator is a package path or a file (oauth2.client, server.key),
                # and a short word that only case or digits set off is chance in a random id (KeyNoe4w).
                dotted = "." in text and not (quoted or sigil)
                chance = label_end - label_start < 4 and not _set_off(line, label_start, label_end)
                hit = ((joined and not dotted and not chance) or quoted or sigil
                       or separator in ("=", ":", "quote", "pair")
                       or separator == "call" and dotted and _call_label(text, label_start - start, label_end - start))
                if not hit and not (joined or dotted or quoted or sigil) and separator is None:
                    cell = _cell_value(line, label_start, label_name_end, delimiters)
                    if cell is not None:
                        hit = any(_value_like(token) for token in cell.split()[:1])
                    else:
                        hit = prose.value_follows(label_name_end)
            else:
                hit = (joined or quoted or sigil or separator in ("=", ":", "quote", "pair", "call")
                       or not prose.after(label_name_end))
            if not hit and kind != STRONG:
                continue  # only a strong word may head a table's column or stand alone without a hit
            # From the end of the name when the rest of it is words (SECRET_KEY, authToken), else
            # from the end of the word, so a value glued to it goes too (key-<hex>); a long piece glued
            # by - or . with nothing after the name is a value too (token-<value>, password.<value>).
            rest = line[label_end:label_name_end]
            mask_from = label_name_end if _NAME_TAIL.fullmatch(rest) else label_end
            if mask_from == label_name_end and rest[:1] in ("-", ".") and separator is None and (
                    len(rest) > 6 or any(character.isdigit() for character in rest)):
                mask_from = label_end
            if mask_from > last_value:
                # Nothing to mask on this line: a label line leaves its value for the lines below. A
                # word alone on its line or heading a table's column is such a label when it names a
                # secret by itself (Password, username,password), a weaker word only with a separator.
                bare = stripped_line == line[label_start:label_end] or stripped_line == line[start:label_name_end]
                field = _field(line, start, label_name_end, delimiters)
                strong = kind == STRONG or kind == WEAK and separator in ("=", ":")
                if bare and strong:
                    return mask_from, "bare", label_end
                if field is not None and strong:
                    return mask_from, "table:" + field, label_end
                if hit and (joined or quoted or sigil or separator in ("=", ":")):
                    return mask_from, "quoted" if quoted and not separator else "label", label_end
                if hit and kind == STRONG:
                    return mask_from, "end", label_end
                continue
            if hit:
                return mask_from, "label", label_end
    return None


def _set_off(line: str, start: int, end: int) -> bool:
    """Whether a word stands between separators of a name or its ends (db_pw, ADMIN_PW, x-pw)."""
    before, after = line[start - 1:start], line[end:end + 1]
    return not (before.isalnum() or after.isalnum())


def _indent(text: str) -> int:
    return len(text) - len(text.lstrip(" \t"))


def _has_value(tail: str) -> bool:
    """Whether the rest of a line after a label holds a value: anything but blanks, the label's own
    closing marks, a separator and an opening bracket, quote or block indicator; punctuation alone
    after a separator is a value (#701 review: ``password: !?*-+`` was kept), sentence punctuation
    without one is not ("... looks like a password.")."""
    if tail.strip(_NO_VALUE):
        return True
    after = _AFTER_SEPARATOR.match(tail)
    return after is not None and not _OPENING_ONLY.fullmatch(after.group("rest"))


def _opening_quote(line: str, mask_from: int, form: str | None) -> tuple[int, str] | None:
    """``(index, quote)`` of a quote that opens a value after a label's separator, a blank or a
    client's option (``password="V``, ``password "V``, ``-p'V``, ``password = \"\"\"``, ``\\"token\\":
    \\"V``), else None. A quote that closes the label's own string or code span is none: its kind
    stands an odd number of times before the separator (``print("password:", pw)``,
    ``Set `password:` in``), and a single quote opens one only with a value after it on its line."""
    i, length = mask_from, len(line)
    if form == "option" and line[i:i + 1] in ("\"", "'"):  # mysql -p'V: the quote opens the option's value
        return (i, line[i]) if line[i + 1:].strip() else None
    while i < length and line[i] in "\"'`)]*\\" and i - mask_from < 8:
        i += 1
    separator_at = i
    while i < length and line[i] in " \t":
        i += 1
    blanks = i > separator_at
    for mark in (":=", "=>", "->", "?=", "+=", "==", "=", ":"):
        if line.startswith(mark, i):
            separator_at, i = i, i + len(mark)
            while i < length and line[i] in " \t":
                i += 1
            break
    else:
        if not (blanks or form == "option" and i == mask_from):
            return None
    escaped = line.startswith("\\", i)
    at = i + 1 if escaped else i
    if at >= length or line[at] not in "\"'`":
        return None
    quote = line[at:at + 3] if line[at:at + 3] in ('"""', "'''") else line[at]
    token = ("\\" if escaped else "") + quote[0]
    if (line.count(token, 0, separator_at) if escaped else _unescaped(line, quote[0], separator_at)) % 2:
        return None
    if len(quote) == 1 and not line[at + 1:].strip():
        return None
    return (at - 1 if escaped else at), ("\\" if escaped else "") + quote


def _unescaped(line: str, quote: str, end: int) -> int:
    count, index = 0, line.find(quote, 0, end)
    while index >= 0:
        if not (index and line[index - 1] == "\\"):
            count += 1
        index = line.find(quote, index + 1, end)
    return count


def _quote_close(text: str, at: int, quote: str, limit: int) -> int | None:
    """The index of the quote closing a value that opens at ``at`` with ``quote``, searched up to
    ``limit``: escaped quotes close escaped, triple quotes with three, and a backslash escapes the
    next character of a plain quote's value. None when no close comes before ``limit``."""
    if quote.startswith("\\"):
        close = text.find(quote, at + len(quote), limit)
        return None if close < 0 else close
    if len(quote) == 3:
        close = text.find(quote, at + 3, limit)
        return None if close < 0 else close
    index = at + 1
    while index < limit:
        character = text[index]
        if character == "\\":
            index += 2
            continue
        if character == quote:
            return index
        index += 1
    return None


def _lines(text: str) -> list[tuple[int, int]]:
    lines, offset = [], 0
    for piece in text.split("\n"):
        end = offset + len(piece)
        lines.append((offset, end - 1 if piece.endswith("\r") else end))
        offset = end + 1
    return lines


def _continued(text: str, lines, index: int) -> int:
    """The last line of the command or value that line ``index`` continues with trailing backslashes."""
    last = index
    while last + 1 < len(lines) and last - index < CONTINUATION_LINES and \
            text[lines[last][0]:lines[last][1]].rstrip(" \t").endswith("\\"):
        last += 1
    return last


def _ends_with_colon(line: str) -> bool:
    return line.rstrip(" \t*_`\"'»)]").endswith((":", "："))


def _ends_with_label(line: str, label_end: int) -> bool:
    """Whether nothing but closing marks, a colon or a dash follows the label: a sentence or a heading
    that leaves its value for the next line ("Try logging in with the password", "### Admin password")."""
    return not line[label_end:].strip(" \t*_`\"'»)]:：-—")


def _value_shaped(row: str) -> bool:
    """Whether a line below a label line reads as its value rather than as prose: one token, a code
    span, a list item or a pair, or four tokens at most with one shaped like a value."""
    tokens = row.split()
    if len(tokens) == 1 or row.strip().startswith(("`", "- ", "* ", "• ")) or _KEY_LINE.match(row):
        return True
    return len(tokens) <= 4 and any(_value_like(token) for token in tokens)


def _below(text: str, lines, index: int, mode: str) -> tuple[list[tuple[int, int]], int]:
    """The spans of a label line's value below it, and the index of the line after them.

    ``block``: the lines indented under it. ``one``: the next non-blank line (a quoted form field's
    value). ``lines``: the next non-blank line whatever it holds, then those after it that read as a
    value, up to a blank line. ``values``: only lines that read as a value. A fenced block first
    below the label loses its content and keeps its fences; a YAML or INI sibling key below a key
    line is no value of it. Every mode is bounded."""
    start, end = lines[index]
    label = text[start:end]
    spans, below, blanks = [], index + 1, 0
    while below < len(lines) and not text[lines[below][0]:lines[below][1]].strip():
        below += 1
        blanks += 1
    if below >= len(lines) or blanks > BELOW_BLANKS and mode != "block":
        return spans, index + 1
    row_start, row_end = lines[below]
    row = text[row_start:row_end]
    if _indent(row) > _indent(label):  # the lines indented under it: a YAML or Terraform block
        while below < len(lines):
            row_start, row_end = lines[below]
            row = text[row_start:row_end]
            if row.strip():
                if _indent(row) <= _indent(label):
                    break
                spans.append((row_start + _indent(row), row_end))
            below += 1
        return spans, below
    if mode == "block" or _ARMOUR_LINE.fullmatch(row):
        return spans, index + 1  # a key block below a label is the key block rule's
    fence = _FENCE.match(row)
    if fence is not None:
        mark, last = fence.group(1), min(len(lines), below + FENCE_LINES + 1)
        inner = below + 1
        while inner < last and not text[lines[inner][0]:lines[inner][1]].lstrip().startswith(mark):
            if text[lines[inner][0]:lines[inner][1]].strip():
                spans.append((lines[inner][0] + _indent(text[lines[inner][0]:lines[inner][1]]), lines[inner][1]))
            inner += 1
        return spans, min(inner + 1, len(lines))
    if mode in ("lines", "one") and _IDENTIFIER_KEY.fullmatch(label) and _KEY_LINE.match(row) and \
            not row.lstrip().startswith("-") and not _value_like(row.split()[0]):
        return spans, index + 1  # password:⏎username: admin, a sibling key in YAML or INI
    count = 0
    while below < len(lines) and count < (1 if mode in ("one", "values") else BELOW_LINES):
        row_start, row_end = lines[below]
        row = text[row_start:row_end]
        if not row.strip() or (count or mode == "values") and not _value_shaped(row):
            break
        spans.append((row_start + _indent(row), row_end))
        below, count = below + 1, count + 1
    return spans, below


def _table_rows(text: str, lines, index: int) -> int | None:
    """The index after the rows of a table whose header at ``index`` names a secret in one of its
    cells, in any delimited form: a pipe table with or without outer pipes above its separator row,
    or comma-, semicolon- or tab-separated values above a row of as many cells; the header's cells
    are read past blanks, emphasis and quotes. None when the line heads no such table."""
    start, end = lines[index]
    line = text[start:end]
    if index + 1 >= len(lines) or not _HINT.search(line):
        return None
    following = text[lines[index + 1][0]:lines[index + 1][1]]
    if "|" in line and _TABLE_SEPARATOR.fullmatch(following):
        cells = line.strip().strip("|").split("|")
    else:
        delimiter = next((mark for mark in ("\t", ";", ",") if mark in line), None)
        if delimiter is None or following.count(delimiter) != line.count(delimiter) or \
                not re.fullmatch(r"[\w .\"'*`\-" + re.escape(delimiter) + "]+", line):
            return None
        cells = line.split(delimiter)
    if len(cells) < 2 or not any(_cell_names_secret(cell.strip(" \t*_`\"'")) for cell in cells):
        return None
    below = index + 1
    while below < len(lines) and below - index <= TABLE_ROWS:
        row = text[lines[below][0]:lines[below][1]]
        if not row.strip() or ("|" if "|" in line else delimiter) not in row:
            break
        below += 1
    return below


def _cell_names_secret(cell: str) -> bool:
    """Whether a header's cell names a secret: a strong word or pair, or one of the short words a
    column of passwords is headed by (Pass, PWD, PIN, OTP); not Key, which heads key-value tables."""
    for name in _IDENT.finditer(cell):
        _words_, words, labels, random_run, _pairable = _name_info(name.group())
        if random_run or words and words[-1] in PROPERTY_WORDS:
            continue
        for _start, _end, kind, label in labels:
            if kind == STRONG or label[0] in ("pass", "pwd", "pw", "passcode", "pin", "otp", "psk", "пасс"):
                return True
            if kind == WEAK and label[0] not in ("key", "keys") and len(words) > 1:
                return True
    return False


def _names_secret(value: str) -> bool:
    """Whether a field's value names a secret: "/prod/db/password", "db_pass", "ConnectionPassword"."""
    for name in _IDENT.finditer(value):
        _words_, words, labels, random_run, _pairable = _name_info(name.group())
        if random_run or words and words[-1] in PROPERTY_WORDS:
            continue
        if any(kind == STRONG or kind == WEAK and (len(words) > 1 or label[0] in ("pass", "pwd", "pw", "psk"))
               for _start, _end, kind, label in labels):
            return True
    return False


def _pair_spans(text: str, lines, index: int) -> list[tuple[int, int]]:
    """The value of a name-value pair whose name field at ``index`` names a secret: a value field
    within ``PAIR_LINES`` lines above or below it, in JSON, YAML or XML (``"key": "db_pass",`` then
    ``"value": "V"``; ``<name>...Password</name>`` then ``<value>V</value>``)."""
    start, end = lines[index]
    match = _NAME_FIELD.fullmatch(text, start, end)
    if match is None or not _names_secret(match.group("value") or match.group("xml") or ""):
        return []
    spans = []
    for other in range(max(0, index - PAIR_LINES), min(len(lines), index + PAIR_LINES + 1)):
        if other == index:
            continue
        found = _VALUE_FIELD.fullmatch(text, lines[other][0], lines[other][1])
        if found is not None:
            group = "value" if found.group("value") is not None else "xml"
            spans.append((found.start(group), found.end(group)))
    return spans


def _before_label_spans(line: str, offset: int, prose=None) -> list[tuple[int, int]]:
    """Values written before their label on ``line`` (owner's call, v701-4 redaction finding 9's first
    group): a value piped into a command that reads a password on its standard input (``echo V |
    docker login --password-stdin``, ``echo "u:V" | chpasswd``, ``| sudo -S``), and a token shaped
    like a value right before ``as``, ``into`` or ``for`` with a word naming a secret after it ("use V
    as the password")."""
    spans = []
    if "|" in line:
        for match in PIPED_VALUE.finditer(line):
            if STDIN_PASSWORD.search(match.group("rest")):
                spans.append((offset + match.start("value"), offset + match.end("value")))
    if prose is None or not _LINK_HINT.search(line):  # no label after a value without these
        return spans
    if not prose.ready:
        prose._read()
    for link, token in enumerate(prose.texts):
        if token.lower() in VALUE_LINKS:
            value = prose.value_before(link)
            if value is not None:
                spans.append((offset + prose.starts[value], offset + prose.ends[value]))
    return spans


def line_spans(text: str) -> list[tuple[int, int]]:
    """Where the line rule masks ``text`` (owner's decision 2026-11-28): after the first label on a
    line that names a secret, to the end of the line (``_trigger``) and of the lines it continues with
    trailing backslashes; a quoted value that opens there and closes within ``QUOTE_LINES`` lines, to
    the end of that line; the value a label line leaves below it (``_below``): the lines indented
    under it, a fenced block's content, the lines that read as a value after a line ending in a label
    or a colon, a heading or a bare label, the next line after a quoted name; a heredoc's body after
    a line that names a secret; the rows under a table's header that names a secret
    (``_table_rows``); a name-value pair's value near a name that names a secret (``_pair_spans``);
    and a value written before its label (``_before_label_spans``)."""
    spans: list[tuple[int, int]] = []
    lines = _lines(text)
    index, covered = 0, 0
    while index < len(lines):
        start, end = lines[index]
        line = text[start:end]
        if start < covered or not line.strip():
            index += 1
            continue
        hinted = _HINT.search(line) is not None
        if hinted:
            rows = _table_rows(text, lines, index)
            if rows is not None:
                spans.extend((lines[row][0] + _indent(text[lines[row][0]:lines[row][1]]), lines[row][1])
                             for row in range(index + 1, rows) if text[lines[row][0]:lines[row][1]].strip())
                spans.extend(_trigger_spans(text, lines, index, line))
                index = rows
                continue
            spans.extend(_pair_spans(text, lines, index))
        prose = _Prose(line) if hinted else None
        if hinted or "|" in line:
            spans.extend(_before_label_spans(line, start, prose))
        found = _trigger(line, prose) if hinted or _CLIENT_HINT.search(line) or "-" in line or "/" in line else None
        last = _continued(text, lines, index)
        if found is None and last > index:
            # A client's option on a continuation line: mysql -u root \⏎  -p V
            joined_text = text[start:lines[last][1]]
            option = _option_trigger(joined_text)
            if option is not None:
                found_line = next(row for row in range(index, last + 1) if lines[row][1] >= start + option)
                spans.append((start + option, lines[found_line][1]))
                spans.extend((lines[row][0] + _indent(text[lines[row][0]:lines[row][1]]), lines[row][1])
                             for row in range(found_line + 1, last + 1))
                index = last + 1
                continue
        if found is None:
            index += 1
            continue
        mask_from, form, label_end = found
        tail = line[mask_from:]
        opening = _opening_quote(line, mask_from, form)
        # A triple quote at the line's end opens a value on the lines below (password = """).
        valued = _has_value(tail) or opening is not None and len(opening[1].lstrip("\\")) == 3
        if not valued and _continued(text, lines, index) > index:
            # The value goes on after a trailing backslash: curl -u \⏎  user:V \⏎  https://...
            last = _continued(text, lines, index)
            spans.extend((lines[row][0] + _indent(text[lines[row][0]:lines[row][1]]), lines[row][1])
                         for row in range(index + 1, last + 1))
            covered = lines[last][1]
            index = last + 1
            continue
        if valued and tail != MASK:
            stop = end
            if opening is not None:
                at, quote = opening
                limit = lines[min(index + QUOTE_LINES, len(lines) - 1)][1]
                close = _quote_close(text, start + at, quote, limit)
                if close is not None and close > end:  # to the end of the line the value closes on
                    stop = _LINE_END.match(text, close).end()
            spans.append((start + mask_from, stop))
            covered = stop
            closing = next(row for row in range(index, len(lines)) if lines[row][1] >= stop)
            last = _continued(text, lines, closing)
            spans.extend((lines[row][0] + _indent(text[lines[row][0]:lines[row][1]]), lines[row][1])
                         for row in range(closing + 1, last + 1))
            covered = max(covered, lines[last][1])
        heredoc = _HEREDOC.search(line)
        if heredoc is not None:
            terminator, below = heredoc.group(2) or heredoc.group(4), index + 1
            while below < len(lines) and below - index <= HEREDOC_LINES and \
                    text[lines[below][0]:lines[below][1]].strip() != terminator:
                if text[lines[below][0]:lines[below][1]].strip():
                    spans.append((lines[below][0], lines[below][1]))
                below += 1
            index = below
            continue
        mode = None
        if form == "option":
            mode = None
        elif valued:
            mode = "lines" if _ends_with_colon(line) else None
        elif form == "quoted":
            mode = "one"
        elif form in ("bare",) or _ends_with_colon(line):
            mode = "lines"
        elif form == "label":
            mode = "block"
        elif form and form.startswith("table:"):
            mode = "table:" + form.split(":", 1)[1]
        elif (_HEADING.match(line) or _ends_with_label(line, label_end)) and not any(
                mark in line[:label_end] for mark in (":", "=")):
            mode = "values"  # not the value of another key (kind: Secret)
        if mode is None:
            index += 1  # lines a quoted value or a continuation covered are skipped above
            continue
        if mode.startswith("table:"):
            delimiter, below = mode.split(":", 1)[1], index + 1
            while below < len(lines) and below - index <= TABLE_ROWS:
                row = text[lines[below][0]:lines[below][1]]
                if not row.strip() or delimiter not in row:
                    break
                spans.append((lines[below][0] + _indent(row), lines[below][1]))
                below += 1
            index = below
            continue
        found_spans, index = _below(text, lines, index, mode)
        spans.extend(found_spans)
    return [span for span in spans if text[span[0]:span[1]] != MASK]


def _trigger_spans(text: str, lines, index: int, line: str) -> list[tuple[int, int]]:
    """The line rule's span on a table's header line itself: after its label, to the end of the line."""
    found = _trigger(line)
    if found is None or not _has_value(line[found[0]:]) or line[found[0]:] == MASK:
        return []
    return [(lines[index][0] + found[0], lines[index][1])]


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
    """The regions ``NOT_SECRET`` names, in order and apart, as ``_inside`` needs them."""
    return [match.span() for match in NOT_SECRET.finditer(text)]


def _inside(span: tuple[int, int], regions: list[tuple[int, int]]) -> bool:
    """Whether ``span`` lies within one of ``regions`` (sorted and apart): one bisection, not a scan of
    every region per span (#701 review: 49.5 s for 18,519 lines of ids)."""
    index = bisect_right(regions, (span[0], float("inf"))) - 1
    return index >= 0 and regions[index][0] <= span[0] and span[1] <= regions[index][1]


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
            if pattern is URL_USER_TOKEN and not (_mixed(value) or not value.isalnum() or len(value) >= 20):
                continue  # https://deploy@host: a user's name, not a token; 20 characters or more are one
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


class _LineTails:
    """Spans from a match to the end of its line, one per line: a later match on a line already masked
    to its end adds nothing, so a line of many matches is read once (#701 review: a JSON array on one
    line with a password field per record took 107 s at 1 MB)."""

    def __init__(self, text: str):
        self.text, self.until = text, -1

    def add(self, spans: list, start: int) -> None:
        if start < self.until:
            return
        end = self.text.find("\n", start)
        end = len(self.text) if end < 0 else end
        if end > start and self.text[end - 1] == "\r":
            end -= 1
        spans.append((start, end))
        self.until = end


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
        tails = _LineTails(text)  # one per pattern: its matches come in order
        for match in pattern.finditer(text):
            if name == "labelled_credential":
                tails.add(spans, match.start("value"))
            else:
                spans.append(match.span())
    for name, pattern in _sync_patterns():
        tails = _LineTails(text)
        for match in pattern.finditer(text):
            if name == "password":  # it stops after the first character of the value
                tails.add(spans, match.end() - 1)
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

    Token formats keep their prefix and a private key block its END line (the scanner masks the
    BEGIN line, #701 review); then, round after
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
