from archivex.post_model import SourceMedia, media_from_payload
import pytest


def test_video_ignores_empty_url_even_with_highest_bitrate():
    payload = {
        "media": {
            "videos": [{"variants": [
                {"url": "https://video.test/valid.mp4", "bitrate": 832000},
                {"url": "", "bitrate": 2176000},
            ]}],
        },
    }

    assert media_from_payload(payload) == (
        SourceMedia("video", "https://video.test/valid.mp4"),
    )


@pytest.mark.parametrize("bitrate", [None, "invalid", {}, True, float("nan"), float("inf"), -10])
def test_video_bad_bitrate_does_not_discard_valid_version(bitrate):
    media = media_from_payload({"media": {"videos": [{"variants": [
        {"url": "https://video.test/unknown.mp4", "bitrate": bitrate},
        {"url": "https://video.test/valid.mp4", "bitrate": 0},
        {"url": "   ", "bitrate": 999999},
    ]}]}})
    assert media == (SourceMedia("video", "https://video.test/valid.mp4"),)


def test_video_uses_highest_valid_version_and_trims_address():
    media = media_from_payload({"media": {"videos": [{"variants": [
        {"url": "https://video.test/low.mp4", "bitrate": 100},
        {"url": " https://video.test/high.mp4 ", "bitrate": 200},
        {"url": "", "bitrate": 300},
    ]}]}})
    assert media == (SourceMedia("video", "https://video.test/high.mp4"),)
