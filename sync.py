#!/usr/bin/env python3
"""Sync 亲小禾 (Qin Xiao He) photos and videos from Mac WeChat mini program cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterator
from urllib.parse import unquote, urlparse

APPID = "wx54ef0cc36d1ddf68"
APP_NAME = "亲小禾"

WECHAT_ROOT = (
    Path.home()
    / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium"
)

# User-published / attendance photos; excludes appletstatic UI assets.
PHOTO_HOST_PATTERNS = (
    re.compile(r"^album-img\.xiaohebook\.com$"),
    re.compile(r"^sign-img\.xiaohebook\.com$"),
    re.compile(r"^notice-img\.xiaohebook\.com$"),
    re.compile(r"^img\.qn\.xiaohebook\.com$"),
    re.compile(r"^img\.xiaohebook\.com$"),
)

IMAGE_SIGNATURES: tuple[tuple[bytes, str], ...] = (
    (b"\xff\xd8\xff", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "png"),
    (b"RIFF", "webp"),
)

URL_RE = re.compile(rb"https?://[a-zA-Z0-9._~:/?#\[\]@!$&'()*+,;=%\-]+")
API_PAGE_MARKER = b"growSpace/page"
ALBUM_IMG_URL_RE = re.compile(r"https://album-img\.xiaohebook\.com/[^\"\\]+")
SIGN_IMG_URL_RE = re.compile(r"https://sign-img\.xiaohebook\.com/[^\"\\]+")
ALBUM_VIDEO_URL_RE = re.compile(r"https://album-video\.xiaohebook\.com/tmp_[^\"\\?\|]+\.mp4")
VIDEOQN_URL_RE = re.compile(r"https://videoqn\.xiaohebook\.com/[^\"\\?\|]+\.mp4")
VIDEO_CONTENT_RE = re.compile(
    r'"contentType":"video"[^}]*"content":"(https://[^"]+\.mp4)"'
)
DOWNLOAD_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) qinxiaohe-photo-sync/1.0"
VIDEO_DIR_NAME = "videos"
STATE_SAVE_EVERY = 20


def log(message: str = "") -> None:
    print(message, flush=True)


def format_bytes(num: int) -> str:
    if num < 1024:
        return f"{num} B"
    if num < 1024 * 1024:
        return f"{num / 1024:.1f} KB"
    if num < 1024 * 1024 * 1024:
        return f"{num / (1024 * 1024):.1f} MB"
    return f"{num / (1024 * 1024 * 1024):.2f} GB"


def synced_photo_urls(photos_state: dict) -> set[str]:
    urls: set[str] = set()
    for entry in photos_state.values():
        url = entry.get("url")
        if url:
            urls.add(url)
    return urls


@dataclass(frozen=True)
class PhotoCandidate:
    url: str
    data: bytes
    ext: str
    source: str
    sha256: str


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def is_photo_url(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    if not any(pattern.match(host) for pattern in PHOTO_HOST_PATTERNS):
        return False
    if "appletstatic" in host:
        return False

    path = unquote(urlparse(url).path).lower()
    ui_markers = (
        "/icon",
        "-icon",
        "head-portrait",
        "banner",
        "button_",
        "welcome-",
        "member-",
        "operate-",
        "shortcut-key",
        "calendar-",
        "filter-icon",
        "close-",
        "arrow-",
        "thumbs-up",
        "publish-comment",
        "publish-share",
        "publish-thumbs",
        "bookdetail",
        "img_czkj",
        "img_switch",
        "non-member",
        "help-service",
        "home-add",
        "home-invite",
        "home-punch",
        "punch-card",
        "new-sign-in",
        "sign-in-shrink",
        "eye-icon",
        "next-icon",
        "video-play",
        "voice.png",
        "mail-list",
        "observeoptimize",
    )
    if any(marker in path for marker in ui_markers):
        return False

    if host == "img.xiaohebook.com":
        # Book covers and marketing art use fixed hex names; keep only tmp uploads.
        basename = Path(path).name
        return basename.startswith("tmp_") or "/tmp_" in path

    if host == "img.qn.xiaohebook.com":
        basename = Path(path).name
        return basename.startswith("tmp_")

    return True


def normalize_media_url(url: str) -> str:
    url = url.split("?")[0].strip()
    if "|" in url:
        url = url.split("|")[0]
    return url


def normalize_photo_url(url: str) -> str:
    return normalize_media_url(url)


def normalize_video_url(url: str) -> str:
    return normalize_media_url(url)


def is_video_url(url: str) -> bool:
    clean = normalize_video_url(url)
    host = urlparse(clean).netloc.lower()
    if host not in {"album-video.xiaohebook.com", "videoqn.xiaohebook.com"}:
        return False
    path = unquote(urlparse(clean).path).lower()
    if not path.endswith(".mp4"):
        return False
    basename = Path(path).name
    return basename.startswith("tmp_") or host == "videoqn.xiaohebook.com"


def ext_from_url(url: str, default: str = "jpg") -> str:
    suffix = Path(unquote(urlparse(url).path)).suffix.lower().lstrip(".")
    return suffix or default


def filename_from_url(url: str, ext: str) -> str:
    parsed = urlparse(url)
    name = Path(unquote(parsed.path)).name or "photo"
    name = name.split("?")[0]
    if not name.lower().endswith(f".{ext}"):
        name = f"{name}.{ext}"
    name = re.sub(r"[^\w.\-]+", "_", name)
    return name[:180] or f"photo.{ext}"


def download_photo(url: str) -> tuple[bytes, str] | None:
    clean_url = normalize_photo_url(url)
    request = urllib.request.Request(
        clean_url,
        headers={"User-Agent": DOWNLOAD_USER_AGENT},
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            data = response.read()
    except (urllib.error.URLError, TimeoutError, OSError):
        return None

    if data.startswith(b"\xff\xd8\xff"):
        return data, "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "png"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return data, "webp"
    if len(data) < 1024:
        return None
    return data, ext_from_url(clean_url)


def download_video(url: str, target: Path, *, on_progress: Callable[[int, int], None] | None = None) -> str | None:
    clean_url = normalize_video_url(url)
    request = urllib.request.Request(
        clean_url,
        headers={"User-Agent": DOWNLOAD_USER_AGENT},
    )
    hasher = hashlib.sha256()
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            total = int(response.headers.get("Content-Length", 0) or 0)
            target.parent.mkdir(parents=True, exist_ok=True)
            downloaded = 0
            with target.open("wb") as handle:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    hasher.update(chunk)
                    handle.write(chunk)
                    downloaded += len(chunk)
                    if on_progress is not None:
                        on_progress(downloaded, total)
    except (urllib.error.URLError, TimeoutError, OSError):
        if target.exists():
            target.unlink(missing_ok=True)
        return None

    if target.stat().st_size < 1024:
        target.unlink(missing_ok=True)
        return None
    return hasher.hexdigest()


def find_image_payload(data: bytes, url_end: int) -> tuple[bytes, str] | None:
    tail = data[url_end:]
    best: tuple[int, str] | None = None
    for signature, ext in IMAGE_SIGNATURES:
        idx = tail.find(signature)
        if idx >= 0 and (best is None or idx < best[0]):
            best = (idx, ext)
    if best is None:
        return None

    offset = best[0]
    ext = best[1]
    payload = tail[offset:]
    if ext == "webp" and not payload.startswith(b"RIFF"):
        return None
    if len(payload) < 1024:
        return None
    return payload, ext


def extract_photos_from_cache_file(path: Path) -> Iterator[PhotoCandidate]:
    try:
        data = path.read_bytes()
    except OSError:
        return

    if b"xiaohebook" not in data:
        return

    for match in URL_RE.finditer(data):
        raw_url = match.group(0)
        try:
            url = raw_url.decode("utf-8", errors="ignore")
        except UnicodeDecodeError:
            continue
        if "xiaohebook" not in url or not is_photo_url(url):
            continue

        extracted = find_image_payload(data, match.end())
        if extracted is None:
            continue
        payload, ext = extracted
        digest = sha256_bytes(payload)
        yield PhotoCandidate(
            url=url,
            data=payload,
            ext=ext,
            source=f"cache:{path.name}",
            sha256=digest,
        )


def sniff_image_file(path: Path) -> tuple[bytes, str] | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if data.startswith(b"\xff\xd8\xff"):
        return data, "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return data, "png"
    if data.startswith(b"RIFF") and len(data) > 12 and data[8:12] == b"WEBP":
        return data, "webp"
    return None


def extract_photo_urls_from_api_cache(path: Path) -> Iterator[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return
    if API_PAGE_MARKER not in data and b'"contentList"' not in data:
        return

    text = data.decode("utf-8", errors="ignore")
    seen: set[str] = set()
    for pattern in (ALBUM_IMG_URL_RE, SIGN_IMG_URL_RE):
        for match in pattern.finditer(text):
            url = normalize_photo_url(match.group(0))
            if url in seen or not is_photo_url(url):
                continue
            seen.add(url)
            yield url


def extract_video_urls_from_api_cache(path: Path) -> Iterator[str]:
    try:
        data = path.read_bytes()
    except OSError:
        return
    if API_PAGE_MARKER not in data and b'"contentList"' not in data:
        return

    text = data.decode("utf-8", errors="ignore")
    seen: set[str] = set()
    patterns = (
        VIDEO_CONTENT_RE,
        ALBUM_VIDEO_URL_RE,
        VIDEOQN_URL_RE,
    )
    for pattern in patterns:
        for match in pattern.finditer(text):
            raw = match.group(1) if pattern is VIDEO_CONTENT_RE else match.group(0)
            url = normalize_video_url(raw)
            if url in seen or not is_video_url(url):
                continue
            seen.add(url)
            yield url


def extract_photos_from_applet_store(path: Path) -> Iterator[PhotoCandidate]:
    sniffed = sniff_image_file(path)
    if sniffed is None:
        return
    data, ext = sniffed
    if len(data) < 8 * 1024:
        # Skip tiny icons / avatars in applet store.
        return
    digest = sha256_bytes(data)
    yield PhotoCandidate(
        url=f"applet-store://{path.name}",
        data=data,
        ext=ext,
        source=f"applet:{path.parent.parent.parent.name}",
        sha256=digest,
    )


def discover_sources() -> tuple[list[Path], list[Path]]:
    cache_dirs: list[Path] = []
    applet_dirs: list[Path] = []

    web_profiles = WECHAT_ROOT / "web" / "profiles"
    if web_profiles.is_dir():
        cache_dirs.extend(
            sorted(p for p in web_profiles.glob("webview_*/Cache/Cache_Data") if p.is_dir())
        )

    users_root = WECHAT_ROOT / "users"
    if users_root.is_dir():
        applet_dirs.extend(
            sorted(
                p
                for p in users_root.glob(f"*/applet/local/{APPID}/store/images")
                if p.is_dir()
            )
        )

    return cache_dirs, applet_dirs


def load_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"version": 2, "photos": {}, "videos": {}}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 2, "photos": {}, "videos": {}}
    state.setdefault("photos", {})
    state.setdefault("videos", {})
    return state


def save_state(state_path: Path, state: dict) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def unique_output_path(output_dir: Path, filename: str, digest: str) -> Path:
    candidate = output_dir / filename
    if not candidate.exists():
        return candidate
    stem = Path(filename).stem
    ext = Path(filename).suffix
    return output_dir / f"{stem}_{digest[:8]}{ext}"


def collect_video_url_candidates(cache_dirs: list[Path]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for cache_dir in cache_dirs:
        for entry in cache_dir.iterdir():
            if not entry.is_file() or entry.name in {"index", "the-real-index"}:
                continue
            for url in extract_video_urls_from_api_cache(entry):
                urls.setdefault(url, f"api:{entry.name}")
    return urls


def collect_url_candidates(cache_dirs: list[Path]) -> dict[str, str]:
    urls: dict[str, str] = {}
    for cache_dir in cache_dirs:
        for entry in cache_dir.iterdir():
            if not entry.is_file() or entry.name in {"index", "the-real-index"}:
                continue
            for url in extract_photo_urls_from_api_cache(entry):
                urls.setdefault(url, f"api:{entry.name}")
    return urls


def collect_embedded_candidates(
    cache_dirs: list[Path],
    applet_dirs: list[Path],
) -> dict[str, PhotoCandidate]:
    found: dict[str, PhotoCandidate] = {}

    for cache_dir in cache_dirs:
        for entry in cache_dir.iterdir():
            if not entry.is_file() or entry.name in {"index", "the-real-index"}:
                continue
            for photo in extract_photos_from_cache_file(entry):
                found.setdefault(photo.sha256, photo)

    for image_dir in applet_dirs:
        for entry in image_dir.iterdir():
            if not entry.is_file():
                continue
            for photo in extract_photos_from_applet_store(entry):
                found.setdefault(photo.sha256, photo)

    return found


def sync_videos(
    output_dir: Path,
    state_path: Path,
    cache_dirs: list[Path],
    *,
    dry_run: bool = False,
    download_urls: bool = True,
) -> tuple[int, int, int]:
    if not download_urls:
        return 0, 0, 0

    video_urls = collect_video_url_candidates(cache_dirs)
    if not video_urls:
        return 0, 0, 0

    total = len(video_urls)
    log(f"发现 {total} 个视频，开始同步…")

    state = load_state(state_path)
    videos_state: dict = state.setdefault("videos", {})
    video_dir = output_dir / VIDEO_DIR_NAME

    copied = 0
    skipped = 0
    for index, url in enumerate(sorted(video_urls), start=1):
        filename = filename_from_url(url, "mp4")
        if dry_run:
            log(f"[{index}/{total}] [dry-run] {VIDEO_DIR_NAME}/{filename}")
            copied += 1
            continue

        existing = videos_state.get(url)
        if existing:
            target = video_dir / existing["filename"]
            if target.exists():
                log(f"[{index}/{total}] 跳过已同步视频: {existing['filename']}")
                skipped += 1
                continue

        log(f"[{index}/{total}] 下载视频: {filename}")
        target = unique_output_path(video_dir, filename, sha256_bytes(url.encode("utf-8")))

        last_reported_mb = 0

        def report_video_progress(downloaded: int, content_length: int) -> None:
            nonlocal last_reported_mb
            current_mb = downloaded // (1024 * 1024)
            if content_length > 0:
                percent = downloaded * 100 // content_length
                if current_mb > last_reported_mb or downloaded >= content_length:
                    log(
                        f"  └─ {format_bytes(downloaded)} / {format_bytes(content_length)} ({percent}%)"
                    )
                    last_reported_mb = current_mb
            elif current_mb > last_reported_mb:
                log(f"  └─ 已下载 {format_bytes(downloaded)}")
                last_reported_mb = current_mb

        digest = download_video(url, target, on_progress=report_video_progress)
        if digest is None:
            log(f"  └─ 下载失败: {url}")
            continue

        videos_state[url] = {
            "url": url,
            "filename": target.name,
            "sha256": digest,
            "source": video_urls[url],
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "bytes": target.stat().st_size,
        }
        log(f"  └─ 已保存: {target} ({format_bytes(target.stat().st_size)})")
        copied += 1
        save_state(state_path, state)

    if total:
        log(f"视频阶段：新增 {copied}，跳过 {skipped}，共 {total} 项")
        log("")

    return copied, skipped, total


def sync_all(
    output_dir: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
    download_urls: bool = True,
) -> None:
    cache_dirs, applet_dirs = discover_sources()
    if not cache_dirs and not applet_dirs:
        log("未找到 Mac 微信缓存目录，请确认已用 Mac 版微信打开过亲小禾。")
        return

    log("开始同步亲小禾照片和视频…")
    log("")

    photo_copied, photo_skipped, photo_total = sync_photos(
        output_dir,
        state_path,
        dry_run=dry_run,
        download_urls=download_urls,
        cache_dirs=cache_dirs,
        applet_dirs=applet_dirs,
    )
    video_copied, video_skipped, video_total = sync_videos(
        output_dir,
        state_path,
        cache_dirs,
        dry_run=dry_run,
        download_urls=download_urls,
    )

    log(
        f"完成：照片新增 {photo_copied} 张，跳过 {photo_skipped} 张，识别 {photo_total} 张；"
        f"视频新增 {video_copied} 个，跳过 {video_skipped} 个，识别 {video_total} 个"
    )
    log(f"保存目录: {output_dir}（视频在 {output_dir / VIDEO_DIR_NAME}）")
    if photo_copied == 0 and photo_total == 0 and video_copied == 0 and video_total == 0:
        log(
            "\n提示：先运行 browse.py 自动滚动加载动态，或手动浏览亲小禾成长空间后再同步："
            "\n  python3 scripts/qinxiaohe-photo-sync/browse.py"
        )


def sync_photos(
    output_dir: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
    download_urls: bool = True,
    cache_dirs: list[Path] | None = None,
    applet_dirs: list[Path] | None = None,
) -> tuple[int, int, int]:
    if cache_dirs is None or applet_dirs is None:
        cache_dirs, applet_dirs = discover_sources()
    if not cache_dirs and not applet_dirs:
        return 0, 0, 0

    state = load_state(state_path)
    photos_state: dict = state.setdefault("photos", {})
    already_synced_urls = synced_photo_urls(photos_state)

    photo_urls = collect_url_candidates(cache_dirs) if download_urls else {}
    embedded = collect_embedded_candidates(cache_dirs, applet_dirs)
    total_tasks = len(photo_urls) + len(embedded)

    if total_tasks:
        log(f"发现 {len(photo_urls)} 个照片 URL + {len(embedded)} 个缓存图片，开始下载…")
    elif download_urls:
        log("未发现新的照片任务")

    copied = 0
    skipped = 0
    failed = 0
    task_index = 0
    output_dir.mkdir(parents=True, exist_ok=True)

    if download_urls:
        for url, source in sorted(photo_urls.items()):
            task_index += 1
            filename = filename_from_url(url, ext_from_url(url))

            if url in already_synced_urls:
                log(f"[{task_index}/{total_tasks}] 跳过已同步照片: {filename}")
                skipped += 1
                continue

            if dry_run:
                log(f"[{task_index}/{total_tasks}] [dry-run] {filename}")
                copied += 1
                continue

            log(f"[{task_index}/{total_tasks}] 下载照片: {filename}")
            downloaded = download_photo(url)
            if downloaded is None:
                log("  └─ 下载失败")
                failed += 1
                continue

            data, ext = downloaded
            digest = sha256_bytes(data)
            if digest in photos_state:
                log("  └─ 跳过重复内容")
                skipped += 1
                continue

            target = unique_output_path(output_dir, filename_from_url(url, ext), digest)
            target.write_bytes(data)
            photos_state[digest] = {
                "url": url,
                "filename": target.name,
                "source": source,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "bytes": len(data),
            }
            already_synced_urls.add(url)
            log(f"  └─ 已保存: {target.name} ({format_bytes(len(data))})")
            copied += 1
            if copied % STATE_SAVE_EVERY == 0:
                state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
                save_state(state_path, state)

    for digest, photo in sorted(embedded.items(), key=lambda item: item[1].url):
        task_index += 1
        if digest in photos_state:
            log(f"[{task_index}/{total_tasks}] 跳过已同步缓存图: {photo.url}")
            skipped += 1
            continue

        filename = filename_from_url(photo.url, photo.ext)
        if dry_run:
            log(f"[{task_index}/{total_tasks}] [dry-run] {filename}")
            copied += 1
            continue

        log(f"[{task_index}/{total_tasks}] 导出缓存图: {filename}")
        target = unique_output_path(output_dir, filename, digest)
        target.write_bytes(photo.data)
        photos_state[digest] = {
            "url": photo.url,
            "filename": target.name,
            "source": photo.source,
            "synced_at": datetime.now(timezone.utc).isoformat(),
            "bytes": len(photo.data),
        }
        log(f"  └─ 已保存: {target.name} ({format_bytes(len(photo.data))})")
        copied += 1

    if not dry_run and (copied or skipped):
        state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)

    if failed:
        log(f"照片下载失败 {failed} 个")

    if total_tasks or copied or skipped:
        log(f"照片阶段：新增 {copied}，跳过 {skipped}，共 {total_tasks} 项")
        log("")

    return copied, skipped, total_tasks


def watch_loop(
    output_dir: Path,
    state_path: Path,
    interval: float,
) -> None:
    log(f"监听中（每 {interval:.0f}s 扫描一次），在 Mac 微信里浏览亲小禾即可自动同步。")
    log("按 Ctrl+C 停止。")
    while True:
        cache_dirs, applet_dirs = discover_sources()
        photo_copied, photo_skipped, photo_total = sync_photos(
            output_dir,
            state_path,
            download_urls=True,
            cache_dirs=cache_dirs,
            applet_dirs=applet_dirs,
        )
        video_copied, video_skipped, video_total = sync_videos(
            output_dir,
            state_path,
            cache_dirs,
            download_urls=True,
        )
        if photo_copied or video_copied:
            log(
                f"本轮新增 照片 {photo_copied} 张 / 视频 {video_copied} 个"
                f"（识别 {photo_total} 张图、{video_total} 个视频）"
            )
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    default_output = Path.home() / "Pictures" / APP_NAME
    default_state = default_output / ".sync-state.json"

    parser = argparse.ArgumentParser(
        description=f"从 Mac 微信缓存同步{APP_NAME}小程序照片和视频（支持一次性全量 + 增量）",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_output,
        help=f"保存目录（默认: {default_output}）",
    )
    parser.add_argument(
        "--state",
        type=Path,
        default=None,
        help="增量状态文件路径（默认: <output>/.sync-state.json）",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="持续监听，浏览小程序时自动增量同步",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="--watch 模式下的扫描间隔秒数（默认 5）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将要保存的文件，不写入磁盘",
    )
    parser.add_argument(
        "--cache-only",
        action="store_true",
        help="只从微信图片缓存提取，不从 API 缓存下载原图",
    )
    parser.add_argument(
        "--list-sources",
        action="store_true",
        help="列出检测到的微信缓存路径",
    )
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    parser = build_parser()
    args = parser.parse_args()
    output_dir: Path = args.output.expanduser()
    state_path: Path = (args.state or output_dir / ".sync-state.json").expanduser()

    if args.list_sources:
        cache_dirs, applet_dirs = discover_sources()
        print("Chromium 缓存目录:")
        for path in cache_dirs:
            print(f"  {path}")
        print("小程序图片缓存:")
        for path in applet_dirs:
            print(f"  {path}")
        return 0

    if args.watch:
        try:
            watch_loop(output_dir, state_path, args.interval)
        except KeyboardInterrupt:
            print("\n已停止监听。")
        return 0

    sync_all(
        output_dir,
        state_path,
        dry_run=args.dry_run,
        download_urls=not args.cache_only,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
