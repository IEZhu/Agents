# Instruction signing: instructions, system files and unsigned-change review

Status: draft for review, amended 2026-10-15. Not implemented.
Profile identifier: `agent-instructions/v1`; OMS v1.0 envelope unchanged.
Tracks [#234](https://github.com/IEZhu/Agents/issues/234), part of
[#233](https://github.com/IEZhu/Agents/issues/233).

This is an implementation contract, including the proposed files, interfaces and
commands below, not a description of shipped protection. The interoperability
observation from 2026-10-07 remains historical; it does not validate this amendment.

## 1. Purpose

Agents-Core puts files from disk into model context: agent prompts, skills,
implants, rules, built-in flows and the personal library (`flows/.user`). Nothing
proves today that those bytes are the ones the maintainer released or the server
wrote. A line appended to `rules/rule-*.mdc` reaches every persona bundle in every
repository that an installation serves, and user sync carries a library edit to
every machine of the user.

This profile defines which files are sealed, where the seals live, what the signed
statement carries, and how verification governs both model context and executable
installation content. System files include runtime code, entry points, startup and
update scripts, dependency manifests and security-affecting configuration. Signing
tracked code only at update time does not cover direct edits or new loadable files
in an existing installation.

An explicit audit must also review changes already introduced without a valid
signature, including changes made before protection was enabled. Integrity,
security findings, owner decisions, signing and activation are separate results:
no findings is neither proof of safety nor a replacement for a signature.

### 1.1 Threat model and scope

In scope are edits bypassing sanctioned writers, instruction injection through
sync, altered update targets, accidental corruption, and discovery/review of
existing unverified changes. Protection applies to Agents-Core installation and
library files, with explicit local selection of additional installation paths.

Arbitrary operating-system files, a privileged or separate-user protection
service, and a targeted attacker running as the daemon's OS user are outside this
profile. The OS, interpreter and independent bootstrap trust base remain explicit
assumptions (§8.2, §12). Sanctioned MCP library writes keep their existing signing
and attribution behavior; this is not a guarantee that those writes are safe.

## 2. Prior art and decision

- **OpenSSF Model Signing (OMS) v1.0** signs a directory tree. A seal is a
  Sigstore bundle whose DSSE envelope carries an in-toto statement listing every
  file with its digest. OMS supports bare keys, certificates and Sigstore keyless
  signing. The reference implementation is `model-signing` on PyPI (1.1.1,
  released 2025-10-10).
- **NVIDIA signs agent skills with OMS.** A detached `skill.oms.sig` at the top of
  a skill directory covers `SKILL.md` and every supporting file, and
  `model_signing verify certificate` checks it.
- Other efforts, such as the Skill Trust & Signing Service (Ed25519 over a SHA-256
  Merkle tree), are tools, not standards. AGENTS.md and MCP define no signing of
  instructions.

Decision: use OMS v1.0 unchanged as the envelope, so that any OMS verifier can
check these seals, and define only a profile on top of it: which files are sealed,
where seals live, what the signed extension carries and how a loader acts on the
result.

## 3. Terms

| Term | Meaning |
|---|---|
| Instruction file | A file whose bytes, or a decision derived from them, reach model context (§5) |
| Seal | One OMS bundle file, `*.oms.sig` |
| Release seal | The seal of the release resources and protected inventory, made with a pinned release key |
| Library seal | The seal of one file of the personal library, made with a machine key |
| Trust store | The machine-local public keys, permitted seal/root scopes and revision |
| System file | Installation code, entry point, script, dependency input or configuration that affects execution/security |
| Protected inventory | Release-authenticated paths, directory coverage, file types and metadata rules (§5.3) |
| Trusted bootstrap | Independently enrolled launcher/verifier and dependencies used before candidate application imports |
| Snapshot | Immutable captured bytes plus canonical inventory and identity (§14.1) |
| Trusted baseline | A verified signed release or explicitly trusted signed artifact, with authenticated file contents |
| Security review | Bounded analysis of snapshot data; yields `findings`, `no_findings` or `inconclusive` |

## 4. Envelope (OMS v1.0, unchanged)

A seal is a JSON Sigstore bundle:

```json
{
  "mediaType": "application/vnd.dev.sigstore.bundle.v0.3+json",
  "verificationMaterial": {
    "publicKey": {"hint": "<SHA-256 hex of the PEM SubjectPublicKeyInfo>"},
    "tlogEntries": []
  },
  "dsseEnvelope": {
    "payloadType": "application/vnd.in-toto+json",
    "payload": "<base64 of the statement>",
    "signatures": [{"sig": "<base64 DER ECDSA signature>", "keyid": ""}]
  }
}
```

The statement:

```json
{
  "_type": "https://in-toto.io/Statement/v1",
  "subject": [{"name": ".user", "digest": {"sha256": "<root digest>"}}],
  "predicateType": "https://model_signing/signature/v1.0",
  "predicate": {
    "serialization": {"method": "files", "hash_type": "sha256",
                      "allow_symlinks": false, "ignore_paths": []},
    "resources": [{"name": "common/review.md", "algorithm": "sha256", "digest": "<hex>",
                   "agent_instructions": {"profile": "agent-instructions/v1", "seal": "library",
                                          "origin": "mcp", "signer": "laptop",
                                          "signed_at": "2026-10-07T09:00:00Z",
                                          "workspace_key": "github.com-owner-repo"}}]
  }
}
```

Rules, taken from `model-signing` 1.1.1 and checked against it:

- `resources` are sorted by `name`. The subject digest is the SHA-256 of the
  concatenated raw resource digests in that order.
- The signature is ECDSA over the DSSE pre-authentication encoding:
  `"DSSEv1" SP len(type) SP type SP len(payload) SP payload`.
- Keys are ECDSA P-256 with SHA-256. OMS key mode uses NIST curves, and
  `model-signing` 1.1.1 fails on Ed25519 keys.
- `hint` is the SHA-256 of the PEM-encoded public key (SubjectPublicKeyInfo), as
  `model-signing` computes it.
- Producers set the subject `name` to the basename of the sealed root, as OMS
  requires. Verification does not compare it with the directory name, and accepts
  any non-empty value.
- The predicate v1.0 schema allows no fields at its top level besides
  `serialization` and `resources` (`additionalProperties: false`), but leaves
  resource descriptors open (`additionalProperties: true`), and OMS verifiers
  ignore unrecognized fields there. The extension in §6 is therefore a field of the
  first resource descriptor, so a schema-validating OMS verifier accepts the seal.

Checked on 2026-10-07: a seal built as above covered a subset of a directory and
carried the extension, then at the top level of the predicate. It passed
`model_signing verify key <root> --signature <seal> --public_key <pem> --ignore_unsigned_files`,
and the same command failed after one byte of a covered file changed. The check is
to be repeated with the extension on the resource descriptor before the first
release seal ships.

## 5. What is sealed

### 5.1 Installation: the release seal

- File: `instructions.oms.sig` at the installation root. Subject name: the
  installation directory's basename.
- Resources: every git-tracked file of the release commit, except the seal itself
  and the paths OMS excludes by default (`.git`, `.gitignore`, `.gitattributes`,
  `.github`), so that a default OMS verifier checks the same set. CI and branch
  protection cover the excluded workflow files (§7.1).
  Runtime enforcement (§8) covers instruction paths (`agents/**`, `skills/**`,
  `implants/**`, `rules/**`, `flows/*.md`, `scripts/templates/**`) and the
  protected system-file inventory (§5.3). It checks the observed file set, not
  just resources that happen to appear in a seal.
- A release seal is made from a clean checkout of exactly the tree that will be
  released (§7.1).

### 5.2 Personal library: library seals

- Root: the library root (`flows/.user`, or `AGENTS_USER_FLOWS_DIR`). Subject name:
  the root's basename, `.user` by default.
- One seal per file, at `.seals/<relative path>.oms.sig`. A library seal has
  exactly one resource, whose `name` is the file's path relative to the library
  root. That path binds scope and ID: a sealed `common/a.md` copied to
  `common/b.md` or `repos/<key>/a.md` does not verify.
- Sealed files:
  - `common/<id>.md` and `common/<id>.meta.json`;
  - `repos/<key>/<id>.md` and `repos/<key>/.repo.json`;
  - `personas/**/<id>.json`;
  - `components.json`.
- Not sealed in v1:
  - `.history/**`: a restore goes through `save_flow`, which seals the result;
  - `.agents-sync/**` and the library root files;
  - `.repo.local.json`: it is machine-local.

### 5.3 Protected system inventory

A release adds `integrity/protected-files.json`, schema
`agents-core/protected-files/1`, as an ordinary resource covered by its release
seal. It stores policy metadata as signed file content; the OMS envelope and
resource descriptor schema are not extended for file modes or directory rules.
It lists paths and metadata, not its own digest. Content digests remain in the
OMS resource list, avoiding a self-hash cycle.

| Inventory field | Required meaning |
|---|---|
| `schema` | Exact supported inventory schema; unknown versions are refused |
| `protected_roots` | Root-relative directories whose complete loadable file set is checked |
| `files` | Sorted unique entries: `path`, class (`instruction`, `runtime`, `configuration` or `dependency`), regular-file type and executable-permission policy |
| `exclusions` | Explicit paths with reason and load-path isolation rule; never an implicit wildcard bypass |
| `dependencies` | Dependency artifacts/content identities and the separately trusted environment boundary |

The mandatory release scope includes runtime/verifier code under `src/`, bridge
and daemon/MCP entry points, applicable startup/install/update scripts, dependency
manifests/lockfiles and shipped security configuration, as well as instructions.
The signed inventory enumerates the actual release paths; these categories are
requirements, not a claim that every installation has the same layout.

For each protected file, its inventory entry must match a release resource.
Verify required paths and metadata, then enumerate protected roots to detect
missing files and unexpected executable/loadable files, including untracked
modules, bytecode, native extensions and import/startup hooks. A favorable subset
verification, including stock OMS `--ignore_unsigned_files`, never establishes
complete protected coverage. A renamed path must verify at its new scope; no
unchanged digest can authorize a path substitution.

Paths use unambiguous root-relative `/` names. Reject absolute/drive/UNC paths,
empty or dot components, traversal, duplicate/platform-aliasing names and symlink
or reparse-point substitution in any component. Only supported regular files may
load. Record and check the required POSIX executable state; on Windows apply the
documented file-type/load policy rather than inventing POSIX permission meaning.
A malformed inventory or unsafe path cannot be allowed by warning mode.

Exclude logs, caches, generated indexes, `.history`, quarantine, secrets and
ordinary mutable state from release sealing. Exclusion does not authorize
execution: mutable directories, ignored files such as `.git` configuration and
excluded workflow content must not supply imports, startup hooks or verifier
configuration. Preserve isolated Git/tool execution (§12); CI and branch protection
cover excluded workflow files. An exclusion intersecting a load path needs either
an authenticated content check or removal from that load path.

A signed requirements file alone does not authenticate installed dependencies.
The bootstrap must authenticate the dependency content it will load, or use an
explicitly enrolled trusted environment outside the candidate tree. Record that
boundary and artifact/content identities; arbitrary mutable site-packages,
current-directory imports and network installation during verification are not
implicitly trusted.

### 5.4 Owner-selected additional coverage

The effective scope is the release minimum plus explicit machine-local owner
selections. The owner can include additional installation files/directories via
the trusted local controls (§7.3). Adding coverage never makes an uncovered file
valid: it remains `unsigned` until covered by an appropriately authorized seal.
Library keys cannot sign system code or replace the release minimum.

Store selections, exclusions, mode and accepted revision in authenticated
bootstrap policy state, separate from editable candidate configuration. Each
owner-authorized transition records the prior revision and new policy digest;
unrecorded edits, missing/corrupt records or rollback produce a diagnostic and
retain the last authenticated policy, or block protected use if none is usable.
Do not fall back to an empty inventory or a weaker mode. The bootstrap's accepted
state is part of the same explicit local trust boundary as its keys, not data
that an incoming installation, flow or sync payload can replace.

An owner can remove an optional selection only through that trusted transition;
removing release-mandatory coverage requires a new release-authorized inventory
and an explicit owner policy decision. MCP, sync and unsigned file edits cannot
perform either operation. This does not resist an attacker who can patch the
bootstrap and its accepted state as the same OS user (§12).

## 6. Profile extension

`agent_instructions` is a field of the first resource descriptor, in `name` order
(a library seal has only one), and is signed together with the rest of the
statement.

| Field | Value |
|---|---|
| `profile` | `agent-instructions/v1` |
| `seal` | `release` or `library` |
| `origin` | `release` for a release seal. For a library seal: `mcp`, `ui`, `cli`, `sync-merge` or `adopt` |
| `signer` | The key's label in the signer's trust store; for display only |
| `signed_at` | RFC 3339 UTC time |
| `workspace_key` | For `origin: mcp`, the caller's repository key (`src.user_flows.repo_key`). Never a local path |

A verifier of this profile rejects a seal in any of these cases:
- `profile` is unknown;
- the `seal` kind does not match the root it is applied to;
- a library seal lists more than one resource.

`signer` and `signed_at` carry no trust. Trust comes only from the key.

## 7. Keys and trust

Machine key material and the trust store live in the daemon state directory
(`~/Library/Application Support/Agents-Core/<id>/signing/`, mode `0700`). They never
go into the library or sync. Private release keys remain outside the installation
under maintainer custody (§7.1).

Each trust store entry records the seal kind its key may sign: `release` or
`library`. A release seal verifies only with a pinned key of kind `release`, and a
library seal only with a key of kind `library`. A machine key trusted for library
seals can therefore never pass as a release key, and a release key never signs a
library file.

### 7.1 Release keys

- The maintainer holds release keys outside the installation.
- The installer shows the fingerprint and pins the key in the trust store. The
  repository also publishes the public keys under `integrity/release-keys/`, but a
  published key counts only when it is pinned.
- `python -m src.instruction_signing seal-release` produces the release seal, which
  is committed with the change.
- Follow the [release procedure](releases.md#procedure): feature PRs target `dev`;
  release PRs promote `dev` to `main` with a merge commit. Seal the final intended
  release resource tree and verify the actual activation/publication tree again.
  Branch protection must require an up-to-date release PR and a seal/coverage
  check; a merge or later generated change that changes resources invalidates
  the seal. Never change the release merge method to make a seal pass.
- Rotation: a new release key is accepted only after the owner confirms its
  fingerprint in the CLI or `/ui`.
- Custody (decided 2026-10-07): the release key is a passphrase-protected
  P-256 PEM file on the maintainer's machine. Its passphrase is not cached in an
  agent or keychain, so `seal-release` asks for it once per release, and an agent
  that runs as the maintainer's user cannot sign without it. CI holds only the
  public key.
- Rejected alternative: a CI secret. It removes the prompt, but then anyone who
  can merge to `main` can release, including an agent that uses the maintainer's
  `gh` login.

### 7.2 Machine keys

- Generated on first start, as the sync key is in `src/user_sync/keys.py`: P-256,
  with `machine.key` (`0600`) and `machine.pub`.
- The machine seals every library write it makes (§9).
- Another machine's key enters the trust store only after the owner confirms its
  fingerprint, the same pattern sync uses for SSH host keys. When the remote holds
  seals from an unknown key, `/ui` shows that key with its fingerprint.
- Removing a key revokes everything it sealed: those files become `untrusted`.

### 7.3 Authorization, local controls and revocation

Trust entries bind a fingerprint to seal kind and allowed root scope. A valid
ECDSA signature from a library machine key cannot authorize an installation,
its release inventory or a reduction of protected policy. Unknown/revoked or
wrong-kind keys yield `untrusted`, not an automatic enrollment prompt in an
unattended operation. A corrupt store remains diagnosable and grants no trust.

Only explicit owner actions through the trusted local CLI or authenticated UI
may change trust, local selections or mode. The UI uses the same backend and
accepted policy revision; no MCP tool, synced value, report or approval grants
these capabilities. Mutations are atomic and recorded without key material.
Revocation advances the trust revision and invalidates cached checks and pending
activation authorization. Revalidate trust/policy immediately before protected
use, signing handoff and activation; an old review is not a revocation bypass.

## 8. Verification

### 8.1 Verified instruction bytes

Every loader uses one read path, `trusted_read(root, relative_path) -> bytes`.
The loaders are `src/utils/prompt_loader.py`, `src/engine/rules.py`,
`src/flows.py`, `src/user_flows.py`, `src/flow_persona.py` and
`src/component_toggles.py`. `trusted_read`:

1. reads the file once, with a size bound and without following symlinks;
2. finds the covering seal: the release seal for installation paths, or
   `.seals/<path>.oms.sig` for library paths;
3. verifies the seal in this order:
   1. the `hint` names a key in the trust store whose kind matches the seal (§7);
      the bundle carries only this fingerprint, so without such a key the
      signature cannot be checked, and the result is `untrusted`;
   2. the signature is valid with that key;
   3. the subject digest matches the resources;
   4. the extension follows §6;
   5. the resource `name` equals `relative_path`;
4. compares the resource digest with the SHA-256 of the bytes from step 1;
5. returns those same bytes, so nothing is re-read after the check.

Verified seals are cached by the SHA-256 of the seal file together with the trust
store's revision and effective policy/inventory revision, which every accepted
change advances. A key's removal thus takes effect at the next read, never
through a cached `valid`. A test fails when a loader reads an instruction path
without `trusted_read`.

A verification has one of these results:

| Result | Meaning |
|---|---|
| `valid` | Every check passed |
| `unsigned` | No seal exists, or the seal does not cover the path |
| `tampered` | Sealed content is missing/changed or required file metadata does not match the authenticated inventory |
| `untrusted` | The seal names a key that the trust store does not hold for its kind. Without the key the signature is not checked, so `untrusted` says nothing about it |
| `invalid` | The seal is malformed, the signature fails, or the extension breaks §6 |

These five integrity results are unchanged. Inventory/path-set problems carry
explicit reasons and cannot produce an overall `valid`: an uncovered addition is
`unsigned`, missing/changed sealed content or required file metadata is
`tampered`, malformed inventory or unsafe path is `invalid`, and wrong signing
authority is `untrusted`. Security
review outcomes (§14) never add a sixth integrity status.

### 8.2 Trusted startup, later use and recovery entry points

The initial installer enrolls a trusted launcher/verifier, its interpreter and
necessary verification dependencies, pinned public keys and accepted policy state
outside the mutable candidate runtime. Record their identities and location in
the installation's trust-base record. A candidate's copy of the verifier is a
protected system file, not authority to verify itself. Bootstrap replacement must
be staged and checked by the previously trusted bootstrap with pinned release
authority and an explicit local owner transition; a candidate cannot redefine its
own trust base. Initial enrollment is an explicit owner trust decision, not proof
obtained by executing an arbitrary downloaded installer.

Every daemon, stdio/MCP, updater and direct CLI entry point enters this bootstrap
before importing candidate application modules. The verifier starts with fixed
trusted dependency/import paths and an isolated environment: no candidate hooks,
user-site/startup customization, current-directory imports or inherited import
configuration executes first. OS/interpreter and separately enrolled dependency
content are outside the release-file guarantee and must be stated and checked as
trust assumptions. Do not claim `python -m src.instruction_signing` inside an
unverified tree establishes this boundary; proposed module commands require an
already verified runtime or the independent trusted entry point.

The bootstrap authenticates policy/trust and release inventory, then verifies the
protected file set and content before permitting the caller's operation (§8.3).
Use a verified immutable execution snapshot, or an equivalent mechanism that
ensures the bytes used are the bytes checked. A check followed by an ordinary
import from a mutable path is insufficient. Later imports, configuration reads,
script launches and activation must consume that snapshot or repeat the same
checks with race protection. A file-set or trust change cannot reuse an old seal
cache. Cached bytecode/native modules and dependency resolution are included in
this boundary, not silently loaded from unchecked alternate locations.

Audit and recovery must remain available through this independent trusted entry
point when normal daemon startup is blocked. The UI is optional presentation of
that service; never start the suspect daemon merely to show a report. The
implementation in #237 must document the concrete bootstrap packaging and prove
these properties on supported platforms before #243 enables enforcement.

### 8.3 Caller policy

The following table describes `enforce` when instruction developer mode is off.
Warning rollout exceptions are limited by §11; path safety, trusted audit
execution, updater activation and sync quarantine are not waived.

| Where | Behavior |
|---|---|
| `run_flow`, `get_flow` | Error `instruction_unsigned`, `instruction_tampered`, `instruction_untrusted` or `instruction_invalid`. The content is not returned; `/ui` shows it and its diff to the owner |
| Persona bundle | The component is left out. The response carries `integrity_warnings`, and the footer carries a `⚠ unsigned` marker. A rule is never dropped silently |
| `components.json`, persona overlay | Ignored as a whole and reported: all components stay enabled, and the flow's own frontmatter persona applies |
| User sync | An incoming file without a `valid` seal from a trusted key is not merged. It goes to `.agents-sync/quarantine/`, and `/ui` lists it. Developer mode does not change this |
| System startup or later protected use | Non-`valid` content or incomplete inventory blocks affected use before application imports/launch. Provide trusted audit/recovery without the suspect runtime |
| Updater | Activate only the exact verified target snapshot whose release seal, protected set and authority pass. The normal updater fast-forwards to a verifiable commit; it never runs unverified target installers/hooks to verify it |
| Review service | Inspect snapshot data only; generating a report neither authorizes loading the snapshot nor modifies trust, seals or active files |

## 9. Signing (writers)

Every sanctioned library write seals its authorized library scope, under the
library lock:

- `save_flow`;
- `delete_flow`, which also removes the seal;
- `set_flow_persona`;
- component toggles;
- edits and restores in `/ui`.

The file and its seal are replaced together. An interrupted pair is recorded in a
journal and repaired at the next start only after validating the recorded
sanctioned operation and intended hashes. An unrelated direct edit is not crash
repair and must remain unverified. Preserve evidence on failed repair.

`adopt` applies only to explicitly owner-approved library snapshots (§14); it
cannot traverse into system paths or create a release seal. Restored `.history`
bytes are data until their baseline verifies or an owner explicitly adopts the
exact library snapshot. Ordinary attributed MCP writes keep their current
sanctioned-writer behavior; a security review adds no release authority.

Some sync merges produce new content, such as the set merge of `components.json`.
The machine re-seals such a result with `origin: sync-merge` only after both
inputs have verified.

## 10. Developer mode

- Setting `allow_unsigned_instructions`, labelled "Developer mode: load unsigned
  instructions".
- Stored in the daemon state directory, not in the library, so sync can never
  switch it on.
- Changed only through `/ui` and the CLI. No MCP tool changes it.
- Switches itself off after 8 hours by default.
- While it is on, unsigned, tampered and untrusted local instructions load with
  warnings, `/ui` shows a banner, and every footer carries the marker. Sync
  quarantine and the updater check stay on. It never exempts system files,
  dependency loading, protected inventory/policy or bootstrap verification, and
  cannot change signing-key authority. It is not an adoption or approval action.

## 11. Rollout

1. **Warn.** Ship the first release seal, pinned key, protected inventory and
   trusted entry point. Verify before local instruction/system use and report
   non-valid content visibly; local use may continue with that warning. This
   exception is rollout policy, not instruction developer mode. Mandatory updater
   activation verification and sync quarantine remain on, and unsafe paths or
   suspect code in the audit service are never executed. Show effective coverage,
   exclusions and which content warning mode allowed.
2. **Audit and adopt.** Inventory existing installations and libraries, including
   pre-protection edits. Provide bounded snapshot review and trusted recovery
   (§14). "Adopt and seal" applies only to an explicitly approved library
   snapshot with `origin: adopt`. System changes require the release-signing
   workflow or restoration of a verified version; no migration auto-signs them.
3. **Enforce.** Make §8's refusal policy the default only after at least one update
   cycle with the first seal/adoption path and the expanded acceptance in §15.
   Missing baselines or incomplete review remain actionable and inconclusive;
   they do not silently disable protection or discard existing changes.

For example, a changed local startup script may run only with a warning during
stage 1, and must be blocked before execution under enforce. Turning on instruction
developer mode does not change that system policy. The same unsigned target must
never be activated by the updater or merged by sync in either mode. An unadopted
library receives one clear owner action; it is not mistaken for system-code
approval.

## 12. Security notes

- **Same-user processes.** While the daemon runs as the same OS user as the agents,
  any process of that user can read the machine key, change the state directory
  or patch the verifier. Seals then catch accidental and untargeted changes and
  stop injection through sync and updates. They do not stop a targeted local
  attacker. Running the daemon as a separate system user needs its own design.
- **API writes.** A prompt-injected agent can call `save_flow`, and the server seals
  that write like any other request. `origin: mcp` and `workspace_key` make the
  change visible in `/ui`, and `.history` lets the owner revert it.
- **Rollback.** An older library seal stays valid for its own bytes, because
  restoring old text is a feature. The normal updater only fast-forwards. Explicit
  owner recovery may select a previously verified signed version in a separate
  recovery operation (§14.3); it never rewrites the update branch or silently
  treats old local Git content as a trusted baseline.
- **Prior execution.** File review cannot establish that unsigned code which may
  already have run had no side effects. Preserve that limitation in the report;
  this workflow is not a host-compromise investigation or automatic cleanup.
- **Subprocesses.** The daemon starts `git`, `ssh-keygen`, `ssh-keyscan` and
  `lsof` by the absolute paths `install` recorded, preferring ones the user cannot
  replace, and runs with system directories as `PATH` (#244). A tool found only in
  a user-writable directory still runs from there, with a warning at install. Git on
  the installation repository runs without the user's global git configuration, and
  the service drops inherited `GIT_*` variables and `DEVELOPER_DIR` (#279). The
  original instruction-only design left interpreter startup processing of
  `PYTHONPATH` and other `PYTHON*` variables outside its protection. The amended
  trusted bootstrap (§8.2) must isolate interpreter startup and import resolution
  before candidate code in this same-user design too; deferring that isolation
  to a future separate-user service is not permitted. This requirement does not
  remove the targeted same-OS-user attacker limitation.

## 13. Implementation ownership and shared contracts

All names below are proposed implementation responsibilities, not shipped APIs.
Keep OMS operations in `src/instruction_signing/oms.py`, key/trust/policy logic in
the same package, instruction acquisition in `reader.py`, and a shared
snapshot/audit/report service with an independently trusted launcher. The final
bootstrap packaging belongs to #237 and must satisfy §8.2; placing everything in
an ordinary application module is insufficient.

| Issue | Responsibility and consumers |
|---|---|
| [#234](https://github.com/IEZhu/Agents/issues/234) | This amended specification, inventory/report/approval contracts and acceptance |
| [#235](https://github.com/IEZhu/Agents/issues/235) | Bounded OMS and protected-set verification primitives, five integrity results, coverage reasons |
| [#236](https://github.com/IEZhu/Agents/issues/236) | Key-kind/root authority, trust revisions and authenticated owner policy transitions |
| [#237](https://github.com/IEZhu/Agents/issues/237) | Trusted bootstrap/runtime verification, immutable snapshot capture, reusable audit/review/report backend and trusted CLI |
| [#238](https://github.com/IEZhu/Agents/issues/238) | Release inventory generation, authorized signing, full-coverage CI and approved-snapshot signing handoff |
| [#239](https://github.com/IEZhu/Agents/issues/239) | Scoped library writers, explicit adoption and validated crash repair |
| [#240](https://github.com/IEZhu/Agents/issues/240) | Verified system activation and known-good recovery, preserving evidence |
| [#241](https://github.com/IEZhu/Agents/issues/241) | Library-only sync, quarantine and explicit review; no system-policy authority |
| [#242](https://github.com/IEZhu/Agents/issues/242) | Coverage/review/decision UI consuming #237's backend, with CLI recovery when blocked |
| [#243](https://github.com/IEZhu/Agents/issues/243) | Platform integration, migration and the complete enforcement acceptance gate |

Dependency order is #234 → #235 → #236; #237 and #238 can then proceed in parallel
using authenticated test fixtures and agreed contracts. #239 uses #236/#237;
#240 uses #237/#238; #241 uses #237/#239; #242 uses their shared services; #243 gates
the complete result. Auditing does not invoke the signer or depend on the UI.

The shared interfaces must exchange verified bytes/path-set results, snapshot
and baseline identities, effective inventory/policy/trust revisions, reports,
exact-snapshot owner decisions and verified activation results. CLI and UI share
one implementation and authorization policy; no alternate UI-only scanner or
signer may bypass the trusted backend. No MCP tool exposes owner controls.
Proposed module commands remain `seal-release`, `verify`, `trust`, `adopt` and
`developer-mode`, plus audit/report and recovery operations through the trusted
entry point. Document actual syntax when implemented; do not suggest running the
candidate module to repair its own trust.

Pin `cryptography` for the implementation and `model-signing==1.1.1` for its
interoperability tests as already planned. Existing loader/writer touch points
remain those in §8.1/§9, plus startup/entry points, updater staging, sync scope and
quarantine, and the daemon UI. Preserve completed absolute-tool and isolated-Git
hardening from #244/#279. Passing this document's checks does not mean these
runtime changes have been implemented.

## 14. Audit, security review and owner decisions

### 14.1 Capture and baseline

The trusted service captures bounded, immutable evidence before remediation. It
must not import/execute the suspect tree, run its tests, build/install commands,
Git hooks or configuration helpers, or follow instructions found in its text.
Treat code, prompts, filenames, diffs and reports as untrusted data. Any optional
model-assisted review receives them as delimited evidence without execution,
secret access or authority to change the installation.

Review processing, including model-assisted analysis, is local by default. A
remote endpoint requires an explicit owner-approved destination and data scope;
MCP, sync and captured instructions cannot grant that approval. Before any
transmission, redact secrets from every outgoing input: captured code, prompts,
filenames, diffs, reports and associated metadata, logs or telemetry. Check the
exact outgoing payload after redaction; if it cannot be safely redacted or the
endpoint/scope is not approved, do not send it. A required check that cannot then
run remains unavailable and the review is `inconclusive`.

Keep original evidence private and local with its original snapshot hashes.
Record the approved destination, redaction method, redacted-view digest and
coverage limitations separately; never overwrite the captured bytes to produce
an apparent clean review. If redaction hides information needed by a required
check, preserve that incompleteness in the outcome or complete the check locally.
Approval to send a redacted view grants no signing or activation authority.

Compare against the last verifiable signed release or an explicitly trusted
signed artifact of the appropriate scope. Verify its seal with the pinned,
currently authorized key and obtain baseline file contents whose hashes match
that seal. A Git commit, a `.history` copy, a cached report, fresh hashes or a seal
without available authenticated baseline bytes is insufficient for a full diff.
Missing provenance or required baseline content makes review `inconclusive`;
retain independently verifiable integrity results and any concrete findings.

The capture manifest includes schema, sorted unique root-relative paths, regular
file/type and executable metadata, byte sizes and SHA-256 content digests. Capture
protected files, coverage anomalies and available baseline content, recording
added, modified, deleted and renamed paths with before/after hashes. Rename is a
reported relation, never signing authorization; ambiguous cases can remain an
addition and deletion. Record unreadable/unsupported input explicitly.

For identities, canonicalize the defined payload using
[RFC 8785 JCS](https://www.rfc-editor.org/rfc/rfc8785.html) and hash its UTF-8 bytes
with SHA-256. This profile permits strings, booleans, nulls and safe-range integer
numbers only; record fractional durations as integer milliseconds and larger
counters as decimal strings. Required fields cannot be omitted. Arrays of files
use canonical path order; report checks/findings use their stable IDs. Reject
duplicate keys, invalid Unicode and ambiguous paths rather than silently repairing
them. Do not normalize Unicode strings while computing an identity.

Exclude an object's own identity/digest field from its hashed payload; include
referenced snapshot/baseline/inventory identities. The resulting `snapshot_id`
binds captured contents and metadata, not their mutable location. Use this same
encoding for baseline, effective inventory and report identities. Validate payload
schemas and recompute identities before an approval is consumed; accepting a
self-declared ID is insufficient. These identity rules do not change the OMS
statement serialization or signature encoding in §4. Any capture race aborts or
yields inconclusive evidence; verify captured hashes again before consuming it.

### 14.2 Bounded review and report

Configure and record finite byte, file-count and elapsed-time limits, required
checks, reviewer/engine versions and settings. Check changed executable behavior,
command execution, privilege/secret access and exfiltration, dependency changes,
weakened authentication/signature/trust checks and instruction injection. A rule
finding is distinct from a failed review engine. Oversized, binary/unsupported,
unreadable, unavailable or skipped required analysis never becomes a clean result.

The machine-readable report contains:

| Field group | Required content |
|---|---|
| Identity | Report schema/version and digest, snapshot/baseline IDs and seal/key provenance, inventory/policy/trust revisions |
| Delta | Path and optional previous path, operation, before/after hash, integrity status and reason; incomplete coverage is explicit |
| Checks | Required check IDs, engine/version/settings, limits, actual completed/skipped/error/unavailable results and coverage |
| Security | Outcome, findings with severity/path/evidence, unresolved limitations and possible prior-execution flag |
| Provenance | Actual reviewer/method and decision references; no invented person, test result or approval |

Outcomes are `findings` when required analysis completed and found issues,
`no_findings` when required analysis completed without findings, and
`inconclusive` when a trusted baseline or any required check is unavailable,
skipped, errored or incomplete. Keep concrete findings even when overall coverage
is inconclusive. Every outcome is separate from per-file `valid`, `unsigned`,
`tampered`, `untrusted` and `invalid`. In particular, a benign unsigned edit with
`no_findings` remains unsigned. No scan certifies arbitrary content as safe.

Preserve original evidence privately with restricted access and bounded capture;
reports/logs/UI escape active content and redact secrets while retaining original
content hashes. Evidence retention must not become an executable load path or be
synced by default. Failures do not silently delete evidence. If unverified code
may have run, explicitly state that effects of prior execution are unknown.

### 14.3 Decision, signing and recovery

An audit alone changes no active file, key, policy, seal or authorization. The
owner can reject and preserve the snapshot, restore a verified known-good version,
or explicitly approve exact reviewed bytes for the appropriate signing workflow.
The decision records action, snapshot/baseline/inventory/report identities,
policy/trust revisions, actor and time. Approval is authenticated by the trusted
owner control channel; importing or syncing a report/decision grants no authority.

Approval is separate from signing and activation. Before consuming it, revalidate
all identities, current trust and policy and the exact bytes. Content, baseline,
inventory or required-check changes invalidate the decision and require a new
review; a revoked key blocks signing/activation authorization even with an old
approval. Record unresolved findings/inconclusive coverage explicitly in any
owner release decision; never present that decision as a successful safety check.
The initial release with no previous signed baseline needs an explicit maintainer
trust decision, not a fabricated clean comparison.

System changes proceed only through release-key signing (§7.1/#238) and verified
activation (#240); a local library key, UI approval or successful scan cannot
substitute. Library adoption remains limited to §9. Keep approvals outside the
snapshot so adding a decision does not mutate the approved bytes. Recheck the
signed output and activated snapshot against the approved content and inventory.

Restore/activation stages a complete verified version and switches atomically or
uses a documented crash-consistent transaction. Preserve the known-good copy and
review evidence on refusal, interruption or failed restore. Re-verify recovery
content with current key authority; a once-signed but now revoked baseline cannot
be used as trusted recovery. If the current installation is suspect, do not keep
serving it as the fallback: recover through the independent trusted environment
or remain blocked. Explicit owner recovery of an older signed version is distinct
from the normal fast-forward updater and does not rewrite its branch.

### 14.4 Presentation and authority examples

- A trusted signed runtime with complete coverage starts in enforce mode. An
  otherwise valid library-key seal over that runtime is `untrusted` for the
  installation and cannot start it.
- A new untracked module in a protected import directory is an uncovered addition
  even if all listed OMS resources verify. An unsigned inventory edit removing
  that directory cannot legitimize it.
- A no-findings report for unsigned code enables an informed owner decision, not
  execution. Editing a byte after approval makes that approval stale.
- No signed baseline, unavailable baseline contents or a timed-out required check
  yields `inconclusive`; known findings remain visible and no signing occurs
  automatically.
- UI and CLI show effective scope/exclusions, integrity and review outcomes,
  findings/limits and the separate decision/signing/activation steps. When the
  daemon is blocked, the same trusted CLI can audit and restore without it.

## 15. Acceptance and completion evidence

These are required implementation tests, not tests run by this documentation
change. Record supported-platform results (macOS, Windows and Linux), tested head,
actual commands, skipped scope and limits. Do not replace historical results with
unexecuted claims.

| ID | Required evidence | Owning issues |
|---|---|---|
| A1 | OMS sign/verify interoperability both ways with the extension on the resource descriptor; invalid/malformed/profile/key-kind cases preserve the five integrity statuses | #234, #235, #236, #238 |
| A2 | Required files, one-byte runtime/script/config/dependency edits, missing/renamed files and unexpected untracked loadable content are detected; a favorable OMS subset cannot clear complete coverage | #235, #237, #238 |
| A3 | Traversal, alias collisions, symlink/reparse substitution and executable-permission changes cannot evade inventory policy; exclusions do not supply unchecked imports/hooks | #234, #235, #237 |
| A4 | Startup, daemon/stdio, direct CLI and recovery verify before protected imports; a candidate verifier cannot authenticate itself; check/use races reject or consume already authenticated bytes | #237, #240 |
| A5 | Unsigned coverage reduction, corrupt/rolled-back policy, revoked/wrong-kind keys and stale caches cannot authorize system use; owner additions/removals follow explicit accepted transitions | #236, #237, #238 |
| A6 | Warn/enforce behavior matches §11; mandatory updater/sync verification stays on; instruction developer mode and its expiry never authorize system code | #237, #240, #241, #243 |
| A7 | A pre-existing unsigned snapshot yields an authenticated baseline/hash/diff report without running suspect code/hooks; all delta types and dependency/security/injection fixtures are covered | #237, #242 |
| A8 | No baseline, missing baseline bytes, unreadable/oversized inputs and timed-out/skipped/error required checks yield inconclusive coverage with retained findings; benign unsigned plus no findings stays unsigned | #237, #242, #243 |
| A9 | Service, trusted CLI and UI use one report/decision contract; previews grant no execution/authority; review is local by default and remote transfer requires owner-approved endpoint/scope plus pre-send redaction of code, prompts, filenames, diffs, reports and metadata; unsafe/unapproved transfer is blocked and incomplete required checks remain inconclusive without changing original snapshot hashes | #237, #242 |
| A10 | Reject/preserve, exact-snapshot approval, correct-key signing and verified restore/activation preserve evidence; post-review edits invalidate approval and revoked recovery keys block use | #238, #239, #240, #242 |
| A11 | Library adoption, journal repair and quarantine cannot seal system paths or modify policy; MCP/sync cannot import approvals, trust or developer-mode authority; normal verified-library behavior remains | #239, #241, #242 |
| A12 | Blocked-daemon recovery and interrupted/failed activation preserve a verified known-good copy; the suspect runtime is not the recovery tool or fallback | #237, #240, #242, #243 |

The amendment must be merged and indexed before dependent expanded behavior is
accepted. #243 and epic #233 are complete only after the new criteria across
#234–#242 pass with linked evidence, rollout prerequisites hold and the user/AI
operational documentation describes the implemented behavior and limitations.
Merging this specification alone completes neither the protection nor the epic.

## References

- RFC 8785, JSON Canonicalization Scheme: <https://www.rfc-editor.org/rfc/rfc8785.html>
- OpenSSF Model Signing specification: <https://github.com/ossf/model-signing-spec>
- Reference implementation, `pip install model-signing`: <https://github.com/sigstore/model-transparency>
- An introduction to the OMS specification (OpenSSF, 2025-06-25):
  <https://openssf.org/blog/2025/06/25/an-introduction-to-the-openssf-model-signing-oms-specification/>
- NVIDIA, Verify Signed Agent Skills: <https://docs.nvidia.com/skills/signing-agent-skills>
