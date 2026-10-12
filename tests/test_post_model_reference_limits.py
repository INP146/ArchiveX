from typing import Any

import pytest

from archivex.post_model import parse_post


@pytest.mark.parametrize("reference_key", ["quotedTweet", "inReplyToTweet"])
def test_rejects_embedded_reference_to_ancestor(reference_key):
    payload: dict[str, Any] = {"id": "100", "rawContent": "outer"}
    child = {"id": "101", "rawContent": "inner", "quotedTweet": payload}
    payload[reference_key] = child
    if reference_key == "inReplyToTweet":
        payload["inReplyToTweetIdStr"] = "101"

    with pytest.raises(ValueError, match="cyclic tweet ID"):
        parse_post(payload)


@pytest.mark.parametrize("reference_fields", [
    {"isQuoteStatus": True, "quotedStatusId": "100"},
    {"inReplyToTweetIdStr": "100"},
    {"retweetedTweetId": "100"},
])
def test_rejects_id_only_reference_to_itself(reference_fields):
    payload = {"id": "100", "rawContent": "self reference", **reference_fields}
    with pytest.raises(ValueError, match="cyclic reference"):
        parse_post(payload, post_type="repost" if "retweetedTweetId" in reference_fields else None)


def quote_chain(length):
    payload = {"id": str(length), "rawContent": "leaf"}
    for identifier in range(length - 1, 0, -1):
        payload = {"id": str(identifier), "rawContent": "quote", "quotedTweet": payload}
    return payload


def test_accepts_reference_chain_at_depth_limit():
    post = parse_post(quote_chain(32))
    count = 1
    while post.referenced is not None:
        post = post.referenced
        count += 1
    assert count == 32


def test_rejects_reference_chain_beyond_depth_limit():
    with pytest.raises(ValueError, match="missing or cyclic tweet ID"):
        parse_post(quote_chain(33))
