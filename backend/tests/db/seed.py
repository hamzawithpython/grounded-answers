"""
Seed data for DB tests: two complete tenants, A and B.

Written as the cluster admin, the only role that bypasses RLS. Seeding is test setup, not app
behaviour. Each tenant has a row in every table, an allowed email domain, and users in both
roles. Tests compute expected results from the admin connection, never from hard-coded counts,
so the same seed works in any test order.
"""

import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import psycopg


@dataclass(frozen=True)
class TenantSeed:
    tenant: uuid.UUID
    admin: uuid.UUID  # active tenant_admin of this tenant
    employee: uuid.UUID  # employee of this tenant
    document: uuid.UUID
    version: uuid.UUID  # ready-or-queued version of document
    domain: str  # the tenant's allowed email domain


@dataclass(frozen=True)
class Seeded:
    a: TenantSeed
    b: TenantSeed
    # A user of tenant B whose email uses A's domain (for the cross-tenant duplicate case).
    user_b_at_a_domain_email: str


def _domain(label: str) -> str:
    return f"{label}-{secrets.token_hex(4)}.test"


def seed(admin_url: str) -> Seeded:
    """Insert both tenants. Runs as the cluster admin (bypasses RLS). Returns the ids."""
    now = datetime.now(UTC)
    ids = {
        k: uuid.uuid4()
        for k in (
            "ta",
            "tb",
            "admin_a",
            "emp_a",
            "admin_b",
            "emp_b",
            "emp_b_at_a",
            "doc_a",
            "ver_a",
            "doc_b",
            "ver_b",
        )
    }
    domain_a = _domain("acme")
    domain_b = _domain("globex")
    email = {
        "admin_a": f"admin.a.{ids['admin_a'].hex[:8]}@{domain_a}",
        "emp_a": f"emp.a.{ids['emp_a'].hex[:8]}@{domain_a}",
        "admin_b": f"admin.b.{ids['admin_b'].hex[:8]}@{domain_b}",
        "emp_b": f"emp.b.{ids['emp_b'].hex[:8]}@{domain_b}",
        "emp_b_at_a": f"dupe.{ids['emp_b_at_a'].hex[:8]}@{domain_a}",
    }

    with psycopg.connect(admin_url, autocommit=True) as admin:
        for key, name in (("ta", "Acme"), ("tb", "Globex")):
            admin.execute(
                "INSERT INTO grounded.tenants (id, name, slug) VALUES (%s, %s, %s)",
                (ids[key], name, f"{name.lower()}-{ids[key].hex[:8]}"),
            )
        for tenant, domain in (("ta", domain_a), ("tb", domain_b)):
            admin.execute(
                "INSERT INTO grounded.tenant_email_domains (id, tenant_id, domain) "
                "VALUES (%s, %s, %s)",
                (uuid.uuid4(), ids[tenant], domain),
            )
        for key in ("admin_a", "emp_a", "admin_b", "emp_b", "emp_b_at_a"):
            admin.execute(
                "INSERT INTO grounded.users (id, email, display_name) VALUES (%s, %s, %s)",
                (ids[key], email[key], key),
            )
        memberships = (
            ("admin_a", "ta", "tenant_admin"),
            ("emp_a", "ta", "employee"),
            ("admin_b", "tb", "tenant_admin"),
            ("emp_b", "tb", "employee"),
            ("emp_b_at_a", "tb", "employee"),
        )
        for user_key, tenant_key, role in memberships:
            admin.execute(
                "INSERT INTO grounded.tenant_memberships (id, tenant_id, user_id, role) "
                "VALUES (%s, %s, %s, %s)",
                (uuid.uuid4(), ids[tenant_key], ids[user_key], role),
            )
        for tenant_key, doc_key, ver_key, creator in (
            ("ta", "doc_a", "ver_a", "admin_a"),
            ("tb", "doc_b", "ver_b", "admin_b"),
        ):
            admin.execute(
                "INSERT INTO grounded.documents (id, tenant_id, title) VALUES (%s, %s, 'Policy')",
                (ids[doc_key], ids[tenant_key]),
            )
            admin.execute(
                """
                INSERT INTO grounded.document_versions (
                  id, tenant_id, document_id, version_no, original_filename, content_type,
                  size_bytes, sha256, storage_key, status, created_by
                ) VALUES (%s, %s, %s, 1, 'policy.txt', 'text/plain', 10, %s, %s, 'queued', %s)
                """,
                (
                    ids[ver_key],
                    ids[tenant_key],
                    ids[doc_key],
                    secrets.token_bytes(32),
                    f"tenant/{ids[tenant_key]}/{uuid.uuid4()}",
                    ids[creator],
                ),
            )
            admin.execute(
                "INSERT INTO grounded.audit_log (id, tenant_id, actor_user_id, action) "
                "VALUES (%s, %s, %s, 'seed')",
                (uuid.uuid4(), ids[tenant_key], ids[creator]),
            )
        for tenant_key, user_key in (("ta", "emp_a"), ("tb", "emp_b")):
            admin.execute(
                "INSERT INTO grounded.refresh_tokens (id, tenant_id, user_id, family_id, "
                "token_hash, issued_at, expires_at) VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    ids[tenant_key],
                    ids[user_key],
                    uuid.uuid4(),
                    secrets.token_bytes(32),
                    now,
                    now + timedelta(days=14),
                ),
            )
            admin.execute(
                "INSERT INTO grounded.password_reset_tokens (id, tenant_id, user_id, created_by, "
                "token_hash, expires_at) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    uuid.uuid4(),
                    ids[tenant_key],
                    ids[user_key],
                    ids["admin_a" if tenant_key == "ta" else "admin_b"],
                    secrets.token_bytes(32),
                    now + timedelta(days=1),
                ),
            )

    return Seeded(
        a=TenantSeed(
            tenant=ids["ta"],
            admin=ids["admin_a"],
            employee=ids["emp_a"],
            document=ids["doc_a"],
            version=ids["ver_a"],
            domain=domain_a,
        ),
        b=TenantSeed(
            tenant=ids["tb"],
            admin=ids["admin_b"],
            employee=ids["emp_b"],
            document=ids["doc_b"],
            version=ids["ver_b"],
            domain=domain_b,
        ),
        user_b_at_a_domain_email=email["emp_b_at_a"],
    )
