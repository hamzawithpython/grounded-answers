"""
Create the JWT key file for local development (ADR 0004, docs/RUNBOOK.md "Auth keys").

    uv run python scripts/make_jwt_keys.py --out "C:/Users/<you>/.grounded-answers/jwt-keys.json"

Prints the path and the kid. Never prints key material. Refuses to overwrite an existing file.
"""

import argparse
import sys
from pathlib import Path

from grounded_answers.auth_keys import write_new_key_file


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Create an Ed25519 JWT key file.")
    parser.add_argument("--out", required=True, type=Path, help="path of the new key file")
    args = parser.parse_args(argv)
    try:
        kid = write_new_key_file(args.out)
    except FileExistsError:
        print(f"Refusing to overwrite {args.out}. Choose a new path.", file=sys.stderr)
        return 1
    print(f"Wrote {args.out} with active kid {kid}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
