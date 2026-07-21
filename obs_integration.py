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
OnStreamStarted = Callable[[], None]
OnStreamFinished = Callable[[Optional[str]], None]


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

    The trigger event is selected by ``stop_event``: ``"record"`` fires the
    callback when recording stops (the recording path is in the event);
    ``"stream"`` invokes the optional lifecycle callbacks.  A recording path
    from the same stream is supplied only as a best-effort fallback.
    """

    def __init__(
        self,
        host: str,
        port: int,
        password: str,
        on_recording_finished: OnRecordingFinished,
        stop_event: str = "stream",
        on_stream_started: OnStreamStarted | None = None,
        on_stream_finished: OnStreamFinished | None = None,
    ) -> None:
        super().__init__()
        self._stopped = False
        self._host = host
        self._port = int(port)
        self._password = password
        self._callback = on_recording_finished
        self._stream_started_callback = on_stream_started
        self._stream_finished_callback = on_stream_finished
        self._trigger = (stop_event or "stream").lower()
        self._client = None
        self._status = "stopped"
        self._last_record_path: Optional[str] = None
        self._state_lock = threading.Lock()

    @property
    def status(self) -> str:
        """Human-readable connection/handler status (safe to poll from UI)."""
        return self._status

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
        self._status = "stopped"

    # --- obsws-python event callbacks -------------------------------------
    # Method names MUST follow the on_<snake_case_event> convention so the
    # library's callback registry matches them to RecordStateChanged /
    # StreamStateChanged.

    def on_record_state_changed(self, data: object) -> None:
        """Handle RecordStateChanged: cache the path; fire when trigger=record."""
        try:
            state = _get(data, "output_state", "outputState")
            path = _get(data, "output_path", "outputPath")
            if state != OBS_WEBSOCKET_OUTPUT_STOPPED:
                return
            if path:
                with self._state_lock:
                    self._last_record_path = str(path)
                logger.info(f"OBS recording stopped: {path}")
            if self._trigger == "record":
                self._dispatch(path)
        except Exception:
            logger.exception("on_record_state_changed failed")

    def on_stream_state_changed(self, data: object) -> None:
        """Handle stream start/stop, optionally without a local recording."""
        try:
            state = _get(data, "output_state", "outputState")
            if self._trigger != "stream":
                return
            if state == OBS_WEBSOCKET_OUTPUT_STARTED:
                logger.info("OBS stream started")
                with self._state_lock:
                    self._last_record_path = None
                if self._stream_started_callback is not None:
                    self._dispatch_stream_callback(self._stream_started_callback)
                return
            if state != OBS_WEBSOCKET_OUTPUT_STOPPED:
                return
            logger.info("OBS stream stopped")
            path: Optional[str] = None
            with self._state_lock:
                path = self._last_record_path
                self._last_record_path = None
            if not path:
                # Stream-stop events don't carry a recording path; try to
                # recover it from OBS via GetRecordStatus.
                path = self._query_last_record_path()
            if self._stream_finished_callback is not None:
                self._dispatch_stream_callback(self._stream_finished_callback, path)
            elif path:
                self._dispatch(path)
            else:
                msg = "配信停止を検知しましたが録画パスを取得できませんでした（録画が同時に有効か確認してください）"
                self._status = msg
                logger.warning(msg)
        except Exception:
            logger.exception("on_stream_state_changed failed")

    # --- internals --------------------------------------------------------

    def _dispatch_stream_callback(self, callback: Callable, *args) -> None:
        """Invoke lightweight callbacks inline to preserve OBS event order."""
        if self._stopped:
            return
        try:
            callback(*args)
        except Exception:
            logger.exception("OBS stream lifecycle callback failed")

    def _query_last_record_path(self) -> Optional[str]:
        """Best-effort: ask OBS for the current recording path via ReqClient."""
        try:
            import obsws_python as obs  # type: ignore[import-not-found]
        except ImportError:
            return None
        rc = None
        try:
            rc = obs.ReqClient(
                host=self._host,
                port=self._port,
                password=self._password,
                timeout=5,
            )
            status = rc.get_record_status()
            output_active = _get(status, "output_active", "outputActive")
            if output_active is False:
                return None
            for attr in ("output_path", "recording_path", "outputPath", "recordingPath"):
                val = getattr(status, attr, None)
                if val:
                    return str(val)
        except Exception as e:
            logger.warning(f"GetRecordStatus での録画パス取得に失敗: {e}")
        finally:
            if rc is not None:
                try:
                    rc.base_client.ws.close()  # type: ignore[attr-defined]
                except Exception:
                    pass
        return None

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
            stop_event=config.get("stop_event", "stream"),
            on_stream_started=on_stream_started,
            on_stream_finished=on_stream_finished,
        )
    raise ValueError(f"未知の検知方式です: {method} (websocket または folder を指定してください)")
