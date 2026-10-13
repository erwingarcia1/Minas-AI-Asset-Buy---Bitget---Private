"""`python -m guardian.validate_config [path]`: check a rules file before shipping it.

Exit 0 when every setting validates, 1 with the problems listed otherwise.
The worker never refuses a bad file (it runs on what validates and pages);
this is where failing fast belongs: before a deploy, not during one.
"""
from __future__ import annotations

import sys

import yaml

from guardian.config import _to_decimal_leaves
from guardian.rules_schema import validate


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv:
        path = argv[0]
    else:
        from api.config import settings
        path = settings.guardian_rules_path
    try:
        with open(path) as f:
            raw = yaml.safe_load(f) or {}
    except (OSError, yaml.YAMLError) as exc:
        print(f"{path}: cannot be read: {exc}", file=sys.stderr)
        return 1
    problems = validate(_to_decimal_leaves(raw))
    if problems:
        for problem in problems:
            print(f"{path}: {problem}", file=sys.stderr)
        return 1
    print(f"{path}: ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
