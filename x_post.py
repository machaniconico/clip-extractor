"""Helpers for publishing or manually composing an X stream announcement.

Automatic publishing is used only when the user explicitly opts in and
provides all four OAuth 1.0a user-context credentials.  Otherwise callers keep
the existing behavior of opening a prefilled composer for manual review.
Credential persistence belongs to the UI layer; this module only uses values
provided for one request and never logs them.
"""

from __future__ import annotations

import webbrowser
from dataclasses import dataclass
from urllib.parse import quote, urlencode, urlparse


X_POST_INTENT_URL = "https://x.com/intent/post"
X_API_POST_URL = "https://api.x.com/2/tweets"
X_API_TIMEOUT = (5, 15)
DEFAULT_X_POST_TEMPLATE = "配信開始しました！\n{links}"


class XPostError(RuntimeError):
    """A user-safe X publishing error that never includes API response data."""


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
