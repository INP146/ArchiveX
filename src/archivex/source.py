from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Protocol

from twscrape import API

from archivex.session import session_database_path

@dataclass(frozen=True)
class SourceAccount:
    x_user_id: str
    username: str
    display_name: str | None
    profile_image_url: str | None = None
    description: str | None = None


@dataclass(frozen=True)
class SourcePost:
    tweet_id: str
    x_user_id: str
    username: str
    post_type: str
    text: str
    posted_at: datetime
    permalink: str
    raw_payload: Mapping[str, Any]
    media: tuple["SourceMedia", ...] = ()


@dataclass(frozen=True)
class SourceMedia:
    media_type: str
    source_url: str


class PostSource(Protocol):
    async def resolve_account(self, username: str) -> SourceAccount | None: ...

    def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]: ...


class TwscrapePostSource:
    """Adapter around twscrape so archive logic is independent of its data model."""

    def __init__(self, session_path: Path) -> None:
        database_path = session_database_path(session_path)
        self.api = API(str(database_path), raise_when_no_account=True)

    async def resolve_account(self, username: str) -> SourceAccount | None:
        user = await self.api.user_by_login(username)
        if user is None:
            return None
        return SourceAccount(
            x_user_id=str(user.id),
            username=user.username,
            display_name=user.displayname or None,
            profile_image_url=user.profileImageUrl or None,
            description=user.rawDescription or None,
        )

    async def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
        async for tweet in self.api.user_tweets_and_replies(int(x_user_id)):
            # Conversation payloads can include another user's quoted/replied post.
            if str(tweet.user.id) != x_user_id:
                continue
            raw_payload = tweet.dict()
            yield SourcePost(
                tweet_id=str(tweet.id),
                x_user_id=str(tweet.user.id),
                username=tweet.user.username,
                post_type=_post_type(tweet),
                text=tweet.rawContent,
                posted_at=tweet.date,
                permalink=tweet.url,
                raw_payload=raw_payload,
                media=media_from_payload(raw_payload),
            )


def _post_type(tweet: Any) -> str:
    if tweet.retweetedTweet is not None:
        return "repost"
    if tweet.quotedTweet is not None or tweet.isQuoteStatus:
        return "quote"
    if tweet.inReplyToTweetId is not None:
        return "reply"
    return "original"


def media_from_payload(payload: Mapping[str, Any]) -> tuple[SourceMedia, ...]:
    """Extract downloadable media from twscrape's serializable tweet payload."""
    media = payload.get("media") or {}
    items = [
        SourceMedia("image", photo["url"])
        for photo in media.get("photos", []) if photo.get("url")
    ]
    items.extend(
        SourceMedia("video", max(video["variants"], key=lambda variant: variant["bitrate"])["url"])
        for video in media.get("videos", []) if video.get("variants")
    )
    items.extend(
        SourceMedia("gif", animated["videoUrl"])
        for animated in media.get("animated", []) if animated.get("videoUrl")
    )
    return tuple(items)
