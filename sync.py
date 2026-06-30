#!/usr/bin/env python3
"""Sync 亲小禾 (Qin Xiao He) photos from Mac WeChat mini program cache."""

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
from typing import Iterator
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
DOWNLOAD_USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) qinxiaohe-photo-sync/1.0"


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


def normalize_photo_url(url: str) -> str:
    return url.split("?")[0].strip()


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
        return {"version": 1, "photos": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "photos": {}}


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


def collect_candidates(
    cache_dirs: list[Path],
    applet_dirs: list[Path],
    *,
    download_urls: bool = True,
    dry_run: bool = False,
) -> dict[str, PhotoCandidate]:
    found: dict[str, PhotoCandidate] = {}

    if download_urls:
        for url, source in collect_url_candidates(cache_dirs).items():
            if dry_run:
                ext = ext_from_url(url)
                placeholder = f"url:{url}".encode("utf-8")
                digest = sha256_bytes(placeholder)
                found.setdefault(
                    digest,
                    PhotoCandidate(url=url, data=b"", ext=ext, source=source, sha256=digest),
                )
                continue
            downloaded = download_photo(url)
            if downloaded is None:
                continue
            data, ext = downloaded
            digest = sha256_bytes(data)
            found.setdefault(
                digest,
                PhotoCandidate(url=url, data=data, ext=ext, source=source, sha256=digest),
            )

    for digest, photo in collect_embedded_candidates(cache_dirs, applet_dirs).items():
        found.setdefault(digest, photo)

    return found


def sync_photos(
    output_dir: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
    download_urls: bool = True,
) -> tuple[int, int, int]:
    cache_dirs, applet_dirs = discover_sources()
    if not cache_dirs and not applet_dirs:
        print("未找到 Mac 微信缓存目录，请确认已用 Mac 版微信打开过亲小禾。", file=sys.stderr)
        return 0, 0, 0

    candidates = collect_candidates(
        cache_dirs,
        applet_dirs,
        download_urls=download_urls,
        dry_run=dry_run,
    )
    state = load_state(state_path)
    photos_state: dict = state.setdefault("photos", {})

    copied = 0
    skipped = 0
    for digest, photo in sorted(candidates.items(), key=lambda item: item[1].url):
        if digest in photos_state:
            skipped += 1
            continue

        output_dir.mkdir(parents=True, exist_ok=True)
        filename = filename_from_url(photo.url, photo.ext)
        target = unique_output_path(output_dir, filename, digest)

        if dry_run:
            print(f"[dry-run] {target.name} <- {photo.url}")
        else:
            target.write_bytes(photo.data)
            photos_state[digest] = {
                "url": photo.url,
                "filename": target.name,
                "source": photo.source,
                "synced_at": datetime.now(timezone.utc).isoformat(),
                "bytes": len(photo.data),
            }
            print(f"已保存: {target}")
        copied += 1

    if not dry_run:
        state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        save_state(state_path, state)

    return copied, skipped, len(candidates)


def watch_loop(
    output_dir: Path,
    state_path: Path,
    interval: float,
) -> None:
    print(f"监听中（每 {interval:.0f}s 扫描一次），在 Mac 微信里浏览亲小禾照片即可自动同步。")
    print("按 Ctrl+C 停止。")
    while True:
        copied, skipped, total = sync_photos(
            output_dir,
            state_path,
            download_urls=True,
        )
        if copied:
            print(f"本轮新增 {copied} 张（缓存中共识别 {total} 张，已同步 {skipped + copied} 张）")
        time.sleep(interval)


def build_parser() -> argparse.ArgumentParser:
    default_output = Path.home() / "Pictures" / APP_NAME
    default_state = default_output / ".sync-state.json"

    parser = argparse.ArgumentParser(
        description=f"从 Mac 微信缓存同步{APP_NAME}小程序照片（支持一次性全量 + 增量）",
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

    copied, skipped, total = sync_photos(
        output_dir,
        state_path,
        dry_run=args.dry_run,
        download_urls=not args.cache_only,
    )
    print(
        f"完成：新增 {copied} 张，跳过已同步 {skipped} 张，"
        f"本次扫描识别 {total} 张（保存目录: {output_dir}）"
    )
    if copied == 0 and total == 0:
        print(
            "\n提示：先运行 browse.py 自动滚动加载动态，或手动浏览亲小禾成长空间后再同步："
            "\n  python3 scripts/qinxiaohe-photo-sync/browse.py",
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
