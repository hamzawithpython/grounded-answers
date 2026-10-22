"""ORM model rules that need no database (ADR 0005)."""

from sqlalchemy import CheckConstraint
from sqlalchemy.orm import configure_mappers

from grounded_answers.models import Base, User


def test_every_relationship_is_lazy_raise() -> None:
    """Reading an unloaded relationship must raise, never run a silent query."""
    configure_mappers()
    relationships = [
        (mapper.class_.__name__, rel.key, rel.lazy)
        for mapper in Base.registry.mappers
        for rel in mapper.relationships
    ]
    assert relationships, "expected at least one relationship to test"
    assert [r for r in relationships if r[2] != "raise"] == []


def test_users_password_hash_and_updated_at_are_deferred() -> None:
    """app_user cannot SELECT these columns, so a plain select(User) must not name them."""
    columns = User.__mapper__.column_attrs
    assert columns["password_hash"].deferred is True
    assert columns["updated_at"].deferred is True
    assert columns["email"].deferred is False


def test_every_mapper_returns_server_defaults_on_insert() -> None:
    """Without this, an attribute such as created_at is expired after a flush (async lazy load)."""
    for mapper in Base.registry.mappers:
        assert mapper.eager_defaults is True, mapper.class_.__name__


def test_models_declare_no_check_constraints() -> None:
    """CHECKs live in migrations and in tests/db/expected_checks.json only (decision B1)."""
    declared = [
        constraint.name
        for table in Base.metadata.tables.values()
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint)
    ]
    assert declared == []
