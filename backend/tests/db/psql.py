"""
Run one bootstrap or migration SQL file through psql (ADR 0006, one path everywhere).

The same files the operator runs are piped to psql on stdin, through PSQL_COMMAND. Any
verifier that appears in psql's error output is replaced before the error is raised.
"""

import subprocess
from collections.abc import Mapping
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]


def run_sql_file(
    psql: list[str],
    sql_file: Path,
    dbname: str,
    env: Mapping[str, str],
    redact: list[str],
    *,
    variables: Mapping[str, str] | None = None,
) -> None:
    """Pipe one SQL file to psql. On failure, raise with stderr, redacting verifiers."""
    extra = [arg for name, value in (variables or {}).items() for arg in ("-v", f"{name}={value}")]
    result = subprocess.run(  # noqa: S603 - PSQL_COMMAND is operator config from the env, not input (ADR 0006)
        [*psql, "-X", "-U", "postgres", "-d", dbname, "-v", "ON_ERROR_STOP=1", *extra, "-f", "-"],
        input=sql_file.read_bytes(),
        env=dict(env),
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=180,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", "replace")
        for value in redact:
            stderr = stderr.replace(value, "<redacted>")
        raise RuntimeError(
            f"{sql_file.name} failed in {dbname!r} (psql exit {result.returncode}):\n"
            f"{stderr[-2000:]}"
        )


def run_sql_query(psql: list[str], env: Mapping[str, str], sql: str) -> str:
    """Run one query through psql (against its maintenance database) and return its single value, unquoted."""
    result = subprocess.run(  # noqa: S603 - PSQL_COMMAND is operator config from the env, not input (ADR 0006)
        [
            *psql,
            "-X",
            "-U",
            "postgres",
            "-d",
            "postgres",
            "-At",
            "-v",
            "ON_ERROR_STOP=1",
            "-c",
            sql,
        ],
        env=dict(env),
        cwd=REPO_ROOT,
        capture_output=True,
        check=False,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError(f"psql query failed (exit {result.returncode})")
    return result.stdout.decode("utf-8", "replace").strip()
