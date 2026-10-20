#!/usr/bin/env python3
"""Run pip-audit with reviewed, expiring vulnerability exceptions."""

from __future__ import annotations

import argparse
import json
import subprocess  # nosec B404
import sys
from datetime import date
from pathlib import Path


IGNORES_FILE = Path(__file__).resolve().parent / "pip_audit_ignores.json"


def load_ignored_ids(path: Path, today: date) -> list[str]:
    """Return the IDs to ignore, failing when any exception has expired."""
    entries = json.loads(path.read_text(encoding="utf-8"))
    ignored: list[str] = []
    expired: list[str] = []
    for entry in entries:
        expires = date.fromisoformat(entry["expires"])
        if not entry.get("reason", "").strip():
            raise ValueError(f"{entry['package']}: exception has no reason")
        if today > expires:
            expired.append(f"{entry['package']} (expired {expires.isoformat()})")
        ignored.extend(entry["ids"])
    if expired:
        raise ValueError(
            "pip-audit exceptions expired; re-review or remove: " + ", ".join(expired)
        )
    return ignored


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("requirements", type=Path)
    parser.add_argument("--ignores", type=Path, default=IGNORES_FILE)
    args = parser.parse_args(argv)

    try:
        ignored = load_ignored_ids(args.ignores, date.today())
    except (OSError, ValueError, KeyError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1

    command = [sys.executable, "-m", "pip_audit", "-r", str(args.requirements)]
    for vuln_id in ignored:
        command += ["--ignore-vuln", vuln_id]
    print(f"Ignoring {len(ignored)} reviewed advisories from {args.ignores.name}")
    return subprocess.run(command, check=False).returncode  # nosec B603


if __name__ == "__main__":
    raise SystemExit(main())
