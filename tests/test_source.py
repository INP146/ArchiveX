import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from archivex.source import TwscrapePostSource


class CloseTrackingTimeline:
    def __init__(self, tweets):
        self.tweets = iter(tweets)
        self.close_called = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self.tweets)
        except StopIteration as exc:
            raise StopAsyncIteration from exc

    async def aclose(self) -> None:
        self.close_called = True


class FakeApi:
    def __init__(self, timeline):
        self.timeline = timeline

    def user_tweets_and_replies(self, x_user_id: int):
        assert x_user_id == 1
        return self.timeline


class FakeTweet:
    id = 10
    user = SimpleNamespace(id=1, username="first")
    retweetedTweet = None
    quotedTweet = None
    isQuoteStatus = False
    inReplyToTweetId = None
    rawContent = "post"
    date = datetime(2026, 8, 8, tzinfo=UTC)
    url = "https://x.com/first/status/10"

    def dict(self):
        return {"id": "10", "media": {}}


def test_closing_source_timeline_closes_twscrape_timeline() -> None:
    upstream = CloseTrackingTimeline([FakeTweet()])
    source = object.__new__(TwscrapePostSource)
    source.api = FakeApi(upstream)

    async def consume_one_post() -> None:
        timeline = source.fetch_timeline("1")
        await anext(timeline)
        await timeline.aclose()
        assert upstream.close_called is True

    asyncio.run(consume_one_post())
