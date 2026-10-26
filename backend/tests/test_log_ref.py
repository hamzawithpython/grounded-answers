"""The keyed hash that stands in for a tenant id in log lines (ADR 0033, decision 8)."""

import hashlib
import hmac
import re
import uuid

from grounded_answers.log_ref import REF_CHARS, tenant_log_ref

KEY = b"k" * 48
TENANT = uuid.UUID("12345678-1234-5678-1234-567812345678")


def test_known_answer() -> None:
    expected = hmac.new(
        KEY, b"log:tenant:12345678-1234-5678-1234-567812345678", hashlib.sha256
    ).hexdigest()
    assert tenant_log_ref(KEY, TENANT) == expected[:16]


def test_shape_is_sixteen_lowercase_hex_characters() -> None:
    ref = tenant_log_ref(KEY, TENANT)
    assert len(ref) == REF_CHARS == 16
    assert re.fullmatch(r"[0-9a-f]{16}", ref)


def test_it_never_contains_the_raw_id_in_any_common_form() -> None:
    ref = tenant_log_ref(KEY, TENANT)
    for form in (str(TENANT), TENANT.hex, str(TENANT).upper(), TENANT.hex[:8]):
        assert form not in ref


def test_the_same_inputs_give_the_same_value() -> None:
    assert tenant_log_ref(KEY, TENANT) == tenant_log_ref(KEY, TENANT)


def test_a_different_key_or_tenant_gives_a_different_value() -> None:
    other_tenant = uuid.UUID(int=TENANT.int + 1)
    assert tenant_log_ref(b"j" * 48, TENANT) != tenant_log_ref(KEY, TENANT)
    assert tenant_log_ref(KEY, other_tenant) != tenant_log_ref(KEY, TENANT)


def test_the_kind_prefix_separates_it_from_other_uses_of_the_key() -> None:
    other_use = hmac.new(KEY, f"tenant:{TENANT}".encode(), hashlib.sha256).hexdigest()[:16]
    user_use = hmac.new(KEY, f"log:user:{TENANT}".encode(), hashlib.sha256).hexdigest()[:16]
    assert tenant_log_ref(KEY, TENANT) not in (other_use, user_use)
