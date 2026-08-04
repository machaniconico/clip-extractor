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
import threading
import time
from pathlib import Path
from typing import Callable, Optional, Sequence

logger = logging.getLogger("clip-extractor.obs")

#: Video extensions recognised by the folder watcher.
RECORDING_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".flv", ".mov")

#: obs-websocket output-state constants emitted when an output starts/stops.
OBS_WEBSOCKET_OUTPUT_STARTED = "OBS_WEBSOCKET_OUTPUT_STARTED"
OBS_WEBSOCKET_OUTPUT_STOPPED = "OBS_WEBSOCKET_OUTPUT_STOPPED"

#: Type alias for the shared completion callback.
OnRecordingFinished = Callable[[str], None]
OnRecordingStopped = Callable[[str], None]
OnStreamStarted = Callable[[], None]
OnStreamFinished = Callable[[], None]


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
    ) -> None:
        super().__init__()
        self._stopped = False
        self._host = host
        self._port = int(port)
        self._password = password
        self._callback = on_recording_finished
        self._recording_stopped_callback = on_recording_stopped
        self._stream_started_callback = on_stream_started
        self._stream_finished_callback = on_stream_finished
        self._trigger = (stop_event or "record").lower()
        self._client = None
        self._status = "stopped"
        self._stream_state_lock = threading.Lock()
        self._stream_active = False
        self._stream_status_checked = False

    @property
    def status(self) -> str:
        """Human-readable connection/handler status (safe to poll from UI)."""
        return self._status

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

    def start(self) -> None:
        """Connect to OBS and subscribe to record/stream state events.

        Never raises: connection failures (OBS not running, wrong password,
        WebSocket disabled, missing dependency) are captured into ``status``.
        """
        try:
            import obsws_python as obs  # type: ignore[import-not-found]
        except ImportError:
            self._status = "error: obsws-python がインストールされていません (pip install obsws-python)"
            logger.warning(self._status)
            return
        try:
            self._client = obs.EventClient(
                host=self._host,
                port=self._port,
                password=self._password,
                timeout=5,
            )
            self._client.callback.register(self.on_record_state_changed)
            self._client.callback.register(self.on_stream_state_changed)
            self._status = (
                f"connected: {self._host}:{self._port} (trigger={self._trigger})"
            )
            logger.info(self._status)
            if (
                self._trigger == "stream"
                or self._stream_started_callback is not None
                or self._stream_finished_callback is not None
            ):
                self._probe_current_stream_status(obs)
        except Exception as e:  # ConnectionRefusedError, TimeoutError, auth errors
            self._client = None
            self._status = f"接続失敗: {e}"
            logger.warning(self._status)

    def stop(self) -> None:
        """Disconnect from OBS. Safe to call multiple times / before start."""
        self._stopped = True
        cl = self._client
        self._client = None
        if cl is not None:
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
        self._join_workers()
        with self._stream_state_lock:
            self._stream_active = False
        self._status = "stopped"

    # --- obsws-python event callbacks -------------------------------------
    # Method names MUST follow the on_<snake_case_event> convention so the
    # library's callback registry matches them to RecordStateChanged /
    # StreamStateChanged.

    def on_record_state_changed(self, data: object) -> None:
        """Handle RecordStateChanged and fire only when trigger=record."""
        try:
            state = _get(data, "output_state", "outputState")
            path = _get(data, "output_path", "outputPath")
            if state != OBS_WEBSOCKET_OUTPUT_STOPPED:
                return
            if path:
                logger.info(f"OBS recording stopped: {path}")
            if self._trigger == "record":
                if path and self._recording_stopped_callback is not None:
                    self._dispatch_stream_callback(
                        self._recording_stopped_callback,
                        str(path),
                    )
                self._dispatch(path)
        except Exception:
            logger.exception("on_record_state_changed failed")

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
                with self._stream_state_lock:
                    if self._stream_active:
                        return
                    self._stream_active = True
                logger.info("OBS stream started")
                if self._stream_started_callback is not None:
                    self._dispatch_stream_callback(self._stream_started_callback)
                return
            if state != OBS_WEBSOCKET_OUTPUT_STOPPED:
                return
            with self._stream_state_lock:
                self._stream_active = False
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
                self.on_stream_state_changed(
                    {"outputState": OBS_WEBSOCKET_OUTPUT_STARTED}
                )
        except Exception as exc:
            logger.warning("OBS stream status probe failed: %s", exc)
        finally:
            disconnect = getattr(request_client, "disconnect", None)
            if callable(disconnect):
                try:
                    disconnect()
                except Exception:
                    pass

    def _dispatch_stream_callback(self, callback: Callable, *args) -> None:
        """Invoke lightweight callbacks inline to preserve OBS event order."""
        if self._stopped:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("OBS stream lifecycle callback failed")

    def _dispatch(self, path: Optional[str]) -> None:
        if not path:
            return

        def _work() -> None:
            try:
                if not wait_until_file_stable(path):
                    self._status = f"録画ファイルが安定しません: {path}"
                    logger.warning(self._status)
                    return
                self._status = f"処理中: {path}"
                if self._stopped:
                    return
                self._callback(str(path))
            except Exception:
                logger.exception("ObsWebsocketWatcher dispatch failed")

        self._spawn_worker(_work)


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
        )
    raise ValueError(f"未知の検知方式です: {method} (websocket または folder を指定してください)")
