from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import aclosing
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
        self.api = API(
            str(database_path),
            raise_when_no_account=True,
            wait_timeout=30,
            wait_interval=1,
        )

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
        timeline = self.api.user_tweets_and_replies(int(x_user_id))
        async with aclosing(timeline):
            async for tweet in timeline:
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
    """Extract downloadable media from a tweet and its embedded tweet payloads."""
    items: list[SourceMedia] = []
    seen_urls: set[str] = set()

    def append(media_type: str, source_url: object) -> None:
        if not isinstance(source_url, str) or not source_url or source_url in seen_urls:
            return
        seen_urls.add(source_url)
        items.append(SourceMedia(media_type, source_url))

    def extract(tweet: Mapping[str, Any]) -> None:
        media = tweet.get("media")
        if isinstance(media, Mapping):
            photos = media.get("photos")
            if isinstance(photos, list):
                for photo in photos:
                    if isinstance(photo, Mapping):
                        append("image", photo.get("url"))

            videos = media.get("videos")
            if isinstance(videos, list):
                for video in videos:
                    if not isinstance(video, Mapping):
                        continue
                    variants = video.get("variants")
                    if not isinstance(variants, list):
                        continue
                    downloadable = [
                        variant for variant in variants
                        if isinstance(variant, Mapping) and isinstance(variant.get("url"), str)
                    ]
                    if downloadable:
                        best = max(downloadable, key=lambda variant: variant.get("bitrate") or -1)
                        append("video", best.get("url"))

            animated_items = media.get("animated")
            if isinstance(animated_items, list):
                for animated in animated_items:
                    if isinstance(animated, Mapping):
                        append("gif", animated.get("videoUrl"))

        for field in ("retweetedTweet", "quotedTweet"):
            embedded = tweet.get(field)
            if isinstance(embedded, Mapping):
                extract(embedded)

    extract(payload)
    return tuple(items)
