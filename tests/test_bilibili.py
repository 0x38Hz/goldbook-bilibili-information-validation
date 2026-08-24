from datetime import datetime, timezone
from pathlib import Path
from subprocess import CompletedProcess

import pytest

from goldbook.bilibili import BilibiliSource, parse_public_source
from goldbook.models import Video


def test_accepts_space_uid_and_bvid_but_rejects_other_hosts():
    assert parse_public_source("https://space.bilibili.com/42").kind == "space"
    assert parse_public_source("42").value == "42"
    assert parse_public_source("BV1TEST12345").kind == "video"
    with pytest.raises(ValueError, match="Bilibili"):
        parse_public_source("https://example.com/video/BV1TEST12345")


def test_filters_flat_playlist_by_publication_time():
    rows = [
        {"id": "BVNEW", "title": "新视频", "timestamp": 1785801600, "duration": 600},
        {"id": "BVOLD", "title": "旧视频", "timestamp": 1751328000, "duration": 600},
    ]
    source = BilibiliSource.for_test(rows)
    videos = source.list_videos(
        "https://space.bilibili.com/42",
        datetime(2026, 2, 18, tzinfo=timezone.utc),
    )
    assert [video.bvid for video in videos] == ["BVNEW"]


def _video(bvid="BV1SAFE12345", url="https://www.bilibili.com/video/BV1SAFE12345"):
    return Video(
        bvid=bvid,
        creator_uid="42",
        title="测试",
        published_at=datetime(2026, 2, 19, tzinfo=timezone.utc),
        duration_sec=60,
        url=url,
    )


@pytest.mark.parametrize(
    "video",
    [
        _video(url="https://example.test/not-bilibili"),
        _video(url="https://www.bilibili.com/video/BV1OTHER12345"),
    ],
)
def test_download_rejects_non_public_or_mismatched_video_before_runner(tmp_path, video):
    calls = []

    def runner(args):
        calls.append(args)
        return CompletedProcess(args, 0, "", "")

    source = BilibiliSource(tmp_path, runner)
    with pytest.raises(ValueError, match="Bilibili"):
        source.download_audio(video, tmp_path / "task")
    assert calls == []


def test_download_uses_local_canonical_url_and_stays_in_task_root(tmp_path):
    output = tmp_path / "task" / "BV1SAFE12345.wav"
    calls = []

    def runner(args):
        calls.append(args)
        return CompletedProcess(args, 0, str(output), "")

    source = BilibiliSource(tmp_path, runner)
    result = source.download_audio(
        _video(url="https://www.bilibili.com/video/BV1SAFE12345?tracking=ignored"),
        tmp_path / "task",
    )
    assert result == output.resolve()
    assert calls[0][-1] == "https://www.bilibili.com/video/BV1SAFE12345"
    assert "--cookies" not in calls[0]
    assert "--cookies-from-browser" not in calls[0]
    assert Path(calls[0][calls[0].index("--output") + 1]).parent == (tmp_path / "task").resolve()


def test_download_rejects_runner_output_outside_task_root(tmp_path):
    source = BilibiliSource(
        tmp_path,
        lambda args: CompletedProcess(args, 0, str(tmp_path.parent / "outside.wav"), ""),
    )
    with pytest.raises(ValueError, match="outside"):
        source.download_audio(_video(), tmp_path / "task")


def test_runner_metadata_normalizes_canonical_video_and_uses_flat_public_options(tmp_path):
    calls = []

    def runner(args):
        calls.append(args)
        return CompletedProcess(
            args,
            0,
            '{"entries": [{"id": "BV1META12345", "title": "标题", "timestamp": 1785801600, '
            '"duration": 61, "uploader_id": "99"}]}',
            "",
        )

    videos = BilibiliSource(tmp_path, runner).list_videos(
        "https://space.bilibili.com/42", datetime(2026, 2, 18, tzinfo=timezone.utc)
    )
    assert videos == [
        Video(
            bvid="BV1META12345",
            creator_uid="99",
            title="标题",
            published_at=datetime.fromtimestamp(1785801600, tz=timezone.utc),
            duration_sec=61,
            url="https://www.bilibili.com/video/BV1META12345",
        )
    ]
    assert calls[0][-1] == "https://space.bilibili.com/42"
    assert "--flat-playlist" in calls[0]
    assert "--cookies" not in calls[0]


def test_space_metadata_uses_public_dynamic_then_creator_scoped_search_after_412(tmp_path):
    calls = []

    def runner(args):
        return CompletedProcess(args, 1, "", "ERROR: HTTP Error 412: Precondition Failed")

    def metadata_get(url, params):
        calls.append((url, params))
        if url.endswith("/arc/search"):
            return {"code": -412, "message": "请求被拦截"}
        if url.endswith("/card"):
            return {"code": 0, "data": {"card": {"name": "黄金频道"}}}
        if url.endswith("/feed/space"):
            return {"code": -412, "message": "请求被拦截"}
        if url.endswith("/search/type"):
            assert params["keyword"] == "黄金频道"
            assert params["page"] == "1"
            return {
                "code": 0,
                "data": {
                    "result": [
                        {
                            "bvid": "BV1SEARCH12345",
                            "mid": 42,
                            "title": "<em class=\"keyword\">黄金</em>观点",
                            "pubdate": 1785801600,
                            "duration": "10:01",
                        },
                        {
                            "bvid": "BV1OTHER12345",
                            "mid": 99,
                            "title": "别人的视频",
                            "pubdate": 1785801600,
                            "duration": "10:01",
                        },
                    ],
                    "numPages": 1,
                },
            }
        raise AssertionError(url)

    videos = BilibiliSource(tmp_path, runner, metadata_get=metadata_get).list_videos(
        "42", datetime(2026, 2, 18, tzinfo=timezone.utc)
    )

    assert videos == [
        Video(
            bvid="BV1SEARCH12345",
            creator_uid="42",
            title="黄金观点",
            published_at=datetime.fromtimestamp(1785801600, tz=timezone.utc),
            duration_sec=601,
            url="https://www.bilibili.com/video/BV1SEARCH12345",
        )
    ]
    assert [url for url, _ in calls] == [
        "https://api.bilibili.com/x/web-interface/nav",
        "https://api.bilibili.com/x/space/arc/search",
        "https://api.bilibili.com/x/space/wbi/arc/search",
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
        "https://api.bilibili.com/x/web-interface/card",
        "https://api.bilibili.com/x/web-interface/search/type",
    ]


def test_dynamic_space_metadata_pages_deduplicates_and_stops_at_old_video(tmp_path):
    calls = []

    def runner(args):
        return CompletedProcess(args, 1, "", "ERROR: HTTP Error 412: Precondition Failed")

    def metadata_get(url, params):
        calls.append((url, params))
        if url.endswith("/arc/search"):
            return {"code": -412, "message": "请求被拦截"}
        if url.endswith("/card"):
            return {"code": 0, "data": {"card": {"name": "黄金频道"}}}
        if url.endswith("/feed/space"):
            if params["offset"] == "":
                return {
                    "code": 0,
                    "data": {
                        "items": [
                            _dynamic_item("BV1DYNAMIC123", 1785801600, "新视频", "10:00"),
                            _dynamic_item("BV1DYNAMIC123", 1785801600, "重复视频", "10:00"),
                            _dynamic_item("BV1OLD123456", 1751328000, "旧视频", "10:00"),
                        ],
                        "has_more": True,
                        "offset": "would-not-be-used",
                    },
                }
            raise AssertionError("old result should terminate dynamic pagination")
        raise AssertionError("search should not run after a usable dynamic response")

    videos = BilibiliSource(tmp_path, runner, metadata_get=metadata_get).list_videos(
        "42", datetime(2026, 2, 18, tzinfo=timezone.utc)
    )

    assert [video.bvid for video in videos] == ["BV1DYNAMIC123"]
    assert videos[0].creator_uid == "42"
    assert videos[0].duration_sec == 600
    assert [url for url, _ in calls] == [
        "https://api.bilibili.com/x/web-interface/nav",
        "https://api.bilibili.com/x/space/arc/search",
        "https://api.bilibili.com/x/space/wbi/arc/search",
        "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space",
    ]


def test_default_metadata_fetch_is_public_and_uses_search_page_headers(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"code": 0, "data": {}}

    def fake_get(url, *, params, timeout, headers):
        captured.update(url=url, params=params, timeout=timeout, headers=headers)
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)
    payload = BilibiliSource._default_metadata_get(
        "https://api.bilibili.com/x/web-interface/search/type",
        {"search_type": "video", "keyword": "黄金", "page": "1", "order": "pubdate"},
    )

    assert payload == {"code": 0, "data": {}}
    assert captured["headers"]["Referer"] == "https://search.bilibili.com/"
    assert "Cookie" not in captured["headers"]
    assert "cookies" not in captured["headers"]


def test_default_metadata_fetch_uses_space_page_headers_for_signed_archive(monkeypatch):
    captured = {}

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"code": 0, "data": {}}

    def fake_get(url, *, params, timeout, headers):
        captured.update(url=url, params=params, timeout=timeout, headers=headers)
        return Response()

    monkeypatch.setattr("httpx.get", fake_get)
    payload = BilibiliSource._default_metadata_get(
        "https://api.bilibili.com/x/space/wbi/arc/search",
        {"mid": "42", "pn": "1"},
    )

    assert payload == {"code": 0, "data": {}}
    assert captured["headers"]["Referer"] == "https://space.bilibili.com/"


def test_search_keeps_first_page_when_a_later_public_page_is_rate_limited(tmp_path):
    def runner(args):
        return CompletedProcess(args, 1, "", "ERROR: HTTP Error 412: Precondition Failed")

    def metadata_get(url, params):
        if url.endswith("/arc/search"):
            return {"code": -412}
        if url.endswith("/card"):
            return {"code": 0, "data": {"card": {"name": "黄金频道"}}}
        if url.endswith("/feed/space"):
            return {"code": -412}
        if url.endswith("/search/type") and params["page"] == "1":
            return {
                "code": 0,
                "data": {
                    "result": [
                        {
                            "bvid": "BV1FIRST12345",
                            "mid": 42,
                            "title": "第一页",
                            "pubdate": 1785801600,
                            "duration": "1:00",
                        }
                    ],
                    "numPages": 3,
                },
            }
        if url.endswith("/search/type") and params["page"] == "2":
            return {"code": -412}
        raise AssertionError(url)

    videos = BilibiliSource(tmp_path, runner, metadata_get=metadata_get).list_videos(
        "42", datetime(2026, 2, 18, tzinfo=timezone.utc)
    )

    assert [video.bvid for video in videos] == ["BV1FIRST12345"]


def test_production_space_discovery_uses_public_api_before_ytdlp(tmp_path):
    ytdlp_calls = 0

    def metadata_get(url, params):
        if url.endswith("/arc/search"):
            return {
                "code": 0,
                "data": {
                    "list": {
                        "vlist": [
                            {
                                "bvid": "BV1ARC1234567",
                                "title": "原生列表视频",
                                "created": 1785801600,
                                "length": "2:00",
                            }
                        ]
                    },
                    "page": {"count": 1},
                },
            }
        raise AssertionError(url)

    source = BilibiliSource(tmp_path, metadata_get=metadata_get)

    def no_ytdlp(url):
        nonlocal ytdlp_calls
        ytdlp_calls += 1
        raise AssertionError("space discovery must not prime Bilibili with yt-dlp")

    source._yt_dlp_metadata = no_ytdlp
    videos = source.list_videos("42", datetime(2026, 2, 18, tzinfo=timezone.utc))

    assert [video.bvid for video in videos] == ["BV1ARC1234567"]
    assert ytdlp_calls == 0


def test_signed_archive_paginates_creator_videos_without_related_recommendations(tmp_path):
    calls = []

    def metadata_get(url, params):
        calls.append((url, params))
        if url.endswith("/nav"):
            return {
                "code": 0,
                "data": {
                    "wbi_img": {
                        "img_url": "https://i0.hdslb.com/bfs/wbi/0123456789abcdef0123456789abcdef.png",
                        "sub_url": "https://i0.hdslb.com/bfs/wbi/fedcba9876543210fedcba9876543210.png",
                    }
                },
            }
        if url.endswith("/wbi/arc/search") and params["pn"] == "1":
            assert params["mid"] == "42"
            assert params["ps"] == "50"
            assert params["order"] == "pubdate"
            assert params["w_rid"]
            assert params["wts"].isdecimal()
            return {
                "code": 0,
                "data": {
                    "list": {"vlist": [
                        {"bvid": "BV1WBI0000001", "title": "第一页", "created": 1785801600, "length": "1:00"},
                        {"bvid": "BV1WBI0000002", "title": "重复", "created": 1785715200, "length": "1:00"},
                    ]},
                    "page": {"count": 51},
                },
            }
        if url.endswith("/wbi/arc/search") and params["pn"] == "2":
            return {
                "code": 0,
                "data": {
                    "list": {"vlist": [
                        {"bvid": "BV1WBI0000002", "title": "重复", "created": 1785715200, "length": "1:00"},
                        {"bvid": "BV1WBI0000003", "title": "第二页", "created": 1785628800, "length": "1:00"},
                    ]},
                    "page": {"count": 51},
                },
            }
        raise AssertionError(url)

    rows = BilibiliSource(tmp_path, metadata_get=metadata_get)._signed_arc_rows(
        "42", datetime(2026, 2, 18, tzinfo=timezone.utc)
    )

    assert [row["id"] for row in rows or ()] == [
        "BV1WBI0000001",
        "BV1WBI0000002",
        "BV1WBI0000003",
    ]
    assert [params["pn"] for url, params in calls if url.endswith("/wbi/arc/search")] == ["1", "2"]


def test_wbi_mixin_key_accepts_the_anonymous_navigation_response(tmp_path):
    source = BilibiliSource(
        tmp_path,
        metadata_get=lambda _url, _params: {
            "code": -101,
            "data": {
                "wbi_img": {
                    "img_url": "https://i0.hdslb.com/bfs/wbi/0123456789abcdef0123456789abcdef.png",
                    "sub_url": "https://i0.hdslb.com/bfs/wbi/fedcba9876543210fedcba9876543210.png",
                }
            },
        },
    )

    assert source._wbi_mixin_key() is not None


def test_legacy_public_archive_pages_until_latest_one_hundred_videos(tmp_path):
    calls = []

    def metadata_get(url, params):
        calls.append((url, dict(params)))
        assert url == "https://api.bilibili.com/x/space/arc/search"
        page = int(params["pn"])
        start = (page - 1) * 50
        videos = [
            {
                "bvid": f"BV{index:010d}",
                "title": f"历史观点 {index}",
                "created": 1704067200 + index * 86400,
                "length": "3:00",
            }
            for index in range(start, min(start + 50, 125))
        ]
        return {
            "code": 0,
            "data": {"list": {"vlist": videos}, "page": {"count": 125}},
        }

    videos = BilibiliSource(tmp_path, metadata_get=metadata_get).list_videos(
        "42", datetime(1970, 1, 1, tzinfo=timezone.utc)
    )

    assert len(videos) == 100
    assert [
        params["pn"]
        for url, params in calls
        if url == "https://api.bilibili.com/x/space/arc/search"
    ] == ["1", "2"]
    assert len({video.bvid for video in videos}) == 100


def test_related_expansion_recurses_backward_and_rejects_other_creators(tmp_path):
    seed = Video(
        "BV10000000000",
        "42",
        "当前最旧视频",
        datetime(2026, 1, 3, tzinfo=timezone.utc),
        60,
        "https://www.bilibili.com/video/BV10000000000",
    )
    calls = []

    def metadata_get(url, params):
        assert url == "https://api.bilibili.com/x/web-interface/archive/related"
        calls.append(params["bvid"])
        if params["bvid"] == seed.bvid:
            return {
                "code": 0,
                "data": [
                    _related_item("BV10000000001", 42, 1767312000),
                    _related_item("BV19999999999", 99, 1767312000),
                ],
            }
        return {
            "code": 0,
            "data": [_related_item("BV10000000002", 42, 1767225600)],
        }

    videos = BilibiliSource(tmp_path, metadata_get=metadata_get).expand_related(
        "42", (seed,), datetime(1970, 1, 1, tzinfo=timezone.utc), limit=3
    )

    assert {video.bvid for video in videos} == {
        "BV10000000000",
        "BV10000000001",
        "BV10000000002",
    }
    assert calls == ["BV10000000000", "BV10000000001"]


def _related_item(bvid, mid, pubdate):
    return {
        "bvid": bvid,
        "title": f"历史观点 {bvid}",
        "pubdate": pubdate,
        "duration": 180,
        "owner": {"mid": mid},
    }


def _dynamic_item(bvid, pub_ts, title, duration):
    return {
        "type": "DYNAMIC_TYPE_AV",
        "modules": {
            "module_author": {"pub_ts": pub_ts},
            "module_dynamic": {
                "major": {"archive": {"bvid": bvid, "title": title, "duration_text": duration}}
            },
        },
    }
