from archivex.post_model import SourceMedia, media_from_payload


def test_video_prefers_explicit_zero_bitrate_over_missing_bitrate():
    payload = {
        "media": {
            "videos": [{"variants": [
                {"url": "https://video.test/unknown.mp4"},
                {"url": "https://video.test/zero.mp4", "bitrate": 0},
            ]}],
        },
    }

    assert media_from_payload(payload) == (
        SourceMedia("video", "https://video.test/zero.mp4"),
    )
