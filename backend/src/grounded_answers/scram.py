"""
SCRAM-SHA-256 password verifiers for database roles (ADR 0006 amendment).

The hashing is not written here. psycopg exposes libpq's PQencryptPasswordConn as
PGconn.encrypt_password(passwd, user, algorithm). libpq derives the verifier
(PBKDF2-HMAC-SHA256, random salt). We only check the input and the output format.

A verifier is not a secret in the same way as a password, but it is still kept out of
logs and command lines. Plaintext passwords are never stored by this module.
"""

import re

import psycopg

MIN_PASSWORD_LENGTH = 16

# SCRAM-SHA-256$<iterations>:<salt>$<StoredKey>:<ServerKey>, all base64
_VERIFIER_RE = re.compile(r"SCRAM-SHA-256\$\d+:[A-Za-z0-9+/=]+\$[A-Za-z0-9+/=]+:[A-Za-z0-9+/=]+")


def check_password(password: str) -> None:
    """
    Reject passwords this project does not accept for database roles.

    ASCII only: the bootstrap SQL is piped through PowerShell, and non-ASCII bytes can be
    changed on the way. Minimum length is the same as the app's policy for service roles.

    Raises:
        ValueError: if the password is not ASCII or is too short.
    """
    if not password.isascii():
        raise ValueError("password must be ASCII only (see ADR 0006)")
    if len(password) < MIN_PASSWORD_LENGTH:
        raise ValueError(f"password must be at least {MIN_PASSWORD_LENGTH} characters")


def is_scram_verifier(value: str) -> bool:
    """True if value has the exact SCRAM-SHA-256 verifier format (and nothing else)."""
    return _VERIFIER_RE.fullmatch(value) is not None


def build_verifier(conn: psycopg.Connection, role: str, password: str) -> str:
    """
    Return the SCRAM-SHA-256 verifier for role's password, computed by libpq.

    Args:
        conn: an open connection. libpq needs it to pick the server's settings.
        role: the role name, passed to libpq as the user.
        password: the plaintext password, checked by check_password first.

    Returns:
        A string such as SCRAM-SHA-256$4096:<salt>$<StoredKey>:<ServerKey>.

    Raises:
        ValueError: if the password fails check_password.
        RuntimeError: if libpq returns something that is not a SCRAM verifier.
    """
    check_password(password)
    raw = conn.pgconn.encrypt_password(
        password.encode("ascii"),
        role.encode("ascii"),
        b"scram-sha-256",
    )
    verifier = raw.decode("ascii")
    if not is_scram_verifier(verifier):
        raise RuntimeError("libpq did not return a SCRAM-SHA-256 verifier")
    return verifier
