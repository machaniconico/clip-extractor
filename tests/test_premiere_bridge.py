"""Regression tests for the Premiere Pro one-click editing bridge."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

import pytest

import premiere_bridge


def _rendered_session(tmp_path: Path) -> dict:
    clips_dir = tmp_path / "clips"
    shorts_dir = tmp_path / "shorts"
    clips_dir.mkdir()
    shorts_dir.mkdir()

    clip = clips_dir / "00m10s-00m40s_見どころ.mp4"
    short = shorts_dir / "00m10s-00m40s_見どころ_short.mp4"
    clip.write_bytes(b"clip")
    short.write_bytes(b"short")

    return {
        "output_dir": str(tmp_path),
        "_premiere_output": {
            "project_name": "配信アーカイブ",
            "clip_paths": [str(clip)],
            "shorts_paths": [str(short)],
        },
    }


def test_build_edit_job_contains_existing_absolute_media(tmp_path):
    session = _rendered_session(tmp_path)

    job = premiere_bridge.build_edit_job(session, include_shorts=True)

    assert job["action"] == "import_clips"
    assert Path(job["project_path"]).is_absolute()
    assert Path(job["project_path"]).suffix == ".prproj"
    assert [item["kind"] for item in job["media"]] == ["clip", "short"]
    assert all(Path(item["path"]).is_absolute() for item in job["media"])
    assert all(Path(item["path"]).is_file() for item in job["media"])
    assert job["media"][0]["sequence_name"].startswith(
        f"ClipExtractor_{tmp_path.name}_clip_"
    )
    assert job["media"][0]["sequence_name"].endswith(
        "00m10s-00m40s_見どころ"
    )


def test_build_edit_job_can_exclude_shorts(tmp_path):
    session = _rendered_session(tmp_path)

    job = premiere_bridge.build_edit_job(session, include_shorts=False)

    assert [item["kind"] for item in job["media"]] == ["clip"]


def test_build_edit_job_avoids_overwriting_existing_project(tmp_path):
    session = _rendered_session(tmp_path)
    existing = tmp_path / "配信アーカイブ_ClipExtractor.prproj"
    existing.write_bytes(b"existing project")

    job = premiere_bridge.build_edit_job(session)

    assert Path(job["project_path"]).name == "配信アーカイブ_ClipExtractor_2.prproj"
    assert existing.read_bytes() == b"existing project"


def test_build_edit_job_rejects_unrendered_or_missing_media(tmp_path):
    with pytest.raises(ValueError, match="書き出し"):
        premiere_bridge.build_edit_job({}, include_shorts=True)

    session = _rendered_session(tmp_path)
    Path(session["_premiere_output"]["clip_paths"][0]).unlink()
    with pytest.raises(ValueError, match="見つかりません"):
        premiere_bridge.build_edit_job(session, include_shorts=False)


def test_bridge_job_lifecycle_and_plugin_heartbeat(tmp_path):
    bridge = premiere_bridge.PremiereBridgeServer(ports=(0,))
    bridge.start()
    try:
        session = _rendered_session(tmp_path)
        job = premiere_bridge.build_edit_job(session)
        job_id = bridge.enqueue(job)

        bridge.record_heartbeat(
            {"plugin_version": "1.0.0", "premiere_version": "26.3.0"}
        )
        leased = bridge.lease_next()
        assert leased is not None
        assert leased["id"] == job_id
        assert bridge.renew_lease(job_id, leased["lease_token"]) is True

        bridge.complete_job(
            job_id,
            {
                "success": True,
                "message": "2個のシーケンスを開きました",
                "sequence_count": 2,
            },
            leased["lease_token"],
        )

        snapshot = bridge.status_snapshot()
        assert snapshot["plugin_connected"] is True
        assert snapshot["plugin_version"] == "1.0.0"
        assert snapshot["premiere_version"] == "26.3.0"
        assert snapshot["last_job"]["state"] == "completed"
        assert snapshot["last_job"]["result"]["sequence_count"] == 2
    finally:
        bridge.stop()


def test_bridge_http_protocol_delivers_and_completes_job(tmp_path):
    bridge = premiere_bridge.PremiereBridgeServer(ports=(0,))
    bridge.start()
    try:
        job_id = bridge.enqueue(
            premiere_bridge.build_edit_job(_rendered_session(tmp_path))
        )

        with urllib.request.urlopen(
            f"{bridge.base_url}/v1/session",
            timeout=2,
        ) as response:
            auth_token = json.load(response)["token"]
        headers = {
            "Content-Type": "text/plain",
            "X-Clip-Extractor-Token": auth_token,
        }

        heartbeat = urllib.request.Request(
            f"{bridge.base_url}/v1/heartbeat",
            data=json.dumps(
                {"plugin_version": "1.0.0", "premiere_version": "26.3.0"}
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(heartbeat, timeout=2) as response:
            assert json.load(response)["ok"] is True

        next_job = urllib.request.Request(
            f"{bridge.base_url}/v1/jobs/next",
            data=b"{}",
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(next_job, timeout=2) as response:
            leased = json.load(response)
        assert leased["job"]["id"] == job_id

        renew = urllib.request.Request(
            f"{bridge.base_url}/v1/jobs/{job_id}/renew",
            data=json.dumps(
                {"lease_token": leased["job"]["lease_token"]}
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(renew, timeout=2) as response:
            assert json.load(response)["ok"] is True

        complete = urllib.request.Request(
            f"{bridge.base_url}/v1/jobs/{job_id}/result",
            data=json.dumps(
                {
                    "success": True,
                    "message": "読み込み完了",
                    "lease_token": leased["job"]["lease_token"],
                }
            ).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(complete, timeout=2) as response:
            assert json.load(response)["ok"] is True

        assert bridge.status_snapshot()["last_job"]["state"] == "completed"
    finally:
        bridge.stop()


def test_bridge_rejects_unauthorized_state_changes(tmp_path):
    bridge = premiere_bridge.PremiereBridgeServer(ports=(0,))
    bridge.start()
    try:
        bridge.enqueue(
            premiere_bridge.build_edit_job(_rendered_session(tmp_path))
        )
        unauthorized = urllib.request.Request(
            f"{bridge.base_url}/v1/jobs/next",
            data=b"{}",
            headers={"Content-Type": "text/plain"},
            method="POST",
        )

        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(unauthorized, timeout=2)

        assert error.value.code == 401
        assert bridge.status_snapshot()["last_job"]["state"] == "pending"

        invalid_host = urllib.request.Request(
            f"{bridge.base_url}/v1/session",
            headers={"Host": "attacker.example"},
        )
        with pytest.raises(urllib.error.HTTPError) as host_error:
            urllib.request.urlopen(invalid_host, timeout=2)
        assert host_error.value.code == 421
    finally:
        bridge.stop()


def test_stale_lease_cannot_overwrite_new_attempt(monkeypatch, tmp_path):
    monkeypatch.setattr(premiere_bridge, "_JOB_LEASE_SECONDS", 0)
    bridge = premiere_bridge.PremiereBridgeServer(ports=(0,))
    job_id = bridge.enqueue(
        premiere_bridge.build_edit_job(_rendered_session(tmp_path))
    )

    first = bridge.lease_next()
    second = bridge.lease_next()

    assert first["lease_token"] != second["lease_token"]
    assert bridge.complete_job(
        job_id,
        {"success": False, "message": "stale"},
        first["lease_token"],
    ) is False
    assert bridge.complete_job(
        job_id,
        {"success": True, "message": "current"},
        second["lease_token"],
    ) is True
    assert bridge.status_snapshot()["last_job"]["state"] == "completed"


def test_request_edit_starts_bridge_only_when_invoked(monkeypatch, tmp_path):
    bridge = premiere_bridge.PremiereBridgeServer(ports=(0,))
    monkeypatch.setattr(premiere_bridge, "_singleton_bridge", bridge)
    monkeypatch.setattr(
        premiere_bridge,
        "launch_premiere",
        lambda explicit_path="": premiere_bridge.PremiereLaunchResult(
            True,
            "Premiere Proを起動しました。",
            explicit_path,
        ),
    )

    assert bridge.running is False
    try:
        message = premiere_bridge.request_premiere_edit(
            _rendered_session(tmp_path),
            executable_path="C:/Adobe Premiere Pro.exe",
        )

        assert bridge.running is True
        assert "ジョブ:" in message
        assert bridge.status_snapshot()["last_job"]["state"] == "pending"
    finally:
        bridge.stop()


def test_request_edit_does_not_queue_when_premiere_launch_fails(
    monkeypatch,
    tmp_path,
):
    bridge = premiere_bridge.PremiereBridgeServer(ports=(0,))
    monkeypatch.setattr(premiere_bridge, "_singleton_bridge", bridge)
    monkeypatch.setattr(
        premiere_bridge,
        "launch_premiere",
        lambda explicit_path="": premiere_bridge.PremiereLaunchResult(
            False,
            "Premiere Proを検出できません。",
        ),
    )

    try:
        message = premiere_bridge.request_premiere_edit(
            _rendered_session(tmp_path)
        )

        assert "Premiere起動エラー" in message
        assert bridge.status_snapshot()["last_job"] is None
        assert "検出できません" in premiere_bridge.get_bridge_status_text()
    finally:
        bridge.stop()


def test_package_plugin_creates_installable_ccx_with_minimal_permissions(tmp_path):
    package_path = premiere_bridge.package_plugin(tmp_path / "premiere-bridge.ccx")

    assert package_path.is_file()
    with zipfile.ZipFile(package_path) as archive:
        names = set(archive.namelist())
        assert {"manifest.json", "index.js", "README.md"} <= names
        manifest = json.loads(archive.read("manifest.json"))

    assert manifest["manifestVersion"] == 5
    assert manifest["host"] == {
        "app": "premierepro",
        "minVersion": "25.6.0",
    }
    assert manifest["hostUIContext"]["hideFromMenu"] is True
    assert manifest["requiredPermissions"]["localFileSystem"] == "fullAccess"
    assert manifest["requiredPermissions"]["network"]["domains"] == [
        f"http://127.0.0.1:{port}" for port in premiere_bridge.DEFAULT_PORTS
    ]


def test_explicit_premiere_executable_is_preferred(tmp_path):
    executable = tmp_path / "Adobe Premiere Pro.exe"
    executable.write_bytes(b"exe")

    found = premiere_bridge.find_premiere_executable(str(executable))

    assert found == executable.resolve()


def test_macos_candidates_include_direct_and_product_folder_layouts(tmp_path):
    direct = (
        tmp_path
        / "Adobe Premiere Pro 2025.app"
        / "Contents"
        / "MacOS"
        / "Adobe Premiere Pro 2025"
    )
    nested = (
        tmp_path
        / "Adobe Premiere Pro 2026"
        / "Adobe Premiere Pro 2026.app"
        / "Contents"
        / "MacOS"
        / "Adobe Premiere Pro 2026"
    )
    direct.parent.mkdir(parents=True)
    nested.parent.mkdir(parents=True)
    direct.write_bytes(b"app")
    nested.write_bytes(b"app")

    candidates = premiere_bridge._macos_premiere_candidates(tmp_path)

    assert direct in candidates
    assert nested in candidates
    assert max(candidates, key=premiere_bridge._version_key) == nested
