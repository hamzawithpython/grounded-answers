"""Verifier checks: input rules (no DB) and what PostgreSQL stored (DB)."""

import psycopg
import pytest

from grounded_answers.scram import check_password, is_scram_verifier

from .database import DbHandle

# Shape of a real verifier, with obviously fake (but well-formed) base64 fields.
FAKE_VERIFIER = "SCRAM-SHA-256$4096:c2FsdHNhbHRzYWx0c2FsdA==$" + "A" * 43 + "=:" + "B" * 43 + "="


def test_check_password_accepts_ascii_and_minimum_length() -> None:
    """A 16-character ASCII password is accepted."""
    check_password("abcdefghijklmnop")


@pytest.mark.parametrize(
    ("password", "message"),
    [
        ("short", "at least 16"),
        ("pässwörd-long-enough", "ASCII only"),
    ],
)
def test_check_password_rejects_short_and_non_ascii(password: str, message: str) -> None:
    """Non-ASCII bytes can be changed by PowerShell piping; short passwords are too weak."""
    with pytest.raises(ValueError, match=message):
        check_password(password)


def test_is_scram_verifier_accepts_the_real_format() -> None:
    """The exact SCRAM-SHA-256 layout passes."""
    assert is_scram_verifier(FAKE_VERIFIER)


@pytest.mark.parametrize(
    "value",
    [
        "plaintext-password-123",  # plaintext must never look like a verifier
        "MD5" + FAKE_VERIFIER,
        FAKE_VERIFIER + " trailing",
        "SCRAM-SHA-256$4096:@@@$x:y",
    ],
)
def test_is_scram_verifier_rejects_everything_else(value: str) -> None:
    """Anything that is not a SCRAM verifier is refused before it reaches ALTER ROLE."""
    assert not is_scram_verifier(value)


@pytest.mark.db
def test_stored_verifier_is_the_one_we_generated(grounded_db: DbHandle) -> None:
    """
    PostgreSQL stores the verifier as-is (ADR 0006). pg_authid is readable by the cluster
    admin. The stored value must equal the one libpq produced for each role.
    """
    with psycopg.connect(grounded_db.admin_url) as admin:
        rows: dict[str, str] = dict(
            admin.execute("SELECT rolname, rolpassword FROM pg_authid").fetchall()
        )
    for role, verifier in grounded_db.verifiers.items():
        assert rows[role] == verifier, f"{role}: stored verifier differs from generated"
