#!/usr/bin/env python3
"""Run a deterministic, local-only secret hygiene check on tracked files."""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
PROTECTED_SIDECARS = (".gemini_key", ".obs_password", ".x_credentials.json")

# These patterns intentionally require either a provider-specific key shape or
# a credential field.  Generic words such as ``secret`` are not findings.
GOOGLE_KEY_RE = re.compile(r"(?<![A-Za-z0-9_-])AIza[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])")
OPENAI_KEY_RE = re.compile(
    r"(?<![A-Za-z0-9])sk-(?:proj-|admin-|org-)?[A-Za-z0-9_-]{20,}(?![A-Za-z0-9_-])"
)
PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN(?: [A-Z0-9]+)* PRIVATE KEY-----", re.IGNORECASE
)
X_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])\d{18,20}-[A-Za-z0-9]{35,50}(?![A-Za-z0-9])")
X_KEY_ASSIGNMENT_RE = re.compile(
    r"(?ix)\b(?:x|twitter)[-_ ]?(?:api[-_ ]?)?key\b\s*[:=]\s*['\"]?"
    r"([A-Za-z0-9]{20,30})"
)
CONSUMER_KEY_ASSIGNMENT_RE = re.compile(
    r"(?ix)\bconsumer[-_ ]?key\b\s*[:=]\s*['\"]?([A-Za-z0-9]{20,30})"
)
X_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?ix)\b(?:(?:x|twitter)[-_ ]*)?(?:api[-_ ]?key|access[-_ ]?token)[-_ ]?secret\b"
    r"\s*[:=]\s*['\"]?([A-Za-z0-9_-]{30,80})"
    r"|\bconsumer[-_ ]?secret\b\s*[:=]\s*['\"]?([A-Za-z0-9_-]{30,80})"
)
GENERIC_TEST_SECRET_RE = re.compile(
    r"(?ix)['\"]?\b(?:api[-_ ]?key|api[-_ ]?secret|access[-_ ]?token|"
    r"refresh[-_ ]?token|client[-_ ]?secret|private[-_ ]?key)\b['\"]?"
    r"\s*[:=]\s*['\"]([^'\"\r\n]{17,})['\"]"
)

PLACEHOLDER_MARKERS = (
    "changeme",
    "dummy",
    "example",
    "fake",
    "invalid",
    "must-not",
    "new-",
    "not-a-real",
    "notareal",
    "no-secret",
    "old-",
    "placeholder",
    "redacted",
    "replace",
    "sample",
    "sentinel",
    "test",
    "your-",
    "your_",
)


def _git(*arguments: str, root: Path) -> subprocess.CompletedProcess[bytes]:
    environment = os.environ.copy()
    # User/system git excludes must not turn a repository hygiene check into
    # an environment-dependent result. Repository .gitignore rules remain.
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = os.devnull
    environment["GIT_CONFIG_SYSTEM"] = os.devnull
    return subprocess.run(
        ["git", *arguments],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        env=environment,
    )


def tracked_files(root: Path) -> tuple[list[str], str | None]:
    result = _git("ls-files", "-z", root=root)
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", errors="replace").strip()
        return [], f"git ls-files failed{': ' + detail if detail else ''}"
    paths = [path for path in result.stdout.decode("utf-8", errors="surrogateescape").split("\0") if path]
    return sorted(paths), None


def _is_ignored(root: Path, path: str) -> bool:
    result = _git("check-ignore", "--no-index", "--quiet", "--", path, root=root)
    return result.returncode == 0


def _is_protected_sidecar_name(name: str) -> bool:
    """Match a sidecar basename and the sibling temp files it creates."""
    for sidecar in PROTECTED_SIDECARS:
        if name == sidecar:
            return True
        prefix = f"{sidecar}."
        if name.startswith(prefix) and name.endswith(".tmp"):
            random_part = name[len(prefix):-len(".tmp")]
            if random_part:
                return True
    return False


def _is_placeholder(value: str) -> bool:
    """Recognize an explicitly synthetic value used by a test fixture."""

    folded = value.casefold()
    if any(marker in folded for marker in PLACEHOLDER_MARKERS):
        return True
    return _is_repetitive(value)


def _is_repetitive(value: str) -> bool:
    """Return true for values whose shape is unambiguously synthetic."""

    folded = value.casefold()
    compact = re.sub(r"[^a-z0-9]", "", folded)
    if compact and len(set(compact)) == 1:
        return True
    candidates = [compact]
    for prefix in ("aiza", "sk", "skproj", "skadmin", "skorg"):
        if compact.startswith(prefix):
            candidates.append(compact[len(prefix) :])
    for candidate in candidates:
        if len(candidate) >= 12 and re.search(r"(.)\1{11,}", candidate):
            return True
    return False


def _is_provider_placeholder(value: str) -> bool:
    """Only suppress provider-shaped values with an unmistakably synthetic shape.

    A real token may contain words such as ``test`` or ``sample`` by chance;
    substring markers are therefore not safe for provider-shaped credentials.
    """

    return _is_repetitive(value)


def _credential_findings(path: str, text: str) -> list[str]:
    findings: list[str] = []

    for pattern, label in (
        (GOOGLE_KEY_RE, "Google API key"),
        (OPENAI_KEY_RE, "OpenAI API key"),
    ):
        for match in pattern.finditer(text):
            if not _is_provider_placeholder(match.group(0)):
                findings.append(f"{path}: {label} pattern")

    for match in PEM_PRIVATE_KEY_RE.finditer(text):
        findings.append(f"{path}: PEM private-key header")

    for match in X_TOKEN_RE.finditer(text):
        if not _is_provider_placeholder(match.group(0)):
            findings.append(f"{path}: X/Twitter access-token pattern")

    for pattern in (X_KEY_ASSIGNMENT_RE, CONSUMER_KEY_ASSIGNMENT_RE):
        for match in pattern.finditer(text):
            value = match.group(1)
            if not _is_provider_placeholder(value):
                findings.append(f"{path}: X/Twitter API-key pattern")

    for match in X_SECRET_ASSIGNMENT_RE.finditer(text):
        value = match.group(1) or match.group(2)
        if value and not _is_provider_placeholder(value):
            findings.append(f"{path}: X/Twitter API-secret pattern")

    if path.startswith("tests/") or "/tests/" in path or "fixture" in path.casefold():
        for match in GENERIC_TEST_SECRET_RE.finditer(text):
            if not _is_placeholder(match.group(1)):
                findings.append(f"{path}: non-placeholder credential in test fixture")

    return sorted(set(findings), key=str.casefold)


def run_check(root: Path, *, directory: bool = False) -> list[str]:
    findings: list[str] = []
    if directory:
        if not root.is_dir():
            return [f"not a directory: {root}"]
        files = sorted(path.relative_to(root).as_posix() for path in root.rglob("*")
                       if path.is_file() or path.is_symlink())
    else:
        files, error = tracked_files(root)
        if error:
            return [error]

    for sidecar in (() if directory else PROTECTED_SIDECARS):
        for candidate in (sidecar, f"{sidecar}.probe.tmp"):
            if not _is_ignored(root, candidate):
                findings.append(f"{candidate} is missing from .gitignore")

    for path in files:
        if _is_protected_sidecar_name(Path(path).name):
            findings.append(f"protected secret sidecar is tracked: {path}")
        absolute_path = root / path
        if directory and absolute_path.is_symlink():
            findings.append(f"symlink is not allowed in distribution: {path}")
            continue
        try:
            data = absolute_path.read_bytes()
        except OSError as exc:
            findings.append(f"cannot read tracked file {path}: {exc}")
            continue
        views: list[str] = []
        try:
            views.append(data.decode("utf-8"))
        except UnicodeDecodeError:
            pass

        # Decode UTF-16 explicitly when NULs/BOMs indicate it. The latin-1
        # fallback is lossless for binary files and never silently drops a
        # byte, so ASCII credential shapes embedded in tracked content remain
        # searchable without treating every binary asset as text.
        if data.startswith((b"\xff\xfe", b"\xfe\xff")):
            try:
                views.append(data.decode("utf-16"))
            except UnicodeDecodeError:
                pass
        elif b"\x00" in data:
            for encoding in ("utf-16-le", "utf-16-be"):
                try:
                    views.append(data.decode(encoding))
                except UnicodeDecodeError:
                    pass
        views.append(data.decode("latin-1"))
        for view in dict.fromkeys(views):
            findings.extend(_credential_findings(path, view))

    return sorted(set(findings), key=str.casefold)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--root",
        type=Path,
        default=REPOSITORY_ROOT,
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--directory", type=Path,
        help="Scan all files in an extracted distribution without requiring Git.",
    )
    args = parser.parse_args(argv)
    if args.directory is not None:
        args.root = args.directory
    root = args.root.resolve()
    findings = run_check(root, directory=args.directory is not None)
    if findings:
        for finding in findings:
            print(f"check_secrets: {finding}", file=sys.stderr)
        return 1
    print("Secret hygiene check passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
