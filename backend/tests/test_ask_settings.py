"""The POST /ask settings (ADR 0033, decisions 3 and 6): defaults and bounds."""

import pytest

from grounded_answers.config import Settings


def _settings(**values: object) -> Settings:
    return Settings(_env_file=None, **values)  # type: ignore[arg-type]


def test_defaults_are_the_approved_values() -> None:
    settings = _settings()
    assert settings.ask_rate_user_per_min == 5
    assert settings.ask_rate_tenant_per_min == 20
    assert settings.ask_inflight_per_tenant == 2
    assert settings.ask_deadline_seconds == 12.0


@pytest.mark.parametrize(
    "bad",
    [
        {"ask_rate_user_per_min": 0},
        {"ask_rate_tenant_per_min": 0},
        {"ask_inflight_per_tenant": 0},
        {"ask_inflight_per_tenant": 21},
        {"ask_deadline_seconds": 4.9},
        {"ask_deadline_seconds": 30.1},
    ],
)
def test_bounds_are_enforced(bad: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        _settings(**bad)


def test_the_environment_can_set_them(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASK_DEADLINE_SECONDS", "20")
    monkeypatch.setenv("ASK_INFLIGHT_PER_TENANT", "3")
    settings = _settings()
    assert settings.ask_deadline_seconds == 20.0 and settings.ask_inflight_per_tenant == 3
