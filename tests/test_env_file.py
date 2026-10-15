""".env bindings (src/env_file.py) and the installers' branch helper that uses them (#381).

Platform-neutral: the Windows installer job runs this file too (init_repo.bat calls
scripts/_helpers/env_branch.py as init_repo.sh does).
"""
import subprocess
import sys
from pathlib import Path

import pytest

from src import env_file

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts/_helpers/env_branch.py"
KEY = "AGENTS_AUTO_UPDATE_BRANCH"
SECRET = 'PRIVATE_KEY="-----BEGIN-----\nAGENTS_AUTO_UPDATE_BRANCH=x\n-----END-----"\n'

CORPUS = [
    "", "\n\n", "A=1", "A=1\n", "A=1\r\nB='x'\r\n", "K=v\r", SECRET + "AGENTS_AUTO_UPDATE_BRANCH=main # c\n",
    "export K='v'  # mine\n\n\n'K'=w\nbad line here\n=x\nK\n#c\n   \n", 'K="a\\nb"\nX=a#b\nY=a #b\n',
    "K='unterminated\nL=2\n", 'D="dev\nINJECTED=1"\n', "a b c\nK=1\r", "K = 'x' # c\nexport\tL=2\n",
]


@pytest.mark.parametrize("text", CORPUS)
def test_the_stdlib_parser_reads_like_python_dotenv(text):
    ours = env_file.parse(text)
    assert ours == env_file.dotenv_parse(text)
    assert "".join(binding.original for binding in ours) == text


def test_a_multi_line_secret_keeps_its_value_and_the_real_key_is_set():
    text = "A=1\n" + SECRET + "B=2\n"
    for parse in (env_file.parse, env_file.dotenv_parse):
        changed = env_file.rewrite_text(text, {KEY: "dev"}, parse)
        assert changed == text + f"{KEY}=dev\n"
        values = {binding.key: binding.value for binding in env_file.dotenv_parse(changed) if binding.key}
        assert values["PRIVATE_KEY"] == "-----BEGIN-----\nAGENTS_AUTO_UPDATE_BRANCH=x\n-----END-----"
        assert values[KEY] == "dev"
        # Removing the key never deletes a line inside the secret.
        assert env_file.rewrite_text(changed, {KEY: None}, parse) == text


def test_a_multi_line_value_of_the_key_itself_goes_whole():
    text = 'AGENTS_AUTO_UPDATE_BRANCH="dev\nINJECTED=1"\nB=2\n'
    changed = env_file.rewrite_text(text, {KEY: "main"})
    assert changed == "AGENTS_AUTO_UPDATE_BRANCH=main\nB=2\n"
    assert "INJECTED" not in {binding.key for binding in env_file.parse(changed)}


@pytest.mark.parametrize("text, updates, expected", [
    ("A=1\r\nAGENTS_AUTO_UPDATE_BRANCH=main\r\n", {KEY: "dev", "P": "main"},
     "A=1\r\nAGENTS_AUTO_UPDATE_BRANCH=dev\r\nP=main\r\n"),
    ("'AGENTS_AUTO_UPDATE_BRANCH'=feature  # mine\n", {KEY: "dev"}, "'AGENTS_AUTO_UPDATE_BRANCH'=dev  # mine\n"),
    ("export AGENTS_AUTO_UPDATE_BRANCH = \"main\"#c\nAGENTS_AUTO_UPDATE_BRANCH=x\n", {KEY: "dev"},
     "export AGENTS_AUTO_UPDATE_BRANCH = dev#c\n"),
    ("A=1\n\nAGENTS_AUTO_UPDATE_BRANCH=main\nB=2", {KEY: None}, "A=1\n\nB=2"),
    ("A=1", {KEY: "dev"}, "A=1\nAGENTS_AUTO_UPDATE_BRANCH=dev\n"),
    ("AGENTS_AUTO_UPDATE_BRANCH\n", {KEY: "dev"}, "AGENTS_AUTO_UPDATE_BRANCH=dev\n"),
])
def test_bindings_are_replaced_whole_and_line_endings_stay(text, updates, expected):
    assert env_file.rewrite_text(text, updates) == expected
    assert env_file.rewrite_text(text, updates, env_file.dotenv_parse) == expected


def test_write_atomic_keeps_crlf_the_mode_and_a_symlink(tmp_path):
    target = tmp_path / "real.env"
    target.write_bytes(b"A=1\r\n")
    if sys.platform != "win32":
        target.chmod(0o640)
        link = tmp_path / ".env"
        link.symlink_to(target)
    else:
        link = target
    env_file.rewrite(link, {KEY: "dev"})
    assert target.read_bytes() == b"A=1\r\nAGENTS_AUTO_UPDATE_BRANCH=dev\r\n"
    if sys.platform != "win32":
        assert link.is_symlink() and target.stat().st_mode & 0o777 == 0o640
    assert [path.name for path in tmp_path.iterdir() if path.name.startswith(".env.")] == []


@pytest.mark.parametrize("before, created, after", [
    ("A=1\nexport AGENTS_AUTO_UPDATE_BRANCH='main'  # mine\nAGENTS_AUTO_UPDATE_BRANCH=main\n", True,
     "A=1\nexport AGENTS_AUTO_UPDATE_BRANCH=dev  # mine\n"),
    ("A=1", False, "A=1\nAGENTS_AUTO_UPDATE_BRANCH=dev\n"),
    ("'AGENTS_AUTO_UPDATE_BRANCH'=dev\n", False, "'AGENTS_AUTO_UPDATE_BRANCH'=dev\n"),
    ("AGENTS_AUTO_UPDATE_BRANCH=main\nAGENTS_AUTO_UPDATE_BRANCH=\"dev\"\n", False,
     "AGENTS_AUTO_UPDATE_BRANCH=main\nAGENTS_AUTO_UPDATE_BRANCH=\"dev\"\n"),  # the last one wins: no warning
    ("AGENTS_AUTO_UPDATE_BRANCH=\nB=2\r\n", False, "AGENTS_AUTO_UPDATE_BRANCH=dev\nB=2\r\n"),  # empty is unset
    ("A=1\r\n" + SECRET.replace("\n", "\r\n"), False,
     "A=1\r\n" + SECRET.replace("\n", "\r\n") + "AGENTS_AUTO_UPDATE_BRANCH=dev\r\n"),
])
def test_the_helper_both_installers_call_sets_or_keeps_the_branch(tmp_path, before, created, after):
    """scripts/_helpers/env_branch.py, which init_repo.bat calls as init_repo.sh does."""
    env = tmp_path / ".env"
    env.write_bytes(before.encode())
    if sys.platform != "win32":
        env.chmod(0o640)
    result = subprocess.run([sys.executable, str(HELPER), str(env), "dev", *(["--created"] if created else [])],
                            capture_output=True, text=True, check=True)
    assert env.read_bytes().decode() == after and "WARNING" not in result.stdout
    if sys.platform != "win32":
        assert env.stat().st_mode & 0o777 == 0o640


def test_the_helper_and_set_branch_write_the_same_bindings(tmp_path):
    """Both writers of the setting agree: the same .env gives the same file."""
    from src import update_channel
    before = "A=1\r\n" + SECRET.replace("\n", "\r\n") + "'AGENTS_AUTO_UPDATE_BRANCH'=main  # c\r\n"
    helper, writer = tmp_path / "helper.env", tmp_path / "writer.env"
    helper.write_bytes(before.encode())
    writer.write_bytes(before.encode())
    subprocess.run([sys.executable, str(HELPER), str(helper), "dev", "--created"], capture_output=True, check=True)
    update_channel.rewrite_env(writer, {KEY: "dev"})
    assert helper.read_bytes() == writer.read_bytes()
    assert helper.read_bytes().decode() == before.replace("=main", "=dev")
