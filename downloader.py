"""Remote video download using yt-dlp."""

import re
from pathlib import Path
from urllib.parse import urlsplit


TITLE_BYTE_LIMIT = 100


def is_youtube_url(input_path: str) -> bool:
    """Check if input is a YouTube URL."""
    return bool(re.match(
        r'https?://(www\.)?(youtube\.com|youtu\.be)/',
        str(input_path or "").strip(),
        flags=re.IGNORECASE,
    ))


_TWITCH_HOSTS = frozenset({"twitch.tv", "www.twitch.tv", "m.twitch.tv"})


def is_twitch_url(input_path: str) -> bool:
    """Check if input is a Twitch channel or VOD URL."""
    try:
        parsed = urlsplit(str(input_path or "").strip())
    except ValueError:
        return False
    hostname = (parsed.hostname or "").lower()
    return (
        parsed.scheme.lower() in {"http", "https"}
        and hostname in _TWITCH_HOSTS
        and bool(parsed.path.strip("/"))
    )


def get_url_source(input_path: str) -> str | None:
    """Return the supported remote source name, or ``None`` for local input."""
    if is_youtube_url(input_path):
        return "youtube"
    if is_twitch_url(input_path):
        return "twitch"
    return None


def build_output_template(output_dir: Path) -> str:
    """Return the yt-dlp outtmpl for videos in this output_dir.

    The title portion is byte-limited (not char-limited) via yt-dlp's
    ``%(title).{N}B`` formatter so multi-byte Japanese titles don't blow
    through the Windows MAX_PATH (~260) limit when the dir already sits
    under a deep OneDrive / Desktop path. ``B`` trims on UTF-8 boundaries,
    so we never end up with a broken codepoint in the filename.
    """
    return str(output_dir / f"%(title).{TITLE_BYTE_LIMIT}B.%(ext)s")


def download_video(url: str, output_dir: Path) -> Path:
    """Download a supported remote video URL and return the local file path."""
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
