"""OBS integration for local recordings and YouTube stream lifecycles.

Two interchangeable watchers report finished local recordings.  The WebSocket
watcher can additionally report stream start/stop without requiring recording:

* ``ObsWebsocketWatcher`` — connects to obs-websocket v5 via ``obsws-python``
  and reacts to Record/Stream state-changed events.
* ``FolderWatcher`` — watches a directory with ``watchdog`` for new video
  files and fires when a freshly-created file stops growing.

Both third-party dependencies are imported lazily (inside ``start()``) so
this module imports cleanly even when ``obsws-python`` / ``watchdog`` are not
installed; the watcher then reports the missing dependency via ``status``
instead of raising at import time.
"""

from __future__ import annotations

import logging
import math
import os
import socket
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger("clip-extractor.obs")

#: Video extensions recognised by the folder watcher.
RECORDING_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".flv", ".mov")
# 録画中にサイズが増えたファイルを特定する。開始時刻は更新時刻の下限に使う。
_RECORD_START_SLACK_SECONDS = 10.0
_RECORD_IDENTIFY_INTERVAL_SECONDS = 2.0
_RECORD_IDENTIFY_SAMPLES = 16
# OBSが応答しない場合、既定では切断検知1秒＋猶予5秒＋再接続間隔3秒
# ＋TCP確認2回×1秒＋安定判定5回×4秒で約31秒。web_app の
# _OBS_RECORDING_EVENT_TIMEOUT（60秒）に収まるようにする。
_RECOVERY_GRACE_SECONDS = 5.0
_RECOVERY_STABLE_CHECKS = 3
_RECOVERY_STABLE_ATTEMPTS = 5

#: obs-websocket output-state constants emitted when an output starts/stops.
OBS_WEBSOCKET_OUTPUT_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
OBS_WEBSOCKET_OUTPUT_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"
OBS_WEBSOCKET_OUTPUT_PAUSED = "OBS_WEBSOCKET_OUTPUT_PAUSED"
OBS_WEBSOCKET_OUTPUT_RESUMED = "OBS_WEBSOCKET_OUTPUT_RESUMED"

#: Type alias for the shared completion callback.
OnRecordingFinished = Callable[[str], None]
OnRecordingStopped = Callable[[str], None]
OnStreamStarted = Callable[..., None]
OnStreamFinished = Callable[[], None]
OnStatusChange = Callable[[str], None]

#: Default delay between background OBS WebSocket reconnection attempts.
DEFAULT_RECONNECT_INTERVAL: float = 3.0


def _is_permanent_connection_error(exc: Exception) -> bool:
    """Return True for OBS WebSocket errors that retrying alone cannot fix."""
    message = str(exc).lower()
    return (
        "authentication enabled" in message
        or "authentication failed" in message
        or "expected response with opcode 2" in message
    )


def _get(data: object, snake: str, camel: str) -> Optional[object]:
    """Read a field from an obsws event data object or a raw dict.

    obsws-python converts event keys to snake_case dataclass attributes
    (``output_state`` / ``output_path``); the raw payload uses camelCase
    (``outputState`` / ``outputPath``). Accept either form so the watcher
    keeps working across library versions.
    """
    val = getattr(data, snake, None)
    if val is None:
        try:
            val = data.get(camel)  # type: ignore[union-attr]
        except AttributeError:
            val = None
    return val


def wait_until_file_stable(
    path: str | Path, checks: int = 2, interval: float = 2.0
) -> bool:
    """Return True when the file size is unchanged across ``checks`` samples.

    Samples the file size ``checks`` times, ``interval`` seconds apart. The
    file is considered stable (write complete) when every sample is equal and
    non-zero. Returns False when the file is missing, unreadable, or still
    growing. Used by both watchers (and the tests) to avoid handing a
    half-written recording to the pipeline.
    """
    p = Path(path)
    n = max(2, int(checks))
    sizes: list[int] = []
    for i in range(n):
        try:
            if not p.exists():
                return False
            sizes.append(p.stat().st_size)
        except OSError:
            return False
        if i < n - 1:
            time.sleep(interval)
    return sizes[-1] > 0 and len(set(sizes)) == 1


@dataclass
class _PendingRecording:
    session: int
    path: str | None
    disconnected_at: float
    offline_attempted: bool = False


class _WorkerMixin:
    """Tiny helper to track daemon worker threads so stop() can join them."""

    def __init__(self) -> None:
        self._workers: list[threading.Thread] = []
        self._workers_lock = threading.Lock()

    def _spawn_worker(self, target: Callable[[], None]) -> None:
        t = threading.Thread(target=target, daemon=True)
        with self._workers_lock:
            self._workers.append(t)
            # drop already-finished threads to avoid unbounded growth
            self._workers = [w for w in self._workers if w.is_alive() or w is t]
        t.start()

    def _join_workers(self, timeout: float = 5.0) -> None:
        with self._workers_lock:
            workers = list(self._workers)
            self._workers.clear()
        for w in workers:
            try:
                w.join(timeout=timeout)
            except Exception:
                pass


class ObsWebsocketWatcher(_WorkerMixin):
    """Watch OBS via obs-websocket v5 and fire on recording/stream stop.

    ``"record"`` fires the recording callback and can also invoke optional
    stream lifecycle callbacks so one coordinator can prefer the local file
    and use the completed archive only as a fallback. ``"stream"`` invokes
    only the lifecycle callbacks.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        on_recording_finished: OnRecordingFinished,
        stop_event: str = "record",
        on_stream_started: OnStreamStarted | None = None,
        on_stream_finished: OnStreamFinished | None = None,
        on_recording_stopped: OnRecordingStopped | None = None,
        on_status_change: OnStatusChange | None = None,
        auto_reconnect: bool = True,
        reconnect_interval: float = DEFAULT_RECONNECT_INTERVAL,
    ) -> None:
        super().__init__()
        self._stopped = False
        self._stop_event = threading.Event()
        self._host = host
        self._port = int(port)
        self._password = password
        self._callback = on_recording_finished
        self._recording_stopped_callback = on_recording_stopped
        self._stream_started_callback = on_stream_started
        self._stream_finished_callback = on_stream_finished
        self._status_change_callback = on_status_change
        self._auto_reconnect = bool(auto_reconnect)
        self._reconnect_interval = max(0.05, float(reconnect_interval))
        self._trigger = (stop_event or "record").lower()
        self._client = None
        self._client_lock = threading.Lock()
        self._monitor_thread: threading.Thread | None = None
        self._reconnecting = False
        self._last_error_logged: str | None = None
        self._status = "stopped"
        self._stream_state_lock = threading.Lock()
        self._stream_active = False
        self._stream_active_from_probe = False
        self._stream_status_checked = False
        self._record_state_lock = threading.Lock()
        self._record_active = False
        self._record_paused = False
        self._record_started_at: float | None = None
        self._record_path: str | None = None
        # 状態変更時に問い合わせ中の応答とファイル特定を無効にする。
        self._record_seq = 0
        self._record_session = 0
        self._recovery_pending: _PendingRecording | None = None
        self._recovery_candidate: _PendingRecording | None = None
        self._notified_record_stop_keys: set[tuple[int, str]] = set()
        self._handled_record_keys: set[tuple[int, str]] = set()
        self._obs_module: object | None = None

    @property
    def status(self) -> str:
        """Human-readable connection/handler status (safe to poll from UI)."""
        return self._status

    @property
    def reconnecting(self) -> bool:
        """Whether background reconnection to OBS is currently active."""
        return self._reconnecting

    @property
    def stream_active(self) -> bool:
        """Whether a stream-start event/current-status probe reports active."""
        with self._stream_state_lock:
            return self._stream_active

    @property
    def stream_status_checked(self) -> bool:
        """Whether OBS current stream status was queried successfully."""
        with self._stream_state_lock:
            return self._stream_status_checked

    @staticmethod
    def _close_client(cl: object) -> None:
        """Best-effort close of an obsws-python EventClient."""
        closed = False
        for meth in ("unsubscribe", "disconnect"):
            fn = getattr(cl, meth, None)
            if callable(fn):
                try:
                    fn()
                    closed = True
                    break
                except Exception:
                    pass
        if not closed:
            # Fall back to closing the underlying websocket directly.
            try:
                cl.base_client.ws.close()  # type: ignore[attr-defined]
            except Exception:
                pass

    @staticmethod
    def _is_client_alive(cl: object | None) -> bool:
        """Return whether an EventClient still appears connected.

        Lightweight test doubles that omit ``worker`` / ``base_client`` are
        treated as alive so unit tests do not trigger spurious reconnects.
        """
        if cl is None:
            return False
        worker = getattr(cl, "worker", None)
        if isinstance(worker, threading.Thread) and not worker.is_alive():
            return False
        ws = getattr(getattr(cl, "base_client", None), "ws", None)
        if ws is not None and getattr(ws, "connected", None) is False:
            return False
        return True

    def _is_endpoint_reachable(self) -> bool:
        """Lightweight TCP probe to avoid heavy EventClient retries while OBS is closed."""
        try:
            with socket.create_connection((self._host, self._port), timeout=1.0):
                return True
        except OSError:
            return False

    def _notify_status_change(self, status: str) -> None:
        cb = self._status_change_callback
        if cb is None or self._stopped:
            return
        try:
            cb(status)
        except Exception:
            logger.exception("OBS status change callback failed")

    def _connect_once(
        self,
        obs_module: object,
        *,
        is_reconnect: bool = False,
    ) -> tuple[bool, bool]:
        """Attempt one EventClient connection.

        Returns ``(connected, permanent_error)``.
        """
        try:
            client = obs_module.EventClient(  # type: ignore[attr-defined]
                host=self._host,
                port=self._port,
                password=self._password,
                timeout=5,
            )
            with self._client_lock:
                stopped = self._stopped
                if not stopped:
                    self._client = client
                    self._obs_module = obs_module
            if stopped:
                self._close_client(client)
                return False, True
            client.callback.register(self.on_record_state_changed)
            client.callback.register(self.on_stream_state_changed)
            client.callback.register(self.on_record_file_changed)
            self._reconnecting = False
            self._last_error_logged = None
            self._status = (
                f"connected: {self._host}:{self._port} (trigger={self._trigger})"
            )
            if is_reconnect:
                logger.info(f"OBS再接続完了: {self._status}")
            else:
                logger.info(self._status)
            if (
                self._trigger == "stream"
                or self._stream_started_callback is not None
                or self._stream_finished_callback is not None
            ):
                self._probe_current_stream_status(obs_module)
            if self._trigger == "record":
                self._probe_current_record_status(obs_module)
            if is_reconnect:
                self._notify_status_change(self._status)
            return True, False
        except Exception as e:  # ConnectionRefusedError, TimeoutError, auth errors
            with self._client_lock:
                self._client = None
            permanent = _is_permanent_connection_error(e)
            self._status = f"接続失敗: {e}"
            if self._status != self._last_error_logged:
                self._last_error_logged = self._status
                logger.warning(self._status)
            if is_reconnect and permanent:
                self._notify_status_change(self._status)
            return False, permanent

    def _monitor_connection(self, obs_module: object) -> None:
        """Keep the OBS WebSocket connected across transient failures and restarts."""
        poll_interval = min(1.0, self._reconnect_interval)
        stop_event = self._stop_event
        try:
            while not stop_event.is_set():
                with self._client_lock:
                    client = self._client
                if self._is_client_alive(client):
                    with self._record_state_lock:
                        needs_probe = self._recovery_pending is not None
                    if needs_probe:
                        self._probe_current_record_status(obs_module)
                        self._resolve_expired_recovery()
                    worker = getattr(client, "worker", None)
                    if isinstance(worker, threading.Thread):
                        worker.join(timeout=poll_interval)
                    else:
                        stop_event.wait(timeout=poll_interval)
                    continue

                self._reconnecting = True
                if client is not None:
                    self._begin_pending_recovery()
                    with self._client_lock:
                        if self._client is client:
                            self._client = None
                    self._close_client(client)
                    with self._stream_state_lock:
                        self._stream_active = False
                        self._stream_active_from_probe = False
                        self._stream_status_checked = False
                    if stop_event.is_set():
                        break
                    self._status = (
                        f"OBS接続が切断されました。再接続待機中: {self._host}:{self._port}"
                    )
                    logger.warning(self._status)
                    self._notify_status_change(self._status)

                if stop_event.wait(timeout=self._reconnect_interval):
                    break
                if self._is_endpoint_reachable():
                    connected, permanent = self._connect_once(obs_module, is_reconnect=True)
                    if permanent:
                        with self._record_state_lock:
                            pending = self._recovery_pending
                            self._recovery_pending = None
                        if pending is not None:
                            logger.warning("OBSに再接続できず録画の終了を確認できなかったため、自動処理しません")
                        break
                    if connected and stop_event.wait(timeout=poll_interval):
                        break
                else:
                    # 猶予を過ぎた周回でも、再接続の確認を先に行う。
                    self._resolve_expired_recovery(endpoint_unreachable=True)
        finally:
            self._reconnecting = False

    def start(self) -> None:
        """Connect to OBS and subscribe to record/stream state events.

        Never raises: connection failures (OBS not running, wrong password,
        WebSocket disabled, missing dependency) are captured into ``status``.
        When ``auto_reconnect`` is enabled, transient failures and subsequent
        OBS restarts are retried in the background until ``stop()`` is called.
        """
        with self._record_state_lock:
            if self._stopped:
                self._stop_event = threading.Event()
            self._stopped = False
        try:
            import obsws_python as obs  # type: ignore[import-not-found]
        except ImportError:
            self._status = "error: obsws-python がインストールされていません (pip install obsws-python)"
            logger.warning(self._status)
            return
        connected, permanent = self._connect_once(obs, is_reconnect=False)
        if self._auto_reconnect and not permanent and not self._stopped:
            if not connected:
                self._reconnecting = True
            monitor = threading.Thread(
                target=self._monitor_connection,
                args=(obs,),
                daemon=True,
                name="obs-ws-monitor",
            )
            self._monitor_thread = monitor
            monitor.start()

    def stop(self) -> None:
        """Disconnect from OBS. Safe to call multiple times / before start."""
        with self._record_state_lock:
            self._stopped = True
            self._stop_event.set()
            # 利用者が停止した場合は保留中の録画と処理済みの記録を破棄する。
            self._record_seq += 1
            self._record_active = False
            self._record_paused = False
            self._record_started_at = None
            self._record_path = None
            self._recovery_pending = None
            self._recovery_candidate = None
            self._notified_record_stop_keys.clear()
            self._handled_record_keys.clear()
        self._reconnecting = False
        with self._client_lock:
            cl = self._client
            self._client = None
        if cl is not None:
            self._close_client(cl)
        monitor = self._monitor_thread
        self._monitor_thread = None
        if monitor is not None and monitor is not threading.current_thread():
            try:
                monitor.join(timeout=5.0)
            except Exception:
                pass
        self._join_workers()
        with self._stream_state_lock:
            self._stream_active = False
            self._stream_active_from_probe = False
            self._stream_status_checked = False
        self._status = "stopped"

    # --- obsws-python event callbacks -------------------------------------
    # Method names MUST follow the on_<snake_case_event> convention so the
    # library's callback registry matches them to RecordStateChanged /
    # StreamStateChanged.

    def on_record_state_changed(self, data: object) -> None:
        """Handle RecordStateChanged and fire only when trigger=record."""
        if self._trigger != "record":
            return
        try:
            state = _get(data, "output_state", "outputState")
            path = _get(data, "output_path", "outputPath")
            if state == OBS_WEBSOCKET_OUTPUT_STARTED:
                self._on_record_started_event(str(path) if path else None)
                return
            if state in (OBS_WEBSOCKET_OUTPUT_PAUSED, OBS_WEBSOCKET_OUTPUT_RESUMED):
                self._on_record_pause_changed(state == OBS_WEBSOCKET_OUTPUT_PAUSED)
                return
            if state != OBS_WEBSOCKET_OUTPUT_STOPPED:
                return
            with self._record_state_lock:
                if self._stopped:
                    return
                self._record_seq += 1
                session = self._record_session
                # 切断中に終わった録画Aが候補のまま、後続録画BのSTOPPEDが届く
                # ことがある。Bと別ファイルならAを取りこぼさないよう先に処理する。
                previous = self._recovery_pending or self._recovery_candidate
                self._recovery_pending = None
                self._recovery_candidate = None
                self._record_active = False
                self._record_paused = False
                self._record_started_at = None
                self._record_path = None
                stop_event = self._stop_event
            if (
                previous is not None
                and previous.path is not None
                and (not path or not self._same_recording_file(previous.path, str(path)))
            ):
                self._dispatch_recovered(previous, stop_event)
            if path:
                logger.info("OBS録画停止: %s", path)
                self._dispatch(str(path), session, stop_event)
        except Exception:
            logger.exception("OBS録画状態の変更を処理できませんでした")

    def on_record_file_changed(self, data: object) -> None:
        """Track RecordFileChanged (file splitting) so recovery uses the live file."""
        if self._trigger != "record":
            return
        try:
            path = _get(data, "new_output_path", "newOutputPath")
            with self._record_state_lock:
                if not self._stopped and path:
                    self._record_path = str(path)
                    self._recovery_candidate = None
        except Exception:
            logger.exception("OBS録画ファイルの切り替えを処理できませんでした")

    def on_stream_state_changed(self, data: object) -> None:
        """Handle stream start/stop, optionally without a local recording."""
        try:
            state = _get(data, "output_state", "outputState")
            if (
                self._trigger != "stream"
                and self._stream_started_callback is None
                and self._stream_finished_callback is None
            ):
                return
            if state == OBS_WEBSOCKET_OUTPUT_STARTED:
                is_probe = bool(
                    _get(
                        data,
                        "_stream_status_probe",
                        "_streamStatusProbe",
                    )
                )
                with self._stream_state_lock:
                    if self._stream_active:
                        if is_probe or not self._stream_active_from_probe:
                            return
                    else:
                        self._stream_active = True
                    self._stream_active_from_probe = is_probe
                logger.info("OBS stream started")
                if self._stream_started_callback is not None:
                    accepts_context = bool(
                        getattr(
                            self._stream_started_callback,
                            "_accepts_obs_stream_start_context",
                            False,
                        )
                    )
                    if is_probe and accepts_context:
                        self._dispatch_stream_callback(
                            self._stream_started_callback,
                            stream_start_context={
                                "source": "probe",
                                "output_duration_ms": _get(
                                    data,
                                    "output_duration",
                                    "outputDuration",
                                ),
                            },
                        )
                    else:
                        self._dispatch_stream_callback(
                            self._stream_started_callback
                        )
                return
            if state != OBS_WEBSOCKET_OUTPUT_STOPPED:
                return
            with self._stream_state_lock:
                self._stream_active = False
                self._stream_active_from_probe = False
            logger.info("OBS stream stopped")
            if self._stream_finished_callback is not None:
                self._dispatch_stream_callback(self._stream_finished_callback)
            else:
                msg = "配信停止を検知しました"
                self._status = msg
                logger.info(msg)
        except Exception:
            logger.exception("on_stream_state_changed failed")

    # --- internals --------------------------------------------------------

    def _probe_current_stream_status(self, obs_module: object) -> None:
        """Detect a stream that was already active when the listener connected.

        EventClient only receives future StreamStateChanged events. A short-lived
        ReqClient query closes that gap. Query failure is non-fatal: the event
        listener remains connected and a compatibility fallback can still run.
        """
        request_client = None
        try:
            request_client = obs_module.ReqClient(  # type: ignore[attr-defined]
                host=self._host,
                port=self._port,
                password=self._password,
                timeout=5,
            )
            response = request_client.get_stream_status()
            with self._stream_state_lock:
                self._stream_status_checked = True
            if bool(getattr(response, "output_active", False)):
                event = {
                    "outputState": OBS_WEBSOCKET_OUTPUT_STARTED,
                    "_streamStatusProbe": True,
                }
                output_duration = _get(
                    response,
                    "output_duration",
                    "outputDuration",
                )
                if output_duration is not None:
                    event["outputDuration"] = output_duration
                self.on_stream_state_changed(event)
        except Exception as exc:
            logger.warning("OBS stream status probe failed: %s", exc)
        finally:
            disconnect = getattr(request_client, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass

    @staticmethod
    def _record_key(path: str) -> str:
        # realpath で別名パス(シンボリックリンク等)も同じキーにそろえる。
        return os.path.normcase(os.path.realpath(path))

    @classmethod
    def _same_recording_file(cls, first: str, second: str) -> bool:
        """同じ録画ファイルか判定する(シンボリックリンク等の別名も同一扱い)。"""
        if cls._record_key(first) == cls._record_key(second):
            return True
        try:
            return os.path.samefile(first, second)
        except OSError:
            return False

    def _on_record_started_event(self, path: str | None) -> None:
        with self._record_state_lock:
            if self._stopped:
                return
            self._record_seq += 1
            previous = self._recovery_pending or self._recovery_candidate
            self._recovery_pending = None
            self._recovery_candidate = None
            self._record_session += 1
            session = self._record_session
            self._record_active = True
            self._record_paused = False
            self._record_started_at = time.time()
            self._record_path = path
            stop_event = self._stop_event
        if previous is not None:
            self._dispatch_recovered(previous, stop_event)
        logger.info("OBS録画開始%s", f": {path}" if path else "")
        self._start_record_identification(session)

    def _on_record_pause_changed(self, paused: bool) -> None:
        with self._record_state_lock:
            if self._stopped or (not self._record_active and self._recovery_pending is None):
                return
            self._continue_pending_recording()
            self._record_seq += 1
            self._record_paused = paused
            session = self._record_session
        if not paused:
            self._start_record_identification(session)

    def _continue_pending_recording(self) -> None:
        """Restore the session under the state lock after OBS confirms activity."""
        pending = self._recovery_pending
        if pending is None:
            return
        self._recovery_pending = None
        self._record_active = True
        # 再接続後にファイル切替が届いていれば、そのパスを優先する。
        if self._record_path is None and pending.path is not None:
            self._record_path = pending.path
            self._recovery_candidate = pending

    def _open_request_client(self, obs_module: object):
        return obs_module.ReqClient(  # type: ignore[attr-defined]
            host=self._host,
            port=self._port,
            password=self._password,
            timeout=5,
        )

    @staticmethod
    def _disconnect_request_client(request_client: object | None) -> None:
        disconnect = getattr(request_client, "disconnect", None)
        if callable(disconnect):
            try:
                disconnect()
            except Exception:
                pass

    def _start_record_identification(self, session: int) -> None:
        with self._record_state_lock:
            candidate = self._recovery_candidate
            if (
                self._stopped or not self._record_active or self._record_paused
                or session != self._record_session
                or (self._record_path is not None and candidate is None)
            ):
                return
            seq = self._record_seq
            started_at = self._record_started_at
            stop_event = self._stop_event
            client = self._client
            obs_module = self._obs_module
        if client is None or obs_module is None:
            return

        # 切断後に増えたファイルや、ファイル切替イベントで得たパスを上書きしない。
        def still_identifying() -> bool:
            return (
                not stop_event.is_set()
                and seq == self._record_seq
                and candidate is self._recovery_candidate
                and (self._record_path is None or candidate is not None)
                and client is self._client
                and self._is_client_alive(client)
            )

        def identify() -> None:
            with self._record_state_lock:
                if not still_identifying():
                    return
            request_client = None
            try:
                request_client = self._open_request_client(obs_module)
                response = request_client.get_record_directory()
                directory = _get(response, "record_directory", "recordDirectory")
            except Exception as exc:
                logger.debug("OBS録画フォルダを取得できませんでした: %s", exc)
                return
            finally:
                self._disconnect_request_client(request_client)
            if not directory:
                return

            previous: dict[Path, tuple[int, int, int]] = {}
            for sample in range(_RECORD_IDENTIFY_SAMPLES):
                if sample and stop_event.wait(_RECORD_IDENTIFY_INTERVAL_SECONDS):
                    return
                with self._record_state_lock:
                    if not still_identifying():
                        return
                try:
                    current = self._sample_record_files(Path(str(directory)), started_at)
                except OSError as exc:
                    logger.warning("OBS録画フォルダを読めませんでした: %s (%s)", directory, exc)
                    return
                growing = [
                    path for path, (device, inode, size) in current.items()
                    if path in previous
                    and (device, inode) == previous[path][:2]
                    and size > previous[path][2]
                ]
                if len(growing) == 1:
                    path = str(growing[0])
                    with self._record_state_lock:
                        if not still_identifying():
                            return
                        self._record_path = path
                        self._recovery_candidate = None
                        if (
                            candidate is None or candidate.path is None
                            or self._record_key(path) == self._record_key(candidate.path)
                        ):
                            return
                        self._record_seq += 1
                        self._record_session += 1
                    self._dispatch_recovered(candidate, stop_event)
                    return
                previous = current

        self._spawn_worker(identify)

    @staticmethod
    def _sample_record_files(
        directory: Path, started_at: float | None
    ) -> dict[Path, tuple[int, int, int]]:
        sizes = {}
        for entry in directory.iterdir():
            if entry.suffix.lower() not in RECORDING_EXTENSIONS:
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            if not stat.S_ISREG(info.st_mode):
                continue
            if started_at is not None and info.st_mtime < started_at - _RECORD_START_SLACK_SECONDS:
                continue
            sizes[entry] = (info.st_dev, info.st_ino, info.st_size)
        return sizes

    def _probe_current_record_status(self, obs_module: object) -> None:
        """Reconcile recording state with OBS after connecting or a failed probe."""
        with self._record_state_lock:
            seq = self._record_seq
            if self._stopped:
                return
        request_client = None
        try:
            request_client = self._open_request_client(obs_module)
            response = request_client.get_record_status()
            active = _get(response, "output_active", "outputActive")
            if not isinstance(active, bool):
                raise ValueError("OBSの応答に録画状態がありません")
            paused = _get(response, "output_paused", "outputPaused") is True
            duration_ms = _get(response, "output_duration", "outputDuration")
            observed_wall_time = time.time()
        except Exception as exc:
            logger.debug("OBS録画状態を取得できませんでした: %s", exc)
            return
        finally:
            self._disconnect_request_client(request_client)

        try:
            duration = float(duration_ms) / 1000.0
        except (TypeError, ValueError, OverflowError):
            duration = None
        if duration is not None and (not math.isfinite(duration) or duration < 0):
            duration = None

        to_dispatch = None
        with self._record_state_lock:
            if seq != self._record_seq or self._stopped:
                return
            if active:
                self._continue_pending_recording()
                self._record_paused = paused
                if not self._record_active:
                    self._record_session += 1
                    self._record_active = True
                    self._record_started_at = (
                        observed_wall_time - duration if duration is not None else None
                    )
            else:
                to_dispatch = self._recovery_pending
                self._recovery_pending = None
                self._recovery_candidate = None
                self._record_active = False
                self._record_paused = False
                self._record_path = None
                self._record_started_at = None
            session = self._record_session
            stop_event = self._stop_event
        if to_dispatch is not None:
            self._dispatch_recovered(to_dispatch, stop_event)
        if active:
            self._start_record_identification(session)

    def _begin_pending_recovery(self) -> None:
        with self._record_state_lock:
            if self._stopped or not self._record_active:
                return
            self._recovery_pending = _PendingRecording(
                self._record_session, self._record_path, time.monotonic()
            )
            self._recovery_candidate = None
            self._record_seq += 1
            self._record_active = False
            self._record_paused = False
            self._record_path = None
        logger.warning("録画中にOBS接続が切断されました。再接続を待ちます")

    def _resolve_expired_recovery(self, *, endpoint_unreachable: bool = False) -> None:
        with self._record_state_lock:
            pending = self._recovery_pending
            if (
                self._stopped or pending is None or pending.offline_attempted
                or time.monotonic() - pending.disconnected_at < _RECOVERY_GRACE_SECONDS
            ):
                return
            stop_event = self._stop_event
        if not endpoint_unreachable and self._is_endpoint_reachable():
            return
        with self._record_state_lock:
            if (
                stop_event.is_set() or pending is not self._recovery_pending
                or pending.offline_attempted
            ):
                return
            pending.offline_attempted = True
        logger.warning("猶予時間内にOBSへ再接続できませんでした")
        self._dispatch_recovered(pending, stop_event, offline=True)

    def _dispatch_recovered(
        self, pending: _PendingRecording, stop_event: threading.Event, *, offline: bool = False
    ) -> None:
        if stop_event.is_set():
            return
        if pending.path is None:
            if not pending.offline_attempted or offline:
                logger.warning("録画中にファイルを特定できなかったため、OBS切断後の処理を見送ります")
            return
        logger.info("OBS切断後の録画ファイルを確認します: %s", pending.path)
        self._dispatch(
            pending.path,
            pending.session,
            stop_event,
            stable_checks=_RECOVERY_STABLE_CHECKS,
            stable_attempts=_RECOVERY_STABLE_ATTEMPTS,
            offline_pending=pending if offline else None,
        )

    def _dispatch_stream_callback(
        self,
        callback: Callable,
        *args,
        **kwargs,
    ) -> None:
        """Invoke lightweight callbacks inline to preserve OBS event order."""
        if self._stopped:
            return
        try:
            callback(*args, **kwargs)
        except Exception:
            logger.exception("OBSイベントのコールバックに失敗しました")

    def _dispatch(
        self,
        path: str,
        session: int,
        stop_event: threading.Event,
        *,
        stable_checks: int | None = None,
        stable_attempts: int = 1,
        offline_pending: _PendingRecording | None = None,
    ) -> None:
        key = (session, self._record_key(path))
        with self._record_state_lock:
            if stop_event.is_set() or key in self._handled_record_keys:
                return
            if offline_pending is not None and offline_pending is not self._recovery_pending:
                return
            stopped_callback = self._recording_stopped_callback
            if key in self._notified_record_stop_keys:
                stopped_callback = None
            elif stopped_callback is not None:
                self._notified_record_stop_keys.add(key)
        if stopped_callback is not None:
            self._dispatch_stream_callback(stopped_callback, path)

        def work() -> None:
            try:
                for _ in range(stable_attempts):
                    if stop_event.is_set():
                        return
                    if stable_checks is None:
                        stable = wait_until_file_stable(path)
                    else:
                        stable = wait_until_file_stable(path, checks=stable_checks)
                    if stable:
                        break
                else:
                    if not stop_event.is_set():
                        self._status = f"録画ファイルが安定しません: {path}"
                        logger.warning(self._status)
                    return

                # 安定判定中にOBSが戻った場合は、再接続後の録画状態を優先する。
                if stop_event.is_set():
                    return
                if offline_pending is not None and self._is_endpoint_reachable():
                    with self._record_state_lock:
                        offline_pending.offline_attempted = False
                    return
                with self._record_state_lock:
                    if stop_event.is_set() or key in self._handled_record_keys:
                        return
                    if self._record_active and self._record_path is not None:
                        if self._record_key(self._record_path) == key[1]:
                            return
                    if offline_pending is not None:
                        if self._recovery_pending is not offline_pending:
                            return
                        self._recovery_pending = None
                        self._record_started_at = None
                    self._handled_record_keys.add(key)
                    self._status = f"処理中: {path}"
                if not stop_event.is_set():
                    self._callback(path)
            except Exception:
                logger.exception("OBS録画ファイルの処理を開始できませんでした")

        self._spawn_worker(work)


class FolderWatcher(_WorkerMixin):
    """Watch a directory for new video files and fire when writes complete.

    Uses ``watchdog`` to detect file creation/move-in events, then waits for
    the file size to stabilise before invoking the callback with the absolute
    path. The stability wait runs on a worker thread so the watchdog observer
    thread is never blocked.
    """

    def __init__(
        self,
        watch_dir: str | Path,
        on_recording_finished: OnRecordingFinished,
        extensions: Sequence[str] = RECORDING_EXTENSIONS,
    ) -> None:
        super().__init__()
        self._stopped = False
        self._dir = str(watch_dir)
        self._callback = on_recording_finished
        self._extensions = tuple(e.lower() for e in extensions)
        self._observer = None
        self._status = "stopped"

    @property
    def status(self) -> str:
        return self._status

    def start(self) -> None:
        """Start the watchdog observer. Captures missing deps / bad folder."""
        try:
            from watchdog.observers import Observer  # type: ignore[import-not-found]
            from watchdog.events import FileSystemEventHandler  # type: ignore[import-not-found]
        except ImportError:
            self._status = "error: watchdog がインストールされていません (pip install watchdog)"
            logger.warning(self._status)
            return
        if not self._dir or not Path(self._dir).is_dir():
            self._status = f"error: 監視フォルダが見つかりません: {self._dir}"
            logger.warning(self._status)
            return

        watcher_ref = self

        class _Handler(FileSystemEventHandler):
            def on_created(self, event):  # noqa: D401 - watchdog callback
                if not event.is_directory:
                    watcher_ref._handle_event(event.src_path)

            def on_moved(self, event):  # noqa: D401 - watchdog callback
                if event.is_directory:
                    return
                dest = getattr(event, "dest_path", None)
                watcher_ref._handle_event(dest or event.src_path)

        try:
            self._observer = Observer()
            self._observer.schedule(_Handler(), self._dir, recursive=False)
            self._observer.start()
            self._status = f"監視中: {self._dir}"
            logger.info(self._status)
        except Exception as e:
            self._observer = None
            self._status = f"監視開始エラー: {e}"
            logger.warning(self._status)

    def stop(self) -> None:
        """Stop the observer and join worker threads."""
        self._stopped = True
        obs = self._observer
        self._observer = None
        if obs is not None:
            try:
                obs.stop()
                obs.join(timeout=5)
            except Exception:
                pass
        self._join_workers()
        self._status = "stopped"

    def retry_latest(self) -> str:
        """Re-scan the watched folder and dispatch its newest recording.

        The existing observer is left untouched. The selected file follows the
        same extension filter, stability wait, and callback path as a normal
        watchdog event. Only direct children are considered because the live
        observer is configured with ``recursive=False`` as well.
        """
        watch_dir = Path(self._dir)
        if not self._dir or not watch_dir.is_dir():
            self._status = f"再検出できません: 監視フォルダが見つかりません: {self._dir}"
            logger.warning(self._status)
            return self._status

        candidates: list[tuple[int, str, Path]] = []
        try:
            for path in watch_dir.iterdir():
                try:
                    if not path.is_file() or path.suffix.lower() not in self._extensions:
                        continue
                    candidates.append((path.stat().st_mtime_ns, path.name, path))
                except OSError:
                    # A file may disappear or become unreadable while OBS is
                    # finalising/moving it. Other candidates can still retry.
                    continue
        except OSError as exc:
            self._status = f"監視フォルダの再検出に失敗しました: {exc}"
            logger.warning(self._status)
            return self._status

        if not candidates:
            self._status = f"再試行対象の録画ファイルがありません: {self._dir}"
            logger.info(self._status)
            return self._status

        latest = max(candidates, key=lambda item: (item[0], item[1]))[2].resolve()
        outcome = f"最新録画を再検出: {latest}"
        self._status = outcome
        logger.info(outcome)
        self._handle_event(str(latest))
        return outcome

    # --- internals --------------------------------------------------------

    def _handle_event(self, path: Optional[str]) -> None:
        """Filter by extension and spawn a stability-wait worker.

        Public-by-convention so tests can simulate a watchdog event without
        spinning up the real observer.
        """
        if not path:
            return
        ext = Path(path).suffix.lower()
        if ext not in self._extensions:
            return
        abs_path = str(Path(path).resolve())

        def _work() -> None:
            try:
                if not wait_until_file_stable(abs_path):
                    self._status = f"ファイルが安定しません: {abs_path}"
                    logger.warning(self._status)
                    return
                self._status = f"処理中: {abs_path}"
                if self._stopped:
                    return
                self._callback(abs_path)
            except Exception:
                logger.exception("FolderWatcher dispatch failed")
            finally:
                with self._workers_lock:
                    self._workers = [w for w in self._workers if w.is_alive()]

        self._spawn_worker(_work)


def create_watcher(
    method: str,
    config: dict,
    on_recording_finished: OnRecordingFinished,
    on_stream_started: OnStreamStarted | None = None,
    on_stream_finished: OnStreamFinished | None = None,
    on_recording_stopped: OnRecordingStopped | None = None,
    on_status_change: OnStatusChange | None = None,
):
    """Factory: build a watcher by ``method`` ("websocket" | "folder").

    ``config`` keys: host, port, password, stop_event (websocket);
    watch_folder / watch_dir, extensions (folder). Unknown methods raise
    ValueError so wiring mistakes surface immediately.
    """
    method = (method or "websocket").lower()
    if method == "folder":
        return FolderWatcher(
            watch_dir=config.get("watch_folder") or config.get("watch_dir") or "",
            on_recording_finished=on_recording_finished,
            extensions=config.get("extensions", RECORDING_EXTENSIONS),
        )
    if method == "websocket":
        return ObsWebsocketWatcher(
            host=config.get("host", "localhost"),
            port=int(config.get("port", 4455)),
            password=config.get("password", ""),
            on_recording_finished=on_recording_finished,
            stop_event=config.get("stop_event", "record"),
            on_recording_stopped=on_recording_stopped,
            on_stream_started=on_stream_started,
            on_stream_finished=on_stream_finished,
            on_status_change=on_status_change,
            auto_reconnect=bool(config.get("auto_reconnect", True)),
            reconnect_interval=float(
                config.get("reconnect_interval", DEFAULT_RECONNECT_INTERVAL)
            ),
        )
    raise ValueError(f"未知の検知方式です: {method} (websocket または folder を指定してください)")
