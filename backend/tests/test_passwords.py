"""Password policy (ADR 0004) and Argon2id hashing defaults (ADR 0010). No database needed."""

import pytest
from argon2 import PasswordHasher, Type

from grounded_answers.passwords import (
    MAX_LENGTH,
    MIN_LENGTH,
    PasswordPolicyError,
    check_password_policy,
    hash_password,
)


def test_minimum_length_is_twelve() -> None:
    assert MIN_LENGTH == 12
    with pytest.raises(PasswordPolicyError, match="at least 12"):
        check_password_policy("x" * 11)
    check_password_policy("correct-horse-battery-staple"[:12])


def test_maximum_length_is_refused() -> None:
    with pytest.raises(PasswordPolicyError, match="at most 256"):
        check_password_policy("x" * (MAX_LENGTH + 1))


def test_common_password_from_the_list_is_refused() -> None:
    # "unbelievable" is 12 characters and is on the SecLists 10k list (see data/README.md).
    with pytest.raises(PasswordPolicyError, match="common-password list"):
        check_password_policy("unbelievable")


def test_common_password_check_ignores_case() -> None:
    with pytest.raises(PasswordPolicyError, match="common-password list"):
        check_password_policy("UnBelievable")


def test_no_composition_rules() -> None:
    # No digit, no symbol, no upper case required. A long plain phrase passes (ADR 0004).
    check_password_policy("correct-horse-battery")


def test_argon2_hash_uses_the_pinned_defaults() -> None:
    """Pins the library defaults we rely on (RFC 9106 second recommended profile)."""
    hasher = PasswordHasher()
    assert hasher.type == Type.ID
    assert hasher.time_cost == 3
    assert hasher.memory_cost == 65536
    assert hasher.parallelism == 4
    assert hasher.hash_len == 32
    assert hasher.salt_len == 16

    stored = hash_password("correct-horse-battery")
    assert stored.startswith("$argon2id$v=19$m=65536,t=3,p=4$")
    assert hasher.verify(stored, "correct-horse-battery")


def test_hashes_are_salted() -> None:
    assert hash_password("correct-horse-battery") != hash_password("correct-horse-battery")
