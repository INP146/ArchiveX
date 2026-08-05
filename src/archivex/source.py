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
        )

    async def fetch_timeline(self, x_user_id: str) -> AsyncIterator[SourcePost]:
        async for tweet in self.api.user_tweets_and_replies(int(x_user_id)):
            # Conversation payloads can include another user's quoted/replied post.
            if str(tweet.user.id) != x_user_id:
                continue
            yield SourcePost(
                tweet_id=str(tweet.id),
                x_user_id=x_user_id,
                username=tweet.user.username,
                post_type=_post_type(tweet),
                text=tweet.rawContent,
                posted_at=tweet.date,
                permalink=tweet.url,
                raw_payload=tweet.dict(),
            )


def _post_type(tweet: Any) -> str:
    if tweet.retweetedTweet is not None:
        return "repost"
    if tweet.quotedTweet is not None or tweet.isQuoteStatus:
        return "quote"
    if tweet.inReplyToTweetId is not None:
        return "reply"
    return "original"
