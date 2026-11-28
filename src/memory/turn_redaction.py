"""Secrets cleaned out of a turn before the turn-log hook writes it (#701, owner's decisions 2026-11-27/28).

The Stop hook ``src.memory.turn_log_hook`` writes every interactive turn by default, so what it
writes must be clean (#701 review): ``redact`` masks what the rules below and the repository's own
detectors find, round after round, and says whether a last scan found nothing more.

- The line rule (owner's decision 2026-11-28, ``line_spans``): a line that names a secret, by a
  word in an identifier or a label (``DB_PASSWORD``, ``"token"``, ``$authToken``, ``-Dapi.key``,
  ``api key:``, ``пароль``) or by a client's password option (``mysql -p``, ``curl -u``), loses
  everything after that word to the end of the line, separators and symbols included. A value
  that a label line leaves for the lines below goes with them, whatever the form (a YAML block, a
  sentence or heading ending in the label, a command's output after a prompt, a lone value after a
  label line that holds none, a fenced block, a heredoc, a continuation line, a name-value pair or
  element pair, a table's header in any delimited or aligned form, a database client's table), and
  so does a value written before its label in the forms the owner chose (#701 reviews, attempts 5
  and 6). A word used as an ordinary word in a sentence keeps its line when the rule can tell
  nothing after it is a value; in doubt the line is masked. No fence line is masked, and a block of
  code under a line naming a secret keeps its lines but its quoted literals.
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

The line rule reads a turn's lines once, and tells the masks earlier steps made from the turn's own
text by their offsets, never by the mask's text (#701 review: an element named secret hid its
value). Every rule works line by line or on bounded windows: a match that masks to its line's end
is read once per line, lookaheads and quoted values are bounded, no pattern restarts inside a run,
a line's tokens are read once for all its words, a name once for all its labels, and a window of
continued lines once (#701 reviews: one pattern took 70 s on a 160,000-character line, ten shapes
of the final verification 5 to over 120 s at 1 MB, and a hyphen-joined name over 150 s). This is
measured, not proven: docs/memory.md gives the times. What still passes is listed there too
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
    "accesskey", "secretkey", "privatekey", "authtoken", "pswd", "assword", "kennwort", "contraseña", "contrasena"})
STRONG_ENDINGS = ("password", "passwd", "passphrase", "secret", "token")
# Russian stems: пароль, токен, секрет in every form, and the transliterated password (пассворд).
STRONG_STEMS = ("парол", "токен", "секрет", "пассворд", "пасворд")
# Russian words without a stem of their own: кред- begins кредит too, and пороль is a common misspelling.
STRONG_RUSSIAN = frozenset({"креды", "кредов", "кредам", "кредами", "кредах", "кредс", "пороль", "пороля",
                            "поролем", "поролю"})
# Plurals of strong words, common in prose ("Passwords are hashed"): alone at the end of a heading, a
# bare label line, a block or a table's column they name a secret as their singular does (#701 review:
# "## Passwords" and a column "Tokens" left every value below them).
STRONG_PLURALS = frozenset({"passwords", "secrets", "tokens", "passphrases", "passwds"})
# After these a value is words by nature, so no sentence of ordinary words exempts the line (#701
# review: "seed phrase amber anchor basil ..." was written whole).
WORD_VALUES = frozenset({("seed", "phrase"), ("recovery", "phrase"), ("mnemonic",), ("passphrase",),
                         ("сид", "фраза"), ("кодовая", "фраза"), ("кодовое", "слово")})
# A word of one case that begins with a strong word goes on with a value's name (tokenstr, secretval,
# PASSWORDVALUE), unless the rest is one of these endings of an everyday word (tokenize, secretary).
EVERYDAY_TAILS = frozenset({
    "s", "es", "ed", "ing", "er", "ers", "ize", "izes", "ized", "izing", "izer", "izers", "ization", "ise", "ises",
    "ised", "ising", "iser", "isers", "isation", "less", "ly", "ary", "aries", "ariat", "ion", "ions", "ive",
    "ively", "iveness", "able", "ish"})
# Words common in prose: they name a secret inside an identifier (api_key, authToken), quoted, after
# a sigil or a dash ($key, -key), before a separator (key: V, key=V, key 'V', 'key', 'V'), as a table
# cell before a value, before a call in a dotted name (req.SetBasicAuth(, hmac.New(), and in a
# sentence when a value follows within three words (pwd V, the key is V, ключ от апи: V).
WEAK_WORDS = frozenset({
    "key", "keys", "apikeys", "pass", "pwd", "pw", "passcode", "auth", "authtok", "authorization", "cookie",
    "cookies", "passwords", "secrets", "tokens", "passphrases", "passwds", "otp", "hmac", "bearer", "oauth", "dsn",
    "mnemonic", "sessionid", "capability", "psk", "psw", "passw", "parol", "пасс", "кей"})
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
EQUALS_WORDS = frozenset({"private", "seed", "salt", "pin", "session", "sig", "signature", "пин", "код"})
COLON_WORDS = frozenset({"pin", "salt", "пин", "код"})
# Calls whose arguments hold a password: conn.login("deploy", "V").
CALL_WORDS = frozenset({"login", "logon", "signin", "authenticate"})
# The last component of a dotted name that builds a keyed object out of the component before it: hmac.New(.
FACTORY_NAMES = frozenset({"new", "create"})
# Two words that name a secret together, side by side in a name or a blank apart.
STRONG_PAIRS = frozenset({("api", "key"), ("access", "key"), ("private", "key"), ("signing", "key"),
                          ("encryption", "key"), ("master", "key"), ("seed", "phrase"), ("recovery", "phrase"),
                          ("identified", "by")})
# Codes that open an account or a door: a code alone is no secret (an exit code, a status code).
WEAK_PAIRS = frozenset({("session", "id"), ("connection", "string"), ("recovery", "code"), ("recovery", "codes"),
                        ("backup", "code"), ("backup", "codes"), ("access", "code"), ("activation", "code"),
                        ("verification", "code"), ("security", "answer"), ("door", "code"), ("gate", "code"),
                        ("alarm", "code"), ("unlock", "code")})
# Russian pairs, by the first word's form and the second word's stem: код доступа, пин-код, сид-фраза,
# кодовое слово, кодовая фраза in their cases (#701 review: the owner writes Russian).
RUSSIAN_PAIRS = ((frozenset({"код", "кода", "коду", "кодом"}), "доступ"), (frozenset({"пин", "pin"}), "код"),
                 (frozenset({"сид", "seed"}), "фраз"),
                 (frozenset({"кодовое", "кодового", "кодовому", "кодовым", "кодовом"}), "слов"),
                 (frozenset({"кодовая", "кодовой", "кодовую"}), "фраз"),
                 (frozenset({"секретное", "секретного", "секретному", "секретным", "секретном"}), "слов"),
                 (frozenset({"секретная", "секретной", "секретную"}), "фраз"))
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
# Words of a sentence that point to its value on the next line: "The new root password is below."
POINTER_WORDS = frozenset({"below", "following", "follows", "underneath", "attached", "ниже", "далее", "следующий",
                           "следующая", "следующее"})
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
                   r"master|psk|pswd|psw|identified|assword|p\w{0,3}word|p/w|kennw|contrase|parol|code|answer|"
                   r"парол|порол|токен|ключ|секрет|кред|пасс|пасвор|кей|код|пин|сид")
_PLAIN_KEBAB = re.compile(rf"[a-zа-џ]+(?:-[a-zа-џ]+)+")
# "p/w" is pw written with a slash; the names a line is read for see it as one word.
_PW_SLASH = re.compile(r"(?<![\w/])[pP]/[wW](?![\w/])")
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
    (r"\b(?:un)?rar\b", r"(?<![\w-])-h?p"),
    (r"::add-mask::", None),
))
_CLIENT_HINT = re.compile(r"mysql|mariadb|docker|sqlcmd|redis-cli|curl|sshpass|http|ldap|smbclient|7z|zip|openssl|"
                          r"mongo|ssh-keygen|rar|::add-mask::")
# The text each client's name holds, so a line is searched only for the clients it may name.
_CLIENT_WORDS = (("mysql", "mariadb"), ("docker",), ("sqlcmd",), ("redis-cli",), ("curl",), ("sshpass",), ("http",),
                 ("ldap",), ("smbclient",), ("7z",), ("zip",), ("openssl",), ("mongo",), ("ssh-keygen",), ("rar",),
                 ("::add-mask::",))
# Any other client: a password option anywhere on a line with a user option (podman login -u U -p V,
# cqlsh -p V -u U, ipmitool -U "U" -P "V", mycli -uroot -pV, xfreerdp /u:U /p:V), and the value a user
# option carries after its user (lftp -u U,V; etcdctl --user U:V; rpcclient -U U%V). A uid:gid pair is
# no password, and neither is a port after -p (docker run -u 1000 -p 8080:80, psql -p 5432 -U app).
USER_OPTION = re.compile(r"(?<![\w/-])(?:(?:-u|-U|--user(?:name)?|-user|/u(?:ser)?:|/ru(?=[ \t]))"
                         r"(?:[ \t]*=[ \t]*|[ \t]+|(?<=:)|(?=[\"']))"
                         r"(?P<user>\"[^\"\r\n]{1,128}\"|'[^'\r\n]{1,128}'|[^\s\"'`;|&<>-][^\s\"'`;|&<>]{0,127})"
                         r"|-u(?P<attached>[A-Za-z_][\w.@-]{0,63})(?=[ \t]|$))")  # a user, not the next option
PASSWORD_OPTION = re.compile(r"(?<![\w/-])(?:(?:-p|-P|-w|-pw|--pass)(?=[ \t=\"']|$)|-[pP](?=[^\s=\"'-])"
                             r"|/p(?:ass(?:word)?)?:|/rp(?=[ \t]))")
_PORT_VALUE = re.compile(r"[ \t=]*\d{1,5}(?::\d{1,5}){0,2}(?:/(?:tcp|udp|sctp))?(?=[\s,;)]|$)")
_ID_PAIR = re.compile(r"[0-9]+[:,%][0-9]+")
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
OBJECT_LINES = 8
_FENCE = re.compile(r"[ \t]{0,3}(`{3,}|~{3,})")
# A fence line at any indentation, with at most an info word: never masked (#701 review: a closing
# fence masked below a label made the rest of the answer code, and a block under a list item lost both).
_FENCE_LINE = re.compile(r"[ \t]*(?:`{3,}|~{3,})[ \t]*[\w+#.-]{0,64}[ \t]*")
_HEADING = re.compile(r"[ \t]{0,3}#{1,6}[ \t]")
# A shell heredoc's opener: a terminator in capitals or quoted (EOF, 'EOF'), not C++'s << or <<<.
_HEREDOC = re.compile(r"(?<!<)<<(?!<)-?[ \t]*(?:(['\"])([A-Za-z_][A-Za-z0-9_]{0,31})\1|()([A-Z_][A-Z0-9_]{0,31})\b)")
# A table's rule line: runs of dashes or of equals signs joined by pipes or plus signs (Markdown, psql,
# a bordered MySQL or reStructuredText grid), or apart by blanks (sqlite's column mode, a simple
# reStructuredText table); colons align a Markdown column.
_TABLE_SEPARATOR = re.compile(r"[ \t]*[|+]?[ \t]*:?(?:-{2,}|={2,}):?[ \t]*"
                              r"(?:[|+][ \t]*:?(?:-{2,}|={2,}):?[ \t]*)*[|+]?[ \t]*")
_SPACED_RULE = re.compile(r"[ \t]*(?:-{2,}|={2,})(?:[ \t]{1,8}(?:-{2,}|={2,}))+[ \t]*")
_BORDER = re.compile(r"[ \t]*\+(?:[-=]{1,}\+)+[ \t]*")
# Cells of a header whose columns are set by blanks: two or more blanks, or a tab, between them.
# Without a rule line below it such a header names its columns in capitals or title case (NAME
# PASSWORD, as kubectl and docker print them), so a listing of keys and values is no table.
_COLUMN_GAP = re.compile(r"[ \t]{2,}|\t")
_COLUMN_NAME = re.compile(r"[A-Z][A-Za-z0-9_.-]{0,31}(?: [A-Z][A-Za-z0-9_.-]{0,31})?")
_COMMENT_COLUMN = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,31}(?: [A-Za-z][A-Za-z0-9_.-]{0,31})?")
# A shell's or a REPL's prompt starting a line: the line is a command, and what follows it up to the
# next prompt is its output (#701 review: `$ printenv DB_PASSWORD`, then the value alone).
_PROMPT = re.compile(
    r"[ \t]{0,8}(?:[$%❯➜λ][ \t]+|PS(?:[ \t][^>\r\n]{0,256})?>[ \t]*|[A-Za-z]:\\[^>\r\n]{0,256}>[ \t]*"
    r"|>>>[ \t]+|In[ \t]?\[\d{1,6}\]:[ \t]+|[\w.-]{1,64}@[\w.-]{1,253}(?::[^\s$#%>]{0,256})?[ \t]?[$#%][ \t]+"
    r"|\[[^\]\r\n]{1,128}\][$#%][ \t]+|(?:ba|z|k|da)?sh-\d[\d.]{0,8}[$#][ \t]+"
    r"|(?:mysql|MariaDB[^>\r\n]{0,64}|sqlite|SQL|cqlsh(?::\w{1,64})?|irb\([^)\r\n]{0,64}\):\d+:\d+|[\w-]{1,64}=|"
    r"\d{1,3}(?:\.\d{1,3}){3}:\d{1,5})[>#*][ \t]+)")
_LIST_MARKER = re.compile(r"[ \t]*(?:[-*•+]|\d{1,3}[.)])[ \t]+")
# A line of code that opens a block: a Python def, class or compound statement ending in a colon, or a
# header ending in an opening brace (function f(token) {, if (!token) {). What is indented under it is
# code, not a label's value (#701 review: the body of a function named for a secret went).
_CODE_HEADER = re.compile(
    r"[ \t]*(?:(?:async[ \t]+)?(?:def|class|if|elif|else|for|while|with|try|except|finally|match|case)\b[^\r\n]*:"
    r"|(?:function|func|fn|class|interface|struct|impl|enum|if|else|for|while|switch|try|catch|do|public|private|"
    r"protected|static|export)\b[^\r\n]*\{"
    r"|[^\r\n]*\)[ \t]*(?:->[^\r\n{]{0,128}|throws[^\r\n{]{0,128}|const)?[ \t]*\{)[ \t]*")
_QUOTED_LITERAL = re.compile(r"\"[^\"\r\n]+\"|'[^'\r\n]+'|`[^`\r\n]+`")
# A YAML or INI key with nothing after it: a sibling key below it is no value of it.
_KEY_LINE = re.compile(r"[ \t]*(?:-[ \t]+)?[\"']?[\w.\-]+[\"']?[ \t]*[:=]")
# A sibling key: YAML's needs a blank after its colon (admin:V is a scalar, #701 review), INI's an equals sign.
_SIBLING_KEY = re.compile(r"[ \t]*[\"']?[\w.\-]+[\"']?[ \t]*(?::(?=[ \t]|$)|=)")
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
# An element alone on its line: after one whose text names a secret, the next one holds its value
# whatever its tag (a property list's <key> and <string>, HTML's <dt> and <dd>, <th> and <td>).
_ELEMENT = re.compile(r"[ \t]*<(?P<tag>[A-Za-z][\w:.-]{0,31})(?:[ \t][^<>\r\n]{0,256})?>(?P<text>[^<\r\n]{1,1024})"
                      r"</(?P=tag)>[ \t]*")
# A value piped into a command that reads a password on its standard input.
PIPED_VALUE = re.compile(r"(?<![\w-])(?:echo|printf)(?:[ \t]+-[neE]{1,3})*[ \t]+(?P<value>[^|;&\r\n]{1,512}?)[ \t]*"
                         r"\|(?P<rest>[^|;&\r\n]{0,512})")
STDIN_PASSWORD = re.compile(r"--password-stdin|--passwd-stdin|-password-stdin|\bchpasswd\b|\bpasswd\b.*--stdin|"
                            r"\bsudo\b(?:[ \t]+-\w+)*[ \t]+-S\b|\bkinit\b|\bsshpass\b|--passphrase-fd\b|"
                            r"-pass(?:in)?[ \t]+stdin\b|\bsecrets?[ \t]+(?:put|set|create)\b|--stdin-name\b")
# A value given to such a command as a here-string: docker login --password-stdin <<< "V".
HERE_STRING = re.compile(r"<<<[ \t]*(?P<value>\"[^\"\r\n]{1,512}\"|'[^'\r\n]{1,512}'|[^\s;|&<>]{1,512})")
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
# A password holding a slash, which ends the form above (postgresql://app:ab/cd@db/app); a port and a
# path before an at sign are no password (https://host:8443/users/@alice).
URL_SLASH_CREDENTIAL = re.compile(r"(?i)//[^\s/:@]{0,128}:(?P<value>(?!\d{1,5}/)[^\s/@]{0,128}/[^\s@]{0,255})@"
                                  r"(?=[\w\[])")
# Oracle's user/password@ after sqlplus, userid=, connect, rman's target and a thin JDBC URL, and
# sqlplus's user/password without a database (sqlplus sys/V as sysdba).
ORACLE_CREDENTIAL = re.compile(r"(?i)(?:\bsqlplus\b[^\r\n/@]{0,64}?[ \t]|\buserid[ \t]*=[ \t]*|\bconn(?:ect)?[ \t]+|"
                               r"\b(?:target|catalog|auxiliary)[ \t]*=?[ \t]*|\bjdbc:oracle:(?:thin|oci8?):)"
                               r"[\"']?[\w$#.]{1,64}/(?P<value>[^\s@/\"']{1,128})@")
SQLPLUS_CREDENTIAL = re.compile(r"(?i)\bsqlplus\b[^\r\n/@]{0,64}?[ \t][\"']?[\w$#.]{1,64}/(?P<value>[^\s@/\"']{1,128})"
                                r"(?=[\"']?(?:[ \t]|$))")
# A proxy's login=user:password (squid's cache_peer).
LOGIN_PAIR = re.compile(r"(?i)(?<![\w.-])login=[^\s:=,;]{1,64}:(?P<value>[^\s,;]{1,256})")
# A URL query parameter that carries a one-time credential: an OAuth code, a session id.
QUERY_CREDENTIAL = re.compile(r"(?i)[?&;](?:code|sid|auth_code|session_?id|sessid|ticket)="
                              r"(?P<value>[^&#\s\"'<>]{4,512})")
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
    alone do not (``PASS:`` in a test report, the shell's PWD), nor does an everyday word that ends
    like one (``COMMON_JOINED``: COMPASS, NO_BYPASS, HOTKEY), a last ``ENVIRONMENT_PROPERTY_WORDS``
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
    # An everyday word that ends like one is no secret's name (COMPASS, NO_BYPASS, HOTKEY, #701 review).
    return any(word not in COMMON_JOINED and (
        word in ENVIRONMENT_WORDS or word.endswith(STRONG_ENDINGS) or word.startswith(STRONG_STEMS + WEAK_STEMS)
        or capitals and len(word) > 3 and word.endswith(CAPS_ENDINGS)) for word in words)


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


def _leading_strong(lower: str) -> bool:
    """A word that begins with a strong word and goes on with letters names a value of it (tokenstr,
    secretval, passwordtext, PASSWORDVALUE, #701 review), unless the rest is a property (tokentype)
    or an ending of an everyday word (tokenize, tokens, secretary, passwordless)."""
    if not lower.startswith(("passw", "passp", "secre", "token")) or not lower.isascii() or not lower.isalpha():
        return False
    for word in STRONG_ENDINGS:
        if lower.startswith(word) and len(lower) >= len(word) + 2:
            rest = lower[len(word):]
            return rest not in PROPERTY_WORDS and rest not in EVERYDAY_TAILS
    return False


def _password_typo(lower: str) -> bool:
    """A misspelling of password one edit away: a letter left out, doubled, swapped with the next one
    or replaced (pasword, passsword, passowrd, passwrod, pssword, passwort; #701 review)."""
    target = "password"
    if not 7 <= len(lower) <= 9 or lower[0] not in "pa" or lower in (target, "passwords") or not lower.isascii():
        return False
    if len(lower) == len(target):
        diff = [index for index in range(len(target)) if lower[index] != target[index]]
        return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                                  and lower[diff[0]] == target[diff[1]] and lower[diff[1]] == target[diff[0]])
    shorter, longer = (lower, target) if len(lower) < len(target) else (target, lower)
    if len(longer) - len(shorter) != 1:
        return False
    return any(longer[:index] + longer[index + 1:] == shorter for index in range(len(longer)))


# Cyrillic letters that look like Latin ones: a word that mixes both scripts is read in Latin, so a
# label typed with one of them still names a secret (pаssword with a Cyrillic а, #701 review).
_LATIN_LOOKALIKES = str.maketrans("аеорсухіјАВЕКМНОРСТУХ", "aeopcyxijABEKMHOPCTYX")


def _word_class(word: str) -> str | None:
    if not word.isascii() and any("a" <= character.lower() <= "z" for character in word):
        latin = word.translate(_LATIN_LOOKALIKES)
        if latin.isascii():
            word = latin
    lower = word.lower()
    # A glued ending or a leading strong word counts in a word of one case or a capitalised one
    # (dbpass, DBPWD, Requirepass, tokenstr); camelCase is split at its case changes instead
    # (HTTPBasicAuth: Auth, tokenStr: token).
    one_case = word.islower() or word.isupper() or word[:1].isupper() and word[1:].islower()
    if (lower in STRONG_WORDS or lower in STRONG_RUSSIAN or lower.startswith(STRONG_STEMS)
            or len(lower) > 5 and lower.endswith(STRONG_ENDINGS) or _password_typo(lower)
            or one_case and _leading_strong(lower)):
        return STRONG
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
    except a last ``Pw`` or ``PW`` after a word of two letters or more (adminPw, olcRootPW, dbPW)."""
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
                       and pieces[number - 1][1] - pieces[number - 1][0] >= 2)
            if b - a > 2 or b - a == len(text) or last_pw:
                out.append((offset + start + a, offset + start + b, text[a:b]))
    return out


PAIR_FIRSTS = frozenset(first for first, _ in STRONG_PAIRS | WEAK_PAIRS) | frozenset(
    word for firsts, _ in RUSSIAN_PAIRS for word in firsts)


# The first words of pairs that hold no hint of their own: a name ending in one is read for its pair.
_PAIR_ONLY_FIRSTS = tuple(sorted(word for word in PAIR_FIRSTS if not _HINT.search(word)))


def _pair_kind(first: str, second: str) -> str | None:
    """STRONG or WEAK when two lower-cased words name a secret together (api key, session id, код
    доступа, пин-код, сид-фраза), else None."""
    if (first, second) in STRONG_PAIRS:
        return STRONG
    if (first, second) in WEAK_PAIRS:
        return WEAK
    if any(first in firsts and second.startswith(stem) for firsts, stem in RUSSIAN_PAIRS):
        return STRONG
    return None


def _word_value(label: tuple[str, ...]) -> bool:
    """Whether a label's value is words by nature (a seed phrase, a mnemonic, a passphrase, a code
    phrase), so no sentence of ordinary words after it exempts the line (#701 review)."""
    return label in WORD_VALUES or len(label) == 2 and label[1].startswith(("фраз", "слов"))


@functools.lru_cache(maxsize=16384)
def _name_info(name: str) -> tuple:
    """``(words, lowered, labels, random, pairable, plain, dotted, tail)`` of a name, offsets within
    it: its words; its labels in order (pairs first where two words form one: api_key, session_id,
    then single words), each ``(start, end, kind, words, index)`` with the index of its first word;
    whether it is a random-looking run; whether its last word may begin a pair with the next name a
    blank away ("API key"); whether it is plain kebab-case; whether it holds a dot; and where the
    rest of it holds only letters, ``_``, ``.`` and ``-``. Read once per name, so a name of many
    labels costs no slice or search of it per label (#701 review: 13.7 s for 80 kB of "key-")."""
    words = _words(name, 0)
    lowered = [word.lower() for _, _, word in words]
    labels, paired = [], set()
    for index, (start, end, word) in enumerate(words):
        if index in paired:
            continue
        if index + 1 < len(words):
            pair = (lowered[index], lowered[index + 1])
            kind = _pair_kind(*pair)
            if kind is not None:
                labels.append((start, words[index + 1][1], kind, pair, index))
                paired.add(index + 1)
                continue
        kind = _word_class(word)
        if kind is not None:
            labels.append((start, end, kind, (lowered[index],), index))
    random_run = bool(TOKEN_RUN.fullmatch(name)) and random_looking(name)
    pairable = bool(lowered) and lowered[-1] in PAIR_FIRSTS and len(words) - 1 not in paired
    tail = len(name)
    while tail > 0 and _NAME_TAIL.fullmatch(name[tail - 1]):
        tail -= 1
    return (tuple(words), tuple(lowered), tuple(labels), random_run, pairable, bool(_PLAIN_KEBAB.fullmatch(name)),
            "." in name, tail)


def _labels(line: str, name: re.Match) -> list[tuple[int, int, str, tuple[str, ...], int]]:
    """``(start, end, class, words, index)`` of the labels in a name, at their place in ``line``; a
    last word and the next name's first word a blank away may form a pair ("API key", "IDENTIFIED
    BY", "код доступа")."""
    words, lowered, labels, _random, pairable = _name_info(name.group())[:5]
    offset = name.start()
    out = [(offset + start, offset + end, kind, label, index) for start, end, kind, label, index in labels]
    if pairable and line[name.end():name.end() + 1] == " ":
        following = _IDENT.match(line, name.end() + 1)
        first = _name_info(following.group())[0][:1] if following is not None else ()
        if first and first[0][0] == 0:
            pair = (lowered[-1], first[0][2].lower())
            kind = _pair_kind(*pair)
            if kind is not None:
                last = offset + words[-1][0]
                out = [label for label in out if label[0] != last]
                out.append((last, following.start() + first[0][1], kind, pair, len(words) - 1))
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
        # ``line`` is read with an earlier round's masks as the word " masked ", which has a mask's
        # length, so a later round decides as the first one did and every offset stays.
        self.line, self.ready = line, False

    def _read(self) -> None:
        words = self.line
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

    def few_after(self, index: int) -> bool:
        """Whether one to four ordinary words and nothing else follow ``index`` to the end of the
        line, one of them pointing below: a sentence that leaves its value for the next line ("The
        new root password is below.", "Password (see below)", #701 review)."""
        if not self.ready:
            self._read()
        first = bisect_left(self.starts, index)
        if len(self.texts) - first > 4:
            return False
        tokens = list(self.texts[first:])
        if first and self.ends[first - 1] > index:  # the word ends inside a token: its rest comes first
            tokens.insert(0, self.words[index:self.ends[first - 1]])
        words = [token.strip("\"'`*") for token in tokens if token.strip("(.,;:!?)\"'`*")]
        return (1 <= len(words) <= 4 and all(_PLAIN_TOKEN.fullmatch(word) and not _value_like(word) for word in words)
                and any(word.strip("(.,;:!?)").lower() in POINTER_WORDS for word in words))

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


def _trigger(line: str, prose=None, view: str | None = None,
             hinted: bool | None = None) -> tuple[int, str | None, int] | None:
    """Where the line rule masks ``line`` from, what kind of label it is (``bare``, ``table:<d>``,
    ``quoted``, ``label``, ``end`` for a word that ends the line without a separator, ``below`` for
    a word in a sentence of a few words that leaves the value for the next line, ``option``), and
    where the label ends, for the first label or password option that names a secret; None when
    none does. ``view`` is the line with an earlier round's masks blanked, which no name is read
    from (#701 review: an element named secret in the turn hid its value as the mask's text did)."""
    names = line if view is None else view
    if hinted is None:
        hinted = _HINT.search(names) is not None  # a caller that read the line already says so
    found = _word_trigger(line, prose, names) if hinted and not _ARMOUR_LINE.fullmatch(line) else None
    option = _option_trigger(line)
    if option is not None and (found is None or option < found[0]):
        found = (option, "option", option)
    return found


def _option_trigger(line: str) -> int | None:
    """Where a client's password option leaves its value on ``line``: a known client's option, a
    password option anywhere on a line with a user option, attached to its value or not and not
    followed by a port, or the value after a user option's user (``U:V``, ``U,V``, ``U%V``)."""
    found = None
    if _CLIENT_HINT.search(line):
        for (client, option), words in zip(CLIENT_OPTIONS, _CLIENT_WORDS):
            if not any(word in line for word in words):
                continue
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
        group = "user" if user.group("user") is not None else "attached"
        name = user.group(group)
        separator = None if name[:1] in "\"'" else next(
            (user.start(group) + offset for offset, character in enumerate(name) if character in ":,%"), None)
        if separator is not None and not _ID_PAIR.fullmatch(name):
            found = separator + 1 if found is None else min(found, separator + 1)
        for password in PASSWORD_OPTION.finditer(line):
            if not _PORT_VALUE.match(line, password.end()):
                found = password.end() if found is None else min(found, password.end())
                break
    return found


def _property_follows(prose: "_Prose", index: int) -> bool:
    """Whether the word after a strong word in a sentence makes it a property, as the last word of a
    name does (the password policy, the token type, as PASSWORD_POLICY and TOKEN_TYPE): a property
    word with no separator after it. ``id``, ``header``, ``command`` and ``param`` carry a secret as
    often as not, so they never do (#701 review: a word of the rule early in a paragraph took it)."""
    if not prose.ready:
        prose._read()
    first = bisect_left(prose.starts, index)
    if first >= len(prose.texts) or first and prose.ends[first - 1] > index:
        return False
    word = prose.texts[first].rstrip(".,;!?)")
    return word.islower() and word in PROPERTY_WORDS and word not in VALUE_PROPERTY_WORDS


def _word_trigger(line: str, prose=None, names: str | None = None) -> tuple[int, str | None, int] | None:
    prose, exempt, documents = prose or _Prose(line), None, "docs.google" in line
    names = line if names is None else names
    if "/" in names and _PW_SLASH.search(names):
        names = _PW_SLASH.sub(" pw", names)  # p/w: as pw, at the same offsets
    # Read once per line, not per word (#701 review: slicing the line per word was quadratic).
    stripped_line = line.strip().rstrip(":=").strip()
    delimiters = {mark for mark in ",;\t|" if mark in line}
    # The last character that can be a value: a label after it has nothing to mask on this line.
    last_value = len(line) - 1
    while last_value >= 0 and line[last_value] in _NO_VALUE:
        last_value -= 1
    below = None  # a strong word a few words of a sentence follow: its value may stand on the next line
    for name in _IDENT.finditer(names):
        text = name.group()
        if not _HINT.search(text) and not (len(text) <= 64 and text.lower().endswith(_PAIR_ONLY_FIRSTS)):
            continue  # no word of the rule, and no first word of a pair a blank away (backup codes)
        start, end = name.span()
        _words_, words, _labels_, random_run, _pairable, plain, dotted_name, tail = _name_info(text)
        # A random-looking run is a value, never a label: the shape rules judge it, and ids that
        # look random and are no secret (toolu_01..., session_01...) stay.
        if random_run:
            continue
        if documents or "_01" in text or "_kw" in text or "_lA" in text:
            if exempt is None:
                exempt = _not_secret_spans(line)
            if _inside((start, end), exempt):
                continue
        for label_start, label_end, kind, label, index in _labels(names, name):
            label_name_end = max(end, label_end)  # a pair a blank apart ends in the next name
            if words and words[-1] in PROPERTY_WORDS and label[-1] != words[-1]:
                if words[-1] not in VALUE_PROPERTY_WORDS or _plain_value(line, label_name_end):
                    continue
            if label[-1] in ("token", "tokens") and index > 0 and words[index - 1] in QUANTITY_WORDS:
                continue
            before = line[start - 1:start] if start else ""
            # A name in a Markdown code span (`key`) is prose about it, not a label.
            quoted = before in ("\"", "'", "<") or line[max(0, start - 2):start] in ("\\\"", "\\'")
            sigil = before in ("$", "@", "%", "-")
            # Offsets, not slices: a name of many labels is read once (#701 review: quadratic time).
            joined = (start, label_name_end) != (label_start, label_end) and not plain
            separator = _separator_after(line, label_name_end)
            last = label_end == label_name_end
            if kind == WEAK and label[0] in STRONG_PLURALS and label_name_end > last_value:
                kind = STRONG  # "## Passwords", "Tokens", "tokens {": a plural alone names its values
            if kind == EQUALS:
                hit = last and (separator == "=" or label[0] in COLON_WORDS and (
                    separator in (":", "quote", "pair") or not (joined or quoted or sigil) and separator is None
                    and prose.value_follows(label_name_end)))
            elif kind == CALL:
                hit = last and separator == "call"
            elif kind == WEAK:
                # A dotted name without a separator is a package path or a file (oauth2.client, server.key),
                # and a short word that only case or digits set off is chance in a random id (KeyNoe4w).
                dotted = dotted_name and not (quoted or sigil)
                chance = label_end - label_start < 4 and not _set_off(line, label_start, label_end)
                hit = ((joined and not dotted and not chance) or quoted or sigil
                       or separator in ("=", ":", "quote", "pair")
                       or separator == "call" and dotted and _call_label(text, label_start - start, label_end - start))
                if not hit and not (joined or dotted or quoted or sigil) and separator is None:
                    if _word_value(label) and label_name_end <= last_value:
                        hit = True  # a mnemonic's words are its value
                    else:
                        cell = _cell_value(line, label_start, label_name_end, delimiters)
                        if cell is not None:
                            hit = any(_value_like(token) for token in cell.split()[:1])
                        else:
                            hit = prose.value_follows(label_name_end)
            else:
                in_prose = not (joined or quoted or sigil or separator in ("=", ":", "quote", "pair", "call"))
                if in_prose and _property_follows(prose, label_name_end):
                    continue  # the password policy, the token type: a property of it
                if in_prose and _word_value(label) and label_name_end <= last_value:
                    hit = True  # a seed phrase's words are its value, however ordinary they read
                else:
                    hit = not in_prose or not prose.after(label_name_end)
                if not hit and below is None and prose.few_after(label_name_end):
                    below = label_end
            if not hit and kind != STRONG:
                continue  # only a strong word may head a table's column or stand alone without a hit
            # From the end of the name when the rest of it is words (SECRET_KEY, authToken), else
            # from the end of the word, so a value glued to it goes too (key-<hex>); a long piece glued
            # by - or . with nothing after the name is a value too (token-<value>, password.<value>).
            rest = label_name_end - label_end
            mask_from = label_name_end if label_end - start >= tail else label_end
            if mask_from == label_name_end and rest and line[label_end] in "-." and separator is None and rest > 6:
                mask_from = label_end
            if mask_from > last_value:
                # Nothing to mask on this line: a label line leaves its value for the lines below. A
                # word alone on its line or heading a table's column is such a label when it names a
                # secret by itself (Password, username,password), a weaker word only with a separator.
                size = len(stripped_line)
                bare = (size == label_end - label_start and stripped_line == line[label_start:label_end]
                        or size == label_name_end - start and stripped_line == line[start:label_name_end])
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
    if below is not None:
        return len(line), "below", below
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


# A single quote that closes a value: none inside a word ("It's", #701 review: two lines of prose
# went between a label's quote and the next apostrophe).
_SINGLE_CLOSE = re.compile(r"'(?![^\W_])")


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
    while True:
        if quote == "'":
            found = _SINGLE_CLOSE.search(text, index, limit)
            close = -1 if found is None else found.start()
        else:
            close = text.find(quote, index, limit)
        if close < 0:
            return None
        # A backslash escapes the next character: a quote after an odd run of them is the value's own.
        escapes = close
        while escapes > at + 1 and text[escapes - 1] == "\\":
            escapes -= 1
        if (close - escapes) % 2:
            index = close + 1
            continue
        return close


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
    return not line[label_end:].strip(" \t*_`\"'»)]:：-—.!")


def _is_fence(row: str) -> bool:
    return _FENCE_LINE.fullmatch(row) is not None


def _opens_block(line: str) -> bool:
    """Whether a label line ends in an opening bracket, so what is indented under it is its value
    (``tokens {``, ``secret = [``)."""
    return line.rstrip(" \t").endswith(("{", "[", "("))


def _code_header(line: str) -> bool:
    """Whether a line opens a block of code (``def check_password(user, password):``, ``if (!token)
    {``); read only for a line of at most 4,096 characters that ends in a colon or a brace."""
    stripped = line.rstrip(" \t")
    return len(stripped) <= 4096 and stripped.endswith((":", "{")) and _CODE_HEADER.fullmatch(stripped) is not None


def _value_shaped(row: str) -> bool:
    """Whether a line below a label line reads as its value rather than as prose: one token, a code
    span, a pair, or four tokens at most with one shaped like a value; a list item only when its
    text does (#701 review: the steps under "To rotate the token:" went); never a fence, nor a line
    of punctuation alone."""
    if _is_fence(row) or not any(character.isalnum() for character in row):
        return False
    marker = _LIST_MARKER.match(row)
    item = row[marker.end():] if marker is not None else row
    tokens = item.split()
    if not tokens:
        return False
    if len(tokens) == 1 or item.lstrip().startswith("`") or _KEY_LINE.match(item):
        return True
    return len(tokens) <= 4 and any(_value_like(token) for token in tokens)


def _lone_value(row: str) -> bool:
    """A line that holds one value alone, as a command prints it below the line that asks for it: a
    quoted string, or one token shaped like a value (a digit, a symbol, a random run); never a fence
    or a prompt."""
    token = row.strip()
    if not token or _is_fence(row) or _PROMPT.match(row):
        return False
    if len(token) > 2 and token[0] in "\"'`" and token[-1] == token[0]:
        return True
    return len(token.split()) == 1 and _value_like(token)


def _same_shape(previous: str, row: str) -> bool:
    """Whether a row has the shape of the row above it, so a list or a run of pairs goes on past the
    bound of lines below a label (#701 review: the ninth key of a list was written whole): the same
    indentation and the same kind of list marker, both pairs, or both one token."""
    if _indent(previous) != _indent(row):
        return False
    marks = _LIST_MARKER.match(previous), _LIST_MARKER.match(row)
    if marks[0] is not None or marks[1] is not None:
        kinds = [None if mark is None else ("digit" if text.lstrip()[:1].isdigit() else text.lstrip()[:1])
                 for mark, text in zip(marks, (previous, row))]
        return kinds[0] is not None and kinds[0] == kinds[1]
    if _KEY_LINE.match(previous) and _KEY_LINE.match(row):
        return True
    return len(previous.split()) == 1 and len(row.split()) == 1


def _indented(text: str, lines, index: int, below: int, label: str) -> tuple[list[tuple[int, int]], int]:
    """The lines indented under a label line, its fences kept: a YAML, JSON or Terraform block. Under
    a line that opens a block of code only the block's quoted literals go, and the rule reads every
    line of the block again (#701 review: a function named for a secret lost its whole body)."""
    code = _code_header(label)
    spans = []
    while below < len(lines):
        row_start, row_end = lines[below]
        row = text[row_start:row_end]
        if row.strip():
            if _indent(row) <= _indent(label):
                break
            if code:
                spans.extend((row_start + match.start(), row_start + match.end())
                             for match in _QUOTED_LITERAL.finditer(row))
            elif not _is_fence(row):
                spans.append((row_start + _indent(row), row_end))
        below += 1
    return spans, index + 1 if code else below


def _fence_openers(text: str, lines) -> frozenset:
    """The indices of the lines that open a fenced block: a fence line of the open block's character,
    at least as long and with no info string, closes it (CommonMark). A closing fence below a label
    inside a block opens nothing (#701 review: the paragraph after a block was masked as its content)."""
    if "```" not in text and "~~~" not in text:
        return frozenset()
    openers, open_mark = set(), None
    for number, (start, end) in enumerate(lines):
        match = _FENCE.match(text, start, end)
        if match is None:
            continue
        mark = match.group(1)
        if open_mark is None:
            openers.add(number)
            open_mark = mark
        elif mark[0] == open_mark[0] and len(mark) >= len(open_mark) and not text[match.end():end].strip():
            open_mark = None
    return frozenset(openers)


def _below(text: str, lines, index: int, mode: str, openers=None) -> tuple[list[tuple[int, int]], int]:
    """The spans of a label line's value below it, and the index of the line the rule goes on at.

    ``block``: the lines indented under it, else a lone value on the next line (the output of
    ``$ printenv DB_PASSWORD`` or of ``op read op://vault/db/password``). ``one``: the next
    non-blank line (a quoted form field's value). ``lines``: the next non-blank line whatever it
    holds, a list item only when its text reads as a value, then those after it that read as a value,
    up to a blank line. ``values``: only lines that read as a value. ``single``: lone values, a
    sentence's value that it says stands below. ``output``: what a command after a prompt prints,
    lines that read as a value up to a blank line or the next prompt. A fenced block first below the
    label loses its content and keeps its fences, and no fence line is masked; a fence that closes
    a block (``openers`` holds the lines that open one) opens nothing. A YAML or INI sibling key below
    a key line is no value of it. After ``BELOW_BLANKS`` blank lines,
    and up to ``BELOW_LINES`` of them, only a line that reads as a value is taken; past
    ``BELOW_LINES`` lines only rows of the shape of the row above go on, up to ``TABLE_ROWS``."""
    start, end = lines[index]
    label = text[start:end]
    spans, below, blanks = [], index + 1, 0
    while below < len(lines) and not text[lines[below][0]:lines[below][1]].strip():
        below += 1
        blanks += 1
    if below >= len(lines) or blanks > BELOW_LINES:
        return spans, index + 1
    far = blanks > BELOW_BLANKS
    row_start, row_end = lines[below]
    row = text[row_start:row_end]
    if _indent(row) > _indent(label) and mode != "output" and (mode == "block" or not far):
        return _indented(text, lines, index, below, label)
    if _ARMOUR_LINE.fullmatch(row):
        return spans, index + 1  # a key block below a label is the key block rule's
    if mode == "block":
        if not far and _lone_value(row):
            spans.append((row_start + _indent(row), row_end))
            return spans, below + 1
        return spans, index + 1
    fence = _FENCE.match(row)
    if fence is not None:
        if mode == "output" or far or openers is not None and below not in openers:
            return spans, index + 1
        mark, last = fence.group(1), min(len(lines), below + FENCE_LINES + 1)
        inner = below + 1
        while inner < last and not text[lines[inner][0]:lines[inner][1]].lstrip().startswith(mark):
            content = text[lines[inner][0]:lines[inner][1]]
            if content.strip() and not _is_fence(content):
                spans.append((lines[inner][0] + _indent(content), lines[inner][1]))
            inner += 1
        return spans, min(inner + 1, len(lines))
    if mode == "single":
        count = 0
        while below < len(lines) and count < BELOW_LINES and _lone_value(text[lines[below][0]:lines[below][1]]):
            row = text[lines[below][0]:lines[below][1]]
            spans.append((lines[below][0] + _indent(row), lines[below][1]))
            below, count = below + 1, count + 1
        return spans, below if count else index + 1
    if mode in ("lines", "one") and _IDENTIFIER_KEY.fullmatch(label) and _SIBLING_KEY.match(row) and \
            not row.lstrip().startswith("-") and not _value_like(row.split()[0]):
        return spans, index + 1  # password:⏎username: admin, a sibling key in YAML or INI
    count, previous = 0, None
    while below < len(lines):
        row_start, row_end = lines[below]
        row = text[row_start:row_end]
        if not row.strip() or _is_fence(row) or mode == "output" and _PROMPT.match(row):
            break
        anything = count == 0 and not far and mode in ("lines", "one") and _LIST_MARKER.match(row) is None
        if not anything and not _value_shaped(row) or mode == "one" and count:
            break
        if count >= BELOW_LINES and not (count < TABLE_ROWS and _same_shape(previous, row)):
            break
        spans.append((row_start + _indent(row), row_end))
        previous, below, count = row, below + 1, count + 1
    return spans, below


def _cells(line: str, delimiter: str) -> list[str]:
    """The cells of a delimited row, a quoted field read as one cell (``"Smith, John"``)."""
    if "\"" not in line and "'" not in line:
        return line.split(delimiter)
    cells, cell, quote, started = [], [], None, False
    for character in line:
        if quote is not None:
            cell.append(character)
            if character == quote:
                quote = None
        elif character == delimiter:
            cells.append("".join(cell))
            cell, started = [], False
        else:
            if character in "\"'" and not started:
                quote = character
            if not character.isspace():
                started = True
            cell.append(character)
    cells.append("".join(cell))
    return cells


def _header_cell(cell: str) -> bool:
    """Whether a cell reads as a column's name: short, four words at most, no sentence's end, and no
    key with its value or bracket of a record (a JSON line is no header)."""
    name = cell.strip(" \t*_`\"'")
    return (0 < len(name) <= 64 and len(name.split()) <= 4 and not name.endswith((".", "!", "?"))
            and not any(mark in name for mark in ("{", "}", "[", "]", "=", "<", ">", ": ")))


def _rule_line(row: str) -> bool:
    return bool(_TABLE_SEPARATOR.fullmatch(row) or _BORDER.fullmatch(row) or _SPACED_RULE.fullmatch(row))


def _table_rows(text: str, lines, index: int, hinted: bool = False) -> tuple[int, list[int]] | None:
    """``(end, rows)``: the index after the table whose header at ``index`` names a secret in one of
    its cells, and the indices of its rows, in any delimited or aligned form: a pipe table with or
    without outer pipes above its rule line, psql's rule of dashes joined by plus signs, a bordered
    MySQL or reStructuredText grid, whose border lines stay; comma-, semicolon- or tab-separated
    values above a row of as many cells, quoted fields read as one; and columns set by blanks above a
    rule of dashes or equals signs (sqlite's column mode, a simple reStructuredText table) or above
    rows of as many columns (``NAME  PASSWORD``, #701 review). The header's cells are read past
    blanks, emphasis and quotes. None when the line heads no such table."""
    start, end = lines[index]
    line = text[start:end]
    if index + 1 >= len(lines) or not (hinted or _HINT.search(line)) or _is_fence(line):
        return None
    following = text[lines[index + 1][0]:lines[index + 1][1]]
    if not any(mark in following for mark in ("--", "==", "|", ",", ";", "\t", "  ")):
        return None  # no rule line and no row of cells below: no table
    cells, kind, ruled = None, None, False
    if "|" in line and (_TABLE_SEPARATOR.fullmatch(following) or _BORDER.fullmatch(following)):
        cells, kind = line.strip().strip("|").split("|"), "|"
    elif "|" not in line and _SPACED_RULE.fullmatch(following):
        cells, kind, ruled = _COLUMN_GAP.split(line.strip()), "aligned", True
    else:
        delimiter = next((mark for mark in ("\t", ";", ",") if mark in line), None)
        # A quoted field's cell holds the naive first piece, so a first piece that is no column's
        # name rules the line out before it is read character by character (a JSON record).
        if delimiter is not None and delimiter in following and _header_cell(line.split(delimiter, 1)[0]):
            header = _cells(line, delimiter)
            if all(_header_cell(cell) for cell in header) and len(_cells(following, delimiter)) == len(header):
                cells, kind = header, delimiter
        if cells is None and "|" not in line:
            # A comment of column names heads a file's lines too (pppd's # client  server  secret).
            comment = line.lstrip().startswith("#")
            header = _COLUMN_GAP.split(line.strip().lstrip("#").strip() if comment else line.strip())
            named = (_COLUMN_NAME, _COMMENT_COLUMN) if comment else (_COLUMN_NAME,)
            if len(header) >= 2 and all(any(name.fullmatch(cell) for name in named) for cell in header) and \
                    len(_COLUMN_GAP.split(following.strip())) >= 2:
                cells, kind, ruled = header, "aligned", comment
    if cells is None or len(cells) < 2 or not any(
            _HINT.search(cell) and _cell_names_secret(cell.strip(" \t*_`\"'")) for cell in cells):
        return None
    rows, below = [], index + 1
    while below < len(lines) and below - index <= TABLE_ROWS:
        row = text[lines[below][0]:lines[below][1]]
        if not row.strip() or _is_fence(row):
            break
        if _rule_line(row):
            below += 1  # a rule or a border holds no value and keeps the table's shape
            continue
        if kind == "aligned":
            if not ruled and len(_COLUMN_GAP.split(row.strip())) != len(cells):
                break
        elif kind not in row:
            break
        rows.append(below)
        below += 1
    return below, rows


def _cell_names_secret(cell: str) -> bool:
    """Whether a header's cell names a secret: a strong word, pair or plural, or one of the short words
    a column of passwords is headed by (Pass, PWD, PIN, OTP); not Key, which heads key-value tables,
    and not a property of a secret (Password hash, Token type)."""
    names = list(_IDENT.finditer(cell))
    words = [word for name in names for word in _name_info(name.group())[1]]
    if not words or words[-1] in PROPERTY_WORDS:
        return False
    if any(_pair_kind(first, second) for first, second in zip(words, words[1:])):
        return True  # API key, Access key, код доступа: a pair over two names
    for name in names:
        _words_, name_words_, labels, random_run = _name_info(name.group())[:4]
        if random_run:
            continue
        for _start, _end, kind, label, _index in labels:
            if kind == STRONG or label[0] in STRONG_PLURALS or label[0] in (
                    "pass", "pwd", "pw", "passcode", "pin", "otp", "psk", "пасс", "пин"):
                return True
            if kind == WEAK and label[0] not in ("key", "keys") and len(name_words_) > 1:
                return True
    return False


def _names_secret(value: str) -> bool:
    """Whether a field's value names a secret: "/prod/db/password", "db_pass", "ConnectionPassword"."""
    for name in _IDENT.finditer(value):
        _words_, words, labels, random_run = _name_info(name.group())[:4]
        if random_run or words and words[-1] in PROPERTY_WORDS:
            continue
        if any(kind == STRONG or kind == WEAK and (len(words) > 1 or label[0] in ("pass", "pwd", "pw", "psk"))
               for _start, _end, kind, label, _index in labels):
            return True
    return False


def _object_lines(text: str, lines, index: int) -> range:
    """The lines of the object a name field stands in, up to ``OBJECT_LINES`` each way: not past a
    line that opens or closes an object, a blank line, or a list item that begins another one; a
    list item above it is the item it belongs to (``- value: V`` above ``name: DB_PASSWORD``)."""
    row = text[lines[index][0]:lines[index][1]]
    first = index
    while first > 0 and index - first < OBJECT_LINES and "{" not in row and "[" not in row \
            and not _LIST_MARKER.match(row):
        above = text[lines[first - 1][0]:lines[first - 1][1]]
        if not above.strip() or "}" in above or "]" in above:
            break
        first, row = first - 1, above
    last = index
    while last + 1 < len(lines) and last - index < OBJECT_LINES:
        under = text[lines[last + 1][0]:lines[last + 1][1]]
        if not under.strip() or _LIST_MARKER.match(under) and _indent(under) <= _indent(
                text[lines[index][0]:lines[index][1]]) or "{" in under:
            break
        last += 1
        if "}" in under or "]" in under:
            break
    return range(first, last + 1)


def _pair_spans(text: str, lines, index: int) -> list[tuple[int, int]]:
    """The value of a name-value pair whose name field at ``index`` names a secret: a value field in
    the same object or list item, in JSON, YAML or XML (``"key": "db_pass",`` then ``"value": "V"``;
    ``<name>...Password</name>`` then ``<value>V</value>``); else, after an element whose whole text
    names a secret, the text of the element on the next non-blank line within ``PAIR_LINES``, whatever its tag
    (``<key>Password</key>`` then ``<string>V</string>``, ``<dt>`` then ``<dd>``, ``<th>`` then
    ``<td>``, #701 review)."""
    start, end = lines[index]
    match = _NAME_FIELD.fullmatch(text, start, end)
    spans = []
    if match is not None and _names_secret(match.group("value") or match.group("xml") or ""):
        for other in _object_lines(text, lines, index):
            if other == index:
                continue
            found = _VALUE_FIELD.fullmatch(text, lines[other][0], lines[other][1])
            if found is not None:
                group = "value" if found.group("value") is not None else "xml"
                spans.append((found.start(group), found.end(group)))
        if spans:
            return spans
    element = _ELEMENT.fullmatch(text, start, end)
    if element is None or len(element.group("text").split()) > 3 or not _names_secret(element.group("text")):
        return spans
    for other in range(index + 1, min(len(lines), index + PAIR_LINES + 1)):
        if not text[lines[other][0]:lines[other][1]].strip():
            continue
        found = _ELEMENT.fullmatch(text, lines[other][0], lines[other][1])
        return [found.span("text")] if found is not None else spans
    return spans


def _parenthesized_label(token: str) -> bool:
    """A word naming a secret in brackets after its value: "admin / V (password)"."""
    word = token.strip("()[].,;:")
    return bool(word) and (_word_class(word) == STRONG or word.lower() in ("pass", "pwd", "pw", "passcode", "pin"))


def _before_label_spans(line: str, offset: int, prose=None) -> list[tuple[int, int]]:
    """Values written before their label on ``line`` (owner's call, v701-4 redaction finding 9's first
    group): a value piped into a command that reads a password on its standard input (``echo V |
    docker login --password-stdin``, ``echo "u:V" | chpasswd``, ``| sudo -S``, ``| gpg
    --passphrase-fd 0``, ``| gh secret set``) or given to one as a here-string (``<<< "V"``), a token
    shaped like a value right before ``as``, ``into`` or ``for`` with a word naming a secret after it
    ("use V as the password"), and one right before a bracketed word that names one ("V
    (password)")."""
    spans = []
    if "|" in line:
        for match in PIPED_VALUE.finditer(line):
            if STDIN_PASSWORD.search(match.group("rest")):
                spans.append((offset + match.start("value"), offset + match.end("value")))
    if "<<<" in line:
        for match in HERE_STRING.finditer(line):
            if STDIN_PASSWORD.search(line, 0, match.start()):
                spans.append((offset + match.start("value"), offset + match.end("value")))
    if prose is None or not (_LINK_HINT.search(line) or "(" in line):  # no label after a value without these
        return spans
    if not prose.ready:
        prose._read()
    for link, token in enumerate(prose.texts):
        if token.lower() in VALUE_LINKS:
            value = prose.value_before(link)
            if value is not None:
                spans.append((offset + prose.starts[value], offset + prose.ends[value]))
        elif link and token.startswith("(") and _parenthesized_label(token) and _value_like(prose.texts[link - 1]):
            spans.append((offset + prose.starts[link - 1], offset + prose.ends[link - 1]))
    return spans


def _mask_views(line: str, start: int, end: int, masks, mask_starts) -> tuple[str, str, list[tuple[int, int]]]:
    """``line`` with the earlier rounds' masks on it blanked, for its names, and as the word
    " masked ", for its prose, and those masks' offsets in it."""
    first = bisect_right(mask_starts, start) - 1
    first = max(first, 0)
    inside = []
    for mask_start, mask_end in masks[first:]:
        if mask_start >= end:
            break
        if mask_end > start:
            inside.append((max(mask_start, start) - start, min(mask_end, end) - start))
    if not inside:
        return line, line, inside
    names, words, kept = [], [], 0
    for mask_start, mask_end in inside:
        names += [line[kept:mask_start], " " * (mask_end - mask_start)]
        filler = " masked " if mask_end - mask_start == len(MASK) else " " * (mask_end - mask_start)
        words += [line[kept:mask_start], filler]
        kept = mask_end
    return "".join(names) + line[kept:], "".join(words) + line[kept:], inside


def _only_masked(line: str, at: int, inside) -> bool:
    """Whether what ``line`` holds from ``at`` on is one earlier round's mask and blanks."""
    rest = line[at:]
    first = at + len(rest) - len(rest.lstrip())
    last = len(line.rstrip())
    return any(mask_start == first and mask_end == last for mask_start, mask_end in inside)


def line_spans(text: str, masks=()) -> list[tuple[int, int]]:
    """Where the line rule masks ``text`` (owner's decision 2026-11-28): after the first label on a
    line that names a secret, to the end of the line (``_trigger``) and of the lines it continues with
    trailing backslashes; a quoted value that opens there and closes within ``QUOTE_LINES`` lines, to
    the end of that line; the value a label line leaves below it (``_below``): the lines indented
    under it, a fenced block's content, the lines that read as a value after a line ending in a label
    or a colon, a heading, a bare label or a sentence of a few words, the next line after a quoted
    name, a lone value after a line whose label holds none, a command's output after a prompt; a
    heredoc's body after a line that names a secret; the rows under a table's header that names a
    secret (``_table_rows``); a name-value pair's value near a name that names a secret
    (``_pair_spans``); and a value written before its label (``_before_label_spans``). ``masks``
    are the spans earlier rounds masked, sorted: no name is read from them and a span inside one is
    no new find. No fence line is masked."""
    spans: list[tuple[int, int]] = []
    lines = _lines(text)
    masks = sorted(masks)
    mask_starts = [mask[0] for mask in masks]
    openers = _fence_openers(text, lines)
    index, covered, windowed = 0, 0, -1
    while index < len(lines):
        start, end = lines[index]
        line = text[start:end]
        if start < covered or not line.strip():
            index += 1
            continue
        names, words, inside = _mask_views(line, start, end, masks, mask_starts)
        hinted = _HINT.search(names) is not None
        if hinted:
            table = _table_rows(text, lines, index, hinted)
            if table is not None:
                rows_end, rows = table
                spans.extend((lines[row][0] + _indent(text[lines[row][0]:lines[row][1]]), lines[row][1])
                             for row in rows)
                spans.extend(_trigger_spans(text, lines, index, line, names, inside))
                index = rows_end
                continue
            spans.extend(_pair_spans(text, lines, index))
        prose = _Prose(words) if hinted else None
        if hinted or "|" in line or "<<<" in line:
            spans.extend(_before_label_spans(line, start, prose))
        readable = hinted or _CLIENT_HINT.search(line) or "-" in line or "/" in line
        found = _trigger(line, prose, names, hinted) if readable else None
        if found is None and index > windowed:
            last = _continued(text, lines, index)
            if last > index:
                # A client's option on a continuation line: mysql -u root \⏎  -p V. The next window
                # starts after this one, so the time stays linear in the lines (#701 review: 12 s).
                windowed = last
                option = _option_trigger(text[start:lines[last][1]])
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
        if valued and not _only_masked(line, mask_from, inside):
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
                body = text[lines[below][0]:lines[below][1]]
                if body.strip() and not _is_fence(body):
                    spans.append((lines[below][0], lines[below][1]))
                below += 1
            index = below
            continue
        mode = None
        if _PROMPT.match(line):
            mode = "output"  # $ printenv DB_PASSWORD, $ vault kv get -field=password secret/db
        elif form == "option":
            mode = None
        elif valued:
            if _ends_with_colon(line):
                mode = "lines"
            elif prose is not None and _AFTER_SEPARATOR.match(line, label_end) is None and prose.few_after(label_end):
                mode = "single"  # The new root password is below.
        elif form == "quoted":
            mode = "one"
        elif form == "bare" or _ends_with_colon(line):
            mode = "lines"
        elif form.startswith("table:"):
            mode = form
        elif form == "below":
            mode = "single"  # Password (see below)
        elif form == "label" or _opens_block(line):
            mode = "block"
        elif form == "end":
            # A heading or a sentence ending in the label; with a separator before it (kind: Secret,
            # op read op://vault/db/password) or another ending, a lone value on the next line.
            heading = _HEADING.match(line) or _ends_with_label(line, label_end)
            mode = "values" if heading and not any(mark in line[:label_end] for mark in (":", "=")) else "block"
        if mode is None:
            index += 1  # lines a quoted value or a continuation covered are skipped above
            continue
        if mode.startswith("table:"):
            delimiter, below = mode.split(":", 1)[1], index + 1
            while below < len(lines) and below - index <= TABLE_ROWS:
                row = text[lines[below][0]:lines[below][1]]
                if not row.strip() or _is_fence(row):
                    break
                if not _rule_line(row):
                    if delimiter not in row:
                        break
                    spans.append((lines[below][0] + _indent(row), lines[below][1]))
                below += 1
            index = below
            continue
        found_spans, index = _below(text, lines, index, mode, openers)
        spans.extend(found_spans)
    return [span for span in spans if not _inside(span, masks)]


def _trigger_spans(text: str, lines, index: int, line: str, names: str, inside) -> list[tuple[int, int]]:
    """The line rule's span on a table's header line itself: after its label, to the end of the line."""
    found = _trigger(line, None, names)
    if found is None or not _has_value(line[found[0]:]) or _only_masked(line, found[0], inside):
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
            spans.extend(_line_pieces(text, start, max(start, end)))
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
            spans.extend(_line_pieces(text, first + 1, stop))
    return spans


def _line_pieces(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """The non-blank lines of ``text[start:end]``, each from its first to its last character that is
    no blank: a block masked this way keeps every line break and every line's indentation (#701
    review: a base64 block took the line break after it, gluing a closing fence to the mask)."""
    pieces, position = [], start
    while position < end:
        line_end = text.find("\n", position, end)
        line_end = end if line_end < 0 else line_end
        first, last = position, line_end
        while first < last and text[first] in " \t\r":
            first += 1
        while last > first and text[last - 1] in " \t\r":
            last -= 1
        if first < last:
            pieces.append((first, last))
        position = line_end + 1
    return pieces


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
            spans.extend(_line_pieces(text, *match.span()))
    for match in HEX_PAIR_LINES.finditer(text):
        spans.extend(_line_pieces(text, *match.span()))
    for pattern in (URL_CREDENTIAL, URL_SLASH_CREDENTIAL, ORACLE_CREDENTIAL, SQLPLUS_CREDENTIAL, QUERY_CREDENTIAL,
                    LOGIN_PAIR, URL_USER_TOKEN, DSN_CREDENTIAL, PGPASS_LINE):
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


@functools.cache
def _memory_patterns() -> tuple:
    """Shared memory's credential patterns as the hook reads them (#701 reviews): the labelled value
    with lookaheads bounded to 256 characters and a group where the value begins, the URL credential
    with a scheme of 21 characters at most, and a JWT only where a run starts. Unbounded, each label
    in a run without blanks read the rest of the run again (23.5 s for 100 kB of "token:"), and a
    JWT match restarted at every "eyJ" inside a run. Shared memory keeps dev's forms for what it
    serves, which also refuse a JWT after a hyphen and a labelled value whose first 257 characters
    hold no letter or no digit; the hook masks those shapes by its other rules."""
    from src.memory.shared import MEMORY_SECRETS

    bounded = {
        "labelled_credential": re.compile(
            r"(?i)(?<![\w-])(?:password|passwd|passphrase|пароль|secret|token|api[_ -]?key|access[_ -]?key|"
            r"client[_ -]?secret)(?![\w-])[\"'`*]*\s*(?::|=|\bis\b|—|-)\s*[\"'`]?"
            r"(?P<value>(?=[^\s\"'`<>]{0,256}[^\W\d_])(?=[^\s\"'`<>]{0,256}[\d!@#%^&*_+=~])(?!\$\{|\{\{)"
            r"[^\s\"'`<>]{6,})"),
        "url_credential": re.compile(r"(?i)\b[a-z][a-z0-9+.-]{0,20}://[^\s/:@]+:[^\s/@]+@"),
        "jwt": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    }
    return tuple((name, bounded.get(name, pattern)) for name, pattern in MEMORY_SECRETS)


def _secret_spans(text: str, scanner, masks=()) -> list[tuple[int, int]] | None:
    """Where the detectors find a secret in ``text``; None when the scanner could not scan it
    whole (too large, too many hits). An entropy hit inside a region that ``NOT_SECRET`` names is
    no secret; token formats and known secrets always are. A labelled credential that shared memory
    or user sync finds is masked to the end of its line, as the line rule masks it. A find inside an
    earlier round's mask (``masks``) is none."""
    from src.schedules import scan

    exempt = _not_secret_spans(text)
    spans = []
    for hit in scanner.scan(text):
        if hit.kind == scan.HitKind.LIMIT:
            return None
        if hit.kind == scan.HitKind.ENTROPY and _inside((hit.start, hit.end), exempt):
            continue
        spans.append((hit.start, hit.end))
    for name, pattern in _memory_patterns():
        tails = _LineTails(text)  # one per pattern: its matches come in order
        for match in pattern.finditer(text):
            if name == "labelled_credential":
                start = match.start("value")
                line_start = text.rfind("\n", match.start(), start) + 1
                if line_start and _FENCE.match(text, line_start):
                    continue  # a fence a label leaves below it is no value: the line rule masks its content
                tails.add(spans, start)
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
    return [span for span in spans if not _inside(span, masks)]


_FENCE_LINES = re.compile(r"(?m)^[ \t]*(?:`{3,}|~{3,})[ \t]*[\w+#.-]{0,64}[ \t]*\r?$")


def _without_fences(text: str, spans) -> list[tuple[int, int]]:
    """``spans`` with every fence line and the line breaks around it left out: a fence is never
    masked, so a block keeps its shape (#701 review)."""
    if "```" not in text and "~~~" not in text:
        return list(spans)
    fences = [(max(0, match.start() - 1), min(len(text), match.end() + 1)) for match in _FENCE_LINES.finditer(text)]
    if not fences:
        return list(spans)
    ends = [fence[1] for fence in fences]
    out = []
    for start, end in spans:
        number, piece = bisect_right(ends, start), start
        while number < len(fences) and fences[number][0] < end:
            if piece < fences[number][0]:
                out.append((piece, fences[number][0]))
            piece = max(piece, fences[number][1])
            number += 1
        if piece < end:
            out.append((piece, end))
    return out


def _mask_spans(text: str, spans, masks=()) -> tuple[str, list[tuple[int, int]]]:
    """``text`` with ``spans`` and the earlier ``masks`` merged and masked, and where its masks then
    stand: a round's finds are told from the turn's own text by these offsets, never by the mask's
    text (#701 review: an element named secret in a turn hid its value)."""
    merged: list[list[int]] = []
    for start, end in sorted([*spans, *masks]):
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    pieces, kept, placed, shift = [], 0, [], 0
    for start, end in merged:
        pieces += [text[kept:start], MASK]
        placed.append((start + shift, start + shift + len(MASK)))
        shift += len(MASK) - (end - start)
        kept = end
    return "".join(pieces) + text[kept:], placed


def _formats(text: str) -> tuple[str, list[tuple[int, int]]]:
    """Token formats, keeping their prefix, and the bodies of private key blocks; the text, and where
    its masks stand."""
    from src.schedules.records import MASK as RECORDS_MASK

    for pattern in _record_patterns():
        text = pattern.sub(lambda match: match[1] + RECORDS_MASK, text)
    sentinel = ""
    while sentinel in text:
        sentinel += ""
    for pattern in EXTRA_SECRET_PATTERNS:
        text = pattern.sub(lambda match: match[1] + sentinel, text)
    pieces, masks, offset = [], [], 0
    for number, part in enumerate(text.split(sentinel)):
        if number:
            pieces.append(MASK)
            masks.append((offset, offset + len(MASK)))
            offset += len(MASK)
        pieces.append(part)
        offset += len(part)
    text = "".join(pieces)
    return _mask_spans(text, key_block_spans(text), masks)


def _scan_estimate(text: str, known) -> float:
    return len(text) * (SCAN_SECONDS_PER_CHARACTER + SCAN_SECONDS_PER_CHARACTER_AND_SECRET * len(known))


def redact(text: str, known=(), deadline: float | None = None) -> tuple[str, bool]:
    """``(text, clean)``: ``text`` with the secrets the rules and the repository's detectors find
    masked, and whether a last scan of the result found nothing more (#701, owner's decisions
    2026-11-27 and 2026-11-28).

    Token formats keep their prefix and a private key block its END line (the scanner masks the
    BEGIN line, #701 review); then the line rule (``line_spans``) masks after each label that
    names a secret, in the first round, on the turn's own lines, and round after round every other
    find becomes ``MASK``, until the scan comes back empty or ``REDACTION_ROUNDS`` pass. Where the
    masks stand is kept by offset, so a mask is no find of a later round and a ``<secret>`` the turn
    holds is read as its own text; no fence line is masked.
    ``known`` are the scanner's known secrets (``environment_secrets``). A round that ``deadline``
    (a ``time.monotonic`` value) would not leave time for is not started: the text is not clean.
    """
    from src.schedules import scan

    text, masks = _formats(text)
    scanner = scan.Scanner(known)
    estimate = _scan_estimate(text, known)
    for round_number in range(REDACTION_ROUNDS + 1):
        if deadline is not None and time.monotonic() + estimate > deadline:
            return text, False
        started = time.monotonic()
        # The detectors read the same text as the line rule, so a known secret is still whole
        # when they look for it, even where the line rule masks part of it.
        spans = _secret_spans(text, scanner, masks)
        estimate = time.monotonic() - started
        if spans is None:
            return text, False
        if round_number == 0:
            # The line rule reads the turn's own lines once: a later round would read a label line
            # its own mask cut short (def check_password<secret> no longer opens a block of code).
            spans += line_spans(text, masks)
        spans = _without_fences(text, spans)
        if not spans:
            return text, True
        text, masks = _mask_spans(text, spans, masks)
    return text, False
