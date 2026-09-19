"""Parse content, events and media ownership once, for crawling and migration."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Mapping

# A non-X identity for references whose payload contains no author ID. Never
# invent a numeric X ID; a later complete snapshot replaces this association.
UNKNOWN_USER_ID = "unknown"


@dataclass(frozen=True)
class UserSnapshot:
    x_user_id: str
    username: str | None = None
    display_name: str | None = None
    profile_image_url: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SourceMedia:
    media_type: str
    source_url: str


@dataclass(frozen=True)
class SourcePost:
    tweet_id: str
    x_user_id: str
    username: str | None
    post_type: str
    text: str
    posted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any] | None
    own_media: tuple[SourceMedia, ...] = ()
    referenced: SourcePost | None = None
    availability: str = "available"
    user_snapshot: UserSnapshot | None = None

    @property
    def author(self) -> UserSnapshot:
        return self.user_snapshot or user_from_payload(
            (self.raw_payload or {}).get("user"), self.x_user_id, self.username
        )


def user_from_payload(
    value: Any, fallback_id: str = UNKNOWN_USER_ID, fallback_username: str | None = None
) -> UserSnapshot:
    user = value if isinstance(value, Mapping) else {}
    return UserSnapshot(
        str(user.get("id_str") or user.get("id") or fallback_id),
        _text(user.get("username")) or fallback_username,
        _text(user.get("displayname") or user.get("displayName")),
        _text(user.get("profileImageUrl")),
        _text(user.get("rawDescription") or user.get("description")),
    )


def _text(value: Any) -> str | None:
    return value.strip() or None if isinstance(value, str) else None


def media_from_payload(payload: Mapping[str, Any]) -> tuple[SourceMedia, ...]:
    """Only this content's own media; never flatten embedded references."""
    media = payload.get("media")
    if not isinstance(media, Mapping):
        return ()
    items: dict[str, SourceMedia] = {}

    def append(kind: str, value: Any) -> None:
        if isinstance(value, str) and value:
            items.setdefault(value, SourceMedia(kind, value))

    for photo in media.get("photos") or ():
        if isinstance(photo, Mapping):
            append("image", photo.get("url"))
    for video in media.get("videos") or ():
        if not isinstance(video, Mapping):
            continue
        variants = [
            v
            for v in video.get("variants") or ()
            if isinstance(v, Mapping) and isinstance(v.get("url"), str)
        ]
        if variants:
            best = max(variants, key=lambda v: v.get("bitrate") or -1)
            append("video", best["url"])
    for animated in media.get("animated") or ():
        if isinstance(animated, Mapping):
            append("gif", animated.get("videoUrl"))
    return tuple(items.values())


def parse_post(
    payload: Mapping[str, Any],
    *,
    tweet_id: str | None = None,
    x_user_id: str = UNKNOWN_USER_ID,
    username: str | None = None,
    post_type: str | None = None,
    text: str | None = None,
    posted_at: datetime | None = None,
    permalink: str | None = None,
    _ancestors: frozenset[str] = frozenset(),
) -> SourcePost:
    identifier = str(payload.get("id_str") or payload.get("id") or tweet_id or "")
    if not identifier or identifier in _ancestors or len(_ancestors) >= 32:
        raise ValueError(f"missing or cyclic tweet ID: {identifier!r}")
    if tweet_id is not None and identifier != tweet_id:
        raise ValueError(f"payload ID {identifier} does not match tweet {tweet_id}")
    author = user_from_payload(payload.get("user"), x_user_id, username)
    retweeted = payload.get("retweetedTweet")
    quoted = payload.get("quotedTweet")
    reply_id = payload.get("inReplyToTweetIdStr") or payload.get("inReplyToTweetId")
    # This priority is part of the archive contract.
    if retweeted is not None or post_type == "repost":
        kind, embedded = "repost", retweeted
        reference_id = payload.get("retweetedTweetId") or payload.get("retweeted_status_id_str")
    elif quoted is not None or payload.get("isQuoteStatus") or post_type == "quote":
        kind, embedded = "quote", quoted
        reference_id = (
            payload.get("quotedTweetId")
            or payload.get("quotedStatusId")
            or payload.get("quoted_status_id_str")
        )
    elif reply_id is not None or post_type == "reply":
        kind, embedded, reference_id = "reply", payload.get("inReplyToTweet"), reply_id
    else:
        kind, embedded, reference_id = "original", None, None

    referenced = None
    ancestors = _ancestors | {identifier}
    if isinstance(embedded, Mapping) and (embedded.get("id_str") or embedded.get("id")):
        referenced = parse_post(embedded, _ancestors=ancestors)
        if referenced.post_type == "repost":
            raise ValueError(f"tweet {identifier} references a repost event instead of content")
    elif reference_id:
        reference_id = str(reference_id)
        if reference_id in ancestors:
            raise ValueError(f"cyclic reference: {reference_id}")
        target_user = (
            user_from_payload(
                payload.get("inReplyToUser"),
                str(payload.get("inReplyToUserId") or UNKNOWN_USER_ID),
                payload.get("inReplyToScreenName"),
            )
            if kind == "reply"
            else UserSnapshot(UNKNOWN_USER_ID)
        )
        referenced = SourcePost(
            reference_id,
            target_user.x_user_id,
            target_user.username,
            "original",
            "",
            _tweet_date(reference_id),
            f"https://x.com/i/status/{reference_id}",
            None,
            availability="unknown",
            user_snapshot=target_user,
        )
    if kind == "repost" and referenced is None:
        raise ValueError(f"repost {identifier} has no resolvable origin ID")

    date_value = payload.get("date")
    date = (
        date_value
        if isinstance(date_value, datetime)
        else datetime.fromisoformat(date_value)
        if isinstance(date_value, str)
        else posted_at or _tweet_date(identifier)
    )
    if date.tzinfo is None:
        date = date.replace(tzinfo=UTC)
    body = payload.get("rawContent", text or "")
    return SourcePost(
        identifier,
        author.x_user_id,
        author.username,
        kind,
        body if isinstance(body, str) else "",
        date.astimezone(UTC),
        str(payload.get("url") or permalink or f"https://x.com/i/status/{identifier}"),
        payload,
        media_from_payload(payload) if kind != "repost" else (),
        referenced,
        "partial"
        if (kind in {"reply", "quote"} and referenced is None)
        or ("rawContent" not in payload and text is None)
        else "available",
        author,
    )


def _tweet_date(tweet_id: str) -> datetime:
    # Snowflakes encode creation time; tiny test/legacy IDs have no useful date.
    if tweet_id.isdigit() and int(tweet_id) > 2**32:
        return datetime.fromtimestamp(((int(tweet_id) >> 22) + 1288834974657) / 1000, UTC)
    return datetime(1970, 1, 1, tzinfo=UTC)
