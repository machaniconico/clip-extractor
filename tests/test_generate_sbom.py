import json
from importlib.util import find_spec
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.generate_sbom as generate_sbom


def _lock(tmp_path: Path, text: str = "Example_Package==1.2.3\n") -> Path:
    path = tmp_path / "requirements.lock"
    path.write_text(text, encoding="utf-8")
    return path


def _document(version: str = "1.2.3") -> dict[str, object]:
    return {
        "bomFormat": "CycloneDX",
        "specVersion": "1.6",
        "version": 1,
        "components": [{"type": "library", "name": "example-package", "version": version}],
    }


def test_verify_document_accepts_matching_lock(tmp_path):
    assert generate_sbom.verify_document(_document(), _lock(tmp_path)) == []


def test_main_reports_lock_mismatches(monkeypatch, tmp_path, capsys):
    output = tmp_path / "sbom.json"
    monkeypatch.setattr(generate_sbom, "LOCK_FILE", _lock(tmp_path))
    monkeypatch.setattr(generate_sbom, "generate_document", lambda: _document("9.9.9"))

    assert generate_sbom.main(["--output", str(output)]) == 1
    assert "Example_Package==1.2.3: SBOM contains version 9.9.9" in capsys.readouterr().err
    assert not output.exists()


def test_main_writes_cyclonedx_16_json(monkeypatch, tmp_path):
    document = _document()
    output = tmp_path / "sbom.json"
    monkeypatch.setattr(generate_sbom, "LOCK_FILE", _lock(tmp_path))
    monkeypatch.setattr(generate_sbom, "generate_document", lambda: document)

    assert generate_sbom.main(["--output", str(output)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["bomFormat"] == "CycloneDX"
    assert written["specVersion"] == "1.6"
    assert written["components"]


def test_generator_invokes_cyclonedx_module(monkeypatch):
    def fake_run(command, **kwargs):
        assert command[:3] == [generate_sbom.sys.executable, "-m", "cyclonedx_py"]
        assert "1.6" in command
        return SimpleNamespace(returncode=0, stdout=json.dumps(_document()), stderr="")

    monkeypatch.setattr(generate_sbom.subprocess, "run", fake_run)
    assert generate_sbom.generate_document()["specVersion"] == "1.6"


@pytest.mark.skipif(find_spec("cyclonedx_py") is None, reason="cyclonedx-bom unavailable")
def test_installed_generator_produces_valid_cyclonedx_16():
    document = generate_sbom.generate_document()
    assert generate_sbom.verify_document(document, generate_sbom.LOCK_FILE) == []
