"""Helpers for opening a prefilled X post composer.

This module deliberately stops at the compose screen.  It does not store X
credentials and it never publishes a post on the user's behalf.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse


X_POST_INTENT_URL = "https://x.com/intent/post"
DEFAULT_X_POST_TEMPLATE = "配信開始しました！\n{links}"


@dataclass(frozen=True)
class DestinationLink:
    """One user-configured simulcast destination."""

    label: str
    url: str


def _infer_label(url: str) -> str:
    hostname = (urlparse(url).hostname or "").lower()
    return hostname.removeprefix("www.") or "配信先"


def parse_destination_links(value: str | None) -> tuple[DestinationLink, ...]:
    """Parse one destination per line.

    Each line may be either ``URL`` or ``表示名|URL``.  Invalid and blank
    lines are ignored so a stray empty line never prevents OBS from starting.
    """
    links: list[DestinationLink] = []
    for raw_line in str(value or "").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if "|" in line:
            label, url = (part.strip() for part in line.split("|", 1))
        else:
            label, url = "", line
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            continue
        links.append(
            DestinationLink(
                label=label or _infer_label(url),
                url=url,
            )
        )
    return tuple(links)


def render_x_post_text(
    template: str | None,
    *,
    youtube_url: str = "",
    title: str = "",
    destinations: str | None = "",
) -> str:
    """Render a post template with current and configured stream links.

    Supported tokens are ``{youtube_url}`` (also ``{url}``), ``{title}``, and
    ``{links}``. Unknown braces are left untouched, which keeps ordinary text
    containing braces safe.
    """
    text = str(template or DEFAULT_X_POST_TEMPLATE)
    live_url = str(youtube_url or "").strip()
    configured = list(parse_destination_links(destinations))

    rendered_links: list[str] = []
    if live_url:
        rendered_links.append(f"YouTube: {live_url}")
    seen_urls = {live_url} if live_url else set()
    for link in configured:
        if link.url in seen_urls:
            continue
        seen_urls.add(link.url)
        rendered_links.append(f"{link.label}: {link.url}")

    replacements = {
        "youtube_url": live_url,
        "url": live_url,
        "title": str(title or "").strip(),
        "links": "\n".join(rendered_links),
    }
    for token, replacement in replacements.items():
        text = text.replace("{" + token + "}", replacement)

    # A template commonly ends up with an empty line where a lookup was not
    # available. Keep intentional spacing, but avoid a blank post body.
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def build_x_post_intent_url(text: str) -> str:
    """Build a fresh X compose URL for one post body."""
    return f"{X_POST_INTENT_URL}?{urlencode({'text': str(text or '')}, quote_via=quote)}"


def open_x_post_composer(text: str, *, opener=webbrowser.open) -> str:
    """Open the X compose screen and return the generated URL.

    ``opener`` is injectable so callers can test the side effect without
    launching a browser.
    """
    intent_url = build_x_post_intent_url(text)
    if not opener(intent_url, new=2, autoraise=True):
        raise RuntimeError("X投稿画面を開けませんでした")
    return intent_url
