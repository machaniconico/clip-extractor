#!/usr/bin/env python3
"""Generate the repository's reproducible third-party license notices.

The dependency graph is resolved from installed distribution metadata rather
than from a hand-maintained package list.  This intentionally makes a missing
distribution or an unidentifiable license a release error: a notice file with
an invented or silently unknown license is worse than no generated file.
"""

from __future__ import annotations

import argparse
import difflib
import re
import sys
from collections import deque
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from pathlib import PurePosixPath, PureWindowsPath
from typing import Iterable

from packaging.markers import default_environment
from packaging.requirements import InvalidRequirement, Requirement
from packaging.utils import canonicalize_name


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
REQUIREMENTS_FILE = REPOSITORY_ROOT / "requirements.txt"
NOTICES_FILE = REPOSITORY_ROOT / "THIRD_PARTY_NOTICES.md"

_UNKNOWN_LICENSES = {
    "",
    "na",
    "n/a",
    "none",
    "not specified",
    "unknown",
    "unknown license",
}

_LICENSE_CLASSIFIER_MAP = {
    "apache software license": "Apache-2.0",
    "apache software license (apache 2.0)": "Apache-2.0",
    "bsd license": "BSD",
    "bsd license (bsd 2-clause)": "BSD-2-Clause",
    "bsd license (bsd 3-clause)": "BSD-3-Clause",
    "bsd license (revised)": "BSD-3-Clause",
    "gnu affero gpl": "AGPL",
    "gnu general public license (gpl)": "GPL",
    "gnu lesser general public license (lgpl)": "LGPL",
    "isc license (isc)": "ISC",
    "mit license": "MIT",
    "mozilla public license 2.0 (mpl 2.0)": "MPL-2.0",
    "python software foundation license": "PSF-2.0",
    "zlib/libpng license": "Zlib",
}

# Keep this local rather than depending on a scanner package being installed.
# The set covers the SPDX identifiers emitted by the installed dependency
# graph, along with common identifiers accepted from package metadata.
_KNOWN_SPDX_IDS = {
    "0BSD",
    "AGPL",
    "AGPL-1.0-only",
    "AGPL-1.0-or-later",
    "AGPL-3.0-only",
    "AGPL-3.0-or-later",
    "Apache-1.1",
    "Apache-2.0",
    "Artistic-1.0",
    "Artistic-2.0",
    "BSD",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD-4-Clause",
    "CC0-1.0",
    "CDDL-1.0",
    "CPL-1.0",
    "EPL-1.0",
    "EPL-2.0",
    "GPL",
    "GPL-1.0-only",
    "GPL-1.0-or-later",
    "GPL-2.0-only",
    "GPL-2.0-or-later",
    "GPL-3.0-only",
    "GPL-3.0-or-later",
    "ISC",
    "LGPL",
    "LGPL-2.0-only",
    "LGPL-2.0-or-later",
    "LGPL-2.1-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0-only",
    "LGPL-3.0-or-later",
    "MIT",
    "MIT-0",
    "MIT-CMU",
    "MPL-1.1",
    "MPL-2.0",
    "PSF-2.0",
    "Python-2.0",
    "Unlicense",
    "W3C",
    "Zlib",
}

_KNOWN_SPDX_EXCEPTIONS = {
    "Autoconf-exception-2.0",
    "Autoconf-exception-3.0",
    "Bison-exception-2.2",
    "Classpath-exception-2.0",
    "FLTK-exception",
    "Font-exception-2.0",
    "GCC-exception-2.0",
    "GCC-exception-3.1",
    "GNAT-exception",
    "GPL-3.0-interface-exception",
    "LLVM-exception",
    "Linux-syscall-note",
    "OpenJDK-assembly-exception-1.0",
    "Qt-GPL-exception-1.0",
    "Qt-LGPL-exception-1.1",
    "Swift-exception",
    "Universal-FOSS-exception-1.0",
    "WxWindows-exception-3.1",
}

_LICENSE_ALIASES = {
    "apache 2.0": "Apache-2.0",
    "apache license, version 2.0": "Apache-2.0",
    "apache license version 2.0": "Apache-2.0",
    "3-clause bsd license": "BSD-3-Clause",
    "bsd 3-clause license": "BSD-3-Clause",
    "bsd 2-clause license": "BSD-2-Clause",
    "bsd 3-clause or apache-2.0": "BSD-3-Clause OR Apache-2.0",
    "isc license": "ISC",
    "mit license": "MIT",
    "mpl 2.0": "MPL-2.0",
    "psfl": "PSF-2.0",
}

_LICENSE_TEXT_PATTERNS = (
    (re.compile(r"SPDX-License-Identifier\s*:\s*([^\s*]+)", re.I), None),
    (re.compile(r"\bISC License\b", re.I), "ISC"),
    (re.compile(r"\bMIT License\b", re.I), "MIT"),
    (re.compile(r"\bApache License\b.*\bVersion 2\.0\b", re.I | re.S), "Apache-2.0"),
    (re.compile(r"\bMozilla Public License\b.*\b2\.0\b", re.I | re.S), "MPL-2.0"),
    (re.compile(r"\bBSD 3-Clause\b", re.I), "BSD-3-Clause"),
    (re.compile(r"\bBSD 2-Clause\b", re.I), "BSD-2-Clause"),
)


@dataclass
class DistributionRecord:
    """Installed distribution plus the extras through which it is reachable."""

    distribution: metadata.Distribution
    extras: set[str] = field(default_factory=set)
    expanded: bool = False


def _strip_requirement_comment(line: str) -> str:
    """Remove a pip-style comment without changing URL fragments in a specifier."""

    for index, character in enumerate(line):
        if character == "#" and (index == 0 or line[index - 1].isspace()):
            return line[:index].rstrip()
    return line.strip()


def read_requirements(path: Path, _seen_files: set[Path] | None = None) -> list[Requirement]:
    """Read PEP 508 requirements and supported local ``-r`` includes."""

    seen_files = set() if _seen_files is None else _seen_files
    resolved_path = path.resolve()
    if resolved_path in seen_files:
        return []
    seen_files.add(resolved_path)

    requirements: list[Requirement] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise RuntimeError(f"cannot read requirements file {path}: {exc}") from exc

    for line_number, raw_line in enumerate(lines, start=1):
        line = _strip_requirement_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("-r ") or line.startswith("--requirement "):
            include_name = line.split(None, 1)[1].strip()
            include_path = (path.parent / include_name).resolve()
            requirements.extend(read_requirements(include_path, seen_files))
            continue
        if line.startswith("-"):
            # Index, constraint, hash, and other pip options do not name an
            # installed distribution to include in the notice graph.
            continue
        try:
            requirements.append(Requirement(line))
        except InvalidRequirement as exc:
            raise RuntimeError(
                f"invalid requirement at {path}:{line_number}: {line!r} ({exc})"
            ) from exc
    return requirements


def pinned_requirements(path: Path) -> dict[str, tuple[str, str]]:
    """Return canonical names mapped to their declared name and exact version."""

    pinned: dict[str, tuple[str, str]] = {}
    for requirement in read_requirements(path):
        exact_versions = [
            specifier.version
            for specifier in requirement.specifier
            if specifier.operator == "==" and "*" not in specifier.version
        ]
        if len(requirement.specifier) != 1 or len(exact_versions) != 1:
            raise RuntimeError(
                f"requirement is not exactly pinned in {path}: {requirement}"
            )
        name = canonicalize_name(requirement.name)
        value = (requirement.name, exact_versions[0])
        if name in pinned and pinned[name] != value:
            raise RuntimeError(f"conflicting pins in {path}: {requirement.name}")
        pinned[name] = value
    return pinned


def _marker_applies(requirement: Requirement, extras: Iterable[str] = ()) -> bool:
    if requirement.marker is None:
        return True
    environment = default_environment()
    # An empty extra includes unconditional metadata markers.  Each explicitly
    # selected extra then enables its own ``extra == ...`` requirements.
    contexts = [""] + sorted(set(extras))
    return any(
        requirement.marker.evaluate({**environment, "extra": extra})
        for extra in contexts
    )


def resolve_distributions(requirements: Iterable[Requirement]) -> tuple[list[DistributionRecord], list[str]]:
    """Resolve all installed distributions reachable from the root requirements."""

    records: dict[str, DistributionRecord] = {}
    # Preserve the marker context used while traversing a parent's selected
    # extra. Re-evaluating such a requirement with the default empty ``extra``
    # would silently drop transitive extra packages.
    pending = deque((requirement, False) for requirement in requirements)
    problems: list[str] = []

    while pending:
        requirement, marker_already_applied = pending.popleft()
        if not marker_already_applied and not _marker_applies(requirement):
            continue
        name = canonicalize_name(requirement.name)
        record = records.get(name)
        if record is None:
            try:
                distribution = metadata.distribution(requirement.name)
            except metadata.PackageNotFoundError:
                problems.append(
                    f"dependency is not installed: {requirement.name}"
                )
                # Keep a sentinel so repeated references do not produce a
                # different result or an unbounded queue.
                records[name] = DistributionRecord(distribution=None)  # type: ignore[arg-type]
                continue
            record = DistributionRecord(distribution=distribution)
            records[name] = record

        previous_extras = set(record.extras)
        record.extras.update(requirement.extras)
        if record.expanded and previous_extras == record.extras:
            continue
        if record.distribution is None:
            continue

        raw_requires = record.distribution.requires or []
        for raw_child in raw_requires:
            try:
                child = Requirement(raw_child)
            except InvalidRequirement as exc:
                problems.append(
                    f"invalid dependency metadata in {record.distribution.metadata.get('Name', requirement.name)}: "
                    f"{raw_child!r} ({exc})"
                )
                continue
            if _marker_applies(child, record.extras):
                pending.append((child, True))
        record.expanded = True

    resolved = [record for record in records.values() if record.distribution is not None]
    resolved.sort(
        key=lambda record: (
            str(record.distribution.metadata.get("Name", "")).casefold(),
            str(record.distribution.version),
        )
    )
    return resolved, sorted(set(problems), key=str.casefold)


def _valid_spdx_expression(value: str) -> str:
    """Return a normalized known SPDX expression, or an empty string."""

    expression = value.strip()
    if not expression or expression.casefold() in _UNKNOWN_LICENSES:
        return ""

    # SPDX expressions have identifiers, AND/OR/WITH operators, and
    # parentheses. Reject all other syntax and every identifier not known to
    # this generator; arbitrary ``License`` metadata is not evidence.
    tokens: list[str] = []
    cursor = 0
    for match in re.finditer(r"[A-Za-z0-9][A-Za-z0-9.-]*|[()]", expression):
        if expression[cursor : match.start()].strip():
            return ""
        tokens.append(match.group(0))
        cursor = match.end()
    if expression[cursor:].strip() or not tokens:
        return ""

    position = 0

    def parse_primary() -> bool:
        nonlocal position
        if position >= len(tokens):
            return False
        token = tokens[position]
        if token == "(":
            position += 1
            if not parse_or() or position >= len(tokens) or tokens[position] != ")":
                return False
            position += 1
            return True
        if token not in _KNOWN_SPDX_IDS:
            return False
        position += 1
        if position < len(tokens) and tokens[position] == "WITH":
            position += 1
            if position >= len(tokens) or tokens[position] not in _KNOWN_SPDX_EXCEPTIONS:
                return False
            position += 1
        return True

    def parse_and() -> bool:
        nonlocal position
        if not parse_primary():
            return False
        while position < len(tokens) and tokens[position] == "AND":
            position += 1
            if not parse_primary():
                return False
        return True

    def parse_or() -> bool:
        nonlocal position
        if not parse_and():
            return False
        while position < len(tokens) and tokens[position] == "OR":
            position += 1
            if not parse_and():
                return False
        return True

    if not parse_or() or position != len(tokens):
        return ""
    return expression


def _normalize_license_value(value: str) -> str:
    value = value.strip()
    if not value or "\n" in value:
        return ""
    alias = _LICENSE_ALIASES.get(value.casefold())
    if alias:
        return alias
    mapped = _LICENSE_CLASSIFIER_MAP.get(value.casefold())
    if mapped:
        return mapped
    return _valid_spdx_expression(value)


def _metadata_license(distribution: metadata.Distribution) -> str:
    expression = (distribution.metadata.get("License-Expression") or "").strip()
    normalized_expression = _normalize_license_value(expression)
    if normalized_expression:
        return normalized_expression

    value = (distribution.metadata.get("License") or "").strip()
    if value and value.casefold() not in _UNKNOWN_LICENSES and "\n" not in value:
        if "::" in value:
            value = value.rsplit("::", 1)[-1].strip()
        normalized_value = _normalize_license_value(value)
        if normalized_value:
            return normalized_value

    classifiers = distribution.metadata.get_all("Classifier") or []
    for classifier in classifiers:
        prefix = "License ::"
        if not classifier.startswith(prefix):
            continue
        label = classifier[len(prefix) :].strip()
        if label.casefold() in {"osi approved", "other/proprietary license"}:
            continue
        if "::" in label:
            label = label.rsplit("::", 1)[-1].strip()
        mapped = _LICENSE_CLASSIFIER_MAP.get(label.casefold())
        normalized = mapped or _normalize_license_value(label)
        if normalized:
            return normalized
    return ""


def _read_license_candidate(
    distribution: metadata.Distribution, candidate: Path | str
) -> str:
    if isinstance(candidate, Path):
        try:
            data = candidate.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
    else:
        try:
            data = distribution.read_text(candidate)
        except (OSError, UnicodeError, FileNotFoundError):
            data = None
        if data is None:
            try:
                path = distribution.locate_file(candidate)
                data = Path(path).read_text(encoding="utf-8", errors="replace")
            except (OSError, TypeError, ValueError):
                return ""
    return data.replace("\r\n", "\n").replace("\r", "\n").rstrip()


def _license_file_candidates(
    distribution: metadata.Distribution,
) -> list[tuple[str, Path | str]]:
    """Find license files advertised by metadata or stored in dist-info.

    PathDistribution exposes its dist-info directory through ``_path``.  Using
    that path avoids re-reading a potentially large RECORD file for every
    package and lets us deduplicate the common ``LICENSE`` and
    ``licenses/LICENSE`` spellings.
    """

    candidates: dict[str, Path | str] = {}
    dist_info_path = getattr(distribution, "_path", None)
    dist_info: Path | None = None
    if dist_info_path is not None:
        try:
            possible_path = Path(dist_info_path)
            if possible_path.is_dir():
                dist_info = possible_path
        except (OSError, TypeError, ValueError):
            dist_info = None

    def safe_relative_name(raw_name: str) -> PurePosixPath | None:
        normalized = raw_name.strip().replace("\\", "/")
        if not normalized:
            return None
        posix = PurePosixPath(normalized)
        windows = PureWindowsPath(normalized)
        if (
            posix.is_absolute()
            or windows.is_absolute()
            or bool(windows.drive)
            or ".." in posix.parts
        ):
            return None
        return posix

    def add_candidate(display_name: str, candidate: Path | str) -> None:
        if isinstance(candidate, Path):
            try:
                resolved = candidate.resolve(strict=True)
                if not resolved.is_file():
                    return
                if dist_info is not None:
                    resolved.relative_to(dist_info.resolve())
                key = str(resolved)
            except (OSError, RuntimeError, ValueError):
                return
        else:
            safe_name = safe_relative_name(candidate)
            if safe_name is None:
                return
            key = safe_name.as_posix().casefold()
        candidates.setdefault(key, (display_name.replace("\\", "/"), candidate))

    metadata_license_files = list(
        distribution.metadata.get_all("License-File") or []
    )
    # Some metadata providers expose the wheel's license_files directly even
    # though the stdlib Distribution API does not require that attribute.
    metadata_license_files.extend(
        str(name) for name in (getattr(distribution, "license_files", None) or [])
    )
    if dist_info is not None:
        for name in metadata_license_files:
            name = name.strip()
            if not name:
                continue
            relative = safe_relative_name(name)
            if relative is None:
                continue
            for candidate in (
                dist_info / relative,
                dist_info / "licenses" / relative,
            ):
                if candidate.is_file():
                    try:
                        display = candidate.relative_to(dist_info).as_posix()
                    except ValueError:
                        display = candidate.name
                    add_candidate(display, candidate)
                    break
        try:
            for candidate in sorted(
                (path for path in dist_info.rglob("*") if path.is_file()),
                key=lambda path: path.relative_to(dist_info).as_posix().casefold(),
            ):
                if candidate.name.casefold().startswith("license"):
                    add_candidate(candidate.relative_to(dist_info).as_posix(), candidate)
        except (OSError, RuntimeError, ValueError):
            pass
    else:
        for name in metadata_license_files:
            name = name.strip()
            if safe_relative_name(name) is not None:
                add_candidate(name, name)
        for file_name in distribution.files or []:
            text_name = str(file_name)
            parts = text_name.replace("\\", "/").split("/")
            lower_parts = [part.casefold() for part in parts]
            has_dist_info = any(part.endswith(".dist-info") for part in lower_parts)
            if (
                has_dist_info
                and parts[-1].casefold().startswith("license")
                and safe_relative_name(text_name) is not None
            ):
                add_candidate(text_name, text_name)

    return [candidates[key] for key in sorted(candidates, key=str.casefold)]


def license_texts(distribution: metadata.Distribution) -> list[tuple[str, str]]:
    texts = []
    for display_name, candidate in _license_file_candidates(distribution):
        text = _read_license_candidate(distribution, candidate)
        if text:
            texts.append((display_name, text))
    return texts


def license_identifier(
    distribution: metadata.Distribution, texts: list[tuple[str, str]]
) -> str:
    identifier = _metadata_license(distribution)
    if identifier:
        return identifier
    combined_text = "\n".join(text for _, text in texts)
    for pattern, fixed_identifier in _LICENSE_TEXT_PATTERNS:
        match = pattern.search(combined_text)
        if not match:
            continue
        if fixed_identifier:
            return fixed_identifier
        return match.group(1).strip()
    return ""


def project_url(distribution: metadata.Distribution) -> str:
    project_urls = distribution.metadata.get_all("Project-URL") or []
    parsed: list[tuple[str, str]] = []
    for value in project_urls:
        label, separator, url = value.partition(",")
        candidate = url.strip() if separator else value.strip()
        if candidate:
            parsed.append((label.strip().casefold(), candidate))
    for preferred in ("homepage", "home", "repository", "source"):
        for label, url in parsed:
            if preferred in label:
                return url
    if parsed:
        return parsed[0][1]
    return (distribution.metadata.get("Home-page") or "").strip()


def _render_license_text(texts: list[tuple[str, str]]) -> str:
    if not texts:
        return "License text: The installed distribution does not ship a license file."
    chunks = []
    for file_name, text in texts:
        chunks.append(f"#### `{file_name}`\n\n{text}")
    return "\n\n".join(chunks)


def render_notices(records: Iterable[DistributionRecord]) -> str:
    lines = [
        "# Third-party notices",
        "",
        "This file is generated by `tools/generate_notices.py`; do not edit it manually.",
        "",
    ]
    for record in records:
        distribution = record.distribution
        name = distribution.metadata.get("Name") or distribution.name
        version = distribution.version
        texts = license_texts(distribution)
        identifier = license_identifier(distribution, texts)
        url = project_url(distribution)
        lines.extend(
            [
                f"## {name} {version}",
                "",
                f"- License: {identifier}",
                f"- Project URL: {url or 'Not declared'}",
                "",
                _render_license_text(texts),
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def _license_problems(records: Iterable[DistributionRecord]) -> list[str]:
    problems = []
    for record in records:
        distribution = record.distribution
        texts = license_texts(distribution)
        if not license_identifier(distribution, texts):
            name = distribution.metadata.get("Name") or distribution.name
            problems.append(
                f"license cannot be determined for {name}=={distribution.version}"
            )
    return sorted(problems, key=str.casefold)


def build_notices() -> tuple[str | None, list[str]]:
    try:
        requirements = read_requirements(REQUIREMENTS_FILE)
        records, problems = resolve_distributions(requirements)
    except RuntimeError as exc:
        return None, [str(exc)]
    problems.extend(_license_problems(records))
    if problems:
        return None, sorted(set(problems), key=str.casefold)
    return render_notices(records), []


def _check_against_file(expected: str, path: Path) -> int:
    try:
        actual = path.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"notice file cannot be read: {path}: {exc}", file=sys.stderr)
        return 1
    if actual == expected:
        return 0
    diff = difflib.unified_diff(
        actual.splitlines(keepends=True),
        expected.splitlines(keepends=True),
        fromfile=str(path),
        tofile="regenerated",
    )
    print("THIRD_PARTY_NOTICES.md is stale:", file=sys.stderr)
    sys.stderr.writelines(diff)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="regenerate in memory and fail if THIRD_PARTY_NOTICES.md is stale",
    )
    args = parser.parse_args(argv)

    rendered, problems = build_notices()
    if problems:
        for problem in problems:
            print(f"generate_notices: {problem}", file=sys.stderr)
        return 1
    assert rendered is not None

    if args.check:
        return _check_against_file(rendered, NOTICES_FILE)
    try:
        NOTICES_FILE.write_text(rendered, encoding="utf-8", newline="\n")
    except OSError as exc:
        print(f"notice file cannot be written: {NOTICES_FILE}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
