"""
Password policy and hashing for user accounts (ADR 0004, Scope for v1).

Policy: at least 12 characters, at most 256, and not on a list of common passwords. No
composition rules (no "must contain a digit"), because those rules push people to predictable
patterns. The common-password list is the SecLists 10k-most-common file, pinned to a commit
(see src/grounded_answers/data/README.md).

Hashing: Argon2id through argon2-cffi, with the library's default parameters. The defaults are
the RFC 9106 second recommended profile: time_cost=3, memory_cost=65536 KiB, parallelism=4,
hash_len=32, salt_len=16. Tests pin them, so a library upgrade that changes them is visible.
The hash string carries its own parameters, so old hashes still verify after a change.
"""

import functools
import secrets
from importlib import resources

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

MIN_LENGTH = 12
MAX_LENGTH = 256

_hasher = PasswordHasher()


class PasswordPolicyError(ValueError):
    """Raised with a message the operator can act on. Never contains the password."""


@functools.cache
def _common_passwords() -> frozenset[str]:
    """Load the list once per process. Entries are lowercase, one per line."""
    data = resources.files("grounded_answers").joinpath("data/common_passwords.txt")
    return frozenset(line.strip() for line in data.read_text(encoding="utf-8").splitlines())


def check_password_policy(password: str) -> None:
    """
    Raise PasswordPolicyError if the password breaks the policy. Return None if it passes.

    The common-password check compares casefolded text, so "Password1234" and "PASSWORD1234"
    are refused the same way as "password1234".
    """
    if len(password) < MIN_LENGTH:
        raise PasswordPolicyError(f"Password must be at least {MIN_LENGTH} characters.")
    if len(password) > MAX_LENGTH:
        raise PasswordPolicyError(f"Password must be at most {MAX_LENGTH} characters.")
    if password.casefold() in _common_passwords():
        raise PasswordPolicyError("Password is on the common-password list. Choose another.")


def hash_password(password: str) -> str:
    """Return an Argon2id hash string (includes salt and parameters). Call the policy first."""
    return _hasher.hash(password)


def verify_password(stored_hash: str, password: str) -> bool:
    """
    True only if the password matches the stored hash. A wrong password, or a stored value that
    is not a valid hash, returns False. Nothing else is raised to the caller.

    This is CPU-heavy. Call it from a worker thread in async code (auth_service.py).
    """
    try:
        return _hasher.verify(stored_hash, password)
    except (VerificationError, InvalidHashError):
        return False


def make_dummy_hash() -> str:
    """
    A hash of a random value, with the same parameters as real hashes (ADR 0004).

    Unknown emails are verified against this hash, so they cost the same as a wrong password.
    The hash is created once at startup, and its plaintext is discarded.
    """
    return _hasher.hash(secrets.token_urlsafe(32))
