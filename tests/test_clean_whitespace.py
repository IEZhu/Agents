"""scripts/clean_whitespace.py leaves the installation's environments alone (#181)."""
import pytest

from scripts import clean_whitespace


def test_environments_under_venv_and_venvs_are_not_cleaned(tmp_path, monkeypatch):
    messy = "x = 1\n\n\n\n"
    for relative in (".venv/lib/site.py", ".venvs/py313-0123456789ab/lib/site.py", "src/module.py"):
        (tmp_path / relative).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / relative).write_text(messy)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as finished:
        clean_whitespace.main()
    assert finished.value.code == 1  # it changed a file
    assert (tmp_path / "src/module.py").read_text() == "x = 1\n"
    assert (tmp_path / ".venv/lib/site.py").read_text() == messy
    assert (tmp_path / ".venvs/py313-0123456789ab/lib/site.py").read_text() == messy
