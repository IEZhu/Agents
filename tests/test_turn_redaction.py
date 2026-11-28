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


def test_a_key_block_keeps_its_armour_lines_and_loses_its_body():
    rnd = Seeded(9100)
    body = [rnd(64, B64) for _ in range(4)]
    text = f"key:\n-----BEGIN RSA {MARKER}-----\n" + "\n".join(body) + f"\n-----END RSA {MARKER}-----\nDone."
    masked, clean = redaction.redact(text)
    assert clean and masked.endswith(f"-----END RSA {MARKER}-----\nDone.")
    assert all(line not in masked for line in body)


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
    ("| user | password |\n|---|---|\n| admin | VALUE |\n\nkept", "| user | password |\n<secret>\n<secret>\n\nkept"),
    ("Password\nVALUE\nkept", "Password\n<secret>\nkept"),
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
    "The password field is required on the sign-up form.",
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
    assert time.monotonic() - started < 6, len(text)
