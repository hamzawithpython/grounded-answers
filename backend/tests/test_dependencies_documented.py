"""
docs/DEPENDENCIES.md lists every direct dependency of pyproject.toml by exact PyPI name.

An unverified near-name package (`httpx2`, for `httpx`) was in the scaffold and was not reviewed by exact name. This guard makes adding, removing or renaming a
direct dependency a visible, reviewed change: the list and the project must agree.
"""

import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PYPROJECT = ROOT / "backend" / "pyproject.toml"
DOC = ROOT / "docs" / "DEPENDENCIES.md"

_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*")
_ROW = re.compile(r"^\| `([^`]+)` \| (\w+) \| ([^|]+) \| (\S+) \|", re.MULTILINE)


def _normalise(name: str) -> str:
    """PEP 503: case-insensitive, and runs of - _ . are the same."""
    return re.sub(r"[-_.]+", "-", name).lower()


def _declared() -> set[str]:
    project = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    specs = list(project["project"]["dependencies"])
    for group in project.get("dependency-groups", {}).values():
        specs += [item for item in group if isinstance(item, str)]
    names = set()
    for spec in specs:
        match = _NAME.match(spec)
        assert match is not None, spec
        names.add(_normalise(match.group(0)))
    return names


def _listed() -> dict[str, tuple[str, str, str]]:
    """normalised name -> (group, license, PyPI link), as written in the table."""
    return {
        _normalise(m.group(1)): (m.group(2), m.group(3).strip(), m.group(4))
        for m in _ROW.finditer(DOC.read_text(encoding="utf-8"))
    }


def test_every_direct_dependency_is_listed_and_nothing_else() -> None:
    declared, listed = _declared(), set(_listed())
    assert declared - listed == set(), (
        f"in pyproject.toml but not in docs/DEPENDENCIES.md: {sorted(declared - listed)}"
    )
    assert listed - declared == set(), (
        f"in docs/DEPENDENCIES.md but not in pyproject.toml: {sorted(listed - declared)}"
    )


def test_each_row_links_to_the_pypi_project_of_the_same_name_and_names_a_license() -> None:
    for name, (_group, license_, link) in _listed().items():
        assert _normalise(link.rstrip("/").rsplit("/", 1)[-1]) == name, f"{name}: {link}"
        assert link.startswith("https://pypi.org/project/"), f"{name}: {link}"
        assert license_, name


def test_no_package_name_resembling_a_known_one_is_declared() -> None:
    """The guard behind the incident: `httpx2` must not come back by accident."""
    assert "httpx2" not in _declared()
    assert "httpx" in _declared()
