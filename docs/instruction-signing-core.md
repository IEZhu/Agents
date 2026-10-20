# Instruction-signing core and local authority

The package in [src/instruction_signing](../src/instruction_signing/) implements
the bounded byte and protected-inventory primitives for
[#235](https://github.com/IEZhu/Agents/issues/235), and the local key, trust and
policy backend for [#236](https://github.com/IEZhu/Agents/issues/236). It performs
no protected-file acquisition, startup enforcement, security review or activation.
Those remain assigned to subsequent issues in the
[specification](instruction-signing-spec.md#13-implementation-ownership-and-shared-contracts).

## OMS and exact-byte API

Runtime imports require only the standard library and the directly pinned
cryptography dependency. The reference model-signing package and its Sigstore
stack are confined to the signing-tests extra.

~~~python
from cryptography.hazmat.primitives.asymmetric import ec
from src.instruction_signing import (
    Status, build_seal, fingerprint, public_key_pem, verify_bytes, verify_seal,
)

key = ec.generate_private_key(ec.SECP256R1())  # ephemeral example key
data = b"Example instruction"
seal = build_seal(
    {"common/example.md": data}, key,
    kind="library", root_name=".user", origin="cli",
    signer="example", signed_at="2026-10-15T21:00:00Z",
)
recognized_keys = {fingerprint(key.public_key()): public_key_pem(key.public_key())}
result = verify_seal(
    seal, recognized_keys,
    expected_kind="library", expected_path="common/example.md",
)
checked = verify_bytes(result, "common/example.md", data)
assert checked.status == Status.VALID
assert checked.data is data
assert checked.authorization_checked is False
~~~

Use expected_kind for profile verification: release or library. The first
resource contains the signed profile; library seals contain one resource.
Verification checks the key fingerprint, DSSE PAE signature, subject/resource
digests, profile and expected path. The subject's display name does not grant
directory authority. Path comparisons are exact; a copied seal cannot cover a
renamed library file.

Omitting expected_kind enables generic OMS verification, including unextended
seals produced by the reference CLI. A generic result has no profile or seal
kind and cannot validate a protected installation inventory. A present unknown
or malformed profile is rejected even in generic mode.

The five statuses remain valid, unsigned, tampered, untrusted and invalid.
Stable reason strings distinguish missing coverage, changed bytes, unknown
keys and invalid input/signatures. Verified fingerprint and claimed seal kind
are available separately. The signer label and timestamp are display metadata.

A valid result establishes the cryptographic and supplied-content checks only.
The public-key mapping recognizes identities; AuthorityStore.verify separately
authorizes each identity for the requested seal kind and enrolled root with
current revocation state. Primitive results start with authorization_checked=False.
An attacker-controlled key must never be added just because its signature
verifies. This layer does not enroll keys or persist an authorization result.

There is no seal-result cache. verify_bytes hashes each new immutable bytes
argument and returns that exact object only on success. Changed bytes cannot
reuse an earlier content result. Callers must reverify/re-authorize after
trust or policy changes and acquire observations safely; the package cannot
establish that a mutable filesystem was captured without races.

## Protected inventory and observations

The exact schema, immutable observation fields and comparison interface are
documented in [inventory.py](../src/instruction_signing/inventory.py).
The inventory is ordinary signed content at integrity/protected-files.json.
Its files entries include its own path and metadata, without a self digest;
content digests come from the OMS resources.

Each protected file must be a signed release resource. Every signed resource
inside a protected root must be inventoried unless explicitly isolated.
The release may also sign documentation outside protected roots without
requiring those unrelated files in a runtime observation. A release signature
over a favorable subset cannot satisfy complete protected-set verification.

verify_protected_set requires immutable observed-file records, an explicit
enumeration_complete=True assertion and independently supplied, nonempty
minimum_paths and minimum_roots. It authenticates inventory bytes before
parsing, matches the signed resource/path set, and checks the same observation
bytes and required metadata. Missing files, added files, renames, malformed
paths, symlink/reparse components and incomplete enumeration cannot pass.
Callers supply the minima from authenticated policy rather than the candidate
inventory. The comparator checks that assertion's consistency; actual safe,
complete enumeration and load-path isolation remain #237's responsibility.

POSIX executable metadata is whether any executable bit is present. Windows
observations use executable=None, rather than interpreting POSIX bits. Both
platforms require supported regular files, portable unambiguous names and no
reported symlink/reparse component. Exclusions require an explicit reason and
the outside-load-path isolation rule. Dependency environment and artifact
identities are signed assertions; the trusted caller must authenticate the
enrolled external environment or protected dependency content before loading it.

Coverage issues retain individual status/reason/path records separately from
the seal evidence. The aggregate precedence is invalid, untrusted, tampered,
unsigned, valid. Coverage results are integrity evidence, not security findings,
an owner approval or signing/activation authority.

## Bounds and interoperability checks

The local authority API and its trusted-runtime precondition are documented in
[Local signing authority](instruction-signing-authority.md).

Default limits are 8 MiB per seal, 4 MiB per statement/inventory JSON, 64 MiB per
file, 256 MiB aggregate file bytes, 20,000 resources/observations and 1,024 UTF-8
bytes per path. Positive custom Limits values may reduce or increase these
bounds explicitly. Unknown inventory versions, duplicate JSON keys, nonfinite
numbers, malformed UTF-8, unsupported key types, file types and unsafe paths
fail closed. ECDSA P-256/SHA-256 is the only supported key/signature profile.

Install the optional reference tests in a development environment:

~~~bash
python -m pip install -r requirements.txt -e ".[signing-tests]"
LANGFUSE_TRACING_ENABLED=false python -B -m pytest tests/test_instruction_signing.py tests/test_instruction_signing_authority.py tests/test_instruction_signing_interop.py tests/test_instruction_inventory.py -q -p no:cacheprovider
uv lock --check
~~~

The pinned model-signing==1.1.1 checks exercise our profile seal through its
stock key-mode verification CLI, and its stock signing CLI through generic OMS
verification. Because that CLI has no profile-extension option, another test
adds the extension to its in-toto payload before its reference signer signs it,
then verifies the result with our strict release profile. Unicode resource names,
raw-digest ordering, PEM key hints and content changes are covered. Tests do not
use keyless signing, network verification or real owner keys.

The [Instruction signing workflow](../.github/workflows/instruction-signing.yml)
runs the focused tests on Linux, macOS and Windows. Passing them does not enable
protection of the daemon, updater, library or system files; implementation of
#237 through #243 remains necessary. Authority tests use temporary local state,
real ephemeral P-256 keys, fault injection and competing owners. Native Windows
jobs verify both private DACL creation and refusal of Everyone/NULL DACLs;
POSIX jobs verify modes and link refusal. No test enrolls the owner's live state.
