"""Every logger the worker imports is covered by the job-log redaction (ADR 0017), or is known to be web-only.

A logger outside JOB_LOGGERS has no handler in the worker, so its INFO lines are dropped. That is how purge.sweep went
missing from the worker's log (PR 9 follow-up). This test imports the worker's module chain in a fresh interpreter, so
the rest of the test session's imports cannot hide a gap, and it fails on any uncovered logger.
"""

import subprocess
import sys
from pathlib import Path

from grounded_answers.jobs.logging_filter import JOB_LOGGERS

# Loggers of modules that only the web app loads (auth, uploads, readiness, the document list). A new logger in the
# worker's chain is not in this list, so it must be added to JOB_LOGGERS instead.
WEB_ONLY_LOGGERS = (
    "grounded_answers.auth",
    "grounded_answers.upload",
    "grounded_answers.readiness",
    "grounded_answers.documents_list",
    "grounded_answers.rate_limit",
)

_PROBE = """
import logging, sys
import grounded_answers.jobs.worker  # the worker's whole module chain
for name, module in sorted(sys.modules.items()):
    if not name.startswith("grounded_answers"):
        continue
    logger = getattr(module, "logger", None)
    if isinstance(logger, logging.Logger):
        print(name, logger.name)
"""


def _covered(logger_name: str) -> bool:
    return any(
        logger_name == prefix or logger_name.startswith(prefix + ".") for prefix in JOB_LOGGERS
    )


def _web_only(logger_name: str) -> bool:
    return any(
        logger_name == prefix or logger_name.startswith(prefix + ".") for prefix in WEB_ONLY_LOGGERS
    )


def test_every_logger_in_the_worker_chain_is_covered_or_web_only() -> None:
    result = subprocess.run(  # noqa: S603 - fixed argv: this interpreter and a constant probe
        [sys.executable, "-c", _PROBE],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    loggers = [line.split(" ", 1)[1] for line in result.stdout.splitlines() if " " in line]
    assert loggers, "the probe must find the worker's loggers, or it proves nothing"
    uncovered = sorted({name for name in loggers if not _covered(name) and not _web_only(name)})
    assert uncovered == [], (
        f"loggers with no handler in the worker: {uncovered}. Add them to JOB_LOGGERS."
    )


def test_the_worker_logs_under_its_package_name_not_main() -> None:
    result = subprocess.run(
        [sys.executable, "-c", "import grounded_answers.jobs.worker as w; print(w.logger.name)"],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=True,
    )
    assert result.stdout.strip() == "grounded_answers.jobs.worker"
