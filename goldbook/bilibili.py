"""Public Bilibili video metadata and audio retrieval.

This module deliberately supports only public space and video URLs.
"""

from __future__ import annotations

import json
import heapq
import re
import time
from hashlib import md5
from dataclasses import dataclass
from datetime import datetime, timezone
from html import unescape
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any, Callable, Iterable
from urllib.parse import urlencode, urlparse

from goldbook.models import Video


_BVID_RE = re.compile(r"^BV[0-9A-Za-z]+$")


@dataclass(frozen=True)
class PublicSource:
    kind: str
    value: str


def parse_public_source(source: str) -> PublicSource:
    """Parse a public Bilibili creator space, video URL, UID, or BVID."""
    value = source.strip()
    if value.isdecimal():
        return PublicSource("space", value)
    if _BVID_RE.fullmatch(value):
        return PublicSource("video", value)

    parsed = urlparse(value)
    host = (parsed.hostname or "").lower()
    path_parts = [part for part in parsed.path.split("/") if part]
    if host == "space.bilibili.com" and len(path_parts) == 1 and path_parts[0].isdecimal():
        return PublicSource("space", path_parts[0])
    if host in {"bilibili.com", "www.bilibili.com"} and len(path_parts) >= 2:
        if path_parts[0] == "video" and _BVID_RE.fullmatch(path_parts[1]):
            return PublicSource("video", path_parts[1])
    raise ValueError("Only public Bilibili space or video sources are supported")


Runner = Callable[[list[str]], CompletedProcess[str]]
MetadataGetter = Callable[[str, dict[str, str]], dict[str, Any]]

_CARD_ENDPOINT = "https://api.bilibili.com/x/web-interface/card"
_LEGACY_ARC_ENDPOINT = "https://api.bilibili.com/x/space/arc/search"
_ARC_ENDPOINT = "https://api.bilibili.com/x/space/wbi/arc/search"
_NAV_ENDPOINT = "https://api.bilibili.com/x/web-interface/nav"
_DYNAMIC_ENDPOINT = "https://api.bilibili.com/x/polymer/web-dynamic/v1/feed/space"
_SEARCH_ENDPOINT = "https://api.bilibili.com/x/web-interface/search/type"
_RELATED_ENDPOINT = "https://api.bilibili.com/x/web-interface/archive/related"
_MAX_METADATA_PAGES = 10
_MAX_SPACE_VIDEOS = 100
_MAX_RELATED_REQUESTS = 40
_WBI_MIXIN_KEY_ENC_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)


class BilibiliSource:
    """Adapter around yt-dlp for public Bilibili sources only."""

    def __init__(
        self,
        temporary_root: Path | None = None,
        runner: Runner | None = None,
        rows: Iterable[dict[str, Any]] | None = None,
        metadata_get: MetadataGetter | None = None,
    ) -> None:
        self._temporary_root = temporary_root.resolve() if temporary_root else None
        self._runner = runner
        self._rows = tuple(rows) if rows is not None else None
        self._metadata_get = metadata_get or self._default_metadata_get

    @classmethod
    def for_test(cls, rows: Iterable[dict[str, Any]]) -> "BilibiliSource":
        """Create an in-memory public metadata adapter with creator UID 42."""
        return cls(rows=rows)

    def list_videos(self, source: str, published_after: datetime) -> list[Video]:
        if published_after.tzinfo is None or published_after.utcoffset() is None:
            raise ValueError("published_after must be timezone-aware")
        parsed = parse_public_source(source)
        rows = self._rows if self._rows is not None else self._metadata_rows(parsed, published_after)
        creator_uid = "42" if self._rows is not None else parsed.value if parsed.kind == "space" else None
        videos = [self._to_video(row, creator_uid) for row in rows]
        return [video for video in videos if video.published_at > published_after]

    def download_audio(self, video: Video, destination: Path) -> Path:
        """Download a video's public audio as WAV into a caller-scoped directory."""
        source = parse_public_source(video.url)
        if source.kind != "video" or source.value != video.bvid:
            raise ValueError("video must have a matching public Bilibili video URL")
        canonical_url = self._canonical_source_url(source)
        target_dir = self._checked_destination(destination)
        template = target_dir / "%(id)s.%(ext)s"
        expected = target_dir / f"{video.bvid}.wav"
        if self._runner is not None:
            completed = self._runner([
                "--quiet",
                "--no-warnings",
                "--format", "bestaudio/best",
                "--extract-audio",
                "--audio-format", "wav",
                "--output", str(template),
                "--print", "after_move:filepath",
                canonical_url,
            ])
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or "yt-dlp failed to download audio")
            returned = self._runner_output_path(completed.stdout, expected)
            return self._checked_download_path(returned)

        from yt_dlp import YoutubeDL

        options = {
            "quiet": True,
            "no_warnings": True,
            "format": "bestaudio/best",
            "outtmpl": str(template),
            "postprocessors": [{"key": "FFmpegExtractAudio", "preferredcodec": "wav"}],
        }
        with YoutubeDL(options) as downloader:
            info = downloader.extract_info(canonical_url, download=True)
            filename = Path(downloader.prepare_filename(info)).with_suffix(".wav")
        return self._checked_download_path(filename)

    def expand_related(
        self,
        creator_uid: str,
        seed_videos: Iterable[Video],
        published_after: datetime,
        *,
        limit: int = _MAX_SPACE_VIDEOS,
    ) -> list[Video]:
        """Expand public same-creator recommendations, oldest seeds first."""
        if published_after.tzinfo is None or published_after.utcoffset() is None:
            raise ValueError("published_after must be timezone-aware")
        if limit < 1:
            return []
        videos_by_bvid = {
            video.bvid: video
            for video in seed_videos
            if video.creator_uid == creator_uid and video.published_at > published_after
        }
        frontier = [
            (video.published_at.timestamp(), video.bvid)
            for video in videos_by_bvid.values()
        ]
        heapq.heapify(frontier)
        requested: set[str] = set()
        while (
            frontier
            and len(videos_by_bvid) < limit
            and len(requested) < _MAX_RELATED_REQUESTS
        ):
            _, seed_bvid = heapq.heappop(frontier)
            if seed_bvid in requested:
                continue
            if requested:
                time.sleep(0.25)
            requested.add(seed_bvid)
            payload = self._request_metadata(_RELATED_ENDPOINT, {"bvid": seed_bvid})
            if payload is None:
                break
            items = payload.get("data")
            if not isinstance(items, list):
                break
            for item in items:
                row = self._related_row(item, creator_uid)
                if row is None:
                    continue
                video = self._to_video(row, creator_uid)
                if video.published_at <= published_after or video.bvid in videos_by_bvid:
                    continue
                videos_by_bvid[video.bvid] = video
                heapq.heappush(frontier, (video.published_at.timestamp(), video.bvid))
                if len(videos_by_bvid) >= limit:
                    break
        return sorted(
            videos_by_bvid.values(), key=lambda video: video.published_at, reverse=True
        )[:limit]

    def _metadata_rows(
        self, source: PublicSource, published_after: datetime
    ) -> tuple[dict[str, Any], ...]:
        url = self._canonical_source_url(source)
        public_attempted = False
        if source.kind == "space" and self._runner is None:
            public_attempted = True
            rows = self._public_space_metadata(source.value, published_after)
            if rows is not None:
                return rows
            raise RuntimeError("Could not retrieve public Bilibili video metadata")
        try:
            payload = self._yt_dlp_metadata(url)
        except Exception as error:
            if source.kind != "space":
                raise error
            rows = None if public_attempted else self._public_space_metadata(source.value, published_after)
            if rows is None:
                raise RuntimeError("Could not retrieve public Bilibili video metadata") from error
            return rows
        entries = payload.get("entries") if isinstance(payload, dict) else None
        if entries is None:
            entries = [payload]
        return tuple(entry for entry in entries if isinstance(entry, dict))

    def _yt_dlp_metadata(self, url: str) -> dict[str, Any]:
        if self._runner is not None:
            completed = self._runner([
                "--quiet",
                "--no-warnings",
                "--flat-playlist",
                "--dump-single-json",
                url,
            ])
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or "yt-dlp failed to list videos")
            payload = json.loads(completed.stdout)
        else:
            from yt_dlp import YoutubeDL

            with YoutubeDL({"quiet": True, "no_warnings": True, "extract_flat": "in_playlist"}) as downloader:
                payload = downloader.extract_info(url, download=False)
        if not isinstance(payload, dict):
            raise RuntimeError("yt-dlp returned invalid public metadata")
        return payload

    def _public_space_metadata(
        self, creator_uid: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        signed_rows = self._signed_arc_rows(creator_uid, published_after)
        if signed_rows is not None:
            return signed_rows
        legacy_rows = self._legacy_arc_rows(creator_uid, published_after)
        if legacy_rows is not None:
            return legacy_rows
        arc_rows = self._arc_rows(creator_uid, published_after)
        if arc_rows is not None:
            return arc_rows
        dynamic_rows = self._dynamic_rows(creator_uid, published_after)
        if dynamic_rows is not None:
            return dynamic_rows
        creator_name = self._creator_name(creator_uid)
        if not creator_name:
            return None
        return self._search_rows(creator_uid, creator_name, published_after)

    def _legacy_arc_rows(
        self, creator_uid: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        return self._archive_rows(_LEGACY_ARC_ENDPOINT, creator_uid, published_after)

    def _arc_rows(
        self, creator_uid: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        return self._archive_rows(_ARC_ENDPOINT, creator_uid, published_after)

    def _signed_arc_rows(
        self, creator_uid: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        mixin_key = self._wbi_mixin_key()
        if mixin_key is None:
            return None
        seen_bvids: set[str] = set()
        rows: list[dict[str, Any]] = []
        for page in range(1, _MAX_METADATA_PAGES + 1):
            if page > 1:
                time.sleep(0.25)
            params = self._signed_wbi_params(
                {"mid": creator_uid, "pn": str(page), "ps": "50", "order": "pubdate"},
                mixin_key,
            )
            payload = self._request_metadata(_ARC_ENDPOINT, params)
            data = self._mapping(payload, "data")
            listing = self._mapping(data, "list")
            if data is None or listing is None:
                return tuple(rows) if rows else None
            videos = listing.get("vlist")
            if not isinstance(videos, list):
                return tuple(rows) if rows else None
            reached_history = False
            for video in videos:
                row = self._arc_row(video, creator_uid)
                if row is None:
                    continue
                timestamp = float(row["timestamp"])
                if datetime.fromtimestamp(timestamp, tz=timezone.utc) <= published_after:
                    reached_history = True
                    continue
                bvid = str(row["id"])
                if bvid not in seen_bvids:
                    rows.append(row)
                    seen_bvids.add(bvid)
                if len(rows) >= _MAX_SPACE_VIDEOS:
                    return tuple(rows)
            page_info = self._mapping(data, "page")
            count = int(page_info.get("count") or 0) if page_info else 0
            if reached_history or not videos or (count and page * 50 >= count):
                return tuple(rows)
        return tuple(rows)

    def _wbi_mixin_key(self) -> str | None:
        try:
            payload = self._metadata_get(_NAV_ENDPOINT, {})
        except Exception:
            return None
        image = self._mapping(payload, "data", "wbi_img")
        if image is None:
            return None
        image_url = str(image.get("img_url") or "")
        sub_url = str(image.get("sub_url") or "")
        image_key = Path(urlparse(image_url).path).stem
        sub_key = Path(urlparse(sub_url).path).stem
        original = image_key + sub_key
        if len(original) < max(_WBI_MIXIN_KEY_ENC_TAB) + 1:
            return None
        return "".join(original[index] for index in _WBI_MIXIN_KEY_ENC_TAB)[:32]

    @staticmethod
    def _signed_wbi_params(params: dict[str, str], mixin_key: str) -> dict[str, str]:
        sanitized = {
            key: re.sub(r"[!'()*]", "", str(value))
            for key, value in params.items()
        }
        sanitized["wts"] = str(int(time.time()))
        query = urlencode(sorted(sanitized.items()))
        return {**sanitized, "w_rid": md5(f"{query}{mixin_key}".encode()).hexdigest()}

    def _archive_rows(
        self, endpoint: str, creator_uid: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        seen_bvids: set[str] = set()
        rows: list[dict[str, Any]] = []
        for page in range(1, _MAX_METADATA_PAGES + 1):
            if page > 1:
                time.sleep(0.25)
            payload = self._request_metadata(
                endpoint,
                {"mid": creator_uid, "pn": str(page), "ps": "50", "order": "pubdate"},
            )
            data = self._mapping(payload, "data")
            listing = self._mapping(data, "list")
            if data is None or listing is None:
                return tuple(rows) if rows else None
            videos = listing.get("vlist")
            if not isinstance(videos, list):
                return tuple(rows) if rows else None
            reached_history = False
            for video in videos:
                row = self._arc_row(video, creator_uid)
                if row is None:
                    continue
                timestamp = float(row["timestamp"])
                if datetime.fromtimestamp(timestamp, tz=timezone.utc) <= published_after:
                    reached_history = True
                    continue
                bvid = str(row["id"])
                if bvid not in seen_bvids:
                    rows.append(row)
                    seen_bvids.add(bvid)
                if len(rows) >= _MAX_SPACE_VIDEOS:
                    return tuple(rows)
            page_info = self._mapping(data, "page")
            count = int(page_info.get("count") or 0) if page_info else 0
            if reached_history or not videos or (count and page * 50 >= count):
                return tuple(rows)
        return tuple(rows)

    def _creator_name(self, creator_uid: str) -> str | None:
        payload = self._request_metadata(_CARD_ENDPOINT, {"mid": creator_uid})
        card = self._mapping(payload, "data", "card")
        name = card.get("name") if card else None
        return str(name).strip() if name else None

    def _dynamic_rows(
        self, creator_uid: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        offset = ""
        seen_offsets: set[str] = set()
        seen_bvids: set[str] = set()
        rows: list[dict[str, Any]] = []
        for page_number in range(_MAX_METADATA_PAGES):
            if page_number:
                time.sleep(0.25)
            payload = self._request_metadata(
                _DYNAMIC_ENDPOINT, {"host_mid": creator_uid, "offset": offset},
            )
            data = self._mapping(payload, "data")
            if data is None:
                return None
            items = data.get("items")
            if not isinstance(items, list):
                return None
            reached_history = False
            for item in items:
                row = self._dynamic_row(item, creator_uid)
                if row is None:
                    continue
                timestamp = float(row["timestamp"])
                if datetime.fromtimestamp(timestamp, tz=timezone.utc) <= published_after:
                    reached_history = True
                    continue
                bvid = str(row["id"])
                if bvid not in seen_bvids:
                    rows.append(row)
                    seen_bvids.add(bvid)
                if len(rows) >= _MAX_SPACE_VIDEOS:
                    return tuple(rows)
            if reached_history or not data.get("has_more"):
                return tuple(rows)
            next_offset = str(data.get("offset") or "")
            if not next_offset or next_offset in seen_offsets:
                return tuple(rows)
            seen_offsets.add(next_offset)
            offset = next_offset
        return tuple(rows)

    def _search_rows(
        self, creator_uid: str, creator_name: str, published_after: datetime
    ) -> tuple[dict[str, Any], ...] | None:
        seen_bvids: set[str] = set()
        rows: list[dict[str, Any]] = []
        for page in range(1, _MAX_METADATA_PAGES + 1):
            if page > 1:
                time.sleep(0.25)
            payload = self._request_metadata(
                _SEARCH_ENDPOINT,
                {
                    "search_type": "video",
                    "keyword": creator_name,
                    "page": str(page),
                    "order": "pubdate",
                },
            )
            data = self._mapping(payload, "data")
            if data is None:
                return tuple(rows) if rows else None
            results = data.get("result")
            if not isinstance(results, list):
                return tuple(rows)
            reached_history = False
            for result in results:
                row = self._search_row(result, creator_uid)
                if row is None:
                    continue
                timestamp = float(row["timestamp"])
                if datetime.fromtimestamp(timestamp, tz=timezone.utc) <= published_after:
                    reached_history = True
                    continue
                bvid = str(row["id"])
                if bvid not in seen_bvids:
                    rows.append(row)
                    seen_bvids.add(bvid)
                if len(rows) >= _MAX_SPACE_VIDEOS:
                    return tuple(rows)
            page_count = int(data.get("numPages") or page)
            if reached_history or page >= page_count:
                return tuple(rows)
        return tuple(rows)

    @staticmethod
    def _dynamic_row(item: Any, creator_uid: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        modules = item.get("modules")
        if not isinstance(modules, dict):
            return None
        major = BilibiliSource._mapping(modules, "module_dynamic", "major")
        archive = BilibiliSource._mapping(major, "archive")
        author = BilibiliSource._mapping(modules, "module_author")
        if archive is None or author is None:
            return None
        bvid = str(archive.get("bvid") or "")
        timestamp = author.get("pub_ts")
        if not _BVID_RE.fullmatch(bvid) or timestamp is None:
            return None
        return {
            "id": bvid,
            "title": BilibiliSource._plain_text(archive.get("title")),
            "timestamp": timestamp,
            "duration": BilibiliSource._duration_seconds(archive.get("duration_text")),
            "uploader_id": creator_uid,
        }

    @staticmethod
    def _arc_row(video: Any, creator_uid: str) -> dict[str, Any] | None:
        if not isinstance(video, dict):
            return None
        bvid = str(video.get("bvid") or "")
        timestamp = video.get("created")
        if not _BVID_RE.fullmatch(bvid) or timestamp is None:
            return None
        return {
            "id": bvid,
            "title": BilibiliSource._plain_text(video.get("title")),
            "timestamp": timestamp,
            "duration": BilibiliSource._duration_seconds(video.get("length")),
            "uploader_id": creator_uid,
        }

    @staticmethod
    def _search_row(result: Any, creator_uid: str) -> dict[str, Any] | None:
        if not isinstance(result, dict) or str(result.get("mid") or "") != creator_uid:
            return None
        bvid = str(result.get("bvid") or "")
        timestamp = result.get("pubdate")
        if not _BVID_RE.fullmatch(bvid) or timestamp is None:
            return None
        return {
            "id": bvid,
            "title": BilibiliSource._plain_text(result.get("title")),
            "timestamp": timestamp,
            "duration": BilibiliSource._duration_seconds(result.get("duration")),
            "uploader_id": creator_uid,
        }

    @staticmethod
    def _related_row(item: Any, creator_uid: str) -> dict[str, Any] | None:
        if not isinstance(item, dict):
            return None
        owner = BilibiliSource._mapping(item, "owner")
        if owner is None or str(owner.get("mid") or "") != creator_uid:
            return None
        bvid = str(item.get("bvid") or "")
        timestamp = item.get("pubdate")
        if not _BVID_RE.fullmatch(bvid) or timestamp is None:
            return None
        return {
            "id": bvid,
            "title": BilibiliSource._plain_text(item.get("title")),
            "timestamp": timestamp,
            "duration": BilibiliSource._duration_seconds(item.get("duration")),
            "uploader_id": creator_uid,
        }

    def _request_metadata(self, url: str, params: dict[str, str]) -> dict[str, Any] | None:
        for attempt in range(2):
            try:
                payload = self._metadata_get(url, params)
            except Exception:
                if attempt == 0:
                    time.sleep(0.25)
                    continue
                return None
            code = payload.get("code") if isinstance(payload, dict) else None
            if code == 0:
                return payload
            if code in {-429, -500, -502, -503, -504} and attempt == 0:
                time.sleep(0.25)
                continue
            return None
        return None

    @staticmethod
    def _default_metadata_get(url: str, params: dict[str, str]) -> dict[str, Any]:
        import httpx

        referer = (
            "https://space.bilibili.com/"
            if "/space/" in url or url.endswith("/nav")
            else "https://search.bilibili.com/"
        )
        response = httpx.get(
            url,
            params=params,
            timeout=10.0,
            headers={
                "Accept": "application/json, text/plain, */*",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": referer,
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/131.0.0.0 Safari/537.36"
                ),
            },
        )
        if response.status_code != 200:
            return {"code": -response.status_code}
        payload = response.json()
        return payload if isinstance(payload, dict) else {"code": -1}

    @staticmethod
    def _mapping(value: Any, *keys: str) -> dict[str, Any] | None:
        current = value
        for key in keys:
            if not isinstance(current, dict):
                return None
            current = current.get(key)
        return current if isinstance(current, dict) else None

    @staticmethod
    def _plain_text(value: Any) -> str:
        return re.sub(r"<[^>]+>", "", unescape(str(value or ""))).strip()

    @staticmethod
    def _duration_seconds(value: Any) -> int:
        if value is None:
            return 0
        if isinstance(value, (int, float)):
            return int(float(value))
        parts = str(value).strip().split(":")
        try:
            return sum(int(part) * 60**index for index, part in enumerate(reversed(parts)))
        except ValueError:
            return 0

    @staticmethod
    def _canonical_source_url(source: PublicSource) -> str:
        if source.kind == "space":
            return f"https://space.bilibili.com/{source.value}"
        return f"https://www.bilibili.com/video/{source.value}"

    @staticmethod
    def _to_video(row: dict[str, Any], fallback_creator_uid: str | None) -> Video:
        bvid = str(row.get("id") or row.get("display_id") or "")
        if not _BVID_RE.fullmatch(bvid):
            raise ValueError("yt-dlp metadata did not contain a Bilibili BVID")
        timestamp = row.get("timestamp")
        if timestamp is None:
            raise ValueError("yt-dlp metadata did not contain a publication timestamp")
        creator_uid = str(row.get("uploader_id") or fallback_creator_uid or "")
        if not creator_uid:
            raise ValueError("yt-dlp metadata did not contain a creator UID")
        return Video(
            bvid=bvid,
            creator_uid=creator_uid,
            title=str(row.get("title") or ""),
            published_at=datetime.fromtimestamp(float(timestamp), tz=timezone.utc),
            duration_sec=int(float(row.get("duration") or 0)),
            url=f"https://www.bilibili.com/video/{bvid}",
        )

    def _checked_destination(self, destination: Path) -> Path:
        if self._temporary_root is None:
            raise ValueError("a task-scoped temporary_root is required for audio downloads")
        destination = destination.resolve()
        try:
            destination.relative_to(self._temporary_root)
        except ValueError as error:
            raise ValueError("destination must be inside the configured temporary root") from error
        destination.mkdir(parents=True, exist_ok=True)
        return destination

    def _checked_download_path(self, path: Path) -> Path:
        assert self._temporary_root is not None
        resolved = path.resolve()
        try:
            resolved.relative_to(self._temporary_root)
        except ValueError as error:
            raise ValueError("yt-dlp returned a path outside the configured temporary root") from error
        return resolved

    @staticmethod
    def _runner_output_path(stdout: str, default: Path) -> Path:
        lines = [line.strip() for line in stdout.splitlines() if line.strip()]
        return Path(lines[-1]) if lines else default
