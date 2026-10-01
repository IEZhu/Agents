import pytest

from src.utils.langfuse_compat import keys_configured


@pytest.mark.parametrize("public,secret,expected", [
    ("pk-lf-real", "sk-lf-real", True),
    ("", "", False),
    (None, "sk-lf-real", False),
    ("pk-lf-...", "sk-lf-...", False),  # placeholders from the old env.example
    ("# Optional: observability", "sk-lf-real", False),  # comment parsed as a value
    ("  pk-lf-real  ", "sk-lf-real", True),
])
def test_keys_configured_ignores_empty_and_placeholder_keys(public, secret, expected):
    assert keys_configured(public, secret) is expected
