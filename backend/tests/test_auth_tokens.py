"""Key file and access-token rules (ADR 0004). No database needed."""

import base64
import hashlib
import hmac
import importlib.util
import json
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from grounded_answers.auth_keys import (
    KeyFileError,
    KeyRing,
    load_key_ring,
    new_private_key_pem,
    write_new_key_file,
)
from grounded_answers.auth_tokens import (
    AUDIENCE,
    ISSUER,
    InvalidAccessToken,
    issue_access_token,
    verify_access_token,
)

# PyJWT's typing does not allow None, but alg=none tokens are signed with no key.
UNSIGNED_KEY: Any = None


def _load_script_main() -> Callable[[list[str]], int]:
    """Load scripts/make_jwt_keys.py by path (scripts/ is not a package)."""
    path = Path(__file__).resolve().parents[1] / "scripts" / "make_jwt_keys.py"
    spec = importlib.util.spec_from_file_location("make_jwt_keys", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    main: Callable[[list[str]], int] = module.main
    return main


make_jwt_keys_main = _load_script_main()


@pytest.fixture
def key_file(tmp_path: Path) -> Path:
    path = tmp_path / "jwt-keys.json"
    write_new_key_file(path)
    return path


@pytest.fixture
def ring(key_file: Path) -> KeyRing:
    return load_key_ring(key_file)


def _claims(now: datetime | None = None, **overrides: Any) -> dict[str, Any]:
    issued = now or datetime.now(UTC)
    base: dict[str, Any] = {
        "sub": str(uuid.uuid4()),
        "tid": str(uuid.uuid4()),
        "role": "employee",
        "iat": issued,
        "exp": issued + timedelta(minutes=15),
        "iss": ISSUER,
        "aud": AUDIENCE,
        "jti": str(uuid.uuid4()),
    }
    base.update(overrides)
    return base


def _without(claims: dict[str, Any], name: str) -> dict[str, Any]:
    return {k: v for k, v in claims.items() if k != name}


def test_valid_token_verifies_and_returns_its_claims(ring: Any) -> None:
    user, tenant = uuid.uuid4(), uuid.uuid4()
    token = issue_access_token(ring=ring, user_id=user, tenant_id=tenant, role_hint="employee")
    claims = verify_access_token(token, ring)
    assert claims.user_id == user
    assert claims.tenant_id == tenant
    assert claims.role_hint == "employee"


def test_expired_token_is_refused(ring: Any, key_file: Path) -> None:
    long_ago = datetime.now(UTC) - timedelta(hours=1)
    token = issue_access_token(
        ring=ring, user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_hint="employee", now=long_ago
    )
    with pytest.raises(InvalidAccessToken, match="ExpiredSignatureError"):
        verify_access_token(token, ring)


def test_wrong_audience_is_refused(ring: Any) -> None:
    token = jwt.encode(
        _claims(aud="someone-else"),
        ring.signing_key,
        algorithm="EdDSA",
        headers={"kid": ring.active_kid},
    )
    with pytest.raises(InvalidAccessToken):
        verify_access_token(token, ring)


def test_wrong_issuer_is_refused(ring: Any) -> None:
    token = jwt.encode(
        _claims(iss="another-issuer"),
        ring.signing_key,
        algorithm="EdDSA",
        headers={"kid": ring.active_kid},
    )
    with pytest.raises(InvalidAccessToken):
        verify_access_token(token, ring)


def test_unknown_kid_is_refused_before_any_signature_check(ring: Any) -> None:
    token = jwt.encode(
        _claims(), ring.signing_key, algorithm="EdDSA", headers={"kid": "ed25519-0000000000000000"}
    )
    with pytest.raises(InvalidAccessToken, match="unknown kid"):
        verify_access_token(token, ring)


def test_alg_none_is_refused(ring: Any) -> None:
    token = jwt.encode(_claims(), UNSIGNED_KEY, algorithm="none", headers={"kid": ring.active_kid})
    with pytest.raises(InvalidAccessToken):
        verify_access_token(token, ring)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def test_hs256_signed_with_the_public_key_is_refused(ring: KeyRing) -> None:
    """
    Algorithm confusion: an attacker builds an HS256 token by hand, using the public key PEM as
    the HMAC secret. PyJWT refuses to sign that way itself, so the token is built directly.
    """
    public_pem = ring.verification_keys[ring.active_kid].public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    header = _b64(json.dumps({"alg": "HS256", "typ": "JWT", "kid": ring.active_kid}).encode())
    payload = _b64(
        json.dumps(
            {
                **_claims(),
                "iat": int(datetime.now(UTC).timestamp()),
                "exp": int((datetime.now(UTC) + timedelta(minutes=15)).timestamp()),
            }
        ).encode()
    )
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = _b64(hmac.new(public_pem, signing_input, hashlib.sha256).digest())
    with pytest.raises(InvalidAccessToken):
        verify_access_token(f"{header}.{payload}.{signature}", ring)


def _tamper_signature(token: str) -> str:
    """
    Change one character in the middle of the signature. The last character of a 64-byte signature carries
    only two significant bits, so changing it can break base64 decoding and hide the signature check.
    """
    head, payload, signature = token.split(".")
    middle = len(signature) // 2
    replacement = "A" if signature[middle] != "A" else "B"
    return f"{head}.{payload}.{signature[:middle]}{replacement}{signature[middle + 1 :]}"


def test_tampered_signature_is_refused(ring: Any) -> None:
    token = issue_access_token(
        ring=ring, user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_hint="employee"
    )
    with pytest.raises(InvalidAccessToken, match="InvalidSignatureError"):
        verify_access_token(_tamper_signature(token), ring)


def test_token_without_a_required_claim_is_refused(ring: Any) -> None:
    token = jwt.encode(
        _without(_claims(), "jti"),
        ring.signing_key,
        algorithm="EdDSA",
        headers={"kid": ring.active_kid},
    )
    with pytest.raises(InvalidAccessToken, match="MissingRequiredClaimError"):
        verify_access_token(token, ring)


def test_a_malformed_token_is_refused(ring: Any) -> None:
    with pytest.raises(InvalidAccessToken):
        verify_access_token("not-a-jwt", ring)


def test_rotation_old_key_still_verifies_until_removed(tmp_path: Path) -> None:
    """Rotation (RUNBOOK): add a key, make it active; the old token still verifies."""
    path = tmp_path / "keys.json"
    old_kid = write_new_key_file(path)
    old_ring = load_key_ring(path)
    old_token = issue_access_token(
        ring=old_ring, user_id=uuid.uuid4(), tenant_id=uuid.uuid4(), role_hint="employee"
    )

    new_kid, new_pem = new_private_key_pem()
    data = json.loads(path.read_text(encoding="utf-8"))
    data["keys"][new_kid] = new_pem
    data["active"] = new_kid
    path.write_text(json.dumps(data), encoding="utf-8")

    rotated = load_key_ring(path)
    assert rotated.active_kid == new_kid
    assert verify_access_token(old_token, rotated).user_id is not None

    del data["keys"][old_kid]
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(InvalidAccessToken, match="unknown kid"):
        verify_access_token(old_token, load_key_ring(path))


def test_key_file_label_must_match_its_public_key(tmp_path: Path) -> None:
    path = tmp_path / "keys.json"
    kid, pem = new_private_key_pem()
    path.write_text(json.dumps({"active": kid, "keys": {"ed25519-wrong": pem}}), encoding="utf-8")
    with pytest.raises(KeyFileError, match="does not match its public key"):
        load_key_ring(path)


def test_key_file_must_hold_ed25519_keys(tmp_path: Path) -> None:
    rsa_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = rsa_key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    ).decode("ascii")
    path = tmp_path / "keys.json"
    path.write_text(json.dumps({"active": "rsa", "keys": {"rsa": pem}}), encoding="utf-8")
    with pytest.raises(KeyFileError, match="not an Ed25519 key"):
        load_key_ring(path)


def test_active_kid_setting_must_match_the_file(key_file: Path) -> None:
    with pytest.raises(KeyFileError, match="does not match the 'active' kid"):
        load_key_ring(key_file, expected_active_kid="ed25519-0000000000000000")


def test_missing_key_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(KeyFileError, match="cannot read the key file"):
        load_key_ring(tmp_path / "absent.json")


def test_key_script_never_prints_key_material(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    target = tmp_path / "new-keys.json"
    assert make_jwt_keys_main(["--out", str(target)]) == 0
    output = capsys.readouterr().out
    assert "PRIVATE KEY" not in output
    assert str(target) in output
    assert json.loads(target.read_text(encoding="utf-8"))["active"] in output


def test_key_script_refuses_to_overwrite(tmp_path: Path) -> None:
    target = tmp_path / "keys.json"
    target.write_text("{}", encoding="utf-8")
    assert make_jwt_keys_main(["--out", str(target)]) == 1
    assert target.read_text(encoding="utf-8") == "{}"
