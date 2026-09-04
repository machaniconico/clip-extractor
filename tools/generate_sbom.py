#!/usr/bin/env python3
"""Generate and verify a CycloneDX SBOM for the installed environment."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

from packaging.utils import canonicalize_name

try:
    from generate_notices import REPOSITORY_ROOT, pinned_requirements
except ModuleNotFoundError:  # Imported as tools.generate_sbom by the test suite.
    from tools.generate_notices import REPOSITORY_ROOT, pinned_requirements


LOCK_FILE = REPOSITORY_ROOT / "requirements.lock"
DEFAULT_OUTPUT = REPOSITORY_ROOT / "sbom.cdx.json"


def generate_document() -> dict[str, object]:
    command = [
        sys.executable,
        "-m",
        "cyclonedx_py",
        "environment",
        "--spec-version",
        "1.6",
        "--output-format",
        "JSON",
        "--output-file",
        "-",
    ]
    try:
        result = subprocess.run(command, check=False, capture_output=True, text=True)
    except OSError as exc:
        raise RuntimeError(f"cannot run cyclonedx-py: {exc}") from exc
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip() or "no error output"
        raise RuntimeError(
            "cyclonedx-py failed; install the pinned cyclonedx-bom CI tool: " + detail
        )
    try:
        document = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"cyclonedx-py produced invalid JSON: {exc}") from exc
    if not isinstance(document, dict):
        raise RuntimeError("cyclonedx-py produced a non-object JSON document")
    return document


def verify_document(document: dict[str, object], lock_file: Path = LOCK_FILE) -> list[str]:
    if document.get("bomFormat") != "CycloneDX" or document.get("specVersion") != "1.6":
        return ["generated document is not CycloneDX 1.6 JSON"]
    components = document.get("components")
    if not isinstance(components, list) or not components:
        return ["generated document has no components"]
    installed: dict[str, str] = {}
    for component in components:
        if isinstance(component, dict):
            name, version = component.get("name"), component.get("version")
            if isinstance(name, str) and isinstance(version, str):
                installed[canonicalize_name(name)] = version

    mismatches = []
    for canonical_name, (declared_name, expected) in pinned_requirements(lock_file).items():
        actual = installed.get(canonical_name)
        if actual is None:
            mismatches.append(f"{declared_name}=={expected}: missing from SBOM")
        elif actual != expected:
            mismatches.append(
                f"{declared_name}=={expected}: SBOM contains version {actual}"
            )
    return mismatches


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args(argv)
    try:
        document = generate_document()
        problems = verify_document(document, LOCK_FILE)
    except RuntimeError as exc:
        print(f"generate_sbom: {exc}", file=sys.stderr)
        return 1
    if problems:
        print(
            "generate_sbom: SBOM does not match requirements.lock; "
            "install the lock file and regenerate:",
            file=sys.stderr,
        )
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    try:
        args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    except OSError as exc:
        print(f"generate_sbom: cannot write {args.output}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
