"""YouTube video download using yt-dlp."""

import re
from pathlib import Path

import yt_dlp


def is_youtube_url(input_path: str) -> bool:
    """Check if input is a YouTube URL."""
    return bool(re.match(
        r'https?://(www\.)?(youtube\.com|youtu\.be)/', input_path
    ))


def download_video(url: str, output_dir: Path) -> Path:
    """Download YouTube video and return the local file path."""
    output_dir.mkdir(parents=True, exist_ok=True)
    output_template = str(output_dir / "%(title)s.%(ext)s")

    ydl_opts = {
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "outtmpl": output_template,
    }

    print(f"Downloading: {url}")
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        # Ensure .mp4 extension after merge
        filepath = Path(filepath).with_suffix(".mp4")

    print(f"Downloaded: {filepath}")
    return filepath
