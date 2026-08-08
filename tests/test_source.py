import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

from archivex.source import SourceMedia, TwscrapePostSource, media_from_payload


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


def test_media_from_payload_includes_reposted_tweet_video() -> None:
    payload = {
        "media": {},
        "retweetedTweet": {
            "media": {
                "videos": [{
                    "variants": [
                        {"bitrate": 256000, "url": "https://video.example/low.mp4"},
                        {"bitrate": 2176000, "url": "https://video.example/high.mp4"},
                    ]
                }]
            }
        },
    }

    assert media_from_payload(payload) == (
        SourceMedia("video", "https://video.example/high.mp4"),
    )


def test_media_from_payload_includes_quote_media_and_deduplicates_urls() -> None:
    shared_url = "https://pbs.twimg.com/media/shared.jpg"
    payload = {
        "media": {"photos": [{"url": shared_url}]},
        "quotedTweet": {
            "media": {
                "photos": [
                    {"url": shared_url},
                    {"url": "https://pbs.twimg.com/media/quoted.jpg"},
                ]
            }
        },
    }

    assert media_from_payload(payload) == (
        SourceMedia("image", shared_url),
        SourceMedia("image", "https://pbs.twimg.com/media/quoted.jpg"),
    )
