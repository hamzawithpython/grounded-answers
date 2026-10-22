"""
The extraction child process (ADR 0020, decisions D-D and the stderr rule).

Run by the parent as: python -I -m grounded_answers.extract.child KIND [MEMORY_LIMIT_BYTES]

- On Linux, the address-space limit is set first, so the parser libraries are loaded under it.
- The file arrives on stdin. Exactly one JSON object leaves on stdout. Nothing is written to stderr
  by this code, and the parent never reads stderr into a log or a result.
- Every failure is reduced to one of the generic reasons. The exception's class name is the only
  detail sent back, because a message can contain document text.
"""

import json
import sys
from typing import Any

from grounded_answers.extract.model import Extracted, Section, Table


def _to_json(extracted: Extracted) -> dict[str, Any]:
    return {
        "format": extracted.format,
        "page_count": extracted.page_count,
        "sections": [
            {
                "number": section.number,
                "heading": section.heading,
                "level": section.level,
                "page": section.page,
                "text": section.text,
                "tables": [[list(row) for row in table.rows] for table in section.tables],
            }
            for section in extracted.sections
        ],
    }


def from_json(payload: dict[str, Any]) -> Extracted:
    sections = tuple(
        Section(
            number=item["number"],
            heading=item["heading"],
            level=item["level"],
            page=item["page"],
            text=item["text"],
            tables=tuple(
                Table(rows=tuple(tuple(row) for row in table)) for table in item["tables"]
            ),
        )
        for item in payload["sections"]
    )
    return Extracted(format=payload["format"], page_count=payload["page_count"], sections=sections)


def _fail(reason: str, exception_name: str) -> dict[str, Any]:
    return {"ok": False, "reason": reason, "exception": exception_name}


def run(kind: str, memory_limit: int, data: bytes) -> dict[str, Any]:
    if memory_limit and sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
    try:
        from grounded_answers.extract.dispatch import extract_bytes

        extracted = extract_bytes(kind, data)
    except MemoryError:
        return _fail("memory", "MemoryError")
    except (
        Exception
    ) as exc:  # any parser failure is "unreadable"; the class name is the only detail
        return _fail("unreadable", type(exc).__name__)
    if not extracted.has_text():
        return _fail("no_text", "NoText")
    return {"ok": True, "result": _to_json(extracted)}


def main(argv: list[str]) -> int:
    kind = argv[1]
    memory_limit = int(argv[2]) if len(argv) > 2 else 0
    data = sys.stdin.buffer.read()
    sys.stdout.write(json.dumps(run(kind, memory_limit, data)))
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
