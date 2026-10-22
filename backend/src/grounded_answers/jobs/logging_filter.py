"""
Remove job arguments from Procrastinate's log records, and keep the outcome (ADR 0017).

Procrastinate writes each job's arguments into three places: the message text (call_string, as
name[id](key=value, ...)), the job's call_string field, and the job's task_kwargs field. Ids in
those arguments are tenant and document ids, so none of them may reach a log. The filter blanks
the values and keeps everything else: the job id, the task name, the status, the timing and the
action. Records are never dropped, so the outcome of every job is still logged.

The filter sits on the handler, not on a logger. A logger's filter does not apply to records from
its child loggers, and Procrastinate logs from procrastinate.worker, procrastinate.manager, and so on.
"""

import logging
import re
from typing import Any

REDACTED = "<redacted>"

# name[id](args): the arguments end at the first ')' that is followed by a space or the end.
CALL_ARGS = re.compile(r"(\[\d+\])\((.*?)\)(?=\s|$)")
# The key of each keyword argument, as written by Python's repr of the call: key=value.
ARG_KEY = re.compile(r"(\w+)=")


def _redact_arguments(arguments: str) -> str:
    """Keep each key and drop every value. Values are never copied, even when they hold commas."""
    return ", ".join(f"{key}={REDACTED}" for key in ARG_KEY.findall(arguments))


def redact_call_string(text: str) -> str:
    """Blank the values of every name[id](key=value, ...) in the text. The keys stay."""
    return CALL_ARGS.sub(
        lambda match: f"{match.group(1)}({_redact_arguments(match.group(2))})", text
    )


# Attributes every LogRecord has. The filter leaves them alone (exc_info and exc_text included).
_STANDARD_ATTRIBUTES = frozenset(vars(logging.LogRecord("", 0, "", 0, "", (), None))) | {
    "message",
    "asctime",
    "taskName",
}


def _redact_value(value: Any) -> Any:
    """Walk an extra field, at any depth. Blank task_kwargs values, and redact every string."""
    if isinstance(value, str):
        return redact_call_string(value)
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if key == "task_kwargs" and isinstance(item, dict):
                redacted[key] = dict.fromkeys(item, REDACTED)
            else:
                redacted[key] = _redact_value(item)
        return redacted
    if isinstance(value, list):
        return [_redact_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(_redact_value(item) for item in value)
    return value


class RedactJobArguments(logging.Filter):
    """
    Blank job arguments in a record's message and in every extra field, at any depth.

    Procrastinate uses several extra names: "job" for one job, "jobs" for a list when a job is
    enqueued, and others. Walking every extra field covers them all, not just a known name.
    Never drops a record.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact_call_string(record.getMessage())
        record.args = ()
        for name in list(vars(record)):
            if name not in _STANDARD_ATTRIBUTES:
                setattr(record, name, _redact_value(getattr(record, name)))
        return True


def redacted_stream_handler(stream: Any = None) -> logging.Handler:
    """A stream handler that applies the filter. The worker process installs this one."""
    handler = logging.StreamHandler(stream)
    handler.addFilter(RedactJobArguments())
    return handler
