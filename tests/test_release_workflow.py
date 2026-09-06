"""Static release gates and archive scanner regression checks."""

from pathlib import Path
import re
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/release.yml"


@pytest.mark.parametrize("element", [
    "workflow_dispatch:", "git archive --format=zip", "--prefix=",
    '"setup.bat", "Clip Extractor.bat"', 'data.replace(b"\\r\\n", b"")',
    "CRLF verification failed", "tools/check_secrets.py --directory",
    "tools/generate_sbom.py", 'bandit -r "$RELEASE_DIR" -x "$RELEASE_DIR/tests" -ll',
    'pip-audit -r "$RELEASE_DIR/requirements.lock"', "--draft",
    "--verify-tag", "SHA256SUMS.txt", "cyclonedx-bom==7.3.1",
    "pip-audit==2.9.0", "bandit==1.8.6",
])
def test_release_gates_present(element):
    assert element in WORKFLOW.read_text(encoding="utf-8")


def test_version_tag_trigger():
    workflow = WORKFLOW.read_text(encoding="utf-8")
    assert re.search(r"push:\s+tags: \[['\"]v\*['\"]\]", workflow)
    assert "permissions:\n  contents: write\n" in workflow
    assert "--draft=false" not in workflow


@pytest.mark.parametrize("directory", [".github/", ".codex-omc/", ".omc/", ".claude/"])
def test_internal_directories_export_ignored(directory):
    attributes = (ROOT / ".gitattributes").read_text(encoding="utf-8")
    assert f"{directory} export-ignore" in attributes.splitlines()


def test_directory_secret_scan_without_git(tmp_path):
    command = [sys.executable, str(ROOT / "tools/check_secrets.py"),
               "--directory", str(tmp_path)]
    (tmp_path / "nested").mkdir()
    source = tmp_path / "nested" / "config.txt"
    source.write_text("safe content", encoding="utf-8")
    assert subprocess.run(command, capture_output=True).returncode == 0
    # Assemble a provider-shaped synthetic token without storing a literal key.
    source.write_text("AIza" + "BcDeFgHiJkLmNoPqRsTuVwXyZ0123456789", encoding="utf-8")
    result = subprocess.run(command, capture_output=True, text=True)
    assert result.returncode == 1
    assert "Google API key pattern" in result.stderr
