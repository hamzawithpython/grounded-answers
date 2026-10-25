"""
The extraction child gets no credentials and no settings (SECURITY.md, "Extraction child environment").

The worker's environment holds WORKER_DATABASE_URL, which carries the worker's password. Before this test existed,
subprocess.Popen passed that environment on to the child. These tests plant fake secrets in the parent's
environment, then start a real child through the production launcher and check what it can see. No database.
"""

import json
import subprocess
import sys

from pytest import MonkeyPatch

from grounded_answers.extract import sandbox

FAKE_CREDENTIAL = "FAKE-worker-password-7f3a9-must-not-appear"
FAKE_KEY = "FAKE-auth-signing-key-9c2e-must-not-appear"
FORBIDDEN_PREFIXES = (
    "WORKER_",
    "AUTH_",
    "DATABASE",
    "MIGRATION_",
    "APP_",
    "TEST_",
    "PSQL_",
    "REDIS",
    "VALKEY",
)


def test_child_environment_keeps_only_the_allowed_names() -> None:
    source = {
        "PATH": "/usr/bin",
        "SYSTEMROOT": "C:\\Windows",
        "TEMP": "C:\\Temp",
        "WORKER_DATABASE_URL": f"postgresql://worker_user:{FAKE_CREDENTIAL}@db/grounded",
        "AUTH_SIGNING_KEY": FAKE_KEY,
        "DATABASE_URL": "postgresql://app_user:x@db/grounded",
        "HOME": "/home/worker",
    }
    env = sandbox.child_environment(source)
    assert set(env) == {"PATH", "SYSTEMROOT", "TEMP"}
    assert FAKE_CREDENTIAL not in " ".join(env.values())


def test_the_child_process_sees_no_worker_or_auth_variables(monkeypatch: MonkeyPatch) -> None:
    """Start a real child with the production launcher and list the names it can read."""
    monkeypatch.setenv(
        "WORKER_DATABASE_URL", f"postgresql://worker_user:{FAKE_CREDENTIAL}@db/grounded"
    )
    monkeypatch.setenv("AUTH_SIGNING_KEY", FAKE_KEY)
    monkeypatch.setenv("DATABASE_URL", "postgresql://app_user:x@db/grounded")
    listing = "import json, os, sys; sys.stdout.write(json.dumps(sorted(os.environ)))"
    process = sandbox._start([sys.executable, "-I", "-c", listing])
    stdout, _stderr = process.communicate(timeout=30)
    assert process.returncode == 0
    seen = json.loads(stdout.decode("utf-8"))
    for name in seen:
        assert not name.startswith(FORBIDDEN_PREFIXES), f"the child can read {name}"
    assert FAKE_CREDENTIAL.encode() not in stdout
    assert FAKE_KEY.encode() not in stdout
    # CPython sets LC_CTYPE itself on Linux (PEP 538, locale coercion). It is not copied from the parent, and it holds
    # no credential, so it is the only name allowed beyond CHILD_ENVIRONMENT_NAMES.
    runtime_set = {"LC_CTYPE"}
    assert set(seen) - set(sandbox.CHILD_ENVIRONMENT_NAMES) <= runtime_set


def test_the_child_environment_keeps_only_names_that_are_set() -> None:
    """A name the parent does not have is left out, not filled in."""
    env = sandbox.child_environment({"PATH": "p", "NOISE": "n"})
    assert env == {"PATH": "p"}


def test_the_launcher_passes_the_environment_to_popen(monkeypatch: MonkeyPatch) -> None:
    """Regression guard: the launcher must pass env=, not inherit the parent's environment."""
    captured: dict[str, object] = {}

    class FakePopen:
        def __init__(self, command: list[str], **options: object) -> None:
            captured.update(options)

    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    sandbox._start(["unused"])
    assert captured["env"] == sandbox.child_environment()
