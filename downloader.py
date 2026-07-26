"""YouTube video download using yt-dlp."""

import json
import re
import subprocess
from pathlib import Path


TITLE_BYTE_LIMIT = 100


def is_youtube_url(input_path: str) -> bool:
    """Check if input is a YouTube URL."""
    return bool(re.match(
        r'https?://(www\.)?(youtube\.com|youtu\.be)/', input_path
    ))


def build_output_template(output_dir: Path) -> str:
    """Return the yt-dlp outtmpl for videos in this output_dir.

    The title portion is byte-limited (not char-limited) via yt-dlp's
    ``%(title).{N}B`` formatter so multi-byte Japanese titles don't blow
    through the Windows MAX_PATH (~260) limit when the dir already sits
    under a deep OneDrive / Desktop path. ``B`` trims on UTF-8 boundaries,
    so we never end up with a broken codepoint in the filename.
    """
    return str(output_dir / f"%(title).{TITLE_BYTE_LIMIT}B.%(ext)s")


def _download_video(
    url: str,
    output_dir: Path,
    *,
    allow_incomplete_live_formats: bool = False,
) -> Path:
    """Download a YouTube video, optionally including post-live DVR formats."""
    import yt_dlp

    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = build_output_template(output_dir)

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
        # Keep yt-dlp's default Deno candidate and add the Node.js runtime
        # installed by setup.bat. Node must be opted into explicitly.
        "js_runtimes": {
            "deno": {"path": None},
            "node": {"path": None},
        },
        "retries": 10,
        "fragment_retries": 10,
        "file_access_retries": 5,
        "retry_sleep_functions": {
            "http": lambda n: min(2 ** n, 30),
            "fragment": lambda n: min(2 ** n, 30),
        },
        "continuedl": True,
        "socket_timeout": 30,
        "http_chunk_size": 10485760,
        "concurrent_fragment_downloads": 1,
    }
    if allow_incomplete_live_formats:
        # Immediately after a broadcast ends, YouTube's player can keep
        # serving the DVR HLS/DASH fragments while the final VOD is still
        # processing. yt-dlp intentionally hides these potentially incomplete
        # formats unless they are explicitly requested.
        ydl_opts["extractor_args"] = {
            "youtube": {
                "formats": ["incomplete"],
            },
        }
        ydl_opts["fragment_retries"] = 20
        ydl_opts["extractor_retries"] = 5

    print(f"Downloading: {url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = Path(ydl.prepare_filename(info))

    # merge_output_format=mp4 normally produces a .mp4 sibling; if it does,
    # prefer it. Otherwise fall back to whatever yt-dlp actually wrote so we
    # never return a path that does not exist on disk.
    merged = filepath.with_suffix(".mp4")
    if merged.exists():
        filepath = merged
    elif not filepath.exists():
        raise FileNotFoundError(
            f"yt-dlp reported success but neither {merged} nor {filepath} exists."
        )

    print(f"Downloaded: {filepath}")
    return filepath


def download_video(url: str, output_dir: Path) -> Path:
    """Download a fully processed YouTube video and return its local path."""
    return _download_video(url, output_dir)


def _probe_duration(video_path: Path) -> float:
    """Return media duration in seconds using ffprobe."""
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(video_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    payload = json.loads(result.stdout)
    return float(payload.get("format", {}).get("duration", 0) or 0)


def download_post_live_video(
    url: str,
    output_dir: Path,
    *,
    expected_duration_seconds: float | None = None,
) -> Path:
    """Download YouTube's post-live DVR fragments before final VOD processing.

    A successful fragment transfer is not sufficient: a transient post-live
    manifest can omit part of a broadcast. When the OBS lifecycle supplies an
    expected duration, reject captures shorter than 95% so the caller can fall
    back to the fully processed archive.
    """
    filepath = _download_video(
        url,
        output_dir,
        allow_incomplete_live_formats=True,
    )
    expected = max(0.0, float(expected_duration_seconds or 0))
    if expected <= 0:
        return filepath

    actual = _probe_duration(filepath)
    minimum = expected * 0.95
    if actual < minimum:
        raise RuntimeError(
            "post-live DVRの取得尺が不足しています "
            f"(取得={actual:.1f}秒, 期待={expected:.1f}秒, 必要={minimum:.1f}秒以上)"
        )
    return filepath
