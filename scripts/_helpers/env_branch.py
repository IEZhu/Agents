"""Point a new installation's updaters at the branch it was installed from (#381).

Usage: python env_branch.py <env_file> <branch> [--created]

``install.sh`` passes ``AGENTS_BRANCH`` to ``init_repo.sh``, and ``init_repo.sh`` /
``init_repo.bat`` call this helper when it is set. With ``--created`` (the installer has
just copied ``.env`` from ``env.example``) or when ``.env`` has no
``AGENTS_AUTO_UPDATE_BRANCH``, the key is set to the branch, so ``AGENTS_BRANCH=dev``
gives an installation whose updaters follow dev. An existing ``.env`` that names another
branch is left as it is, with a warning: the owner chose it (``channel set``, the web UI).

Standard library only: it runs before the virtual environment exists.
"""
import os
import re
import sys
import tempfile

KEY = "AGENTS_AUTO_UPDATE_BRANCH"
# The names `src.update_channel.plausible_branch` accepts: written into .env unquoted.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")
# Every assignment form python-dotenv reads: `export KEY=`, `'KEY'=`, spaces around the key.
ASSIGNMENT = re.compile(r"^(?P<lead>\s*(?:export\s+)?(?:'" + KEY + r"'|" + KEY + r")\s*=[ \t]*)"
                        r"(?P<value>'[^']*'|\"[^\"]*\"|[^\s#]*)(?P<rest>.*)$", re.S)


def unquoted(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    return value


def main(argv):
    if len(argv) < 2 or argv[2:] not in ([], ["--created"]):
        print("Usage: env_branch.py <env_file> <branch> [--created]", file=sys.stderr)
        return 2
    env_file, branch, created = argv[0], argv[1], argv[2:] == ["--created"]
    if not NAME.fullmatch(branch) or ".." in branch or branch.endswith((".lock", "/", ".")):
        print(f"  WARNING: AGENTS_BRANCH={branch!r} is not a usable branch name; {KEY} in .env is unchanged")
        return 0
    with open(env_file, encoding="utf-8") as stream:
        lines = stream.readlines()
    found = [index for index, line in enumerate(lines) if ASSIGNMENT.match(line)]
    if found and not created:
        current = unquoted(ASSIGNMENT.match(lines[found[-1]])["value"])  # the last assignment wins
        if current != branch:
            print(f"  WARNING: .env keeps {KEY}={current}, but AGENTS_BRANCH is {branch}: the updaters follow "
                  f"{current}. To follow {branch} instead, run in the installation directory: "
                  f"python -m src.daemon channel set {branch} (python -m src.update_channel set {branch} "
                  "without the service)")
        return 0
    if found:
        for index in found[1:]:
            lines[index] = None
        match = ASSIGNMENT.match(lines[found[0]])
        if unquoted(match["value"]) == branch and len(found) == 1:
            return 0
        lines[found[0]] = match["lead"] + branch + match["rest"]
    else:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"{KEY}={branch}\n")
    mode = os.stat(env_file).st_mode & 0o7777
    descriptor, staging = tempfile.mkstemp(prefix=".env.", dir=os.path.dirname(os.path.abspath(env_file)))
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write("".join(line for line in lines if line is not None))
        os.chmod(staging, mode)
        os.replace(staging, env_file)
    except BaseException:
        os.unlink(staging)
        raise
    print(f"  {KEY}={branch}: the updaters follow the branch this installation came from")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
