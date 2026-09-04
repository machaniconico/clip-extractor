"""Regression tests for the repository secret-hygiene scanner."""

import subprocess
from pathlib import Path

import pytest

from tools import check_secrets


@pytest.mark.parametrize(
    "sidecar_name",
    [
        ".gemini_key.ab12cd.tmp",
        ".obs_password.ab12cd.tmp",
        ".x_credentials.json.ab12cd.tmp",
    ],
)
def test_tracked_temp_secret_sidecar_fails_check(
    tmp_path,
    sidecar_name,
):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    (tmp_path / ".gitignore").write_text(
        ".gemini_key\n"
        ".gemini_key.*.tmp\n"
        ".obs_password\n"
        ".obs_password.*.tmp\n"
        ".x_credentials.json*\n",
        encoding="utf-8",
    )
    (tmp_path / sidecar_name).write_text("leftover fallback", encoding="utf-8")
    subprocess.run(
        ["git", "add", "-f", "--", sidecar_name],
        cwd=tmp_path,
        check=True,
    )

    findings = check_secrets.run_check(Path(tmp_path))

    assert f"protected secret sidecar is tracked: {sidecar_name}" in findings
