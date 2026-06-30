#!/usr/bin/env python3
"""Auto-scroll 亲小禾 feed in Mac WeChat to load album API pages into cache."""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from pathlib import Path

WECHAT_ROOT = (
    Path.home()
    / "Library/Containers/com.tencent.xinWeChat/Data/Documents/app_data/radium"
)
API_PAGE_MARKER = b"growSpace/page"


def discover_cache_dirs() -> list[Path]:
    web_profiles = WECHAT_ROOT / "web" / "profiles"
    if not web_profiles.is_dir():
        return []
    return sorted(p for p in web_profiles.glob("webview_*/Cache/Cache_Data") if p.is_dir())


def scan_feed_cache() -> tuple[set[int], int | None, int]:
    pages: set[int] = set()
    total_posts: int | None = None
    photo_urls = 0

    for cache_dir in discover_cache_dirs():
        for entry in cache_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                continue
            if API_PAGE_MARKER not in data:
                continue

            text = data.decode("utf-8", errors="ignore")
            page_match = re.search(
                r"growSpace/page\?[^\"]*pageNum=(\d+).*?\"total\":(\d+)",
                text,
                re.DOTALL,
            )
            if page_match:
                pages.add(int(page_match.group(1)))
                total_posts = int(page_match.group(2))

            photo_urls += len(
                re.findall(r"https://album-img\.xiaohebook\.com/[^\"\\]+", text)
            )

    return pages, total_posts, photo_urls


def expected_pages(total_posts: int | None, page_size: int) -> int | None:
    if total_posts is None:
        return None
    return (total_posts + page_size - 1) // page_size


def accessibility_trusted() -> bool:
    script = 'tell application "System Events" to return true'
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    return result.returncode == 0


def activate_wechat() -> None:
    subprocess.run(["open", "-a", "WeChat"], check=False)
    subprocess.run(
        ["osascript", "-e", 'tell application "WeChat" to activate'],
        check=False,
    )


def scroll_down(lines: int = 8) -> None:
    import Quartz
    from Quartz import (
        CGEventCreateScrollWheelEvent,
        CGEventPost,
        kCGHIDEventTap,
        kCGScrollEventUnitLine,
    )

    event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitLine, 1, -lines)
    CGEventPost(kCGHIDEventTap, event)


def countdown(seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}s 后自动开始滚动…", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")


def run_browse(
    *,
    scroll_count: int,
    scroll_lines: int,
    pause: float,
    until_pages: int | None,
    prep_seconds: int,
) -> int:
    if not accessibility_trusted():
        print(
            "需要「辅助功能」权限才能自动控制滚动。\n"
            "请到 系统设置 → 隐私与安全性 → 辅助功能，"
            "为 Terminal（或 Cursor）开启权限后重试。",
            file=sys.stderr,
        )
        return 1

    pages, total_posts, photo_urls = scan_feed_cache()
    target_pages = until_pages or expected_pages(total_posts, page_size=10)

    print("亲小禾自动浏览")
    print("=" * 40)
    print("请先手动完成以下步骤：")
    print("  1. 打开 Mac 版微信")
    print("  2. 进入「亲小禾」小程序")
    print("  3. 打开「成长空间 / 班级动态」列表页")
    print("  4. 把鼠标移到动态列表区域（滚轮会在这里生效）")
    print()
    if total_posts:
        print(f"当前缓存：已加载 {len(pages)} 页 API，共 {total_posts} 条动态，约 {photo_urls} 个图片 URL")
        if target_pages:
            print(f"目标：加载全部 {target_pages} 页 API 数据")
    print()
    countdown(prep_seconds)

    activate_wechat()
    time.sleep(0.8)

    last_pages = len(pages)
    stagnant_rounds = 0

    for i in range(1, scroll_count + 1):
        scroll_down(scroll_lines)
        time.sleep(pause)

        pages, total_posts, photo_urls = scan_feed_cache()
        target_pages = until_pages or expected_pages(total_posts, page_size=10)
        page_info = f"{len(pages)}"
        if target_pages:
            page_info = f"{len(pages)}/{target_pages}"

        print(
            f"滚动 {i}/{scroll_count} | API 页 {page_info} | "
            f"图片 URL {photo_urls}",
            flush=True,
        )

        if target_pages and len(pages) >= target_pages:
            print(f"\n已加载全部 {target_pages} 页 API 数据，可以停止浏览。")
            break

        if len(pages) == last_pages:
            stagnant_rounds += 1
            if stagnant_rounds >= 8:
                print("\n连续多轮没有新页面，可能已到底或需要手动向上再向下刷新。")
                break
        else:
            stagnant_rounds = 0
            last_pages = len(pages)

    pages, total_posts, photo_urls = scan_feed_cache()
    target_pages = until_pages or expected_pages(total_posts, page_size=10)
    print()
    print("浏览结束。接下来运行同步下载原图：")
    print("  python3 scripts/qinxiaohe-photo-sync/sync.py")
    print()
    print(
        f"最终缓存：API 页 {len(pages)}"
        + (f"/{target_pages}" if target_pages else "")
        + f"，图片 URL {photo_urls}"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自动滚动亲小禾动态列表，让微信缓存相册 API 数据",
    )
    parser.add_argument(
        "--scrolls",
        type=int,
        default=120,
        help="最大滚动次数（默认 120）",
    )
    parser.add_argument(
        "--lines",
        type=int,
        default=10,
        help="每次滚动的行数（默认 10）",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=1.2,
        help="每次滚动后的等待秒数（默认 1.2）",
    )
    parser.add_argument(
        "--until-pages",
        type=int,
        default=None,
        help="加载到指定 API 页数后自动停止",
    )
    parser.add_argument(
        "--prep",
        type=int,
        default=8,
        help="开始滚动前的准备时间秒数（默认 8）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="只查看当前缓存进度，不滚动",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.status:
        pages, total_posts, photo_urls = scan_feed_cache()
        target = expected_pages(total_posts, page_size=10)
        print(f"API 页: {len(pages)}" + (f"/{target}" if target else ""))
        print(f"动态总数: {total_posts or '未知'}")
        print(f"图片 URL: {photo_urls}")
        if pages:
            print(f"已缓存页码: {', '.join(str(p) for p in sorted(pages))}")
        return 0

    return run_browse(
        scroll_count=args.scrolls,
        scroll_lines=args.lines,
        pause=args.pause,
        until_pages=args.until_pages,
        prep_seconds=args.prep,
    )


if __name__ == "__main__":
    raise SystemExit(main())
