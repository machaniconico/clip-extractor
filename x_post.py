"""Helpers for publishing or manually composing an X stream announcement.

Automatic publishing is used only when the user explicitly opts in and
provides all four OAuth 1.0a user-context credentials.  Otherwise callers keep
the existing behavior of opening a prefilled composer for manual review.
Credential persistence belongs to the UI layer; this module only uses values
provided for one request and never logs them.
"""

from __future__ import annotations

import re
import webbrowser
from dataclasses import dataclass
from urllib.parse import parse_qs, quote, urlencode, urlparse


X_POST_INTENT_URL = "https://x.com/intent/post"
X_API_POST_URL = "https://api.x.com/2/tweets"
X_API_TIMEOUT = (5, 15)
DEFAULT_X_POST_TEMPLATE = "{title}\n配信開始しました！\n{links}"


class XPostError(RuntimeError):
    """A user-safe X publishing error that never includes API response data."""


class XPostTooLongError(ValueError):
    """The rendered post cannot fit X's weighted limit even without a title.

    The message is Japanese and safe to show in the UI. It is deliberately not
    an :class:`XPostError` so callers never treat it as an API failure that
    falls back to the manual composer.
    """


# twitter-text v3 configuration (https://github.com/twitter/twitter-text).
X_MAX_WEIGHTED_LENGTH = 280
_X_DEFAULT_WEIGHT = 200
_X_URL_LENGTH = 23
_X_WEIGHT_100_RANGES = (
    (0x0000, 0x10FF),
    (0x2000, 0x200D),
    (0x2010, 0x201F),
    (0x2032, 0x2037),
)
_X_URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
_X_SKIN_TONES = range(0x1F3FB, 0x1F400)
_X_REGIONAL = range(0x1F1E6, 0x1F200)
_X_ELLIPSIS = "\u2026"


def _x_char_weight(char: str) -> int:
    code = ord(char)
    for low, high in _X_WEIGHT_100_RANGES:
        if low <= code <= high:
            return 100
    return _X_DEFAULT_WEIGHT


def _x_plain_weight(text: str) -> int:
    """Weight of text without URLs, in twitter-text units (scale 100).

    Each code point is weighed individually, except that an emoji sequence
    (base + variation selector / skin tone / keycap, ZWJ-joined parts, or a
    regional-indicator flag pair) counts once as 200, matching twitter-text's
    "every emoji is 2" rule. Detection is a simple structural heuristic, not
    the full Unicode emoji regex, so exotic sequences may differ slightly.
    """
    total = 0
    i = 0
    n = len(text)
    while i < n:
        start = i
        code = ord(text[i])
        i += 1
        if code in _X_REGIONAL and i < n and ord(text[i]) in _X_REGIONAL:
            i += 1
        while i < n:
            nxt = ord(text[i])
            if nxt in (0xFE0F, 0x20E3) or nxt in _X_SKIN_TONES:
                i += 1
            elif nxt == 0x200D and i + 1 < n:
                i += 2
            else:
                break
        if i - start > 1 and any(
            ord(c) in (0xFE0F, 0x20E3, 0x200D) or ord(c) in _X_SKIN_TONES
            or ord(c) in _X_REGIONAL
            for c in text[start:i]
        ):
            total += _X_DEFAULT_WEIGHT
        else:
            total += sum(_x_char_weight(c) for c in text[start:i])
    return total


def x_weighted_length(text: str) -> int:
    """Return the twitter-text v3 weighted length of a post (limit 280).

    Every http(s) URL counts as 23 regardless of its real length.
    """
    text = str(text or "")
    total = 0
    last = 0
    urls = 0
    for match in _X_URL_RE.finditer(text):
        total += _x_plain_weight(text[last:match.start()])
        last = match.end()
        urls += 1
    total += _x_plain_weight(text[last:])
    return (total + 99) // 100 + urls * _X_URL_LENGTH


@dataclass(frozen=True, repr=False)
class XCredentials:
    """OAuth 1.0a user-context credentials for one X account."""

    api_key: str = ""
    api_key_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""

    def is_complete(self) -> bool:
        return all(
            value.strip()
            for value in (
                self.api_key,
                self.api_key_secret,
                self.access_token,
                self.access_token_secret,
            )
        )

    def any_set(self) -> bool:
        return any(
            value.strip()
            for value in (
                self.api_key,
                self.api_key_secret,
                self.access_token,
                self.access_token_secret,
            )
        )

    def as_dict(self) -> dict[str, str]:
        """Return the storage representation; callers must treat it as secret."""
        return {
            "api_key": self.api_key.strip(),
            "api_key_secret": self.api_key_secret.strip(),
            "access_token": self.access_token.strip(),
            "access_token_secret": self.access_token_secret.strip(),
        }


@dataclass(frozen=True)
class XPostResult:
    """Identifiers returned after X accepts a new post."""

    post_id: str
    status_url: str


@dataclass(frozen=True)
class DestinationLink:
    """One user-configured simulcast destination."""

    label: str
    url: str


def _http_error_message(status_code: int) -> str:
    if status_code == 401:
        return "X APIの認証情報が拒否されました (HTTP 401)"
    if status_code == 403:
        return "X APIの投稿権限がありません (HTTP 403)"
    if status_code == 429:
        return "X APIの利用上限に達しました (HTTP 429)"
    if 400 <= status_code < 500:
        return f"X APIが投稿を受け付けませんでした (HTTP {status_code})"
    if status_code >= 500:
        return f"X APIで一時的な障害が発生しました (HTTP {status_code})"
    return f"X API投稿に失敗しました (HTTP {status_code})"


def post_x_post(
    text: str,
    credentials: XCredentials,
    *,
    session_factory=None,
) -> XPostResult:
    """Publish one post through X API v2 using OAuth 1.0a user context.

    The response body and underlying exception text are deliberately omitted
    from raised errors because providers and test doubles may echo request
    details.  Callers can safely show :class:`XPostError` messages in a status
    field without exposing credentials.
    """
    body = str(text or "")
    if not body.strip():
        raise XPostError("X投稿文が空です")
    if not isinstance(credentials, XCredentials) or not credentials.is_complete():
        raise XPostError("X API認証情報が不足しています")

    if session_factory is None:
        try:
            from requests_oauthlib import OAuth1Session
        except Exception:
            raise XPostError("X API認証ライブラリを読み込めませんでした") from None
        session_factory = OAuth1Session

    session = None
    try:
        session = session_factory(
            client_key=credentials.api_key.strip(),
            client_secret=credentials.api_key_secret.strip(),
            resource_owner_key=credentials.access_token.strip(),
            resource_owner_secret=credentials.access_token_secret.strip(),
        )
        response = session.post(
            X_API_POST_URL,
            json={"text": body},
            timeout=X_API_TIMEOUT,
        )
    except Exception:
        raise XPostError("X APIへの接続に失敗しました") from None
    finally:
        close = getattr(session, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    try:
        status_code = int(response.status_code)
    except (AttributeError, TypeError, ValueError):
        raise XPostError("X APIから不正な応答を受信しました") from None
    if status_code != 201:
        raise XPostError(_http_error_message(status_code))

    try:
        payload = response.json()
        post_id = str(payload["data"]["id"]).strip()
    except (AttributeError, KeyError, TypeError, ValueError):
        raise XPostError("X APIの成功応答に投稿IDがありません") from None
    if not post_id.isdigit():
        raise XPostError("X APIの成功応答に投稿IDがありません")

    return XPostResult(
        post_id=post_id,
        status_url=f"https://x.com/i/web/status/{post_id}",
    )


# Exact hostnames with human readable labels. Lookup is exact-equality only so
# suffix lookalikes (e.g. "eviltwitch.tv") never match.
_X_DESTINATION_LABEL_BY_HOST = {
    "twitch.tv": "Twitch",
    "www.twitch.tv": "Twitch",
    "youtube.com": "YouTube",
    "www.youtube.com": "YouTube",
    "m.youtube.com": "YouTube",
    "youtu.be": "YouTube",
    "www.youtu.be": "YouTube",
    "live.nicovideo.jp": "ニコ生",
}


def _infer_label(url: str) -> str:
    try:
        hostname = (urlparse(url).hostname or "").lower()
    except ValueError:
        return "配信先"
    if hostname in _X_DESTINATION_LABEL_BY_HOST:
        return _X_DESTINATION_LABEL_BY_HOST[hostname]
    return hostname.removeprefix("www.") or "配信先"


def parse_destination_links(value: str | None) -> tuple[DestinationLink, ...]:
    """Parse one destination per line.

    Each line may be either ``URL`` or ``表示名|URL``.  Invalid and blank
    lines are ignored so a stray empty line never prevents OBS from starting.
    """
    links: list[DestinationLink] = []
    for raw_line in str(value or "").splitlines():
        try:
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
        except ValueError:
            continue
    return tuple(links)


X_POST_MODE_COMBINED = "combined"
X_POST_MODE_SEPARATE = "separate"

# Hostnames treated as YouTube for per-destination posts and video-ID
# dedup. Lookup is exact-equality only so suffix lookalikes (e.g.
# "www.youtube.com.evil.example") never match.
_X_YOUTUBE_HOSTS = frozenset(
    {
        "youtube.com",
        "www.youtube.com",
        "m.youtube.com",
        "youtu.be",
        "www.youtu.be",
    }
)

_YOUTUBE_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# Known template tokens. Unknown braces are left untouched by the
# single-pass renderer below, which keeps ordinary text safe.
_X_POST_TOKEN_RE = re.compile(r"\{(youtube_url|url|title|links|platform)\}")


def normalize_x_post_mode(value: object) -> str:
    """Return ``'separate'`` for an explicit separate request, else combined."""
    return (
        X_POST_MODE_SEPARATE if value == X_POST_MODE_SEPARATE else X_POST_MODE_COMBINED
    )


def _is_youtube_host(url: str) -> bool:
    """Return whether a URL sits on a supported YouTube hostname."""
    try:
        return (urlparse(str(url or "")).hostname or "").lower() in _X_YOUTUBE_HOSTS
    except ValueError:
        return False


def _youtube_video_id(url: str) -> str:
    """Extract a valid 11-char YouTube video ID, or ``""`` when absent.

    Recognises ``watch?v=ID``, ``youtu.be/ID``, ``/live/ID`` and
    ``/shorts/ID`` variants on supported hostnames only. Never raises.
    """
    try:
        parsed = urlparse(str(url or ""))
        hostname = (parsed.hostname or "").lower()
        if hostname not in _X_YOUTUBE_HOSTS:
            return ""
        if hostname in ("youtu.be", "www.youtu.be"):
            stripped = parsed.path.strip("/")
            candidate = stripped.split("/")[0] if stripped else ""
        else:
            path = parsed.path or ""
            if path == "/watch":
                candidate = parse_qs(parsed.query).get("v", [""])[0]
            elif path.startswith("/live/") or path.startswith("/shorts/"):
                parts = path.split("/")
                candidate = parts[2] if len(parts) > 2 else ""
            else:
                return ""
        candidate = (candidate or "").strip()
        return candidate if _YOUTUBE_VIDEO_ID_RE.match(candidate) else ""
    except ValueError:
        return ""


def _render_template_once(text: str, replacements: dict[str, str]) -> str:
    """Replace known tokens in one pass so values are never re-interpolated.

    Braces inside a title, label or URL therefore stay literal, and unknown
    placeholders remain unchanged.
    """
    return _X_POST_TOKEN_RE.sub(
        lambda match: replacements.get(match.group(1), match.group(0)),
        text,
    )


def _strip_surrounding_blank_lines(text: str) -> str:
    lines = [line.rstrip() for line in text.splitlines()]
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(lines)


def _fit_title(render, title: str) -> str:
    """Render with ``title`` shortened (``…`` appended) to fit 280 weighted.

    ``render(title)`` returns the final post text. URLs and fixed template
    text are never touched. Raises :class:`XPostTooLongError` when the post is
    still too long with an empty title.
    """
    text = render(title)
    if x_weighted_length(text) <= X_MAX_WEIGHTED_LENGTH:
        return text
    base = render("")
    if x_weighted_length(base) > X_MAX_WEIGHTED_LENGTH:
        raise XPostTooLongError(
            "X投稿文が文字数上限（重み付き280）を超えています"
            f"（{x_weighted_length(base)}/{X_MAX_WEIGHTED_LENGTH}）。"
            "タイトルを除いてもテンプレートと配信先URLだけで上限を超えるため、"
            "投稿しません。テンプレートや配信先の数を減らしてください"
        )
    low, high, best = 1, len(title) - 1, ""
    while low <= high:
        mid = (low + high) // 2
        candidate = title[:mid].rstrip() + _X_ELLIPSIS
        if x_weighted_length(render(candidate)) <= X_MAX_WEIGHTED_LENGTH:
            best = candidate
            low = mid + 1
        else:
            high = mid - 1
    return render(best)


def build_x_post_texts(
    template: str | None,
    *,
    youtube_url: str = "",
    title: str = "",
    destinations: str | None = "",
    mode: str = "combined",
) -> tuple[str, ...]:
    """Build one post body per announcement in ``mode``.

    Combined mode returns :func:`render_x_post_text`'s result exactly in a
    one-element tuple. Separate mode returns one post per deduplicated
    destination (discovered YouTube URL first), each containing only its own
    labelled link, or an empty tuple when no valid destination exists.
    """
    if normalize_x_post_mode(mode) != X_POST_MODE_SEPARATE:
        return (
            render_x_post_text(
                template,
                youtube_url=youtube_url,
                title=title,
                destinations=destinations,
            ),
        )
    shared_title = str(title or "").strip()
    live_url = str(youtube_url or "").strip()
    configured = list(parse_destination_links(destinations))

    ordered: list[DestinationLink] = []
    if live_url:
        ordered.extend(parse_destination_links(live_url))
    ordered.extend(configured)

    targets: list[DestinationLink] = []
    seen_urls: set[str] = set()
    seen_video_ids: set[str] = set()
    for link in ordered:
        if link.url in seen_urls:
            continue
        video_id = _youtube_video_id(link.url)
        if video_id and video_id in seen_video_ids:
            continue
        seen_urls.add(link.url)
        if video_id:
            seen_video_ids.add(video_id)
        targets.append(link)

    base_text = str(template or DEFAULT_X_POST_TEMPLATE)
    texts: list[str] = []
    for link in targets:
        replacements = {
            "youtube_url": link.url if _is_youtube_host(link.url) else "",
            "url": link.url,
            "platform": link.label,
            "title": shared_title,
            "links": f"{link.label}: {link.url}",
        }
        def _render(shown_title: str, replacements=replacements, link=link) -> str:
            rendered = _strip_surrounding_blank_lines(
                _render_template_once(
                    base_text, {**replacements, "title": shown_title}
                )
            )
            if link.url not in rendered:
                labelled = f"{link.label}: {link.url}"
                rendered = f"{rendered}\n{labelled}" if rendered else labelled
            return rendered

        texts.append(_fit_title(_render, shared_title))
    return tuple(texts)


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
    containing braces safe. A title that pushes the post over X's weighted
    limit is shortened with ``…``; :class:`XPostTooLongError` is raised when
    the post cannot fit even without a title.
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

    def _render(shown_title: str) -> str:
        replacements = {
            "youtube_url": live_url,
            "url": live_url,
            "title": shown_title,
            "links": "\n".join(rendered_links),
        }
        return _strip_surrounding_blank_lines(
            _render_template_once(text, replacements)
        )

    return _fit_title(_render, str(title or "").strip())


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
