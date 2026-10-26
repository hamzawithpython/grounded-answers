"""
No log call in src/ may carry a raw tenant, user, document, version or actor id, or an email (ADR 0033, decision 8: the
project-wide rule). A tripwire, not a proof: it reads the source of every logging call and looks at the names it
passes, the keys of an `extra` dictionary, and the format string. The runtime check is the sentinel test, which puts
known ids into a real request and searches every log record.

What it flags:
- a variable or attribute whose name is an id of one of those kinds (`tenant_id`, `ctx.user_id`, `principal.tenant_id`)
  or an email (`email`, `user_email`), anywhere in the arguments of a call on a logger;
- a string key of that kind (`extra={"tenant_id": ...}`);
- a format string that names one (`"tenant_id=%s"`).

`tenant_ref` (the keyed hash) and `request_id` are fine. There is no allowlist: a line that needs one gets a better
design, or an ADR.
"""

import ast
import re
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src" / "grounded_answers"
LOG_METHODS = {"debug", "info", "warning", "error", "exception", "critical", "log"}
_KIND = r"(?:tenant|user|document|version|actor|membership)"
IDENTIFIER = re.compile(rf"(?:^|_){_KIND}_id$|(?:^|_)email$")
FORMAT = re.compile(rf"\b{_KIND}_id\b|\bemail\b", re.IGNORECASE)


def _is_logger_call(node: ast.Call) -> bool:
    func = node.func
    if not isinstance(func, ast.Attribute) or func.attr not in LOG_METHODS:
        return False
    owner = ast.unparse(func.value).lower()
    return "log" in owner


def _problems(call: ast.Call) -> list[str]:
    found: list[str] = []
    for node in ast.walk(call):
        if isinstance(node, ast.Name) and IDENTIFIER.search(node.id):
            found.append(f"name {node.id}")
        elif isinstance(node, ast.Attribute) and IDENTIFIER.search(node.attr):
            found.append(f"attribute {node.attr}")
        elif isinstance(node, ast.keyword) and node.arg and IDENTIFIER.search(node.arg):
            found.append(f"keyword {node.arg}")
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            if IDENTIFIER.search(node.value) or FORMAT.search(node.value):
                found.append(f"text {node.value[:40]!r}")
    return found


def scan(root: Path) -> list[tuple[str, int, list[str]]]:
    hits: list[tuple[str, int, list[str]]] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and _is_logger_call(node):
                problems = _problems(node)
                if problems:
                    hits.append((str(path.relative_to(root)), node.lineno, problems))
    return hits


def test_no_log_call_in_src_carries_a_raw_id_or_an_email() -> None:
    hits = scan(SRC)
    assert hits == [], "raw ids or emails in log calls:\n" + "\n".join(map(str, hits))


def test_the_scanner_finds_what_it_is_meant_to_find(tmp_path: Path) -> None:
    source = (
        "import logging\n"
        "logger = logging.getLogger(__name__)\n"
        "def f(tenant_id, ctx, request_id, tenant_ref):\n"
        "    logger.info('a %s', tenant_id)\n"  # name
        "    logger.info('b %s', ctx.user_id)\n"  # attribute
        "    logger.info('c', extra={'document_id': 1})\n"  # string key
        "    logger.warning('tenant_id=%s', request_id)\n"  # format text
        "    logger.error(f'd {ctx.principal.tenant_id}')\n"  # f-string
        "    logger.info('e', extra={'actor_id': 1})\n"
        "    logger.info('f', user_email=1)\n"  # keyword
        "    logger.info('ok %s %s', request_id, tenant_ref)\n"  # allowed
        "    print(tenant_id)\n"  # not a logger call
    )
    (tmp_path / "sample.py").write_text(source, encoding="utf-8")
    lines = sorted(line for _, line, _ in scan(tmp_path))
    assert lines == [4, 5, 6, 7, 8, 9, 10]
