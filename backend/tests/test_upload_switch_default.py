"""The upload switch's default, read without any .env file (a developer's backend/.env may turn it on)."""

import pytest
from pytest import MonkeyPatch

from grounded_answers.config import Settings


def test_the_default_is_off(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("UPLOAD_ENABLED", raising=False)
    assert Settings(_env_file=None).upload_enabled is False


@pytest.mark.parametrize("value, expected", [("true", True), ("false", False)])
def test_the_environment_sets_it(monkeypatch: MonkeyPatch, value: str, expected: bool) -> None:
    monkeypatch.setenv("UPLOAD_ENABLED", value)
    assert Settings(_env_file=None).upload_enabled is expected
