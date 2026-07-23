"""One-click bridge from Clip Extractor to Adobe Premiere Pro.

The Python app owns a loopback-only HTTP queue.  A small Premiere UXP
companion plugin polls that queue, imports rendered clips, creates sequences,
and reports completion.  No job-enqueue HTTP endpoint is exposed: only this
process can decide which local files Premiere is allowed to import.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterable
from urllib.parse import urlsplit


logger = logging.getLogger("clip-extractor.premiere")

PLUGIN_VERSION = "1.0.0"
DEFAULT_PORTS = (43127, 43128, 43129)
PLUGIN_DIR = Path(__file__).resolve().parent / "premiere_uxp"
_PLUGIN_FILES = ("manifest.json", "index.js", "README.md")
_PLUGIN_CONNECTED_TTL_SECONDS = 8.0
_JOB_LEASE_SECONDS = 90.0
_MAX_REQUEST_BYTES = 64 * 1024
_MAX_RETAINED_JOBS = 20
_AUTH_HEADER = "X-Clip-Extractor-Token"


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_project_stem(value: str) -> str:
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (value or "").strip())
    stem = stem.rstrip(" .")
    if not stem:
        stem = "Clip Extractor"
    return stem[:80]


def _safe_sequence_name(value: str) -> str:
    name = re.sub(r'[\x00-\x1f]', "_", (value or "").strip()).rstrip(" .")
    return (name or "Clip Extractor")[:120]


def _resolved_existing_files(paths: Iterable[str | os.PathLike]) -> list[Path]:
    resolved: list[Path] = []
    missing: list[str] = []
    for raw_path in paths:
        path = Path(raw_path).expanduser().resolve()
        if not path.is_file():
            missing.append(str(path))
        else:
            resolved.append(path)
    if missing:
        raise ValueError(
            "Premiereへ渡す書き出しファイルが見つかりません: "
            + ", ".join(missing)
        )
    return resolved


def _available_project_path(output_dir: Path, project_name: str) -> Path:
    base = output_dir / f"{_safe_project_stem(project_name)}_ClipExtractor.prproj"
    if not base.exists():
        return base
    for suffix in range(2, 1000):
        candidate = base.with_name(f"{base.stem}_{suffix}{base.suffix}")
        if not candidate.exists():
            return candidate
    raise ValueError("Premiereプロジェクトの保存名を確保できません")


def build_edit_job(render_state: dict, include_shorts: bool = True) -> dict:
    """Build and validate one UXP import job from a completed render state."""
    if not isinstance(render_state, dict):
        raise ValueError("先に切り抜きを書き出してください")

    output = render_state.get("_premiere_output")
    if not isinstance(output, dict):
        output = render_state

    clip_paths = output.get("clip_paths") or []
    if not clip_paths:
        raise ValueError("先に切り抜きを書き出してください")

    clips = _resolved_existing_files(clip_paths)
    shorts = (
        _resolved_existing_files(output.get("shorts_paths") or [])
        if include_shorts
        else []
    )

    raw_output_dir = output.get("output_dir") or render_state.get("output_dir")
    if raw_output_dir:
        output_dir = Path(raw_output_dir).expanduser().resolve()
    else:
        output_dir = clips[0].parent.parent
    if not output_dir.is_dir():
        raise ValueError(f"出力フォルダが見つかりません: {output_dir}")

    project_name = str(output.get("project_name") or "Clip Extractor")
    project_path = _available_project_path(output_dir, project_name)
    sequence_scope = _safe_project_stem(output_dir.name)[:48]

    media: list[dict] = []
    for kind, paths in (("clip", clips), ("short", shorts)):
        for path in paths:
            sequence_name = _safe_sequence_name(
                f"ClipExtractor_{sequence_scope}_{kind}_{path.stem}"
            )
            media.append(
                {
                    "path": str(path),
                    "kind": kind,
                    "sequence_name": sequence_name,
                }
            )

    return {
        "action": "import_clips",
        "protocol_version": 1,
        "project_name": project_name,
        "project_path": str(project_path),
        "output_dir": str(output_dir),
        "media": media,
        "open_first_sequence": True,
    }


class _BridgeHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class PremiereBridgeServer:
    """Thread-safe in-memory job queue served on loopback only."""

    def __init__(self, ports: Iterable[int] = DEFAULT_PORTS):
        self._ports = tuple(int(port) for port in ports)
        if not self._ports:
            raise ValueError("at least one bridge port is required")
        self._lock = threading.RLock()
        self._jobs: dict[str, dict] = {}
        self._job_order: list[str] = []
        self._plugin_last_seen_monotonic: float | None = None
        self._plugin_last_seen_at = ""
        self._plugin_version = ""
        self._premiere_version = ""
        self._auth_token = secrets.token_urlsafe(32)
        self._last_error = ""
        self._server: _BridgeHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def port(self) -> int | None:
        server = self._server
        return int(server.server_address[1]) if server is not None else None

    @property
    def base_url(self) -> str:
        if self.port is None:
            raise RuntimeError("Premiere bridge is not running")
        return f"http://127.0.0.1:{self.port}"

    @property
    def auth_token(self) -> str:
        return self._auth_token

    @property
    def running(self) -> bool:
        return self._server is not None

    def start(self) -> "PremiereBridgeServer":
        with self._lock:
            if self._server is not None:
                return self

            last_error: OSError | None = None
            for port in self._ports:
                try:
                    server = _BridgeHTTPServer(
                        ("127.0.0.1", port),
                        self._handler_class(),
                    )
                except OSError as exc:
                    last_error = exc
                    continue
                self._server = server
                break

            if self._server is None:
                detail = f": {last_error}" if last_error else ""
                raise RuntimeError(
                    "Premiere連携用のローカルポートを開けません"
                    f" ({', '.join(map(str, self._ports))}){detail}"
                )

            self._thread = threading.Thread(
                target=self._server.serve_forever,
                kwargs={"poll_interval": 0.2},
                name=f"premiere-bridge-{self.port}",
                daemon=True,
            )
            self._thread.start()
            logger.info("Premiere bridge listening on %s", self.base_url)
            return self

    def stop(self) -> None:
        with self._lock:
            server = self._server
            thread = self._thread
            self._server = None
            self._thread = None
        if server is not None:
            server.shutdown()
            server.server_close()
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=2)

    def enqueue(self, payload: dict) -> str:
        if not isinstance(payload, dict) or payload.get("action") != "import_clips":
            raise ValueError("unsupported Premiere bridge job")

        job_id = uuid.uuid4().hex
        now = _utc_now()
        record = {
            "id": job_id,
            "state": "pending",
            "payload": dict(payload),
            "created_at": now,
            "leased_at": "",
            "lease_until": 0.0,
            "lease_token": "",
            "attempts": 0,
            "completed_at": "",
            "result": None,
        }
        with self._lock:
            self._last_error = ""
            self._jobs[job_id] = record
            self._job_order.append(job_id)
            self._prune_jobs_locked()
        return job_id

    def lease_next(self) -> dict | None:
        now_monotonic = time.monotonic()
        with self._lock:
            for job_id in self._job_order:
                record = self._jobs.get(job_id)
                if record is None:
                    continue
                available = record["state"] == "pending" or (
                    record["state"] == "leased"
                    and record["lease_until"] <= now_monotonic
                )
                if not available:
                    continue
                record["state"] = "leased"
                record["leased_at"] = _utc_now()
                record["lease_until"] = now_monotonic + _JOB_LEASE_SECONDS
                record["lease_token"] = secrets.token_urlsafe(24)
                record["attempts"] += 1
                return {
                    "id": record["id"],
                    "created_at": record["created_at"],
                    "lease_token": record["lease_token"],
                    **record["payload"],
                }
        return None

    def renew_lease(self, job_id: str, lease_token: str) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record["state"] != "leased"
                or not lease_token
                or not secrets.compare_digest(
                    str(record["lease_token"]),
                    str(lease_token),
                )
            ):
                return False
            record["lease_until"] = time.monotonic() + _JOB_LEASE_SECONDS
            return True

    def complete_job(
        self,
        job_id: str,
        result: dict,
        lease_token: str,
    ) -> bool:
        with self._lock:
            record = self._jobs.get(job_id)
            if (
                record is None
                or record["state"] != "leased"
                or not lease_token
                or not secrets.compare_digest(
                    str(record["lease_token"]),
                    str(lease_token),
                )
            ):
                return False
            success = bool(result.get("success"))
            record["state"] = "completed" if success else "failed"
            record["result"] = dict(result)
            record["completed_at"] = _utc_now()
            record["lease_until"] = 0.0
            record["lease_token"] = ""
            return True

    def record_heartbeat(self, payload: dict) -> None:
        with self._lock:
            self._plugin_last_seen_monotonic = time.monotonic()
            self._plugin_last_seen_at = _utc_now()
            self._plugin_version = str(payload.get("plugin_version") or "")
            self._premiere_version = str(payload.get("premiere_version") or "")
            self._last_error = ""

    def record_error(self, message: str) -> None:
        with self._lock:
            self._last_error = str(message or "")

    def status_snapshot(self) -> dict:
        with self._lock:
            connected = (
                self._plugin_last_seen_monotonic is not None
                and (
                    time.monotonic() - self._plugin_last_seen_monotonic
                    <= _PLUGIN_CONNECTED_TTL_SECONDS
                )
            )
            last_record = None
            if self._job_order:
                record = self._jobs.get(self._job_order[-1])
                if record is not None:
                    last_record = {
                        key: record[key]
                        for key in (
                            "id",
                            "state",
                            "created_at",
                            "leased_at",
                            "attempts",
                            "completed_at",
                            "result",
                        )
                    }
            return {
                "running": self._server is not None,
                "port": self.port,
                "plugin_connected": connected,
                "plugin_last_seen_at": self._plugin_last_seen_at,
                "plugin_version": self._plugin_version,
                "premiere_version": self._premiere_version,
                "last_error": self._last_error,
                "last_job": last_record,
            }

    def _prune_jobs_locked(self) -> None:
        if len(self._job_order) <= _MAX_RETAINED_JOBS:
            return
        removable = [
            job_id
            for job_id in self._job_order
            if self._jobs[job_id]["state"] in {"completed", "failed"}
        ]
        while len(self._job_order) > _MAX_RETAINED_JOBS and removable:
            job_id = removable.pop(0)
            self._job_order.remove(job_id)
            self._jobs.pop(job_id, None)

    def _handler_class(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            server_version = "ClipExtractorPremiereBridge/1"

            def log_message(self, format_string, *args):
                logger.debug("Premiere bridge HTTP: " + format_string, *args)

            def _write_json(self, status: int, payload: dict) -> None:
                encoded = json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(encoded)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.wfile.write(encoded)
                self.close_connection = True

            def _read_json(self) -> dict:
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError as exc:
                    raise ValueError("invalid Content-Length") from exc
                if length < 0 or length > _MAX_REQUEST_BYTES:
                    raise ValueError("request body too large")
                raw = self.rfile.read(length)
                if not raw:
                    return {}
                payload = json.loads(raw.decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("JSON body must be an object")
                return payload

            def _authorized(self) -> bool:
                supplied = self.headers.get(_AUTH_HEADER, "")
                return bool(supplied) and secrets.compare_digest(
                    supplied,
                    bridge.auth_token,
                )

            def _host_allowed(self) -> bool:
                expected = f"127.0.0.1:{bridge.port}"
                return self.headers.get("Host", "").lower() == expected

            def do_GET(self):
                if not self._host_allowed():
                    self._write_json(
                        421,
                        {"ok": False, "error": "invalid host"},
                    )
                    return
                path = urlsplit(self.path).path
                if path == "/v1/health":
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "bridge_version": PLUGIN_VERSION,
                            "port": bridge.port,
                        },
                    )
                    return
                if path == "/v1/session":
                    self._write_json(
                        200,
                        {
                            "ok": True,
                            "token": bridge.auth_token,
                        },
                    )
                    return
                self._write_json(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if not self._host_allowed():
                    self._write_json(
                        421,
                        {"ok": False, "error": "invalid host"},
                    )
                    return
                path = urlsplit(self.path).path
                if not self._authorized():
                    self._write_json(
                        401,
                        {"ok": False, "error": "unauthorized"},
                    )
                    return
                try:
                    payload = self._read_json()
                except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                    self._write_json(400, {"ok": False, "error": str(exc)})
                    return

                if path == "/v1/heartbeat":
                    bridge.record_heartbeat(payload)
                    self._write_json(200, {"ok": True})
                    return

                if path == "/v1/jobs/next":
                    self._write_json(200, {"job": bridge.lease_next()})
                    return

                renew_match = re.fullmatch(
                    r"/v1/jobs/([0-9a-f]{32})/renew",
                    path,
                )
                if renew_match:
                    renewed = bridge.renew_lease(
                        renew_match.group(1),
                        str(payload.get("lease_token") or ""),
                    )
                    self._write_json(
                        200 if renewed else 409,
                        {"ok": renewed},
                    )
                    return

                match = re.fullmatch(
                    r"/v1/jobs/([0-9a-f]{32})/result",
                    path,
                )
                if match:
                    lease_token = str(payload.pop("lease_token", "") or "")
                    updated = bridge.complete_job(
                        match.group(1),
                        payload,
                        lease_token,
                    )
                    self._write_json(
                        200 if updated else 409,
                        {"ok": updated},
                    )
                    return

                self._write_json(404, {"ok": False, "error": "not found"})

        return Handler


def package_plugin(destination: str | os.PathLike | None = None) -> Path:
    """Package the companion plugin as a directly installable CCX archive."""
    missing = [name for name in _PLUGIN_FILES if not (PLUGIN_DIR / name).is_file()]
    if missing:
        raise FileNotFoundError(
            "Premiere UXPプラグインのソースが不足しています: "
            + ", ".join(missing)
        )

    if destination is None:
        destination_path = (
            Path(tempfile.gettempdir())
            / "clip-extractor-premiere"
            / "clip-extractor-premiere-bridge.ccx"
        )
    else:
        destination_path = Path(destination).expanduser().resolve()
    destination_path.parent.mkdir(parents=True, exist_ok=True)

    temporary_path = destination_path.with_name(
        f".{destination_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        with zipfile.ZipFile(
            temporary_path,
            mode="w",
            compression=zipfile.ZIP_DEFLATED,
        ) as archive:
            for name in _PLUGIN_FILES:
                archive.write(PLUGIN_DIR / name, arcname=name)
        os.replace(temporary_path, destination_path)
    finally:
        temporary_path.unlink(missing_ok=True)
    return destination_path


def open_plugin_installer() -> str:
    """Build the CCX package and open Adobe Creative Cloud's installer UI."""
    package_path = package_plugin()
    try:
        if os.name == "nt":
            os.startfile(str(package_path))
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(package_path)])
        else:
            subprocess.Popen(["xdg-open", str(package_path)])
    except Exception as exc:
        raise RuntimeError(
            f"プラグインインストーラーを開けません: {package_path}: {exc}"
        ) from exc
    return (
        "Premiere連携プラグインのインストーラーを開きました。"
        "Creative Cloudで許可したあと、Premiere Proを再起動してください。"
    )


def _version_key(path: Path) -> tuple[int, ...]:
    for part in reversed(path.parts):
        if "Adobe Premiere Pro" not in part:
            continue
        numbers = re.findall(r"\d+", part)
        if numbers:
            return tuple(int(value) for value in numbers)
    return (0,)


def _registry_premiere_candidates() -> list[Path]:
    if os.name != "nt":
        return []
    try:
        import winreg
    except ImportError:
        return []

    candidates: list[Path] = []
    for version in range(40, 20, -1):
        key_name = (
            rf"Adobe.Premiere.Pro.Project.{version}\shell\open\command"
        )
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, key_name) as key:
                command, _ = winreg.QueryValueEx(key, None)
        except OSError:
            continue
        match = re.match(r'\s*"([^"]+Adobe Premiere Pro\.exe)"', str(command))
        if match:
            candidates.append(Path(match.group(1)))
    return candidates


def _macos_premiere_candidates(applications: Path) -> list[Path]:
    app_bundles = list(applications.glob("Adobe Premiere Pro *.app"))
    for product_dir in applications.glob("Adobe Premiere Pro *"):
        if product_dir.suffix == ".app" or not product_dir.is_dir():
            continue
        app_bundles.extend(product_dir.glob("Adobe Premiere Pro *.app"))

    return [
        app / "Contents" / "MacOS" / app.stem
        for app in app_bundles
        if "Beta" not in app.name and "Beta" not in app.parent.name
    ]


def find_premiere_executable(explicit_path: str = "") -> Path | None:
    """Find a stable Premiere executable, preferring the newest installation."""
    explicit_text = os.path.expandvars((explicit_path or "").strip())
    if explicit_text:
        explicit = Path(explicit_text).expanduser()
        if explicit.is_file():
            return explicit.resolve()

    candidates: list[Path] = _registry_premiere_candidates()
    if os.name == "nt":
        roots = {
            os.environ.get("ProgramFiles", r"C:\Program Files"),
            os.environ.get("ProgramW6432", r"C:\Program Files"),
        }
        for root in roots:
            adobe_root = Path(root) / "Adobe"
            if not adobe_root.is_dir():
                continue
            for directory in adobe_root.glob("Adobe Premiere Pro *"):
                if "Beta" in directory.name:
                    continue
                candidates.append(directory / "Adobe Premiere Pro.exe")
    elif sys.platform == "darwin":
        candidates.extend(_macos_premiere_candidates(Path("/Applications")))

    found = {
        candidate.resolve()
        for candidate in candidates
        if candidate.is_file()
    }
    if found:
        return max(found, key=_version_key)

    which = shutil.which("Adobe Premiere Pro.exe")
    return Path(which).resolve() if which else None


def is_premiere_running(executable: Path | None = None) -> bool:
    """Return whether a Premiere process is already running."""
    try:
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            result = subprocess.run(
                [
                    "tasklist",
                    "/FI",
                    "IMAGENAME eq Adobe Premiere Pro.exe",
                    "/FO",
                    "CSV",
                    "/NH",
                ],
                capture_output=True,
                text=True,
                timeout=5,
                creationflags=creationflags,
            )
            return "Adobe Premiere Pro.exe" in (result.stdout or "")
        pattern = str(executable) if executable else "Adobe Premiere Pro"
        result = subprocess.run(
            ["pgrep", "-f", pattern],
            capture_output=True,
            timeout=5,
        )
        return result.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


@dataclass(frozen=True)
class PremiereLaunchResult:
    ok: bool
    message: str
    executable: str = ""
    already_running: bool = False


def launch_premiere(explicit_path: str = "") -> PremiereLaunchResult:
    executable = find_premiere_executable(explicit_path)
    if executable is None:
        return PremiereLaunchResult(
            False,
            "Premiere Proを検出できません。Settingsで実行ファイルを指定してください。",
        )
    if is_premiere_running(executable):
        return PremiereLaunchResult(
            True,
            "起動中のPremiere Proへジョブを送りました。",
            str(executable),
            True,
        )

    kwargs: dict = {
        "cwd": str(executable.parent),
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
        "close_fds": True,
    }
    if os.name == "nt":
        kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen([str(executable)], **kwargs)
    except OSError as exc:
        return PremiereLaunchResult(
            False,
            f"Premiere Proを起動できません: {exc}",
            str(executable),
        )
    return PremiereLaunchResult(
        True,
        "Premiere Proを起動しました。プラグイン接続後に自動で読み込みます。",
        str(executable),
    )


_singleton_lock = threading.Lock()
_singleton_bridge: PremiereBridgeServer | None = None


def get_bridge(start: bool = False) -> PremiereBridgeServer | None:
    global _singleton_bridge
    with _singleton_lock:
        if _singleton_bridge is None and start:
            _singleton_bridge = PremiereBridgeServer()
            _singleton_bridge.start()
        elif _singleton_bridge is not None and start:
            _singleton_bridge.start()
        return _singleton_bridge


def request_premiere_edit(
    render_state: dict,
    include_shorts: bool = True,
    executable_path: str = "",
) -> str:
    """Queue a validated edit job and start/focus Premiere Pro."""
    job = build_edit_job(render_state, include_shorts=include_shorts)
    bridge = get_bridge(start=True)
    if bridge is None:  # defensive; get_bridge(start=True) always returns one
        raise RuntimeError("Premiere連携サーバーを起動できません")

    if bridge.status_snapshot().get("plugin_connected"):
        launch = PremiereLaunchResult(
            True,
            "起動中のPremiere Proへジョブを送ります。",
            already_running=True,
        )
    else:
        launch = launch_premiere(executable_path)
    if not launch.ok:
        bridge.record_error(launch.message)
        return format_bridge_status(bridge.status_snapshot())

    job_id = bridge.enqueue(job)
    status = format_bridge_status(bridge.status_snapshot())
    return f"{launch.message}\nジョブ: {job_id[:8]}\n{status}"


def format_bridge_status(snapshot: dict) -> str:
    if not snapshot.get("running"):
        return (
            "Premiere連携は待機中です。切り抜き書き出し後に"
            "「Premiere Proで編集」を押してください。"
        )

    if snapshot.get("last_error"):
        return "Premiere起動エラー: " + str(snapshot["last_error"])

    last_job = snapshot.get("last_job")
    connected = bool(snapshot.get("plugin_connected"))
    if last_job:
        state = last_job.get("state")
        if state == "completed":
            result = last_job.get("result") or {}
            return "Premiere読み込み完了: " + str(
                result.get("message") or "シーケンスを開きました"
            )
        if state == "failed":
            result = last_job.get("result") or {}
            return "Premiere読み込み失敗: " + str(
                result.get("message") or "プラグイン側でエラーが発生しました"
            )
        if state == "leased":
            return "Premiereで切り抜きを読み込み中です…"
        if state == "pending" and not connected:
            return (
                "Premiereプラグインの接続待ちです。未導入の場合は"
                "「連携プラグインをインストール」を押し、Premiereを再起動してください。"
            )
        if state == "pending":
            return "Premiereへ読み込みジョブを送信しました。"

    if connected:
        details = " / ".join(
            value
            for value in (
                f"Plugin {snapshot.get('plugin_version')}"
                if snapshot.get("plugin_version")
                else "",
                f"Premiere {snapshot.get('premiere_version')}"
                if snapshot.get("premiere_version")
                else "",
            )
            if value
        )
        return f"Premiere連携プラグイン接続済み{': ' + details if details else ''}"
    return "Premiere連携サーバー起動済み。プラグイン接続待ちです。"


def get_bridge_status_text() -> str:
    bridge = get_bridge(start=False)
    if bridge is None:
        return format_bridge_status({"running": False})
    return format_bridge_status(bridge.status_snapshot())
