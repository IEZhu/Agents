"""Point a new installation's updaters at the branch it was installed from (#381).

Usage: python env_branch.py <env_file> <branch> [--created]

``install.sh`` passes ``AGENTS_BRANCH`` to ``init_repo.sh``, and ``init_repo.sh`` /
``init_repo.bat`` call this helper when it is set. With ``--created`` (the installer has
just copied ``.env`` from ``env.example``) or when ``.env`` has no
``AGENTS_AUTO_UPDATE_BRANCH``, the key is set to the branch, so ``AGENTS_BRANCH=dev``
gives an installation whose updaters follow dev. An existing ``.env`` that names another
branch is left as it is, with a warning: the owner chose it (``channel set``, the web UI).

``.env`` is changed binding by binding, as python-dotenv reads it and as
``src.update_channel.set_branch`` changes it (``src/env_file.py``, the same code): another
key's multi-line quoted value, the quoted-key form and the line endings stay as they are.

Standard library only: it runs before the virtual environment exists.
"""
import importlib.util
import os
import re
import sys


def _load_env_file():
    """This installation's ``src/env_file.py``, by its path: another ``src`` on ``sys.path`` must not answer."""
    root = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    spec = importlib.util.spec_from_file_location("agents_core_env_file", os.path.join(root, "src", "env_file.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


env_file = _load_env_file()

KEY = "AGENTS_AUTO_UPDATE_BRANCH"
# The names `src.update_channel.plausible_branch` accepts: written into .env unquoted.
NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]*")


def usable(branch):
    return bool(NAME.fullmatch(branch)) and ".." not in branch and not branch.endswith((".lock", "/", "."))


def main(argv):
    if len(argv) < 2 or argv[2:] not in ([], ["--created"]):
        print("Usage: env_branch.py <env_file> <branch> [--created]", file=sys.stderr)
        return 2
    path, branch, created = argv[0], argv[1], argv[2:] == ["--created"]
    if not usable(branch):
        print(f"  WARNING: AGENTS_BRANCH={branch!r} is not a usable branch name; {KEY} in .env is unchanged")
        return 0
    text = env_file.read_text(path)
    found = [binding for binding in env_file.parse(text) if binding.key == KEY]
    current = found[-1].value if found else None  # the last binding wins, as python-dotenv reads it
    if current and not created:
        if current != branch:
            print(f"  WARNING: .env keeps {KEY}={current}, but AGENTS_BRANCH is {branch}: the updaters follow "
                  f"{current}. To follow {branch} instead, run in the installation directory: "
                  f"python -m src.daemon channel set {branch} (python -m src.update_channel set {branch} "
                  "without the service)")
        return 0
    changed = env_file.rewrite_text(text, {KEY: branch})
    if changed != text:
        env_file.write_atomic(path, changed)
        print(f"  {KEY}={branch}: the updaters follow the branch this installation came from")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
