# Instruction signing: an OpenSSF Model Signing profile for agent instructions

Status: draft for review, 2026-10-07. Not implemented.
Profile identifier: `agent-instructions/v1`.

## 1. Purpose

Agents-Core puts files from disk into model context: agent prompts, skills,
implants, rules, built-in flows and the personal library (`flows/.user`). Nothing
proves today that those bytes are the ones the maintainer released or the server
wrote. A line appended to `rules/rule-*.mdc` reaches every persona bundle in every
repository that an installation serves, and user sync carries a library edit to
every machine of the user.

This profile defines which files are sealed, where the seals live, what the signed
statement carries, and how the server applies a verification result before any
byte reaches a model.

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
| Release seal | The seal of an installation's tracked files, made with a release key |
| Library seal | The seal of one file of the personal library, made with a machine key |
| Trust store | The machine-local list of public keys whose seals are accepted |

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
  Covering the code lets the updater check a whole target tree. Runtime
  enforcement (§8) applies to the instruction paths: `agents/**`, `skills/**`,
  `implants/**`, `rules/**`, `flows/*.md` and `scripts/templates/**`.
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

All key material lives in the daemon state directory
(`~/Library/Application Support/Agents-Core/<id>/signing/`, mode `0700`). It never
goes into the library and never syncs.

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
- With GitHub squash merges, the merged tree equals the sealed tree only when the
  PR is up to date with `main`. Branch protection must therefore require an
  up-to-date branch and a CI check that verifies the seal with the published key.
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

## 8. Verification

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
store's revision, which every change to the store (a key added, removed or given
another kind) advances. A key's removal thus takes effect at the next read, never
through a cached `valid`. A test fails when a loader reads an instruction path
without `trusted_read`.

A verification has one of these results:

| Result | Meaning |
|---|---|
| `valid` | Every check passed |
| `unsigned` | No seal exists, or the seal does not cover the path |
| `tampered` | The digest of the bytes does not match the sealed digest |
| `untrusted` | The seal names a key that the trust store does not hold for its kind. Without the key the signature is not checked, so `untrusted` says nothing about it |
| `invalid` | The seal is malformed, the signature fails, or the extension breaks §6 |

What happens when the result is not `valid` and developer mode is off (the default):

| Where | Behavior |
|---|---|
| `run_flow`, `get_flow` | Error `instruction_unsigned`, `instruction_tampered`, `instruction_untrusted` or `instruction_invalid`. The content is not returned; `/ui` shows it and its diff to the owner |
| Persona bundle | The component is left out. The response carries `integrity_warnings`, and the footer carries a `⚠ unsigned` marker. A rule is never dropped silently |
| `components.json`, persona overlay | Ignored as a whole and reported: all components stay enabled, and the flow's own frontmatter persona applies |
| User sync | An incoming file without a `valid` seal from a trusted key is not merged. It goes to `.agents-sync/quarantine/`, and `/ui` lists it. Developer mode does not change this |
| Updater | A target commit whose release seal does not verify is not activated. The updater fast-forwards to the newest commit on the tracked branch whose seal verifies |

## 9. Signing (writers)

Every sanctioned write seals what it writes, under the library lock:

- `save_flow`;
- `delete_flow`, which also removes the seal;
- `set_flow_persona`;
- component toggles;
- edits and restores in `/ui`.

The file and its seal are replaced together. An interrupted pair is recorded in a
journal and repaired at the next start, so a crash is never reported as tampering.

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
  quarantine and the updater check stay on.

## 11. Rollout

1. **Warn.** Verification runs and reports, but refuses nothing. The first release
   seal and the pinned release key ship in this release.
2. **Adopt.** `/ui` lists the existing library, and "Adopt and seal" seals it with
   `origin: adopt`.
3. **Enforce.** The policy of §8 becomes the default once the release with steps 1
   and 2 has been out for one update cycle.

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
  restoring old text is a feature. The updater only fast-forwards.
- **Subprocesses.** The daemon starts `git`, `ssh-keygen` and `lsof` through
  `PATH`, which today includes user-writable directories. Absolute paths are a
  prerequisite for the separate-user design.

## 13. Implementation outline

- **Package `src/instruction_signing/`:**
  - `oms.py`: builds and verifies bundles with `cryptography`; `model-signing` is
    not needed at runtime;
  - `keys.py` and `trust.py`;
  - `policy.py`;
  - `reader.py`, which holds `trusted_read`;
  - `__main__.py`, with the commands `seal-release`, `verify`, `trust`, `adopt`
    and `developer-mode`.
- **Touch points:**
  - the loaders listed in §8 and the writers listed in §9;
  - `src/user_sync/merge.py` and `scope.py`: a seal travels with its file, and
    unverified files go to quarantine;
  - the prepare phase of `src/self_update.py`;
  - `src/daemon/flows_ui.py`: trust, adopt, developer mode and the banner;
  - `src/server.py`: warnings and the footer marker.
- **Dependencies:** pin `cryptography`, which is installed today only as a
  transitive dependency. Use `model-signing==1.1.1` as a test dependency.
- **Tests:**
  - interoperability with `model_signing` in both directions;
  - tampering, renaming and substitution across scopes;
  - untrusted and revoked keys;
  - sync quarantine;
  - expiry of developer mode;
  - a crash between writing a file and its seal.

## References

- OpenSSF Model Signing specification: <https://github.com/ossf/model-signing-spec>
- Reference implementation, `pip install model-signing`: <https://github.com/sigstore/model-transparency>
- An introduction to the OMS specification (OpenSSF, 2025-06-25):
  <https://openssf.org/blog/2025/06/25/an-introduction-to-the-openssf-model-signing-oms-specification/>
- NVIDIA, Verify Signed Agent Skills: <https://docs.nvidia.com/skills/signing-agent-skills>
