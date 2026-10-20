import json
import sys
from datetime import date
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import run_pip_audit  # noqa: E402


def _write(tmp_path, entries):
    path = tmp_path / "ignores.json"
    path.write_text(json.dumps(entries), encoding="utf-8")
    return path


def test_active_exceptions_are_returned(tmp_path):
    path = _write(tmp_path, [
        {"package": "pillow", "ids": ["GHSA-a", "GHSA-b"],
         "expires": "2026-12-31", "reason": "unreachable"},
    ])
    assert run_pip_audit.load_ignored_ids(path, date(2026, 12, 31)) == ["GHSA-a", "GHSA-b"]


def test_expired_exception_fails(tmp_path):
    path = _write(tmp_path, [
        {"package": "pillow", "ids": ["GHSA-a"],
         "expires": "2026-12-31", "reason": "unreachable"},
    ])
    with pytest.raises(ValueError, match="expired"):
        run_pip_audit.load_ignored_ids(path, date(2027, 1, 1))


def test_exception_without_reason_fails(tmp_path):
    path = _write(tmp_path, [
        {"package": "pillow", "ids": ["GHSA-a"], "expires": "2026-12-31", "reason": " "},
    ])
    with pytest.raises(ValueError, match="no reason"):
        run_pip_audit.load_ignored_ids(path, date(2026, 1, 1))


def test_repository_exceptions_are_well_formed():
    ignored = run_pip_audit.load_ignored_ids(run_pip_audit.IGNORES_FILE, date(2026, 10, 21))
    assert ignored and all(i.startswith(("GHSA-", "PYSEC-")) for i in ignored)


def test_main_passes_ignores_to_pip_audit(tmp_path, monkeypatch):
    path = _write(tmp_path, [
        {"package": "pillow", "ids": ["GHSA-a"], "expires": "2099-01-01", "reason": "r"},
    ])
    calls = []

    class Done:
        returncode = 0

    monkeypatch.setattr(run_pip_audit.subprocess, "run", lambda cmd, check: calls.append(cmd) or Done())
    assert run_pip_audit.main(["requirements.lock", "--ignores", str(path)]) == 0
    assert calls[0][-4:] == ["-r", "requirements.lock", "--ignore-vuln", "GHSA-a"]
