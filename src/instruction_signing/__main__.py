"""Local owner controls, requiring an already trusted Python runtime (#237)."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import sys

from cryptography.exceptions import UnsupportedAlgorithm

from .authority import AuthorityStore, _public
from .oms import fingerprint
from .storage import AuthorityError


def _file(path, limit):
    # These are candidate inputs, never authority state or executable imports.
    with Path(path).open("rb") as stream:
        value = stream.read(limit + 1)
    if len(value) > limit:
        raise AuthorityError("Input exceeds its byte limit")
    return value


def _confirm(prompt):
    try:
        return input(prompt + " [yes/no]: ").strip().lower() == "yes"
    except EOFError:
        return False


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", help="Enrolled daemon state directory (contains signing/)")
    parser.add_argument("--installation", help="Enrolled installation root")
    commands = parser.add_subparsers(dest="command", required=True)
    initialize = commands.add_parser("init", help="Explicit first-start enrollment; never resets existing state")
    initialize.add_argument("--label", required=True, help="Owner-chosen machine label")
    initialize.add_argument("--library", required=True, help="Existing personal library root")
    trust = commands.add_parser("trust").add_subparsers(dest="operation", required=True)
    trust.add_parser("list")
    add = trust.add_parser("add")
    add.add_argument("public_key")
    add.add_argument("--label", required=True)
    add.add_argument("--release", action="store_true", help="Pin installation release authority instead of library authority")
    remove = trust.add_parser("remove")
    remove.add_argument("fingerprint")
    policy = commands.add_parser("policy").add_subparsers(dest="operation", required=True)
    policy.add_parser("show")
    accept = policy.add_parser("accept")
    accept.add_argument("--inventory", required=True)
    accept.add_argument("--release-seal", required=True)
    accept.add_argument("--include", action="append", default=[], help="Additional root-relative file/directory; repeat for several paths")
    accept.add_argument("--rollout-mode", choices=("enforce", "warn"), default="enforce")
    args = parser.parse_args(argv)
    try:
        from src.runtime_paths import installation_root
        from src.daemon.state import state_dir
        installation = args.installation or installation_root()
        store = AuthorityStore(args.state_dir or state_dir(installation), installation)
        if args.command == "init":
            library = Path(args.library).resolve(strict=True)
            print(f"Enroll installation: {store.installation}\nLibrary: {library}\nMachine label: {args.label}")
            if not _confirm("Create local signing authority?"):
                return 1
            snapshot = store.initialize(label=args.label, library_root=library,
                                        expected_library_root=library)
            print(f"Machine fingerprint: {snapshot.entries[0].fingerprint}")
            return 0
        snapshot = store.snapshot()
        if not snapshot.valid:
            raise AuthorityError(snapshot.diagnostic)
        if args.command == "trust":
            if args.operation == "list":
                print(f"Authority revision: {snapshot.revision} ({snapshot.digest})")
                for entry in snapshot.entries:
                    print(f"{entry.fingerprint}  {entry.kind}  {entry.label}\n  roots: {', '.join(entry.roots)}\n  added: {entry.added_at}; source: {entry.source}")
                return 0
            if args.operation == "add":
                key, pem = _public(_file(args.public_key, 16384))
                kind = "release" if args.release else "library"
                # The backend selects the exact enrolled root; input files do
                # not nominate their own authority or trust metadata.
                root = snapshot.installation_root if kind == "release" else snapshot.library_root
                print(f"Fingerprint: {fingerprint(key)}\nKind: {kind}\nRoot: {root}\nLabel: {args.label}")
                if not _confirm("Trust this public key for this scope?"):
                    return 1
                store.add_key(pem, kind=kind, label=args.label, expected_token=snapshot.token)
            else:
                print(f"Revoke: {args.fingerprint}")
                if not _confirm("Remove this key's signing authority?"):
                    return 1
                store.remove_key(args.fingerprint, expected_token=snapshot.token)
        elif args.operation == "show":
            print(json.dumps(store.policy(expected_token=snapshot.token), indent=2))
        else:
            inventory = _file(args.inventory, 4 * 1024 * 1024)
            seal = _file(args.release_seal, 8 * 1024 * 1024)
            preview = store.preview_policy(inventory_bytes=inventory, release_seal=seal,
                additional_paths=tuple(args.include), mode=args.rollout_mode, expected_token=snapshot.token)
            print(f"Inventory SHA-256: {hashlib.sha256(inventory).hexdigest()}\nPrior authority revision: {snapshot.revision}")
            print(json.dumps(preview, indent=2))
            if not _confirm("Accept this release minimum and these owner policy selections?"):
                return 1
            store.set_policy(inventory_bytes=inventory, release_seal=seal, additional_paths=tuple(args.include),
                             mode=args.rollout_mode, expected_token=snapshot.token)
        return 0
    except (OSError, ValueError, TypeError, UnsupportedAlgorithm) as error:
        print(f"Instruction authority: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
