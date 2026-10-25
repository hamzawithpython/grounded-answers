"""
Run text extraction in a child process, with time and memory limits (ADR 0020, decision D-D).

The parent:
- starts the child as python -I -m grounded_answers.extract.child (no PYTHONPATH, no current directory);
- writes the file to the child's stdin, and reads at most MAX_OUTPUT_BYTES from its stdout;
- kills the whole process tree when the timeout passes or the output is too large;
- passes the memory limit only on Linux (production and CI). On Windows the limit is not enforced:
  the stdlib has no per-process memory limit there (documented as a development limit).

What the parent logs: the reason, and the exception's class name. Never stdout, never stderr,
never the file. stderr is read and discarded, so a traceback with document text cannot leak.
"""

import json
import logging
import os
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from typing import Any, Final

from grounded_answers.extract.child import from_json
from grounded_answers.extract.model import Extracted

logger = logging.getLogger(__name__)

REASON_TIMEOUT: Final = "timeout"
REASON_MEMORY: Final = "memory"
REASON_UNREADABLE: Final = "unreadable"
REASON_NO_TEXT: Final = "no_text"
REASONS: Final = frozenset({REASON_TIMEOUT, REASON_MEMORY, REASON_UNREADABLE, REASON_NO_TEXT})

TIMEOUT_SECONDS: Final[dict[str, float]] = {"pdf": 30.0, "docx": 10.0, "md": 5.0, "txt": 5.0}
MEMORY_LIMIT_BYTES: Final = 1024 * 1024 * 1024  # 1 GiB, Linux only (D-D)
MAX_OUTPUT_BYTES: Final = 50 * 1024 * 1024


class ExtractionFailed(Exception):
    """Extraction did not produce text. The reason is one of REASONS, and carries no document content."""

    def __init__(self, reason: str) -> None:
        if reason not in REASONS:
            raise ValueError("an extraction failure must use one of the four generic reasons")
        super().__init__(reason)
        self.reason = reason


# The only environment variables the child receives. The worker's environment holds WORKER_DATABASE_URL, which
# carries the worker's password, and the settings of the other processes. The child needs none of them (SECURITY.md,
# "Extraction child environment"). PATH finds the interpreter's DLLs, SYSTEMROOT is needed by Windows itself,
# and TEMP and TMP are where Python puts temporary files on Windows.
CHILD_ENVIRONMENT_NAMES: Final = ("PATH", "SYSTEMROOT", "TEMP", "TMP")


def child_environment(source: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for the child: only CHILD_ENVIRONMENT_NAMES that are set in source (default: os.environ)."""
    env = os.environ if source is None else source
    return {name: env[name] for name in CHILD_ENVIRONMENT_NAMES if name in env}


def _child_command(kind: str) -> list[str]:
    command = [sys.executable, "-I", "-m", "grounded_answers.extract.child", kind]
    if sys.platform == "linux":
        command.append(str(MEMORY_LIMIT_BYTES))
    return command


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Stop the child and everything it started, by the PID this run created."""
    if sys.platform == "win32":
        taskkill = os.path.join(os.environ["SystemRoot"], "System32", "taskkill.exe")
        # Fixed argument list; the only variable is this run's own child PID.
        result = subprocess.run(  # noqa: S603 - fixed argv; only variable is this run's PID
            [taskkill, "/T", "/F", "/PID", str(process.pid)],
            capture_output=True,
            check=False,
        )
        if result.returncode not in (0, 128):  # 128: the process had already exited
            logger.warning("extract.kill_failed returncode=%d", result.returncode)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass  # already gone
    process.wait()


def _read_capped(stream: Any, limit: int, sink: bytearray, overflow: threading.Event) -> None:
    while True:
        chunk = stream.read(65536)
        if not chunk:
            return
        if len(sink) + len(chunk) > limit:
            overflow.set()
            return
        sink.extend(chunk)


def _drain(stream: Any) -> None:
    """Read and discard. The child must never block on a full stderr pipe; nothing is kept or logged."""
    while stream.read(65536):
        pass


def _start(command: list[str]) -> subprocess.Popen[bytes]:
    options: dict[str, Any] = {}
    if sys.platform == "linux":
        options["start_new_session"] = (
            True  # its own process group, so the kill reaches its children
        )
    # The command is sys.executable, a fixed module, and a kind checked against TIMEOUT_SECONDS
    # by extract(). The file arrives on stdin, never on the command line.
    return subprocess.Popen(  # noqa: S603 - fixed module argv; file goes on stdin, not argv
        command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=child_environment(),
        **options,
    )


def _run_child(kind: str, data: bytes, timeout: float) -> dict[str, Any]:
    process = _start(_child_command(kind))
    stdout = bytearray()
    overflow = threading.Event()
    if process.stdin is None or process.stdout is None or process.stderr is None:
        raise RuntimeError("child process pipes were not created")

    def write_input() -> None:
        try:
            process.stdin.write(data)  # type: ignore[union-attr]
        except BrokenPipeError:
            pass  # the child exited before reading everything; its result decides the outcome
        finally:
            process.stdin.close()  # type: ignore[union-attr]

    threads = [
        threading.Thread(target=write_input, daemon=True),
        threading.Thread(
            target=_read_capped,
            args=(process.stdout, MAX_OUTPUT_BYTES, stdout, overflow),
            daemon=True,
        ),
        threading.Thread(target=_drain, args=(process.stderr,), daemon=True),
    ]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    while process.poll() is None:
        if overflow.is_set():
            # Checked while waiting: a child that filled the output cap is stopped at once,
            # not when its timeout runs out.
            _kill_tree(process)
            logger.info("extract.failed reason=%s", REASON_UNREADABLE)
            raise ExtractionFailed(REASON_UNREADABLE)
        if time.monotonic() > deadline:
            _kill_tree(process)
            logger.info("extract.failed reason=%s", REASON_TIMEOUT)
            raise ExtractionFailed(REASON_TIMEOUT)
        time.sleep(0.05)
    for thread in threads:
        thread.join(timeout=5)
    if overflow.is_set():
        logger.info("extract.failed reason=%s", REASON_UNREADABLE)
        raise ExtractionFailed(REASON_UNREADABLE)
    return _decode(bytes(stdout), process.returncode)


def _decode(stdout: bytes, returncode: int) -> dict[str, Any]:
    """The child's one JSON object, or an unreadable failure if there is none."""
    try:
        payload = json.loads(stdout.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        logger.info("extract.failed reason=%s exit=%d", REASON_UNREADABLE, returncode)
        raise ExtractionFailed(REASON_UNREADABLE) from None
    if not isinstance(payload, dict):
        logger.info("extract.failed reason=%s", REASON_UNREADABLE)
        raise ExtractionFailed(REASON_UNREADABLE)
    return payload


def extract(
    kind: str, data: bytes, *, run: Callable[[str, bytes, float], dict[str, Any]] = _run_child
) -> Extracted:
    """Extract one file in a child process. Raises ExtractionFailed with a generic reason."""
    if kind not in TIMEOUT_SECONDS:
        raise ValueError(f"no extractor for kind {kind!r}")
    payload = run(kind, data, TIMEOUT_SECONDS[kind])
    if payload.get("ok") is True:
        return from_json(payload["result"])
    reason = payload.get("reason")
    if reason not in REASONS:
        reason = REASON_UNREADABLE
    exception = payload.get("exception")
    logger.info(
        "extract.failed reason=%s exception=%s",
        reason,
        exception if isinstance(exception, str) else "-",
    )
    raise ExtractionFailed(reason)
