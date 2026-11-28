"""The line rule of ``src.memory.turn_redaction`` and the final verification's corpora (#701, attempt 4).

The owner decided on 2026-11-28 that a line naming a secret loses everything after the word that
names it, rather than one more pattern per syntax. The verification of attempt 3 attacked the
label rules with 141 secret shapes, 117 syntaxes around a secret's name and 1,000 symbol
passwords; every item is a test here, made at run time from seeded generators, so this file's own
text stays clean for the tracked-files scan. What docs/memory.md names as still passing is pinned
as passing, and over-masking is measured on the verification's 79 benign strings and on 60
sentences of everyday engineering prose.
"""
from __future__ import annotations

import base64
import functools
import json
import os
import random
import string
import time
import zlib

import pytest

from src.memory import turn_redaction as redaction

ALNUM = string.ascii_letters + string.digits
LOWER = string.ascii_lowercase + string.digits
UPPER = string.ascii_uppercase + string.digits
HEX = "0123456789abcdef"
URLSAFE = ALNUM + "-_"
B64 = ALNUM + "+/"
BASE58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
BECH32 = "QPZRY9X8GF2TVDW0S3JN54KHCE6MUA7L"
MARKER = "PRIVATE " + "KEY"
MASK = redaction.MASK
# A shape scanned in linear time takes seconds at 1 MB, a quadratic one minutes. CI runners are slower
# than a developer's machine and shared, so there the bound only catches the minutes.
SLOW_SECONDS = 30.0 if os.environ.get("CI") else 6.0


class Seeded:
    """The verification's generator: each value from its own seed, in the order the corpus asks."""

    def __init__(self, seed: int):
        self.seed = seed

    def __call__(self, length: int, alphabet: str = ALNUM) -> str:
        self.seed += 1
        generator = random.Random(self.seed)
        return "".join(generator.choice(alphabet) for _ in range(length))


def human(index: int) -> str:
    """A made-up human-style password: not random-looking, 11 to 16 characters."""
    return ["Tr0ub4dor&3xQ", "Winter2026!Rain", "c0rrect-Horse9", "Blue$Kettle77", "Maple#Syrup42x", "qu1et.River!88",
            "N0rth_Star-515", "Cedar&Stone2026", "p4per.Lantern!3", "Sunny%Field908", "gr4nite=Peak71", "Velvet^Moon64z",
            "0cean.Breeze!19", "Am8er-Forest_27", "s1lver/Comet33", "Copper@Harbor5", "R4ven+Meadow80", "t1mber~Wolf921",
            "Lilac*Orchard6", "Fr0sty-Pine!447", "H0ney,Bridge12", "j4de;Garden903"][index]


def longest_left(secret: str, masked: str) -> int:
    """The longest run of ``secret``'s own characters that ``masked`` still holds."""
    best = 0
    for start in range(len(secret)):
        length = best + 1
        while start + length <= len(secret) and secret[start:start + length] in masked:
            best = length
            length += 1
    return best


def masked_away(secret: str, masked: str, allowance: int = 0) -> bool:
    return secret not in masked and longest_left(secret, masked) <= max(allowance, len(secret) // 3)


def b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def pem(rnd, kind: str, lines: int = 6, width: int = 64) -> tuple[str, str]:
    body = "\n".join(rnd(width, B64) for _ in range(lines))
    return f"-----BEGIN {kind}{MARKER}-----\n{body}\n-----END {kind}{MARKER}-----", body


# --- The final verification's 141 secret shapes ------------------------------------------------------


@functools.cache
def _attack() -> tuple[dict[str, tuple[str, str, int]], int]:
    """The verification's redaction attack, in its order and seeds, and the seed its benign strings go on from."""
    rnd, out = Seeded(1000), {}

    def add(name, text, secret, allowance=0):
        out[name] = (text, secret, allowance)

    v = rnd(3) + "8Q~" + rnd(34, URLSAFE)
    add("Azure AD client secret in prose", f"the app registration's client secret value {v} expires in May", v)
    sig = rnd(43, B64).replace("+", "%2B").replace("/", "%2F") + "%3D"
    add("Azure SAS URL signature", "https://acct.blob.core.windows.net/c/b.txt?sv=2022-11-02&ss=b&srt=sco&sp=rwdlacx"
        f"&se=2026-12-31T00:00:00Z&spr=https&sig={sig}", sig)
    v = "GOCSPX-" + rnd(28, URLSAFE)
    add("Google OAuth client secret in prose", f"rotate {v} in the console", v, 7)
    v = "1//0" + rnd(60, URLSAFE)
    add("Google refresh token in a log", f"oauth: refreshed with {v}", v, 4)
    v = "ya29.a0" + rnd(150, URLSAFE)
    add("Google access token", f"curl -H 'X-Goog: 1' 'https://www.googleapis.com/drive/v3/files?access={v}'", v, 7)
    v = "dop_v1_" + rnd(64, HEX)
    add("DigitalOcean token", f"doctl auth init -t {v}", v, 7)
    v = "dapi" + rnd(32, HEX)
    add("Databricks token", f"databricks configure with {v} failed", v, 4)
    v = "shpat_" + rnd(32, HEX)
    add("Shopify admin token", f"the store's admin API access is {v} now", v, 6)
    v = "key-" + rnd(32, HEX)
    add("Mailgun key", f"mailgun rejected {v} today", v, 4)
    v = rnd(32, HEX) + "-us21"
    add("Mailchimp key", f"mailchimp answered 401 for {v}", v, 5)
    v = "PMAK-" + rnd(24, HEX) + "-" + rnd(34, HEX)
    add("Postman key", f"newman run with {v}", v, 5)
    v = "NRAK-" + rnd(27, UPPER)
    add("New Relic user key", f"nr1 profile uses {v}", v, 5)
    v = "pat" + rnd(14) + "." + rnd(64, HEX)
    add("Airtable token", f"airtable base opened with {v}", v, 3)
    v = "sk_" + rnd(48, HEX)
    add("sk_ prefix with a hex body", f"tts request signed with {v}", v, 3)
    v = rnd(37, HEX)
    add("Cloudflare global key, unlabelled", f"cloudflare global key {v} was rotated", v)
    v = "pypi-AgEIcHlwaS5vcmc" + rnd(150, URLSAFE)
    add("PyPI token", f"twine upload stopped: {v} is revoked", v, 20)
    v = "dckr_pat_" + rnd(27, URLSAFE)
    add("Docker Hub token", f"echo {v} | docker login -u deploy --password-stdin", v, 9)
    v = "glsa_" + rnd(32) + "_" + rnd(8, HEX)
    add("Grafana service-account token", f"grafana said invalid: {v}", v, 5)
    v = "lin_api_" + rnd(40)
    add("Linear API key", f"linear import used {v}", v, 8)
    v = "ntn_" + rnd(46)
    add("Notion token", f"notion integration {v} lost access", v, 4)
    v = "hvs." + rnd(92, URLSAFE)
    add("Vault service token", f"vault login {v}", v, 4)
    v = "AGE-SECRET-KEY-1" + rnd(58, BECH32)
    add("age secret key", f"# created: 2026-11-27\n# public key: age1{rnd(58, BECH32.lower())}\n{v}", v, 16)
    v = "sq0atp-" + rnd(22, URLSAFE)
    add("Square access token", f"square sandbox call with {v}", v, 7)
    v = "r8_" + rnd(37)
    add("Replicate token", f"replicate run failed for {v}", v, 3)
    v = "sbp_" + rnd(40, HEX)
    add("Supabase token", f"supabase link --token-from {v}", v, 4)
    v = "oy2" + rnd(43, LOWER)
    add("NuGet key", f"dotnet nuget push pkg.nupkg -k {v}", v, 3)
    v = "5" + rnd(50, BASE58)
    add("Bitcoin WIF private key", f"imported {v} into the wallet", v)
    v = "0x" + rnd(64, HEX)
    add("Ethereum private key, unlabelled", f"signer loaded {v} from disk", v)
    v = rnd(32, "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567")
    add("TOTP seed, base32", f"scan the QR or type {v} into the authenticator", v)
    v = "-".join([rnd(8, HEX), rnd(4, HEX), rnd(4, HEX), rnd(4, HEX), rnd(12, HEX)])
    add("Heroku API key (a UUID), unlabelled", f"heroku auth:token printed {v}", v)
    v = rnd(26, "0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    add("session id header, upper-case base32", f"X-Session-Id: {v}", v)

    add("Redis URL with an empty user", f"Error: connect ECONNREFUSED redis://:{human(0)}@cache.internal:6379/0", human(0))
    add("Go MySQL DSN after a label", f"DB_DSN=app:{human(1)}@tcp(127.0.0.1:3306)/app?parseTime=true", human(1))
    add("Go MySQL DSN in a panic", f'panic: dial tcp: lookup failed for "app:{human(2)}@tcp(db:3306)/app"', human(2))
    add("ODBC string with Pwd", f"Driver={{ODBC Driver 18 for SQL Server}};Server=db;Uid=sa;Pwd={human(3)};Encrypt=yes",
        human(3))
    add("pgpass line", f"db.internal:5432:app:appuser:{human(4)}", human(4))
    add("URL whose password holds an at sign", f"postgres://app:p@{human(5)}@db.internal:5432/app", human(5))
    add("sqlcmd -P", f"sqlcmd -S db.internal -U sa -P {human(6)} -Q 'select 1'", human(6))
    add("mysql -p joined", f"mysqldump -u root -p{human(7)} app > dump.sql", human(7))
    v = rnd(18)
    add("mongodb+srv URL", f"MongoServerError at mongodb+srv://app:{v}@cluster0.ab1cd.mongodb.net/app", v)
    v = rnd(16)
    add("JDBC URL with a password parameter",
        f"Caused by: org.postgresql.util.PSQLException (url=jdbc:postgresql://db/app?user=app&password={v}&ssl=true)", v)
    add("ADO.NET connection string", f"Server=tcp:db,1433;Database=app;User ID=sa;Password={human(8)};Encrypt=true",
        human(8))
    add("libpq keyword string", f'psycopg2.connect("host=db user=app password={human(9)} dbname=app")', human(9))
    add("SQLAlchemy URL in a traceback",
        f"sqlalchemy.exc.OperationalError: engine postgresql+psycopg2://app:{human(10)}@db:5432/app", human(10))
    add("redis-cli -a", f"redis-cli -h cache.internal -a {human(11)} ping", human(11))

    body = "\n".join(rnd(70, B64) for _ in range(5))
    add("ssh.com SSH2 format", "---- BEGIN SSH2 ENCRYPTED " + MARKER + " ----\nComment: \"rsa-key-20261127\"\n"
        f"{body}\n---- END SSH2 ENCRYPTED " + MARKER + " ----", body)
    private_line = rnd(64, B64)
    add("PuTTY PPK", "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\nComment: deploy\nPublic-Lines: 2\n"
        f"{rnd(64, B64)}\n{rnd(12, B64)}\nPrivate-Lines: 1\n{private_line}\nPrivate-MAC: {rnd(64, HEX)}", private_line)
    block, body = pem(rnd, "RSA ")
    indented = "\n".join("    " + line for line in block.splitlines())
    add("PEM in a YAML block scalar, CRLF", ("tls:\n  key: |\n" + indented + "\n  cert: none\n").replace("\n", "\r\n"),
        body.splitlines()[2])
    block, body = pem(rnd, "")
    add("PEM flattened onto one line", "key is " + block.replace("\n", " "), body.splitlines()[1])
    der = rnd(1600, B64)
    add("DER as one base64 line, no armour",
        f"openssl pkey -outform DER | base64 -w0\nMIIEvQIBADANBgkqhkiG9w0BAQEFAASC{der}", der[:200])
    dump = "\n".join("    " + ":".join(rnd(2, HEX) for _ in range(15)) + ":" for _ in range(6))
    add("openssl -text dump of the private exponent", f"privateExponent:\n{dump}\nprime1:\n{dump[:60]}",
        dump.splitlines()[1].strip())
    d_value = rnd(342, URLSAFE)
    add("RSA JWK private parts", json.dumps({"kty": "RSA", "n": rnd(342, URLSAFE), "e": "AQAB", "d": d_value,
                                            "p": rnd(171, URLSAFE), "q": rnd(171, URLSAFE)}), d_value)
    v = rnd(43, URLSAFE)
    add("EC JWK private d", json.dumps({"kty": "EC", "crv": "P-256", "x": rnd(43, URLSAFE), "y": rnd(43, URLSAFE),
                                        "d": v}), v)
    v = rnd(43, B64) + "="
    add("WireGuard key in wg show output",
        f"interface: wg0\n  public key: {rnd(43, B64)}=\n  private key: {v}\n  listening port: 51820", v)
    v = rnd(212, B64)
    add("minisign secret key", f"untrusted comment: minisign encrypted secret key\nRWRTY0Iy{v}", v[:100])
    block, body = pem(rnd, "EC ", 3)
    wrapped = b64(block)
    add("base64 of a PEM block", f"kubectl get secret tls -o jsonpath='{{.data.pem}}' printed {wrapped}", wrapped[60:200])
    words = "abandon ability able about above absent absorb abstract absurd abuse access accident"
    add("BIP39 seed phrase", f"wallet recovery phrase: {words}", words)
    v = rnd(64, HEX)
    add("hex private key after a label", f"SIGNING_PRIVATE_KEY=0x{v}", v)
    block, body = pem(rnd, "OPENSSH ", 4, 70)
    add("OpenSSH key inside a heredoc", f"cat > ~/.ssh/id_ed25519 <<'EOF'\n{block}\nEOF", body.splitlines()[0])

    add("YAML block scalar", f"database:\n  password: |\n    {human(12)}\n  host: db", human(12))
    add("YAML value on the next line", f"credentials:\n  secret:\n    {human(13)}", human(13))
    add("YAML name/value pair list", f"env:\n  - name: DB_PASSWORD\n    value: {human(14)}", human(14))
    a, b = rnd(14), rnd(14)
    add("TOML array under a plural name", f'api_keys = ["{a}", "{b}"]', a)
    add("TOML quoted value", f'[smtp]\nuser = "mailer"\npassword = "{human(15)}"', human(15))
    add("JSON pass key (nodemailer shape)", json.dumps({"auth": {"user": "mailer@example.com", "pass": human(16)}}),
        human(16))
    add("JSON escaped inside a JSON string", json.dumps({"body": json.dumps({"user": "admin", "password": human(17)})}),
        human(17))
    add("JSON pair array", json.dumps([["username", "admin"], ["password", human(18)]]), human(18))
    add("SSM parameter JSON", json.dumps({"Parameter": {"Name": "/prod/db/password", "Type": "SecureString",
                                                      "Value": human(19)}}), human(19))
    add("JSON pwd key", json.dumps({"uid": "sa", "pwd": human(20)}), human(20))
    add("XML element", f"<server><id>nexus</id><username>deploy</username><password>{human(21)}</password></server>",
        human(21))
    for name, frame in (("XML appSettings value", '<add key="DbPassword" value="{v}" />'),
                        ("PHP define", "define('DB_PASSWORD', '{v}');"),
                        ("PHP variable assignment", "$authToken = '{v}';"),
                        ("subscript assignment", 'config["password"] = "{v}"'),
                        ("environ subscript assignment", "os.environ['SERVICE_TOKEN'] = '{v}'"),
                        ("requests auth tuple", 'requests.get(url, auth=("deploy", "{v}"), timeout=5)'),
                        ("function-call setter", 'os.environ.setdefault("DB_PASSWORD", "{v}")'),
                        ("properties with dots", "spring.datasource.password={v}"),
                        ("Makefile conditional assignment", "DEPLOY_TOKEN ?= {v}"),
                        ("GraphQL body with escaped quotes",
                         '{{"query":"mutation{{login(user:\\"a\\",password:\\"{v}\\"){{ok}}}}"}}'),
                        ("Markdown table row", "| Field | Value |\n|---|---|\n| password | {v} |"),
                        ("vault kv get table", "====== Data ======\nKey         Value\n---         -----\npassword    {v}"),
                        ("CSV credentials", "username,password\nadmin,{v}"),
                        ("Terraform variable default",
                         'variable "db_password" {{\n  type    = string\n  default = "{v}"\n}}')):
        v = rnd(16)
        add(name, frame.format(v=v), v)

    for name, frame in (("Dockerfile ENV, space form", "FROM alpine\nENV DB_PASSWORD {v}\nRUN true"),
                        ("Dockerfile ARG default", "ARG NPM_TOKEN={v}"),
                        ("docker login -p", "docker login -u deploy -p {v} registry.example.com"),
                        ("fish set -gx", "set -gx API_TOKEN {v}"),
                        ("csh setenv", "setenv API_TOKEN {v}"),
                        ("PowerShell env assignment", '$env:API_TOKEN = "{v}"'),
                        ("PowerShell ConvertTo-SecureString",
                         'New-LocalUser ops -Password (ConvertTo-SecureString "{v}" -AsPlainText -Force)'),
                        ("single-dash long option (Go flag style)", "./svc -token={v} -addr=:8080"),
                        ("JVM system property", "java -Dapi.key={v} -jar app.jar"),
                        ("compiler definition", 'cmake -DAPI_TOKEN="{v}" ..'),
                        ("kubectl --from-literal", "kubectl create secret generic db --from-literal=password={v}"),
                        ("helm --set", "helm upgrade app ./chart --set global.postgresql.auth.postgresPassword={v}"),
                        ("netrc line", "machine api.example.com login deploy password {v}"),
                        ("SSHPASS variable", "SSHPASS={v} sshpass -e ssh deploy@host"),
                        ("openssl -passin pass:", "openssl rsa -in key.pem -passin pass:{v} -out plain.pem"),
                        ("curl -u joined", "curl -sS -udeploy:{v} https://example.com/api"),
                        ("curl --proxy-user", "curl --proxy-user deploy:{v} -x proxy:3128 https://example.com"),
                        ("HTTPie -a", "http -a deploy:{v} GET example.com/api"),
                        ("systemd Environment", 'Environment="DB_PASSWORD={v}"'),
                        ("GitHub Actions add-mask", "echo ::add-mask::{v}"),
                        ("Kubernetes Secret, stringData",
                         "apiVersion: v1\nkind: Secret\nstringData:\n  DB_PASSWORD: {v}\n  username: app")):
        v = rnd(16)
        add(name, frame.format(v=v), v)
    v = b64(human(0) + "-k8s")
    add("Kubernetes Secret, data under a plain name", f"apiVersion: v1\nkind: Secret\ndata:\n  db: {v}\n  username: YXBw", v)
    v = b64(json.dumps({"auths": {"registry.example.com": {"username": "deploy", "password": human(1),
                                                         "auth": b64("deploy:" + human(1))}}}))
    add("Kubernetes dockerconfigjson", f"data:\n  .dockerconfigjson: {v}", v[40:160])
    v = rnd(900, B64)
    add("kubeconfig client-key-data", f"users:\n- name: admin\n  user:\n    client-key-data: {v}", v[:120])
    v = rnd(16)
    add("setx on Windows", f'setx DB_PASSWORD "{v}"', v)
    v = rnd(16)
    add("cmdkey /pass", f"cmdkey /add:fileserver /user:corp\\deploy /pass:{v}", v)

    v = rnd(32, LOWER)
    add("Cookie, session pair second", f"Cookie: theme=dark; sessionid={v}; lang=en", v)
    v = "s%3A" + rnd(32, URLSAFE) + "." + rnd(43)
    add("Set-Cookie connect.sid", f"Set-Cookie: connect.sid={v}; Path=/; HttpOnly", v)
    v = rnd(300, B64)
    add("X-Amz-Security-Token", f"x-amz-security-token:{v}", v)
    v = b64("deploy:" + human(2))
    add("Proxy-Authorization Basic", f"Proxy-Authorization: Basic {v}", v)
    v = rnd(64, HEX)
    add("SigV4 Authorization signature",
        "Authorization: AWS4-HMAC-SHA256 Credential=EXAMPLEKEYID/20261127/us-east-1/s3/aws4_request, "
        f"SignedHeaders=host;x-amz-date, Signature={v}", v)
    v = rnd(64, HEX)
    add("presigned URL signature", "https://bucket.s3.amazonaws.com/report.pdf?X-Amz-Algorithm=AWS4-HMAC-SHA256"
        f"&X-Amz-Date=20261127T120000Z&X-Amz-Expires=604800&X-Amz-Signature={v}", v)
    v = "4/0A" + rnd(60, URLSAFE)
    add("OAuth authorization code in a callback", f"GET /oauth/callback?code={v}&state=xyz HTTP/1.1", v, 4)
    v = rnd(12)
    add("short Bearer value", f"> authorization: Bearer {v}", v)
    v = rnd(20, URLSAFE)
    add("GitLab PRIVATE-TOKEN header", f"curl -H 'PRIVATE-TOKEN: {v}' https://gitlab.example.com/api/v4/projects", v)
    v = rnd(32)
    add("token in a path", f'10.0.0.5 - - "POST /api/v1/hooks/{v}/trigger HTTP/1.1" 204', v)
    v = rnd(24)
    add("Rails Token header", f'Authorization: Token token="{v}", nonce="def"', v)

    add("password in quotes without a separator",
        f"WARN auth: failed login for 'sa' with password '{human(3)}' from 10.0.0.5", human(3))
    add("form body with pass=", f"DEBUG http: POST /login body=user=admin&pass={human(4)}", human(4))
    add("smtp log with pass=", f"ERROR smtp: 535 auth failed (user=mailer pass={human(5)})", human(5))
    add("key=value log with pwd", f'event="login" user="sa" pwd="{human(6)}" ok=false', human(6))
    add("credentials tuple in a traceback local",
        f"  File \"deploy.py\", line 41, in connect\n    creds = ('admin', '{human(7)}')\nConnectionError: refused",
        human(7))
    add("slash-separated credentials", f"DEBUG ldap: binding with credentials admin/{human(8)}", human(8))
    v = rnd(20)
    add("invalid key message", f"RuntimeException: Invalid API key '{v}' provided", v)
    add("sudo log with a joined -p", "sudo: deploy : TTY=pts/0 ; PWD=/home/deploy ; USER=root ; "
        f"COMMAND=/usr/bin/mysql -uroot -p{human(9)}", human(9))
    v = rnd(16)
    add("log line with password=", f"2026-11-27 12:00:01,123 DEBUG auth: login user='sa' password='{v}' ok", v)
    v = rnd(44, B64)
    add("unseal key in a log", f"INFO [vault] unsealed with key {v}", v)
    add("multipart form value", f'Content-Disposition: form-data; name="password"\r\n\r\n{human(10)}\r\n--boundary',
        human(10))

    env = f"DB_PASSWORD={human(11)}\nAPI_TOKEN={rnd(24)}\n"
    v = b64(env)
    add("base64 of an env file", f"echo {v} | base64 -d > .env", v)
    v = b64(json.dumps({"db_password": human(12), "user": "app"})).rstrip("=")
    add("base64 of a JSON config", f"CONFIG_B64 is {v}", v)
    v = b64(human(13))
    add("base64 of a short password", f"echo {v} | base64 -d | docker login -u deploy --password-stdin", v)
    v = b64("deploy:" + human(14))
    add("base64 user:password, unlabelled", f"the registry answered 401 for {v}", v)
    mime = base64.encodebytes(("\n".join(f"SECRET_{i}={rnd(30)}" for i in range(8))).encode()).decode()
    add("MIME base64 of a config, wrapped lines", f"attached config:\n{mime}", mime.splitlines()[1])
    v = base64.b64encode(zlib.compress((f"password={human(15)}\n" * 3).encode())).decode()
    add("zlib and base64", f"blob={v}", v)
    v = (f"password={human(16)}").encode().hex()
    add("hex of password=value", f"xxd -r -p <<< {v}", v)
    v = base64.b32encode(human(17).encode()).decode()
    add("base32 of a password", f"decoded from {v}", v)
    v = "password%3D" + human(18).replace("#", "%23").replace("&", "%26")
    add("percent-encoded pair", f"callback?next=%2Flogin%3F{v}", human(18).split("#")[0].split("&")[0])
    return out, rnd.seed


def attack_secrets() -> dict[str, tuple[str, str, int]]:
    """``{name: (text, secret, allowance)}``: the verification's 141 secret shapes."""
    return _attack()[0]


# docs/memory.md, "these shapes still pass": 64 hexadecimal digits without a label have the form of a
# SHA-256 digest (and of an Ethereum transaction id), whatever the 0x before them.
ATTACK_PASSING = {"Ethereum private key, unlabelled"}


def test_the_attack_corpus_has_the_verifications_141_shapes():
    assert len(attack_secrets()) == 141


@pytest.mark.parametrize("name", sorted(set(attack_secrets()) - ATTACK_PASSING))
def test_every_secret_shape_of_the_final_verification_is_masked(name):
    text, secret, allowance = attack_secrets()[name]
    masked, clean = redaction.redact(f"Deployed it.\n{text}\nAll green.")
    assert clean, name
    assert masked.startswith("Deployed it.") and masked.endswith("All green."), (name, masked)
    assert masked_away(secret, masked, allowance), (name, masked)


@pytest.mark.parametrize("name", sorted(ATTACK_PASSING))
def test_the_attack_shapes_the_docs_name_as_passing_still_pass(name):
    text, secret, _ = attack_secrets()[name]
    assert secret in redaction.redact(text)[0]


# --- The 117 syntaxes around a secret's name ----------------------------------------------------------

LABEL_FRAMES = {
    "name after a variable sigil": [
        "$authToken = '{v}';", '$token = "{v}";', '$apiKey = "{v}";', "$secret = '{v}';", "$db_pass = '{v}';",
        '$privateKey = "{v}"', "$dbPassword = '{v}';", "my $token = '{v}';", '$Password = "{v}"',
        "@token = '{v}'", "%token% = {v}", "$(TOKEN) = {v}"],
    "name after a dash": [
        "./svc -token={v} -addr=:8080", "./svc -password={v}", "./svc -api-key={v}", "tool -secret={v}",
        "java -Dapi.key={v} -jar a.jar", "mvn -Dsonar.token={v} verify", "java -Dspring.datasource.password={v}",
        'cmake -DAPI_TOKEN="{v}" ..', "gcc -DSECRET_KEY={v} main.c", "curl -xtoken={v}"],
    "JSON key with escaped quotes": [
        '{{\\"password\\": \\"{v}\\"}}', '{{\\"token\\":\\"{v}\\"}}', '{{\\"api_key\\": \\"{v}\\"}}',
        '{{\\"clientSecret\\": \\"{v}\\"}}', '"{{\\\\\\"secret\\\\\\": \\\\\\"{v}\\\\\\"}}"',
        "{{\\'password\\': \\'{v}\\'}}"],
    "name in a subscript or a call": [
        'config["password"] = "{v}"', "os.environ['SERVICE_TOKEN'] = '{v}'", "settings['SECRET_KEY'] = '{v}'",
        'headers["X-Api-Key"] = "{v}"', 'os.environ.setdefault("DB_PASSWORD", "{v}")', "define('DB_PASSWORD', '{v}');",
        'props.put("password", "{v}");', 'conn.login("deploy", "{v}")', 'auth=("deploy", "{v}")',
        "creds = ('admin', '{v}')", 'HTTPBasicAuth("deploy", "{v}")'],
    "other separators and layouts": [
        "DEPLOY_TOKEN ?= {v}", "DEPLOY_TOKEN += {v}", "token -> {v}", "| password | {v} |", "password\t{v}",
        "ENV DB_PASSWORD {v}", "set -gx API_TOKEN {v}", "setenv API_TOKEN {v}", 'setx DB_PASSWORD "{v}"',
        "machine h login u password {v}", "password:\n  {v}", "password: |\n  {v}", "password: >-\n  {v}",
        "- name: DB_PASSWORD\n  value: {v}", "Password\n{v}"],
    "XML": [
        "<password>{v}</password>", "<Password>{v}</Password>", '<property name="password" value="{v}"/>',
        '<add key="ApiToken" value="{v}" />', '<entry key="password">{v}</entry>', '<Resource password="{v}" />',
        "<apiKey>{v}</apiKey>"],
    "short option flags": [
        "mysql -u root -p{v} app", "mysql -u root -p {v}", "docker login -u deploy -p {v}", "sqlcmd -U sa -P {v}",
        "redis-cli -a {v} ping", "ldapsearch -D cn=admin -w {v}", "smbclient //h/s -U deploy%{v}",
        "curl -udeploy:{v} https://h", "curl --proxy-user deploy:{v} https://h", "http -a deploy:{v} h",
        "7z a -p{v} a.7z f", "zip -P {v} a.zip f", "openssl enc -k {v}", "openssl rsa -passin pass:{v}",
        "gpg --passphrase {v} -d f", "sshpass -p {v} ssh h", "curl -u deploy:{v} https://h", "mysql --password={v}"],
    "pass and pwd alone, and names the word split missed": [
        "pass={v}", "pwd={v}", "Pwd={v};", '"pass": "{v}"', "PWD={v}", "passcode: {v}", "pw: {v}", "passwd={v}",
        "SSHPASS={v}", "MYSQL_PWD={v}", "smtp_pass={v}", "api_keys = {v}", "TOKENS={v}", "signature={v}", "sig={v}",
        "oauth: {v}", "bearer={v}", "session={v}", "sessionid={v}", "otp: {v}", "pin={v}", "license_key={v}",
        "encryption_key={v}", "master_key: {v}", "hmac={v}", "salt={v}", "private={v}", "seed={v}"],
    "URL credentials": [
        "redis://:{v}@cache:6379/0", "redis://default:{v}@cache:6379/0", "https://:{v}@host/path", "amqp://:{v}@mq",
        "postgres://app:{v}@db/app", "app:{v}@tcp(db:3306)/app", "DB_DSN=app:{v}@tcp(db:3306)/app",
        "//deploy:{v}@registry.example.com/", "user:{v}@host (no scheme)", "https://{v}@github.com/o/r.git"],
}
FRAMES = [(group, frame) for group, frames in LABEL_FRAMES.items() for frame in frames]


def test_the_verifications_117_syntaxes_are_all_here():
    assert len(FRAMES) == 117


@pytest.mark.parametrize("group,frame", FRAMES, ids=[frame for _, frame in FRAMES])
def test_every_syntax_around_a_secrets_name_is_masked(group, frame):
    rnd = Seeded(5000 + FRAMES.index((group, frame)) * 3)
    for value in (rnd(16), "Tr0ub4dor&3xQ", rnd(24)):
        masked, clean = redaction.redact("Deployed it.\n" + frame.format(v=value) + "\nAll green.")
        assert clean and value not in masked and masked.endswith("All green."), (frame, masked)


HEX_FRAMES = ["shpat_{h}", "key-{h}", "{h}-us21", "dapi{h}", "tok_{h}", "{h}_v2", "api-{h}-prod", "v1.{h}", "id:{h}",
              '"{h}"', "={h}", "/{h}/", "x{h}", "{h}Z", "0x{h}", "#{h}", "{h}.sig", "sk_{h}"]


@pytest.mark.parametrize("frame", HEX_FRAMES)
def test_a_32_digit_hex_key_is_masked_glued_to_a_prefix_or_a_suffix(frame):
    value = Seeded(7000 + HEX_FRAMES.index(frame))(32, HEX)
    assert value not in redaction.redact("value " + frame.format(h=value) + " end")[0]


@pytest.mark.parametrize("length,masked", [(16, False), (24, False), (31, False), (32, True), (33, True), (36, True),
                                           (40, False), (48, True), (56, False), (64, False), (96, False), (128, False)])
def test_unlabelled_hex_is_masked_unless_it_has_a_commit_or_digest_form(length, masked):
    """docs/memory.md: 40, 56, 64, 96 and 128 digits are commit names and digests, under 32 short ids."""
    value = Seeded(8000 + length)(length, HEX)
    assert (value not in redaction.redact("value " + value + " end")[0]) is masked


# --- 1,000 synthetic symbol passwords ----------------------------------------------------------------

SYMBOLS = "!#$%&()*+,-./:;<=>?@[]^_{|}~"
TAIL_FRAMES = ["DB_PASSWORD={v}", "export API_TOKEN={v}", "password: {v}", "ENV SMTP_PASSWORD={v}", "--password {v}",
               'DB_PASSWORD="{v}"', '"password": "{v}"']


def symbol_passwords() -> list[str]:
    """The verification's passwords: 16 characters of letters, digits and 28 symbols, as a generator draws them."""
    generator, samples = random.Random(2026), []
    alphabet = string.ascii_letters + string.digits + SYMBOLS
    while len(samples) < 1000:
        value = "".join(generator.choice(alphabet) for _ in range(16))
        if value[0] in "<$%{[(*-=" or not any(character.isalpha() for character in value):
            continue
        samples.append(value)
    return samples


@pytest.mark.parametrize("frame", TAIL_FRAMES)
def test_no_symbol_password_after_a_label_keeps_any_of_its_run(frame):
    """Final verification: 42.4% of these were written whole as DB_PASSWORD=VALUE; now none keeps four
    characters in a row (a shorter run is a letter of the label itself)."""
    for value in symbol_passwords():
        masked, clean = redaction.redact("Deployed it.\n" + frame.format(v=value) + "\nAll green.")
        assert clean and longest_left(value, masked) < 4, (frame, value, masked)


@pytest.mark.parametrize("value", ["Tr0ub4dor&3xQ", "Summer,2026;x&y", "Winter2026!!", "kX9&mP2;vL,qR7!",
                                   "abc(def)123", "pa$$w0rd1234", "Zx81[kQ]pl99", "a,b", "N0rth_Star-515?",
                                   "p@ss:word/2026", "<kX9&mP2>", "${not_a_placeholder}", "=leading", "-dash-first"])
def test_every_character_after_the_label_goes_with_the_line(value):
    assert redaction.redact(f"DB_PASSWORD={value}") == ("DB_PASSWORD" + MASK, True)
    assert redaction.redact(f"password: {value}") == ("password" + MASK, True)


# --- Other lines of the final verification ------------------------------------------------------------

MISC_LINES = [
    "токен: Zx81kQplMn27", "ключ: Zx81kQplMn27", "секрет — Zx81kQplMn27", "пароль: Zx81kQplMn27",
    "пароль Zx81kQplMn27", "логин/пароль: admin/Zx81kQplMn27", "Пароль от базы: Zx81kQplMn27",
    "Password for the admin account: Tr0ub4dor3xQ", "password (prod): Tr0ub4dor3xQ",
    "The root password: Tr0ub4dor3xQ", "export API_TOKEN='Zx81kQplMn27,ef;gh'",
    'declare -x API_TOKEN="Zx81kQplMn27"', "ENV API_TOKEN=Zx81kQplMn27", "env API_TOKEN=Zx81kQplMn27 ./run.sh",
    "password:\u00a0Zx81kQplMn27", "API_TOKEN:\u00a0Zx81kQplMn27",
]


@pytest.mark.parametrize("line", MISC_LINES)
def test_labels_in_russian_and_after_prose_mask_the_rest_of_the_line(line):
    masked, clean = redaction.redact(line)
    assert clean and "Zx81kQplMn27" not in masked and "Tr0ub4dor3xQ" not in masked, masked


def test_a_private_key_begin_line_in_prose_masks_no_more_than_its_line():
    """Final verification: the BEGIN line of a key mentioned in prose masked the rest of the text."""
    text = (f"PEM files start with -----BEGIN {MARKER}----- and end with a matching END line.\n"
            "The next paragraph explains how to rotate them.")
    masked, clean = redaction.redact(text)
    assert clean and masked.endswith("\nThe next paragraph explains how to rotate them.")


def test_a_key_block_keeps_its_end_line_and_loses_its_body():
    """v701-4 redaction: "keeps its armour lines" was not so; the scanner's private-key pattern masks the
    BEGIN line, and only the END line stays."""
    rnd = Seeded(9100)
    body = [rnd(64, B64) for _ in range(4)]
    text = f"key:\n-----BEGIN RSA {MARKER}-----\n" + "\n".join(body) + f"\n-----END RSA {MARKER}-----\nDone."
    masked, clean = redaction.redact(text)
    assert clean and masked.endswith(f"-----END RSA {MARKER}-----\nDone.")
    assert all(line not in masked for line in body) and f"-----BEGIN RSA {MARKER}-----" not in masked


def test_a_begin_line_without_its_end_takes_the_key_lines_after_it_only():
    rnd = Seeded(9200)
    body = [rnd(64, B64) for _ in range(3)]
    text = f"-----BEGIN {MARKER}-----\n" + "\n".join(body) + "\nThat was the whole key, cut short."
    masked, clean = redaction.redact(text)
    assert clean and all(line not in masked for line in body) and masked.endswith("\nThat was the whole key, cut short.")


@pytest.mark.parametrize("text,masked", [
    # A label line without its value leaves it for the lines below.
    ("database:\n  password: |\n    VALUE\n    MORE\n  host: db", "database:\n  password: |\n    <secret>\n    <secret>\n  host: db"),
    ("credentials:\n  secret:\n    VALUE\nnext: kept", "credentials:\n  <secret>\n    <secret>\nnext: kept"),
    ("- name: DB_PASSWORD\n  value: VALUE\n- name: HOST\n  value: db", "- name: DB_PASSWORD\n  <secret>\n- name: HOST\n  value: db"),
    ("username,password\nadmin,VALUE\nops,OTHER\n\nkept", "username,password\n<secret>\n<secret>\n\nkept"),
    ("| user | password |\n|---|---|\n| admin | VALUE |\n\nkept", "| user | password |\n|---|---|\n<secret>\n\nkept"),
    # v701-4 redaction finding 1: one line was not enough for a value of two lines (a login, then
    # its password); a bare label takes the lines up to the next blank line that read as a value.
    ("Password\nVALUE\nkept", "Password\n<secret>\n<secret>"),
    ("Password\nVALUE\n\nkept", "Password\n<secret>\n\nkept"),
    ('name="password"\r\n\r\nVALUE\r\n--boundary', 'name="password"\r\n\r\n<secret>\r\n--boundary'),
    # A quoted value that closes on a later line goes to its closing quote.
    ('password="abc\ndef456" after\nkept', "password<secret>\nkept"),
])
def test_a_value_below_its_label_goes_with_it(text, masked):
    assert redaction.redact(text) == (masked, True)


# --- Over-masking: the verification's benign strings and everyday prose -------------------------------


def benign_strings() -> dict[str, str]:
    """``{name: text}``: the verification's 79 benign strings, in its order and seeds."""
    rnd, out = Seeded(_attack()[1]), {}

    def ok(name, text):
        out[name] = text

    ok("git log line", f"commit {rnd(40, HEX)} (HEAD -> dev, origin/dev)")
    ok("git range of short SHAs", "compare 3c2f6b13..7c24a796 and 10e1e6a1...93b8dd84")
    ok("git ls-tree line", f"100644 blob {rnd(40, HEX)}\tsrc/memory/history.py")
    ok("pip hash pin", f"requests==2.32.3 --hash=sha256:{rnd(64, HEX)}")
    ok("docker digest pull", f"docker pull ghcr.io/example/app@sha256:{rnd(64, HEX)}")
    ok("go.sum line", f"golang.org/x/sys v0.27.0 h1:{rnd(43, B64)}=")
    ok("Cargo.lock checksum", f'checksum = "{rnd(64, HEX)}"')
    ok("yarn resolved line", f'resolved "https://registry.yarnpkg.com/lodash/-/lodash-4.17.21.tgz#{rnd(40, HEX)}"')
    ok("MD5 of a file", f"md5sum: {rnd(32, HEX)}  dist/app.tar.gz")
    ok("ETag header", f'ETag: "{rnd(32, HEX)}"')
    ok("ssh key fingerprint", f"256 SHA256:{rnd(43, B64)} dev@laptop (ED25519)")
    ok("ssh public key", f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI{rnd(43, B64)} dev@laptop")
    ok("nix store path", f"/nix/store/{rnd(32, '0123456789abcdfghijklmnpqrsvwxyz')}-python3-3.13.1/bin/python")
    ok("git describe", "v0.42.1-14-g2414721")
    ok("crc and size", "crc32 8f3a2b1c, 48213 bytes")
    ok("UUID in a URN", "urn:uuid:5f0c7a2e-1b6d-4c2a-9e51-0d7b3c4a8f10")
    ok("UUID in braces", "{5F0C7A2E-1B6D-4C2A-9E51-0D7B3C4A8F10}")
    ok("UUID without dashes", f"request {rnd(32, HEX)} finished")
    ok("UUIDv7 list", "0193a4b2-7c3d-7e8f-9a0b-1c2d3e4f5a6b, 0193a4b2-7c3d-7e8f-9a0b-1c2d3e4f5a6c")
    ok("W3C traceparent", f"traceparent: 00-{rnd(32, HEX)}-{rnd(16, HEX)}-01")
    ok("X-Ray trace id", f"X-Amzn-Trace-Id: Root=1-67891233-{rnd(24, HEX)}")
    ok("CloudFlare ray id", f"CF-Ray: {rnd(16, HEX)}-SJC")
    ok("x-amz-id-2", f"x-amz-id-2: {rnd(75, B64)}=")
    ok("span and trace in a log", f"trace_id={rnd(32, HEX)} span_id={rnd(16, HEX)} level=info")
    ok("ULID", "01ARZ3NDEKTSV4RRFFQ69G5FAV created")
    ok("KSUID", f"event {rnd(27)} stored")
    ok("nanoid", f"doc id {rnd(21, URLSAFE)}")
    ok("cuid", f"user c{rnd(24, LOWER)}")
    ok("Mongo ObjectId", f"_id: ObjectId(\"{rnd(24, HEX)}\")")
    ok("Kubernetes pod name", "pod agents-core-7d9f8b6c5d-x2k4q restarted 3 times")
    ok("AWS resource ids", "instance i-0abcd1234efgh5678 in vpc-0a1b2c3d4e5f67890 (sg-0123456789abcdef0)")
    ok("IPFS CID", f"pinned Qm{rnd(44, BASE58)}")
    ok("Idempotency-Key header", "Idempotency-Key: 5f0c7a2e-1b6d-4c2a-9e51-0d7b3c4a8f10")
    ok("Sec-WebSocket-Key header", "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==")
    ok("site-packages path", "/usr/local/lib/python3.13/site-packages/google/protobuf/internal/api_implementation.py")
    ok("webpack chunk names", "dist/assets/index-BxK9zL2a.js and main.3f2a1b9c.chunk.js")
    ok("pip cache path", f"~/.cache/pip/wheels/ab/cd/{rnd(56, HEX)}/numpy-2.1.0-cp313-cp313-macosx_14_0_arm64.whl")
    ok("Next.js build path", f"_next/static/{rnd(21, URLSAFE)}/_buildManifest.js")
    ok("Rust mangled symbol", "at _ZN4core3fmt9Formatter9write_str17h3f2a1b9c8d7e6f50E (src/fmt.rs:12)")
    ok("Java class path", "org.springframework.security.oauth2.client.registration.ClientRegistrationRepository")
    ok("branch and ref names", "refs/heads/claude/issue-701-hook-logs-delivered-answer -> origin")
    ok("Windows SID and registry", r"HKLM\SOFTWARE\Policies\ClaudeCode S-1-5-21-3623811015-3361044348-30300820-1013")
    ok("S3 object key", "s3://reports-bucket/2026/11/27/daily-summary-eu-central-1.parquet")
    ok("keychain and key file paths", "~/.ssh/id_ed25519.pub and /etc/ssl/private/server.key exist")
    ok("attribute assigned to a token name", "self.token = token")
    ok("setting read into api_key", "api_key = settings.API_KEY")
    ok("sorted with a key function", "rows = sorted(rows, key=str.lower)")
    ok("cache key f-string", 'cache_key = f"user:{user_id}:profile"')
    ok("JSON key event", '{"key": "ArrowDown", "modifiers": ["shift"]}')
    ok("type annotations", "def sign(key: KeyType, token: Token) -> bytes:")
    ok("Django settings", 'AUTH_USER_MODEL = "accounts.User"\nSESSION_COOKIE_AGE = 1209600')
    ok("FastAPI token lifetime", "ACCESS_TOKEN_EXPIRE_MINUTES = 30")
    ok("header name constant", 'api_key_header = "X-API-Key"')
    ok("SQL primary and foreign keys", "PRIMARY KEY (id), FOREIGN KEY (user_id) REFERENCES users(id)")
    ok("ORM foreign key", 'author = ForeignKey("users.id", foreign_key="users.id")')
    ok("token counts", "input_tokens=1812 output_tokens=377 max_tokens: 4096")
    ok("dict access with key", "value = cache[key] if key in cache else compute(key)")
    ok("token loop", "for token in tokens:\n    count += len(token)")
    ok("password hashing call", "hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt())")
    ok("secret from a manager", 'secret = client.get_secret_value(SecretId="prod/db")["SecretString"]')
    ok("React key prop", '<Row key={item.id} token={token} />')
    ok("CSS and i18n keys", '"key": "welcome.title", "token": "{count} tokens"')
    ok("shell reading a variable", 'curl -H "Authorization: Bearer $API_TOKEN" "$URL"')
    ok("GitHub Actions secret reference", "env:\n  NPM_TOKEN: ${{ secrets.NPM_TOKEN }}")
    ok("terraform sensitive output", "~ secret_string = (sensitive value)")
    ok("masked password in a log", "password: ******** (hidden)")
    ok("heading and bullets", "## Key findings\n\n- **Token budget:** 4096\n- **Password policy:** at least 12 characters")
    ok("bold label with a plain word", "**Key:** Enter")
    ok("prose after a label", "Token: an opaque string the server issues, valid for one hour.")
    ok("table with key and value columns", "| key | value |\n|---|---|\n| timeout | 30 |")
    ok("link with a query string", "[the docs](https://example.com/guide?utm_source=newsletter&ref=abc123)")
    ok("fenced YAML example", "```yaml\nkey: value\nname: example\n```")
    ok("auth description", "Auth: OAuth 2.0 with PKCE; Cookie: SameSite=Lax")
    ok("keyboard shortcut", "Key: Ctrl+C stops the server")
    ok("footer line", "**Agent**: security_expert | **Skills**: skill-dev-security | **Rules**: no-fabrication")
    ok("certificate block (public)", "-----BEGIN CERTIFICATE-----\n" + "\n".join(rnd(64, B64) for _ in range(4))
       + "\n-----END CERTIFICATE-----")
    ok("numbers and dates", "On 2026-11-27 at 20:53:21Z, 1164 passed, 63 skipped; phone +1 415 555 0100")
    ok("test card number", "use the test card 4242 4242 4242 4242 with any future date")
    ok("MAC and IPv6", "00:1A:2B:3C:4D:5E at 2001:0db8:85a3:0000:0000:8a2e:0370:7334")
    return out


# Measured at attempt 4 and listed in the PR: 39 of the 79 stay as written. The other 40 are a
# label's line in code or Markdown (self.token = token, **Key:** Enter, Auth: OAuth 2.0), header
# lines named for a key (Idempotency-Key, Sec-WebSocket-Key), and ids that look random (an MD5, a
# dashless UUID, a KSUID, an ssh key's body).
BENIGN_KEPT = {
    "git log line", "git range of short SHAs", "git ls-tree line", "pip hash pin", "docker digest pull",
    "Cargo.lock checksum", "yarn resolved line", "git describe", "crc and size", "UUID in a URN", "UUID in braces",
    "UUIDv7 list", "W3C traceparent", "X-Ray trace id", "CloudFlare ray id", "ULID", "Mongo ObjectId",
    "Kubernetes pod name", "AWS resource ids", "site-packages path", "webpack chunk names", "pip cache path",
    "Java class path", "branch and ref names", "Windows SID and registry", "S3 object key",
    "keychain and key file paths", "Django settings", "FastAPI token lifetime", "header name constant",
    "SQL primary and foreign keys", "token counts", "dict access with key", "table with key and value columns",
    "link with a query string", "footer line", "numbers and dates", "test card number", "MAC and IPv6",
}


def test_the_benign_corpus_has_the_verifications_79_strings():
    assert len(benign_strings()) == 79 and BENIGN_KEPT <= set(benign_strings())


@pytest.mark.parametrize("name", sorted(BENIGN_KEPT))
def test_these_benign_strings_stay_as_written(name):
    text = benign_strings()[name]
    assert redaction.redact(text) == (text, True)


def test_over_masking_of_the_benign_corpus_stays_at_its_measure():
    changed = [name for name, text in benign_strings().items() if redaction.redact(text)[0] != text]
    assert len(changed) <= 40, changed


PROSE = [
    "The key point is that the hook runs after every turn.",
    "The auth flow works the same way in both clients.",
    "We rotate the signing key every ninety days.",
    "Each request carries a session cookie that the server checks.",
    "The token budget for this model is generous.",
    "Make sure the password policy matches the company standard.",
    "The API key header is optional for local development.",
    "Tokens are counted before the prompt is sent.",
    "Store secrets in the vault, never in the repository.",
    "The secret sauce here is caching the parsed journal.",
    "A primary key uniquely identifies each row in the table.",
    "Press any key to continue the installation.",
    "The keyboard shortcut opens the command palette.",
    "Authentication failed because the clock drifted on the runner.",
    "The login page shows a spinner while it waits.",
    "Reset your password from the account settings page.",
    "The credentials file lives next to the configuration.",
    "Use a strong passphrase for the backup archive.",
    "Our tokenizer splits words into subword pieces.",
    "The private key never leaves the hardware module.",
    "The public key can be shared with anyone.",
    "Cookies expire when the browser session ends.",
    "OAuth lets users sign in without sharing a password.",
    "The pass rate of the test suite went up this week.",
    "All tests pass on Linux and macOS.",
    "Keep the key file readable only by its owner.",
    "Rotating credentials regularly limits the damage of a leak.",
    "The password reset email arrives within a minute.",
    "The token expires after one hour of inactivity.",
    "Every API key belongs to exactly one project.",
    "The hook masks anything that looks like a password.",
    "Session ids should be long and random.",
    "The key insight came from reading the transcript.",
    "Do not paste a token into a public issue.",
    "The secret is to keep each change small.",
    "The deployment key only has read access.",
    "A connection string tells the driver where the database is.",
    "The auth middleware runs before every handler.",
    "We pin the dependency versions in the lock file.",
    "The seed phrase must be written down on paper.",
    "The salt makes each stored hash unique.",
    "The signature on the release tag was verified.",
    "The build passed and the coverage stayed flat.",
    "Merge the pull request after both reviews approve it.",
    "The journal entry records the prompt and the answer.",
    "The daemon restarts after the update finishes.",
    "Run the regular suite before pushing the branch.",
    "The cache key includes the model name and the revision.",
    "Two sessions never share an idempotency key.",
    "The access token is refreshed in the background.",
    "Passwords are hashed with a slow function.",
    "The secret manager returns the value at startup.",
    "A bearer token grants access to whoever holds it.",
    "The PIN pad accepts four digits.",
    "The worker reads its contract from a private file.",
    "Пароль хранится только в зашифрованном виде.",
    "Ключевой момент в том, что хук работает после каждого хода.",
    "Токены считаются до отправки запроса.",
    "Secret scanning runs on every push to the repository.",
    "The password field is required on the sign-up form.",
]
# A word that names a secret followed by "is" or another verb that introduces a value: the rule
# cannot tell "the password is stored" from "the password is hunter2", and masks the rest.
PROSE_MASKED = {
    "The token budget for this model is generous.",
    "The API key header is optional for local development.",
    "The secret sauce here is caching the parsed journal.",
    "The secret is to keep each change small.",
    "The access token is refreshed in the background.",
    # Attempt 6 (v701-5 redaction F20): after a seed phrase or a passphrase no sentence exempts the line,
    # since such a value is ordinary words by nature; "the password field" names a property and keeps it.
    "The seed phrase must be written down on paper.",
    "Use a strong passphrase for the backup archive.",
}


def test_the_prose_corpus_has_sixty_sentences():
    assert len(PROSE) == 60 and PROSE_MASKED <= set(PROSE)


@pytest.mark.parametrize("sentence", sorted(set(PROSE) - PROSE_MASKED))
def test_a_secret_word_used_as_an_ordinary_word_keeps_its_sentence(sentence):
    assert redaction.redact(sentence) == (sentence, True)


@pytest.mark.parametrize("sentence", sorted(PROSE_MASKED))
def test_a_secret_word_before_a_verb_that_introduces_a_value_masks_the_rest(sentence):
    masked, clean = redaction.redact(sentence)
    assert clean and masked.endswith(MASK) and len(masked) < len(sentence)


@pytest.mark.parametrize("text,masked", [
    ("my password is sunshine", "my password<secret>"),
    ("set the password to sunshine", "set the password<secret>"),
    ("the token abcdefgh", "the token<secret>"),
    ("send the token to the server", "send the token to the server"),
])
def test_a_value_in_a_sentence_is_masked_when_the_rule_cannot_tell(text, masked):
    assert redaction.redact(text) == (masked, True)


# --- Time ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize("make,size", [
    (lambda n: "curl " * n, 32000), (lambda n: "a." * n, 48000), (lambda n: "ab-" * n, 70000),
    (lambda n: "password " * n, 17778), (lambda n: "key=" * n, 40000), (lambda n: "mysql -p" * n, 20000),
    (lambda n: "x://:" * n, 32000), (lambda n: "sshpass -a " * n, 20000), (lambda n: "token: 'x " * n, 20000),
], ids=["curl", "dotted", "hyphenated", "password", "key=", "mysql -p", "scheme", "sshpass", "unclosed quote"])
def test_one_long_line_is_scanned_in_linear_time(make, size):
    """Final verification: one line of curl words took 70 s at 160,000 characters (now 0.3 s)."""
    text = make(size)
    started = time.monotonic()
    redaction.redact(text)
    assert time.monotonic() - started < SLOW_SECONDS, len(text)


def json_one_line(size: int, field: str) -> str:
    """A JSON array on one line, as an API response prints it: a labelled value per record."""
    generator, parts, total = random.Random(10), [], 0
    while total < size:
        value = "".join(generator.choice(ALNUM) for _ in range(16))
        part = '{"id":%d,"email":"u%d@example.com","%s":"%s"}' % (len(parts) + 1, len(parts) + 1, field, value)
        parts.append(part)
        total += len(part) + 1
    return "[" + ",".join(parts) + "]"


def exempt_ids_and_hex(size: int) -> str:
    """Ids of an exempt form and 32-digit hex ids, a pair per line."""
    generator, lines, total = random.Random(11), [], 0
    while total < size:
        line = "msg_01" + "".join(generator.choice(ALNUM) for _ in range(14)) + " " + "".join(
            generator.choice(HEX) for _ in range(32))
        lines.append(line)
        total += len(line) + 1
    return "\n".join(lines)


NESTED_QUOTES = "password = \"it's \\\"quoted\\\" 'inside' `ticks` \\\\ \" + '\"' + \"\\'\" ; "
KB = 1024


@pytest.mark.parametrize("make", [
    lambda: json_one_line(512 * KB, "password"), lambda: json_one_line(512 * KB, "token"),
    lambda: "token:" * (512 * KB // 6), lambda: "-eyJ" * (512 * KB // 4), lambda: "password=" * (512 * KB // 9),
    lambda: exempt_ids_and_hex(512 * KB), lambda: NESTED_QUOTES * (512 * KB // len(NESTED_QUOTES)),
    lambda: "key " * (1024 * KB // 4), lambda: "pass\u200bword=x " * (1024 * KB // 12),
    lambda: "the token budget and " * (1024 * KB // 21),
], ids=["json-password", "json-token", "token-colon", "jwt-dash", "password-equals", "exempt-ids-and-hex",
        "nested-quotes", "weak-word-line", "zero-width", "strong-prose-line"])
def test_the_final_verifications_slow_shapes_are_scanned_in_linear_time(make):
    """v701-4 redaction finding 13: ten shapes took 5 to over 120 s at 300 kB to 1 MB, each match
    reading to its line's end, lookaheads rescanning a run, a JWT pattern restarting inside a run,
    every exempt region scanned per match and the line sliced per word."""
    text = make()
    started = time.monotonic()
    masked, clean = redaction.redact(text)
    assert time.monotonic() - started < SLOW_SECONDS, len(text)
    assert clean


# --- The verification of attempt 4 (#701 review, v701-4 redaction) ------------------------------------
# Its shapes, by finding; "{v}" is where a synthetic secret goes, made at run time. Each class is
# also held by shapes the verification did not list, so the rule is tested for the class.

BELOW_A_LABEL = [
    # Finding 1: a value below a label line in a form the summary did not spell out.
    "The password for the server:\n{v}", "Here are the credentials:\nadmin\n{v}", "Try logging in with the password\n{v}",
    "Пароль от сервера:\n{v}", "Логин и пароль:\nadmin\n{v}", "### Admin password\n\n`{v}`",
    "Passwords:\n- admin: Q7w2e9R4t1Yx\n- root: {v}", '{{\n  "key": "db_pass",\n  "value": "{v}",\n  "enabled": true\n}}',
    '{{\n  "Name": "/prod/db/password",\n  "Type": "SecureString",\n  "Value": "{v}"\n}}',
    "<property>\n  <name>javax.jdo.option.ConnectionPassword</name>\n  <value>{v}</value>\n</property>",
    "mysql -u root \\\n  -p{v} \\\n  shop", "chpasswd <<EOF\ndeploy:{v}\nEOF", "My password:\n{v}", "Credentials:\nadmin\n{v}",
    "## Password\n{v}", 'expect "Password:"\nsend "{v}\\r"', "**Password:**\n{v}", "Here is the new root password\n{v}",
    "<entry>\n  <key>db.password</key>\n  <value>{v}</value>\n</entry>", "- value: {v}\n  name: DB_PASSWORD",
    "passwd deploy <<'EOF'\n{v}\n{v}\nEOF", "curl -u \\\n  ops:{v} \\\n  https://api.example.com/",
    "export API_TOKEN=\\\n{v}", "Secret:\n\n    {v}",
    # Finding 2: a fenced block under a label keeps its fences and loses its content.
    "Password:\n```\n{v}\n```", "Here is the root password:\n\n```\n{v}\n```", "Here is the token:\n\n```text\n{v}\n```",
    "Пароль:\n```\n{v}\n```",
    # Finding 3: a table whose header names a secret, in any delimited form.
    "| User | Password | Notes |\n|---|---|---|\n| app | {v} | prod |", "| **User** | **Password** |\n|---|---|\n| app | {v} |",
    "| Сервис | Пароль | Комментарий |\n|---|---|---|\n| db | {v} | прод |",
    "| Env | API key | Owner |\n|---|---|---|\n| prod | {v} | ops |", "User | Password\n--- | ---\napp | {v}",
    "user,password,role\nalice,{v},admin", '"user","password"\n"alice","Q7w2e9R4t1Yx"\n"bob","{v}"',
    "user, password, role\nalice, {v}, admin", "user\tpassword\trole\nalice\t{v}\tadmin",
    "login;password;comment\nadmin;{v};temporary", "| server | login | password | owner |\n|:--|:--|:--|:--|\n| db1 | app | {v} | ops |",
]
NAMES_AND_OPTIONS = [
    # Finding 4: names in no word list, joined names by their ending, config keywords.
    'command: ["redis-server", "--requirepass", "{v}"]', "wifi:\n  ssid: office\n  psk: {v}", "requirepass {v}",
    "masterauth {v}", 'network={{\n    ssid="office"\n    psk="{v}"\n}}', "rootpw {v}", "bindpw {v}",
    "ldap_default_authtok = {v}", "rootpw --plaintext {v}", ': PSK "{v}"', "dbpass={v}", "WIFI_PSK={v}", "DBPWD={v}",
    "mysql -u root -e \"CREATE USER 'app'@'%' IDENTIFIED BY '{v}';\"", "docker run -d redis:7 redis-server --requirepass {v}",
    "expect -c 'spawn ssh deploy@host; expect \"assword:\"; send \"{v}\\r\"; interact'",
    "keytool -importkeystore -srckeystore app.p12 -srcstorepass {v} -destkeystore app.jks",
    'const wifi = {{ ssid: "office", psk: "{v}" }};', "$dbpass = '{v}';", 'curl_setopt($ch, CURLOPT_USERPWD, "admin:{v}");',
    "CREATE USER 'app'@'%' IDENTIFIED BY '{v}';", "The new passwords are {v} and Q7w2e9R4t1Yx.", "пасс: {v}",
    "креды: admin / {v}", "пороль: {v}", "olcRootPW: {v}", "adminPw: {v}", "masterkey = {v}", "tailscale up --authkey {v}",
    "privkey: {v}", "pswd: {v}", "JSESSIONID={v}", "PIN: {v}", "smtppass {v}", "ADMINPASS={v}",
    # Finding 5: a client's password option after a user option, and a user option's value.
    "podman login -u ci -p {v} quay.io", "oc login -u admin -p {v} https://api.cluster.example.com:6443",
    "cqlsh db.internal -u cassandra -p {v}",
    "az login --service-principal -u 6f1c2a3b-0000-4000-a000-000000000001 -p {v} --tenant example.onmicrosoft.com",
    "xfreerdp /u:admin /p:{v} /v:10.0.0.9", "ssh-keygen -t ed25519 -N '{v}' -f ~/.ssh/id_deploy",
    "mssql-cli -S db1 -U sa -P {v}", "ipmitool -I lanplus -H bmc1 -U admin -P {v} chassis status",
    "mosquitto_pub -h mq -u dev -P {v} -t t -m hi", "psexec \\\\host -u admin -p {v} cmd", "twine upload -u ci -p {v} dist/*",
    "lftp -u deploy,{v} ftp.example.com", "etcdctl --user root:{v} get /", "unrar x -p{v} backup.rar",
    "influx -host db -username admin -password {v}", "mongodump --username backup --password {v} --out /backups",
]
PROSE_WORDS = [
    # Finding 6: a word common in prose before a value.
    "the pw is {v}", "ssh into 10.0.0.5 as deploy, pass is {v}", "Can you check why login fails? user ops, pwd {v}",
    "My passcode is {d6}", "the PIN is {d4}", "The key is {v}", "My seed phrase is {words}", "ключ от апи: {v}",
    "ключ шифрования {h64}", "AUTH {v}", "| Service | Login | Pass |\n|---|---|---|\n| db | admin | {v} |",
    "| Setting | Value |\n|---|---|\n| host | db1 |\n| pass | {v} |", "<tr><td>pass</td><td>{v}</td></tr>", "* pass — {v}",
    'cy.get("#pwd").type("{v}");', 'if subtle.ConstantTimeCompare([]byte(pw), []byte("{v}")) == 1 {{',
    "define('NONCE_SALT', '{v}');", "The recovery phrase is {words}", "salt: {v}",
    # Finding 7: a call after a dotted name ending in a word of the rule.
    'req.SetBasicAuth("ops", "{v}")', "r.auth('{v}')", 'basic = requests.auth.HTTPBasicAuth("ops", "{v}")',
    'mac = hashlib.pbkdf2_hmac("sha256", b"{v}", salt, 100_000)',
    'const mac = crypto.createHmac("sha256", "{v}").update(body).digest("hex");',
    'err := smtp.SendMail(addr, smtp.PlainAuth("", "bot@example.com", "{v}", host), from, to, msg)',
    'mac := hmac.New(sha256.New, []byte("{v}"))', 'digest = hmac.new(b"{v}", msg, hashlib.sha256)',
    # Finding 8: a property word after a word of the rule, with a value that is no path, number or word.
    "VAULT_SECRET_ID={uuid}", "vault write auth/approle/login role_id=app secret_id={uuid}",
    'AUTH_HEADER="Basic {b64}"', 'PASSWORD_COMMAND="echo {v}"', "auth_params = ('ops', '{v}')",
    # Finding 14: the prose exemption only for ordinary words.
    "пароль от вайфая {low}", "use passphrase {chain} for the vault", "wifi password {caps}", "door password {acronyms}",
    # Finding 15: punctuation alone, and a value glued with a hyphen or a dot.
    "password: {punct}", "token-{low}", "password.{low}",
]
BEFORE_ITS_LABEL = [
    # Finding 9, first group (owner's call): the value before its label on the same line.
    "echo {v} | docker login -u ci --password-stdin registry.example.com", 'echo "deploy:{v}" | chpasswd',
    "Use {v} as the password for the staging admin.", "I typed {v} into the password field and it failed.",
    "printf '%s' {v} | docker login --username ci --password-stdin ghcr.io", "echo '{v}' | sudo -S apt-get update",
]
QUOTES = [
    # Finding 11: quoted values over lines.
    'password = """\n{v}\n"""', "token = '''\n{v}\n'''", 'password "abc\n{v}"', "mysql -u root -p'abc\n{v}' shop",
    'API_TOKEN = "abc" \\\n    "{v}"',
    # Finding 12: a token in a URL's user position.
    "git push https://{h40}@github.com/org/repo.git main",
]


def fill(template: str, seed: int) -> tuple[str, list[str]]:
    """``template`` with synthetic secrets in place, and the secrets that must not stay."""
    rnd = Seeded(seed)
    words = ["copper", "window", "anchor", "violet", "harbor", "mellow", "tunnel", "pocket", "saddle", "lantern",
             "breeze", "candle"]
    shapes = {"v": rnd(12, ALNUM) + "7Q", "d4": rnd(4, "0123456789"), "d6": rnd(6, "0123456789"),
              "h64": rnd(63, HEX) + "a", "h40": rnd(39, HEX) + "b", "uuid": "5f0c7a2e-1b6d-4c2a-9e51-" + rnd(12, HEX),
              "b64": base64.b64encode(("ops:" + rnd(12, ALNUM)).encode()).decode(), "words": " ".join(words),
              "low": "qwhzk" + rnd(5, "bcdfghjklmnpqrstvwxz"), "chain": "-".join(words[:4]),
              "caps": "Purple Monkey", "acronyms": "ABCDE FGHIJ", "punct": "!?*-+!?*-+"}
    used = [shape for name, shape in shapes.items() if "{" + name + "}" in template]
    return template.format(**shapes), used


def all_masked(template: str, seed: int) -> tuple[bool, str]:
    text, secrets = fill(template, seed)
    masked, clean = redaction.redact(text)
    left = [secret for secret in secrets for piece in secret.split() if len(piece) >= 4 and piece in masked]
    return clean and not left, masked


@pytest.mark.parametrize("template", BELOW_A_LABEL)
def test_a_value_below_a_label_line_is_masked_in_every_form(template):
    """v701-4 redaction findings 1 to 3: 25 of 35 cases of a value below its label were written whole."""
    masked, shown = all_masked(template, 11000 + BELOW_A_LABEL.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", NAMES_AND_OPTIONS)
def test_names_and_options_outside_the_old_lists_name_a_secret(template):
    """v701-4 redaction findings 4 and 5: joined names by their ending, psk, requirepass, IDENTIFIED BY,
    Russian slang, and a password option after a user option on the same line."""
    masked, shown = all_masked(template, 12000 + NAMES_AND_OPTIONS.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", PROSE_WORDS)
def test_prose_words_calls_and_property_names_before_a_value_are_masked(template):
    """v701-4 redaction findings 6 to 8, 14 and 15."""
    masked, shown = all_masked(template, 13000 + PROSE_WORDS.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", BEFORE_ITS_LABEL)
def test_a_value_written_before_its_label_is_masked(template):
    """v701-4 redaction finding 9, its first group (the owner's call): use V as the password, echo V
    piped into a login with --password-stdin, into chpasswd or into sudo -S."""
    masked, shown = all_masked(template, 14000 + BEFORE_ITS_LABEL.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", QUOTES)
def test_quoted_values_over_lines_and_url_user_tokens_are_masked(template):
    """v701-4 redaction findings 11 and 12."""
    masked, shown = all_masked(template, 15000 + QUOTES.index(template))
    assert masked, shown


# Finding 9's second group, documented as passing: nothing names the value, only its position does.
POSITIONAL = ['conn = pymysql.connect("db.internal", "app", "{v}", "shop")', 'encoded = jwt.encode(payload, "{v}", algorithm="HS256")',
              "rabbitmqctl add_user app {v}", 'USERS = {{"admin": "{v}", "guest": "guest"}}']


@pytest.mark.parametrize("template", POSITIONAL)
def test_a_positional_secret_with_nothing_naming_it_still_passes(template):
    text, secrets = fill(template, 16000 + POSITIONAL.index(template))
    assert secrets[0] in redaction.redact(text)[0]


# Finding 10: a quote that closes the label's own string or code span opened nothing, and the rule
# masked the lines below it to the next such character or to the end of the text.
MISREAD_QUOTES = [
    "Set `password:` in config.yaml and restart.\n\nParagraph two.\nParagraph three.\n\nRun `systemctl status app`.\nLast line.",
    'print("password:", pw)\nx = 1\ny = 2\nprint("done")\nz = 3',
    'Run `read -s -p "Password: " PW` and then start the service.\n\nSecond paragraph.\nThird paragraph.',
    'password: "unclosed\nline two\nline three',
]


@pytest.mark.parametrize("text", MISREAD_QUOTES)
def test_a_quote_closing_the_labels_own_string_masks_no_later_line(text):
    masked, clean = redaction.redact(text)
    assert clean and masked.count("\n") == text.count("\n"), masked
    assert masked.split("\n")[1:] == text.split("\n")[1:], masked


def test_a_quoted_value_is_masked_only_within_its_bound_of_lines():
    """A close more than ``QUOTE_LINES`` lines below is no close: the label's line alone goes."""
    lines = [f"plain line {number}" for number in range(redaction.QUOTE_LINES + 3)]
    text = 'password = "abc\n' + "\n".join(lines) + '\nend"'
    masked, clean = redaction.redact(text)
    assert clean and masked.split("\n")[1:] == text.split("\n")[1:], masked


def test_line_breaks_of_benign_text_stay_outside_masked_spans():
    """The verification's benign corpus lost 8 line breaks as one text, where the line rule took a
    closing quote for an opening one; now no span of the line rule holds a line break of the final
    verification's benign strings, one by one or as one text."""
    texts = list(benign_strings().values()) + ["\n".join(benign_strings().values()), "\n\n".join(PROSE),
                                               *MISREAD_QUOTES, "\n\n".join(MISREAD_QUOTES)]
    for text in texts:
        assert all("\n" not in text[start:end] for start, end in redaction.line_spans(text)), text[:200]


@pytest.mark.parametrize("text", [
    "api_key_header = \"X-API-Key\"", "TOKEN_ID = 12", "SECRET_ID_PATH=/run/secrets/id", "password:\nusername: admin",
    "The password field is required on the sign-up form.", "Passwords are hashed with a slow function.",
    "The key point is that the hook runs after every turn.", "| key | value |\n|---|---|\n| timeout | 30 |",
    "entry 3afab3fea63f, key digest 0123", "This PR adds a password strength meter to the signup form.",
    "kind: Secret\napiVersion: v1", "The hook masks anything that looks like a password.\nNext line stays.",
    "sort -u -k2,2 data.csv", "docker run -u 1000:1000 nginx",
])
def test_the_new_classes_leave_these_lines_alone(text):
    """Over-masking the new classes could cause on lines a reader needs: a property's plain value, a
    YAML sibling key, prose with a word of the rule, a key-value table, a kind of resource."""
    assert redaction.redact(text) == (text, True)


@pytest.mark.parametrize("template,expected", [
    ("Password:\n```\n{v}\n```", "Password:\n```\n<secret>\n```"),
    ("Here is the root password:\n\n```\n{v}\n```\nDone.", "Here is the root password:\n\n```\n<secret>\n```\nDone."),
    ("Пароль:\n~~~text\n{v}\n{v}\n~~~", "Пароль:\n~~~text\n<secret>\n<secret>\n~~~"),
    ("The token:\n```\n" + "line {v}\n" * 12 + "```\nkept", "The token:\n```\n" + "<secret>\n" * 12 + "```\nkept"),
])
def test_a_fenced_block_under_a_label_loses_its_content_and_keeps_its_fences(template, expected):
    """v701-4 redaction finding 2: the fence line was taken for the value and the block kept; its content
    goes now, past the eight lines a value below a label may take, and the fences stay."""
    text, _secrets = fill(template, 17000)
    assert redaction.redact(text) == (expected, True)


def test_a_fenced_block_under_a_label_is_masked_to_its_bound():
    """An unclosed fence takes ``FENCE_LINES`` lines at most, never the rest of the answer."""
    lines = [f"line {number}" for number in range(redaction.FENCE_LINES + 5)]
    text = "Password:\n```\n" + "\n".join(lines)
    masked, clean = redaction.redact(text)
    assert clean and masked.endswith("\n".join(lines[redaction.FENCE_LINES:])), masked[-200:]


# --- The verification of attempt 5 (#701 review, v701-5 redaction) ------------------------------------
# Each class by its finding, held by shapes the verification named and by others of the same class;
# "{v}" is where a synthetic secret goes, made at run time by ``fill``.

BELOW_A_COMMAND = [
    # F1: the value line after a command or a label line that names a secret, in any form.
    "$ printenv DB_PASSWORD\n{v}", "$ echo $DB_PASSWORD\n{v}", "$ cat /run/secrets/db_password\n{v}",
    "$ terraform output -raw db_password\n{v}", "$ vault kv get -field=password secret/db\n{v}",
    "$ kubectl get secret db -o jsonpath='{{.data.password}}' | base64 -d\n{v}",
    "dev@build:~$ op read op://Private/db/password\n{v}", "PS C:\\Users\\dev> $env:DB_PASSWORD\n{v}",
    ">>> os.environ['DB_PASSWORD']\n'{v}'", "% heroku config:get SECRET_KEY\n{v}", "printenv DB_PASSWORD\n{v}",
    "op read op://Private/db/password\n{v}", "echo $API_TOKEN\n{v}", "What's the wifi password?\n{v}",
    "127.0.0.1:6379> CONFIG GET requirepass\n1) \"requirepass\"\n2) \"{v}\"",
    "```console\n$ security find-generic-password -s registry -w\n{v}\n```",
]
DATABASE_TABLES = [
    # F2: database clients' tables and aligned listings under a header that names a secret.
    " id | login |   password\n----+-------+---------------\n  1 | admin | {v}\n(1 row)",
    "+----+-------+----------------+\n| id | user  | password       |\n+----+-------+----------------+\n"
    "|  1 | admin | {v} |\n+----+-------+----------------+",
    "user        password\n----------  --------------\nadmin       {v}",
    "NAME      PASSWORD\nadmin     {v}\nreport    {v}",
    "+--------+--------------+\n| user   | token        |\n+========+==============+\n| ci     | {v} |\n+--------+--------------+",
    "=====  ========\nuser   password\n=====  ========\nadmin  {v}\n=====  ========",
    "# client  server  secret  IP addresses\nvpnuser  *  {v}  *",
    "SERVICE   API KEY\nbilling   {v}",
]
MASK_TEXT = [
    # F3: an element or a word equal to the mask's text names a secret like any other.
    "<secret>{v}</secret>", "<secret><![CDATA[{v}]]></secret>", "<secret> = {v}", "client <secret>: {v}",
    "<config>\n  <secret>{v}</secret>\n</config>",
]
PAIRS_AND_WORDS = [
    # F4: a name and its value in neighbouring elements, and a JSON pair four lines apart.
    "<key>Password</key>\n<string>{v}</string>", "<dt>Password</dt>\n<dd>{v}</dd>",
    "<tr>\n  <th>Password</th>\n  <td>{v}</td>\n</tr>",
    '{{\n  "name": "DB_PASSWORD",\n  "type": "string",\n  "scope": "prod",\n  "enabled": true,\n  "value": "{v}"\n}}',
    # F5: plurals of strong words heading a heading, a column and a block.
    "## Passwords\n\n{v}", "## Tokens\n{v}", "| Service | Tokens |\n|---------|--------|\n| ci | {v} |",
    'tokens {{\n  master = "{v}"\n}}', "Secrets\n{v}",
    # F6: a quoted comma in a row, any text in a header cell.
    'name,password,notes\n"Smith, John",{v},ok', "id,user,password,created_at(UTC)\n1,admin,{v},2026-11-01",
    # F7: a password option attached, after a quoted user, before the user, after user%; rar's -hp.
    "pgcli -h db -u admin -p{v} shop", "mycli -uroot -p{v}", 'ipmitool -H bmc1 -U "admin" -P "{v}" chassis status',
    "cqlsh -p {v} -u cassandra node1", "twine upload -p {v} -u __token__ dist/*", "rpcclient -U backup%{v} fs01",
    "rar a -hp{v} out.rar report.csv", "schtasks /create /sc daily /ru CORP\\svc /rp {v}",
    # F8: a password with a slash, Oracle's user/password@, query parameters, squid's login=.
    "DATABASE_URL=postgresql://app:ab/{v}@db.internal:5432/app", "sqlplus system/{v}@//ora1:1521/ORCL",
    "sqlplus sys/{v} as sysdba", "jdbc:oracle:thin:scott/{v}@db:1521:orcl", "exp userid=scott/{v}@prod file=x.dmp",
    "rman target sys/{v}@prod", "https://app.example.com/callback?code={v}&state=xyz",
    "https://app.example.com/?sid={v}", "cache_peer proxy parent 3128 0 login=user:{v}",
    # F13: past the bounds of lines and blank lines below a label.
    "API keys:\n" + "".join("- k%d-{v}\n" % number for number in range(10)), "Password\n\n\n\n{v}",
    "### Admin password\n\n{v}\n{v}",
    # F14: a sentence that says its value stands below.
    "The new root password is below.\n{v}", "The new root password is below.\n\n```text\n{v}\n```",
    "Password (see below)\n{v}", "I reset the admin password.\nIt is now {v}",
    # F15: a name of one case that begins with a strong word.
    'tokenstr := "{v}"', "secretval={v}", "passwordtext: {v}", "PASSWORDVALUE={v}", 'dbPW = "{v}"',
    # F16: Russian words, misspellings and other words for a password or a code.
    "код доступа: {v}", "кодовое слово: {v}", "кодовая фраза: {words}", "пин-код: {d4}", "пин: {d4}",
    "сид-фраза: {words}", "пассворд: {v}", "кей: {v}", "секретное слово {low}", "pasword: {v}", "passsword: {v}",
    "passowrd: {v}", "passwrod={v}", "pssword={v}", "p/w: {v}", "psw: {v}", "passw: {v}", "parol: {v}",
    "recovery code: {v}", "backup codes: {v}", "security answer: {low}", "activation code: {v}",
    "The door code is {d4}", "pаssword: {v}",
    # F17: a value before its label in a here-string or into another command that reads standard input.
    'docker login -u ci --password-stdin registry.example.com <<< "{v}"',
    'echo "{v}" | gpg --batch --passphrase-fd 0 -d backup.tar.gpg', 'echo "{v}" | wrangler secret put API_TOKEN',
    "printf '%s' '{v}' | gh secret set DEPLOY_PASSWORD", "echo {v} | openssl enc -aes256 -pass stdin -in x",
    "echo -n '{v}' | ansible-vault encrypt_string --stdin-name db_password", "admin / {v} (password)",
    # F20: after a seed phrase, a recovery phrase, a mnemonic or a passphrase the words are the value.
    "my wallet seed phrase {words}", "the recovery phrase {words}", "mnemonic {words}", "passphrase {chain} for the vault",
]


@pytest.mark.parametrize("template", BELOW_A_COMMAND)
def test_a_value_after_a_command_or_a_label_line_that_names_it_is_masked(template):
    """v701-5 redaction F1: a joined name, a sigil, a path or a separator earlier on the line gave
    the label line no mode that takes the next line, and a prompt line's output stayed."""
    masked, shown = all_masked(template, 18000 + BELOW_A_COMMAND.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", DATABASE_TABLES)
def test_the_rows_of_a_database_clients_table_are_masked(template):
    """v701-5 redaction F2: psql, MySQL and reStructuredText rule lines, sqlite's column mode and an
    aligned listing were no table to the rule."""
    masked, shown = all_masked(template, 19000 + DATABASE_TABLES.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", MASK_TEXT)
def test_an_element_named_like_the_mask_hides_no_value(template):
    """v701-5 redaction F3: every <secret> was blanked before names were read, so an element of that
    name kept its value; the masks are told apart by their offsets now."""
    masked, shown = all_masked(template, 20000 + MASK_TEXT.index(template))
    assert masked, shown


@pytest.mark.parametrize("template", PAIRS_AND_WORDS)
def test_the_narrower_boundaries_of_attempt_5_are_closed(template):
    """v701-5 redaction F4 to F8, F13 to F17 and F20."""
    masked, shown = all_masked(template, 21000 + PAIRS_AND_WORDS.index(template))
    assert masked, shown


B64_LINES = ["QUJDREVGR0hJSktMTU5PUFFSU1RVVldYWVphYmNkZWZnaGlqa2xtbm9wcXJzdHV2d3h5ejAx",
             "MjM0NTY3ODlBQkNERUZHSElKS0xNTk9QUVJTVFVWV1hZWmFiY2RlZmdoaWprbG1ub3BxcnN0"]


@pytest.mark.parametrize("text,kept", [
    ("The CSR:\n\n```\n{0}\n{1}\n```\n\nSend it to the CA.", "\n```\n\nSend it to the CA."),
    ("{0}\n{1}\nThat was the thumbnail.", "\nThat was the thumbnail."),
    ("-----BEGIN CERTIFICATE-----\n{0}\n{1}\n-----END CERTIFICATE-----\nDone.", "\n-----END CERTIFICATE-----\nDone."),
    ("tls:\n  key: |\n    -----BEGIN " + MARKER + "-----\n    {0}\n    {1}\n    -----END " + MARKER + "-----\n  ok: 1",
     "tls:\n  key: |\n    <secret>\n    <secret>\n    <secret>\n    <secret>\n  ok: 1"),
    ("-----BEGIN " + MARKER + "-----\n  {0}\n  {1}\n-----END " + MARKER + "-----\nkept", "\n  <secret>\n  <secret>\n-----END"),
])
def test_a_block_of_base64_or_key_lines_keeps_its_line_breaks_and_indentation(text, kept):
    """v701-5 redaction F9: a base64 block's span took the line break after it, so a closing fence, an
    END line or prose joined the mask; every line of a block is masked on its own now."""
    text = text.format(*B64_LINES)
    masked, clean = redaction.redact(text)
    assert clean and kept in masked and masked.count("\n") == text.count("\n"), masked
    assert not any(line in masked for line in B64_LINES), masked


@pytest.mark.parametrize("text,kept", [
    ("```console\n$ tool login\nPassword:\nLogged in as dev.\n```\n\nThen run the sync.", "\n```\n\nThen run the sync."),
    ("1. Reset the password:\n\n   ```bash\n   passwd deploy\n   ```\n\n2. Log in again.", "   ```bash"),
    ("```bash\nsudoedit /etc/app/secrets.env   # DB_PASSWORD is added by hand\n```\n\nKeep it out of the shell history.",
     "```\n\nKeep it out of the shell history."),
    ("Here is the token:\n\n```\n{v}\n```\n\nRotate it monthly.", "```\n<secret>\n```\n\nRotate it monthly."),
])
def test_no_fence_line_is_masked(text, kept):
    """v701-5 redaction F10: a closing fence was a value line below a label inside the block, both
    fences of a block under a list item went with the indented lines, and a closing fence below a
    label read as an opening one took the paragraph after it."""
    text, _secrets = fill(text, 22000)
    masked, clean = redaction.redact(text)
    assert clean and kept in masked and masked.count("\n") == text.count("\n"), masked


@pytest.mark.parametrize("text", [
    "def check_password(user, password):\n    if not user.active:\n        return False\n    return verify(user, password)",
    "class TokenBucket:\n    rate = 10\n    capacity = 100\n\n    def take(self):\n        return True",
    "To rotate the token:\n- open Settings\n- choose Developer\n- click Regenerate\n\nThat is all.",
    "The password policy changed last week. Accounts created before 2024 keep their old rules and the audit log "
    "shows who changed what.",
    "      Name                    Value             Type    Location\n      ----                    -----             ----"
    "    --------\n   profile                <not set>             None    None",
    "auth.method    password\nauth.mfa       disabled\ntoken.ttl      3600",
    "CREATE TABLE users (\n    id            bigserial PRIMARY KEY,\n    created_at    timestamptz NOT NULL DEFAULT now()\n);",
    '<label for="email">Password reset</label>\n<input id="email" name="email">\n<button type="submit">Sign in</button>',
    "Password 'temporary\nThe account is locked for now.\nIt's reopened on Monday.\nSee the runbook.",
])
def test_lines_below_a_label_that_hold_no_value_stay(text):
    """v701-5 redaction F11 and F18, the narrowing the owner allowed where no corpus loses masking: a
    block of code under a def, class or brace line keeps its lines but its quoted literals, list items
    that read as steps stay, a word naming a property (the password policy) is no label, a listing of
    keys is no table, an element pair is the next line only, and an apostrophe closes no quote."""
    masked, clean = redaction.redact(text)
    lines = text.split("\n")
    assert clean and masked.count("\n") == text.count("\n"), masked
    kept = [line for line in lines[1:] if line in masked.split("\n")]
    assert len(kept) >= len(lines[1:]) - 1, masked


def test_a_code_block_under_a_label_loses_its_quoted_literals():
    """The value a block of code holds is a literal: def get_password(): return "V" still masks it."""
    text, secrets = fill('def get_password():\n    return "{v}"\n\nprint(get_password())', 22100)
    masked, clean = redaction.redact(text)
    assert clean and secrets[0] not in masked and masked.endswith("\n\nprint(get_password())"), masked


@pytest.mark.parametrize("name,counts", [
    ("COMPASS", False), ("BYPASS", False), ("NO_BYPASS", False), ("PROXY_BYPASS", False), ("HOTKEY", False),
    ("MONKEY", False), ("SSHPASS", True), ("DB_PASS", True), ("MASTERKEY", True), ("PGPASSWORD", True),
])
def test_an_environment_name_that_is_an_everyday_word_names_no_secret(name, counts):
    """v701-5 redaction F21: secret_name counted COMPASS, BYPASS and HOTKEY by their ending."""
    assert redaction.secret_name(name) is counts


def test_shared_memory_keeps_its_own_patterns_and_the_hook_its_bounded_copies():
    """v701-5 regression: attempt 5 bounded shared memory's patterns for the hook's speed, so
    read_shared_memory served a JWT after a hyphen and a long labelled value that dev refuses. Shared
    memory's patterns are dev's again; the hook reads bounded copies of three of them, and still masks
    both shapes by its other rules."""
    from src.memory import shared
    jwt = "eyJ" + "a1B2c3D4e5" * 2 + "." + "f6G7h8I9j0" * 2 + "." + "k1L2m3N4o5" * 2
    long_value = "password: " + "a" * 300 + "1"
    for note in ("session sess-" + jwt, long_value):
        assert any(pattern.search(note) for _, pattern in shared.MEMORY_SECRETS), note
        masked, clean = redaction.redact(note)
        assert clean and jwt not in masked and "a" * 300 not in masked, masked
    names = [name for name, _ in shared.MEMORY_SECRETS]
    assert [name for name, _ in redaction._memory_patterns()] == names
    bounded = dict(redaction._memory_patterns())
    for name in ("labelled_credential", "url_credential", "jwt"):
        assert bounded[name] is not dict(shared.MEMORY_SECRETS)[name]
    for sample in ("password: Tr0ub4dor&3", "postgres://app:Tr0ub4dor@db/app", "token " + jwt):
        assert any(pattern.search(sample) for _, pattern in shared.MEMORY_SECRETS) == \
            any(pattern.search(sample) for pattern in bounded.values()), sample


KB_SHAPES = [
    lambda size: ("key-" * (size // 4)).rstrip("-"), lambda size: ("public-key-value-" * (size // 17)).rstrip("-"),
    lambda size: "see " + ("key." * (size // 4)).rstrip("."), lambda size: "mysql -u root \\\n" * (size // 16),
    lambda size: "echo Tr0ub4dor3xQ | docker login -u ci --password-stdin ghcr.io\n" * (size // 128),  # half
    lambda size: "password-" * (size // 9) + " goes here", lambda size: "x-" * (size // 4) + "key-" * (size // 8),
]


@pytest.mark.parametrize("make", KB_SHAPES, ids=["kebab-key", "kebab-words", "dotted-after-a-word", "continuations",
                                                 "piped-login", "kebab-strong-before-prose", "kebab-late-labels"])
def test_the_attempt_5_slow_shapes_are_linear_at_one_megabyte(make):
    """v701-5 redaction F12: a hyphen-joined name was read once per label (13.7 s at 80 kB, over 150 s
    at 1 MB), a dotted name was sliced per label (6.7 s at 1 MB), and every line of continued client
    lines read the fifty lines after it again (12 s at 1 MB). Each takes seconds at 1 MB now (lines piped
    into a login, whose time is mostly the scanner's, at half of it), and four times the text takes at
    most about four times as long."""
    small, large = make(256 * KB), make(1024 * KB)
    started = time.monotonic()
    redaction.redact(small)
    middle = time.monotonic()
    masked, clean = redaction.redact(large)
    finished = time.monotonic()
    assert finished - middle < SLOW_SECONDS, len(large)
    assert finished - middle < 8 * (middle - started) + 1.0, (middle - started, finished - middle)
