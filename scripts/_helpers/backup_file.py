"""Create a unique sibling backup without overwriting an existing snapshot."""
import argparse
import os
from pathlib import Path
import shutil
import sys
import tempfile


def backup_file(path: Path) -> Path:
    """Copy bytes into an exclusively reserved backup; remove partial copies."""
    with path.open("rb") as source:
        descriptor, name = tempfile.mkstemp(prefix=path.name + ".backup.", dir=path.parent)
        backup = Path(name)
        try:
            with os.fdopen(descriptor, "wb") as destination:
                shutil.copyfileobj(source, destination)
        except BaseException:
            try:
                backup.unlink(missing_ok=True)
            except OSError as error:
                print(f"WARNING: Could not remove incomplete backup {backup}: {error}", file=sys.stderr)
            raise
    return backup


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("path", type=Path)
    args = parser.parse_args()
    try:
        backup = backup_file(args.path)
    except OSError as error:
        print(f"ERROR: Failed to back up {args.path}: {error}", file=sys.stderr)
        return 1
    print(f"Backup created: {backup}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
