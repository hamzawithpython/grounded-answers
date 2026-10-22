"""
Ed25519 signing keys for access tokens (ADR 0004, Key management).

The key file is JSON, outside the repository:

    {"active": "<kid>", "keys": {"<kid>": "<PKCS#8 PEM private key>", ...}}

- The kid is derived from the public key: "ed25519-" plus the first 16 hex characters of
  SHA-256(raw public key). A kid therefore cannot name a different key, and two keys cannot
  share one. The loader checks this and refuses a mismatch.
- Every key in the file verifies tokens. Only "active" signs them. Rotation is described in
  docs/RUNBOOK.md.
- Nothing here logs or prints key material. Errors name the problem, never a key.
"""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)


class KeyFileError(RuntimeError):
    """The key file is missing, malformed, or inconsistent. The message never contains a key."""


def derive_kid(public_key: Ed25519PublicKey) -> str:
    raw = public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    return "ed25519-" + hashlib.sha256(raw).hexdigest()[:16]


@dataclass(frozen=True)
class KeyRing:
    """The signing key (active) and every verification key, by kid."""

    active_kid: str
    signing_key: Ed25519PrivateKey  # gitleaks:allow type annotation, not a secret value
    verification_keys: dict[str, Ed25519PublicKey]


def build_key_ring(keys: dict[str, Ed25519PrivateKey], active_kid: str | None = None) -> KeyRing:
    """Build a ring from loaded private keys. Used by the file loader and by tests."""
    if not keys:
        raise KeyFileError("the key file has no keys")
    verification: dict[str, Ed25519PublicKey] = {}
    for kid, private in keys.items():
        public = private.public_key()
        if derive_kid(public) != kid:
            raise KeyFileError(f"key label '{kid}' does not match its public key")
        verification[kid] = public
    chosen = active_kid if active_kid is not None else next(iter(keys))
    if chosen not in keys:
        raise KeyFileError("the active kid is not one of the keys in the file")
    return KeyRing(
        active_kid=chosen,
        signing_key=keys[chosen],
        verification_keys=verification,
    )


def load_key_ring(path: Path, expected_active_kid: str | None = None) -> KeyRing:
    """Read the key file. Refuses a missing file, a non-Ed25519 key, or a bad label."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise KeyFileError(f"cannot read the key file ({type(exc).__name__})") from exc
    try:
        data: Any = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise KeyFileError("the key file is not valid JSON") from exc
    if not isinstance(data, dict) or not isinstance(data.get("keys"), dict):
        raise KeyFileError("the key file must hold a 'keys' object")

    private_keys: dict[str, Ed25519PrivateKey] = {}
    for kid, pem in data["keys"].items():
        if not isinstance(kid, str) or not isinstance(pem, str):
            raise KeyFileError("every key must be a string label with a PEM value")
        try:
            loaded = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
        except (ValueError, TypeError, UnicodeEncodeError) as exc:
            raise KeyFileError(f"key '{kid}' is not a valid PEM private key") from exc
        if not isinstance(loaded, Ed25519PrivateKey):
            raise KeyFileError(f"key '{kid}' is not an Ed25519 key")
        private_keys[kid] = loaded

    file_active = data.get("active")
    if not isinstance(file_active, str):
        raise KeyFileError("the key file must name the 'active' kid")
    if expected_active_kid is not None and expected_active_kid != file_active:
        raise KeyFileError("AUTH_JWT_ACTIVE_KID does not match the 'active' kid in the key file")
    return build_key_ring(private_keys, active_kid=file_active)


def new_private_key_pem() -> tuple[str, str]:
    """Generate one Ed25519 key. Returns (kid, PKCS#8 PEM). Used by the key script."""
    private = Ed25519PrivateKey.generate()
    pem = private.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    return derive_kid(private.public_key()), pem


def write_new_key_file(path: Path) -> str:
    """
    Create a new key file with one active key. Returns the kid. Never overwrites a file.

    The exclusive-create mode ("x") makes an existing file an error, so a rerun cannot silently
    replace a key that tokens still depend on.
    """
    kid, pem = new_private_key_pem()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump({"active": kid, "keys": {kid: pem}}, handle, indent=2)
        handle.write("\n")
    return kid
