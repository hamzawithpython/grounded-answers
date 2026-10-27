"""
The provider policy, the metering vocabulary and the cap arithmetic (ADR 0033, Step 5a). No database needed.

The CHECK lists in migration 0017 are copies of lists in the code, because a migration must not import application
code. These tests compare the copies, so one cannot change without the other.
"""

import importlib.util
import re
from pathlib import Path
from types import ModuleType

import pytest

from grounded_answers.admin_service import AdminError, micro_to_usd, usd_to_micro
from grounded_answers.answering import policy, usage, validate
from grounded_answers.answering.messages import REFUSAL_MESSAGES

BACKEND = Path(__file__).resolve().parents[1]
MIGRATION = BACKEND / "alembic" / "versions" / "0017_answer_usage_settings.py"
MIGRATION_CODES = BACKEND / "alembic" / "versions" / "0018_more_invalid_codes.py"


def _load(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _migration() -> ModuleType:
    return _load("migration_0017", MIGRATION)


# ---- provider policy ----


def test_real_data_accepts_only_approved_providers() -> None:
    assert policy.check_providers(["anthropic"], "real") == ("anthropic",)
    with pytest.raises(policy.ProviderPolicyError, match="Not approved for real data: groq"):
        policy.check_providers(["groq"], "real")
    with pytest.raises(policy.ProviderPolicyError, match="gemini"):
        policy.check_providers(["anthropic", "gemini"], "real")


def test_fictional_data_accepts_every_known_provider_in_order() -> None:
    assert policy.check_providers(["groq", "gemini", "anthropic"], "fictional") == (
        "groq",
        "gemini",
        "anthropic",
    )


def test_unknown_duplicate_and_bad_class_are_refused() -> None:
    with pytest.raises(policy.ProviderPolicyError, match="Unknown provider"):
        policy.check_providers(["openai"], "fictional")
    with pytest.raises(policy.ProviderPolicyError, match="twice"):
        policy.check_providers(["groq", "groq"], "fictional")
    with pytest.raises(policy.ProviderPolicyError, match="Data class"):
        policy.check_providers([], "public")


def test_empty_list_is_valid_and_allows_nothing() -> None:
    assert policy.check_providers([], "real") == ()
    assert not policy.is_allowed("anthropic", [], "real")


def test_is_allowed_needs_the_list_and_the_class() -> None:
    assert policy.is_allowed("groq", ["groq"], "fictional")
    assert not policy.is_allowed("gemini", ["groq"], "fictional")  # not on the tenant's list
    assert not policy.is_allowed("groq", ["groq"], "real")  # on the list, refused for real data
    assert policy.is_allowed("anthropic", ["anthropic"], "real")


def test_approved_providers_are_known_providers() -> None:
    assert policy.APPROVED_FOR_REAL_DATA <= set(policy.KNOWN_PROVIDERS)
    # Groq and Gemini free tiers are demo-only (ADR 0031) until a person records otherwise in an ADR.
    assert policy.APPROVED_FOR_REAL_DATA == {"anthropic"}


# ---- the migration's lists match the code ----


def test_migration_provider_list_matches_known_providers() -> None:
    migration = _migration()
    assert re.findall(r"'([a-z]+)'", migration.PROVIDERS) == list(policy.KNOWN_PROVIDERS)


def test_migration_outcomes_and_codes_match_the_code() -> None:
    migration = _migration()
    assert tuple(migration.OUTCOMES) == usage.OUTCOMES
    # 0017 is applied and unchanged; 0018 replaced the code list, and its list is the current one.
    assert tuple(_load("migration_0018", MIGRATION_CODES).INVALID_CODES) == usage.INVALID_CODES
    assert tuple(migration.INVALID_CODES) == usage.INVALID_CODES[: len(migration.INVALID_CODES)]


def test_every_validation_code_is_on_the_invalid_code_list() -> None:
    source = Path(validate.__file__).read_text(encoding="utf-8")
    codes = set(re.findall(r'Invalid\("([a-z_]+)"\)', source))
    assert codes, "no validation codes found: the pattern in this test is out of date"
    assert codes <= set(usage.INVALID_CODES)
    assert "invalid_output" in usage.INVALID_CODES  # an unparsable reply (respond.py)


def test_every_refusal_kind_has_an_outcome() -> None:
    for kind in REFUSAL_MESSAGES:
        assert usage.outcome_for_refusal(kind) in usage.OUTCOMES
    assert "answered" in usage.OUTCOMES


# ---- the cap, in dollars and millionths of a dollar ----


@pytest.mark.parametrize(
    ("raw", "micro"),
    [("0.05", 50_000), ("0", 0), ("1", 1_000_000), ("0.000001", 1), (" 12.5 ", 12_500_000)],
)
def test_usd_to_micro(raw: str, micro: int) -> None:
    assert usd_to_micro(raw) == micro


@pytest.mark.parametrize("raw", ["abc", "", "0.0000001", "-1", "1000.01", "NaN", "Infinity"])
def test_usd_to_micro_refuses(raw: str) -> None:
    with pytest.raises(AdminError):
        usd_to_micro(raw)


def test_micro_to_usd_round_trips_the_demo_cap() -> None:
    assert micro_to_usd(50_000) == "0.050000"
    assert usd_to_micro(micro_to_usd(50_000)) == 50_000
