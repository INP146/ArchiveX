from archivex.post_model import SourceMedia, media_from_payload


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
