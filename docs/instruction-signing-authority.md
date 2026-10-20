# Local signing authority

The #236 backend manages local keys, scoped trust and accepted policy. It requires
an already trusted runtime. Running a module from an unverified candidate tree
cannot authenticate its own imports. #237 supplies the independent bootstrap
and protected-use checks; no loader or daemon is gated by this backend yet.

## Owner controls

~~~bash
python -m src.instruction_signing init --label "work laptop" --library /absolute/library
python -m src.instruction_signing trust list
python -m src.instruction_signing trust add /absolute/other-machine.pub --label "home machine"
python -m src.instruction_signing trust add /absolute/release.pub --release --label "maintainer release"
python -m src.instruction_signing trust remove <full-fingerprint>
python -m src.instruction_signing policy show
python -m src.instruction_signing policy accept --inventory /absolute/protected-files.json --release-seal /absolute/instructions.oms.sig --include local/instructions --rollout-mode enforce
~~~

Global options --installation and --state-dir precede the command. The latter
is the daemon state directory, containing signing/. The default is the same
src.daemon.state.state_dir(installation) used by daemon and stdio. The trusted
bootstrap must pass its independently enrolled state location; candidate-provided
environment overrides are not enrollment authority. State inside the candidate
installation or selected library is refused.

init resolves the library root before confirmation and enrolls the displayed
canonical path, even if the original input symlink is retargeted during the
prompt. The backend compares its resolved enrollment value with that confirmed
path before creating state and persists the checked value; a changed binding
is refused. It creates
one P-256 machine.key and derived machine.pub. Its label is required and never
taken from the hostname. Installers
and first startup will explicitly call initialize with the owner label and
library root; ordinary reads never enroll or repair. The machine is enrolled
for the exact library root only. A separate random authority.key authenticates
control records; machine library signatures cannot authorize policy. Existing,
corrupt or incomplete enrollment is refused without replacing keys. Missing or
changed private/public key material blocks machine signing; it is not silently
regenerated. No private release key is stored here. Published public keys,
labels, seal signer fields and timestamps never automatically grant trust.

trust add displays the canonical OMS fingerprint (SHA-256 of P-256 SPKI PEM),
kind and exact enrolled root before requiring the literal answer yes. EOF or a
decline changes nothing. Entries contain canonical public keys, recomputed
fingerprints, labels, source and time. A fingerprint has one kind: release for
the enrolled installation, library for the enrolled library. The local machine
key cannot be promoted to release authority. Unknown, removed, wrong-kind or
wrong-root keys are untrusted; no approval receipt is another seal kind.

## Backend and consumers

AuthorityStore(state_directory, installation) exposes initialize, snapshot,
add_key, remove_key, verify, machine_key, preview_policy, set_policy and policy.
initialize accepts expected_library_root to bind enrollment to the canonical
root a trusted control displayed before asking for confirmation. Only trusted
owner controls may call mutations. These Python methods do not
themselves prove human intent, and no MCP endpoint exposes them. The later UI
must provide its own authenticated owner-action boundary. A same-user auto-login
is not proof of consent to repository instructions.

snapshot returns immutable entries, authority_id, enrolled roots, revision,
digest, policy_digest and immutable policy bytes. token is (revision, digest).
All trust and policy mutations advance this single generation, deliberately
invalidating both types of cached/pending decisions. Mutations and machine_key
require the exact expected_token. verify reads current authority each time,
selects keys by kind/root, runs strict profile verification and sets
authorization_checked only on valid. expected_token binds a check to an earlier
snapshot. It still returns seal evidence, not permission to run a protected file;
#237 must require usable policy and revalidate the current token immediately
before consuming verified bytes, signing handoff or activation. No result cache
or already-returned private-key object can enforce that later caller obligation.

preview_policy verifies the exact inventory bytes against a pinned release key
and checks that every inventory file is sealed, before an owner prompt. It shows
the prior/new policy and their digests. set_policy rechecks the same immutable
inputs and expected token under the lock. The release inventory, including its
exclusions, remains the mandatory minimum; additional_paths cannot remove it.
Removing a mandatory path needs a newly release-signed inventory and an explicit
owner transition. Changing optional selections or rollout mode also needs an
owner transition. Modes are enforce and the explicit warn rollout; neither grants
signing authority or disables updater/sync verification. Instruction developer
mode is a separate later control, not a way to edit this protected policy.
policy refuses absent/unusable policy and a revoked policy release key; it never
falls back to empty coverage or a weaker mode. Runtime file-set, dependency and
load-isolation validation still belongs to #237. Repository snapshot receipts,
guard enrollment and cross-chat deduplication belong to #463–#468, not these seals.

## Storage and failure behavior

signing/ and its revision directory are 0700 on POSIX; files, including private
keys, are 0600. Windows uses verified current-user-only DACLs, including inherited
child access. Existing loose modes/ACLs, symlinks/reparse components and file hard
links are refused without silently repairing them. A stable OS lock serializes
writers and permits simultaneous read-only snapshots through shared leases.
Acquisition retries contention for at most five seconds, then refuses; only
acquisition is retried, never the protected operation. Owner transitions recheck
their expected revision token after acquiring the exclusive lease. A
process-local locking fallback is refused.

State is a bounded chain of full JSON revisions authenticated by HMAC-SHA256,
each including its prior record digest and new policy digest. A revision record
is fsynced before atomic replacement of accepted.json. Reads check the retained
chain and accepted head. A crash between these publications, an edited record,
missing record, or rolled-back head grants no trust and preserves evidence. The
store blocks at 4,096 revisions, 2 MiB per record or 64 MiB of retained records;
it never drops revocations/history automatically. Trusted maintenance/recovery
must preserve the accepted boundary. Authority also becomes unavailable when
its remaining capacity cannot accommodate another record of the current state:
that conservative bound guarantees room for a one-key revocation while trust is
usable. It covers revision count, record size and retained bytes. At that boundary,
including stores already at a limit, snapshots grant no entries or token,
verification is untrusted, and signing handoff, policy use and further owner
transitions are refused. An accepted final transition can itself reach this
boundary and return an unavailable snapshot; no earlier trust remains usable.
The authenticated history and accepted head stay intact, and reads never write a
disable flag, discard history or reset enrollment. Trusted maintenance is needed
to restore availability. Within a usable store, identical explicit policy
acceptances still advance the generation, and explicit re-enrollment of the same
release fingerprint can restore the previously accepted policy.
snapshot also returns no entries and a diagnostic on corruption; mutations refuse
it. This design detects rollback of the accepted
head against retained records, not coordinated replacement of the entire local
boundary. Same-OS-user processes can read both keys and patch state/verifier;
permissions and local MACs do not provide a stronger isolation boundary.

See [the specification](instruction-signing-spec.md#7-keys-and-trust),
[the primitive API](instruction-signing-core.md) and
[the focused tests](../tests/test_instruction_signing_authority.py).
