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
ALBUM_IMG_URL_RE = re.compile(r"https://album-img\.xiaohebook\.com/[^\"\\]+")
DEFAULT_WINDOW_TITLE = "亲小禾"


def discover_cache_dirs() -> list[Path]:
    web_profiles = WECHAT_ROOT / "web" / "profiles"
    if not web_profiles.is_dir():
        return []
    return sorted(p for p in web_profiles.glob("webview_*/Cache/Cache_Data") if p.is_dir())


def scan_feed_cache() -> tuple[set[int], int | None, int]:
    pages: set[int] = set()
    total_posts: int | None = None
    unique_urls: set[str] = set()

    for cache_dir in discover_cache_dirs():
        for entry in cache_dir.iterdir():
            if not entry.is_file():
                continue
            try:
                data = entry.read_bytes()
            except OSError:
                continue
            if API_PAGE_MARKER not in data and b"album-img.xiaohebook.com" not in data:
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

            for match in ALBUM_IMG_URL_RE.finditer(text):
                unique_urls.add(match.group(0).split("?")[0])

    return pages, total_posts, len(unique_urls)


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


def run_applescript(script: str) -> str:
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "AppleScript failed")
    return result.stdout.strip()


def find_window_center(title: str = DEFAULT_WINDOW_TITLE) -> tuple[float, float] | None:
    safe_title = title.replace('"', '\\"')
    script = f'''
tell application "System Events"
    tell process "WeChat"
        set frontmost to true
        repeat with w in windows
            if name of w contains "{safe_title}" then
                set p to position of w
                set s to size of w
                set cx to (item 1 of p) + (item 1 of s) / 2
                set cy to (item 2 of p) + (item 2 of s) / 2
                return (cx as text) & "|" & (cy as text)
            end if
        end repeat
    end tell
end tell
'''
    try:
        output = run_applescript(script)
    except RuntimeError:
        return None
    if not output or "|" not in output:
        return None
    x_str, y_str = output.split("|", 1)
    return float(x_str), float(y_str)


def focus_wechat_window(title: str = DEFAULT_WINDOW_TITLE) -> bool:
    safe_title = title.replace('"', '\\"')
    script = f'''
tell application "WeChat" to activate
delay 0.2
tell application "System Events"
    tell process "WeChat"
        set frontmost to true
        repeat with w in windows
            if name of w contains "{safe_title}" then
                perform action "AXRaise" of w
                return "ok"
            end if
        end repeat
    end tell
end tell
'''
    try:
        return run_applescript(script) == "ok"
    except RuntimeError:
        return False


def move_mouse(x: float, y: float) -> None:
    from Quartz import (
        CGEventCreateMouseEvent,
        CGEventPost,
        kCGEventMouseMoved,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )

    event = CGEventCreateMouseEvent(None, kCGEventMouseMoved, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, event)


def click_at(x: float, y: float) -> None:
    from Quartz import (
        CGEventCreateMouseEvent,
        CGEventPost,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )

    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, (x, y), kCGMouseButtonLeft)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, (x, y), kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.03)
    CGEventPost(kCGHIDEventTap, up)


def scroll_pixels(amount: int) -> None:
    from Quartz import (
        CGEventCreateScrollWheelEvent,
        CGEventPost,
        kCGHIDEventTap,
        kCGScrollEventUnitPixel,
    )

    # Negative values scroll content downward (see older posts).
    event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitPixel, 1, -amount)
    CGEventPost(kCGHIDEventTap, event)


def press_page_down() -> None:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        kCGHIDEventTap,
    )

    # macOS virtual key code for Page Down.
    for event_type in (True, False):
        event = CGEventCreateKeyboardEvent(None, 121, event_type)
        CGEventPost(kCGHIDEventTap, event)


def press_arrow_down(times: int = 5) -> None:
    from Quartz import (
        CGEventCreateKeyboardEvent,
        CGEventPost,
        kCGHIDEventTap,
    )

    for _ in range(times):
        for pressed in (True, False):
            event = CGEventCreateKeyboardEvent(None, 125, pressed)
            CGEventPost(kCGHIDEventTap, event)
        time.sleep(0.02)


def drag_scroll_up(x: float, y: float, distance: int = 260) -> None:
    """Drag upward inside the feed to mimic touch scrolling."""
    from Quartz import (
        CGEventCreateMouseEvent,
        CGEventPost,
        kCGEventLeftMouseDragged,
        kCGEventLeftMouseDown,
        kCGEventLeftMouseUp,
        kCGHIDEventTap,
        kCGMouseButtonLeft,
    )

    start = (x, y)
    end = (x, y - distance)
    down = CGEventCreateMouseEvent(None, kCGEventLeftMouseDown, start, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, down)
    time.sleep(0.05)
    steps = 8
    for step in range(1, steps + 1):
        ratio = step / steps
        point = (
            start[0] + (end[0] - start[0]) * ratio,
            start[1] + (end[1] - start[1]) * ratio,
        )
        drag = CGEventCreateMouseEvent(None, kCGEventLeftMouseDragged, point, kCGMouseButtonLeft)
        CGEventPost(kCGHIDEventTap, drag)
        time.sleep(0.02)
    up = CGEventCreateMouseEvent(None, kCGEventLeftMouseUp, end, kCGMouseButtonLeft)
    CGEventPost(kCGHIDEventTap, up)


def prepare_scroll_target(title: str) -> tuple[float, float] | None:
    if not focus_wechat_window(title):
        print(f"未找到标题包含「{title}」的微信窗口，请确认亲小禾小程序已打开。", file=sys.stderr)
        return None

    center = find_window_center(title)
    if center is None:
        return None

    x, y = center
    # Aim slightly below center where the feed list usually sits.
    list_y = y + 80
    move_mouse(x, list_y)
    time.sleep(0.1)
    click_at(x, list_y)
    time.sleep(0.15)
    return x, list_y


def perform_scroll_step(x: float, y: float, *, scroll_pixels_amount: int) -> None:
    move_mouse(x, y)
    time.sleep(0.05)
    press_page_down()
    time.sleep(0.1)
    scroll_pixels(scroll_pixels_amount)
    time.sleep(0.08)
    scroll_pixels(scroll_pixels_amount)
    time.sleep(0.08)
    press_arrow_down(times=4)
    time.sleep(0.08)
    drag_scroll_up(x, y, distance=220)


def countdown(seconds: int) -> None:
    for remaining in range(seconds, 0, -1):
        print(f"  {remaining}s 后自动开始滚动…", end="\r", flush=True)
        time.sleep(1)
    print(" " * 40, end="\r")


def run_browse(
    *,
    scroll_count: int,
    scroll_pixels_amount: int,
    pause: float,
    until_pages: int | None,
    prep_seconds: int,
    stagnant_limit: int,
    window_title: str,
    use_ai: bool = False,
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
    print("  4. 保持亲小禾窗口可见（脚本会自动点击列表区域）")
    print()
    if total_posts:
        print(f"当前缓存：已加载 {len(pages)} 页 API，共 {total_posts} 条动态，约 {photo_urls} 个图片 URL")
        if target_pages:
            print(f"目标：加载全部 {target_pages} 页 API 数据")
    if use_ai:
        print("AI 视觉模式：已开启（截图分析判断是否到底 / 滚动是否生效）")
    print()
    countdown(prep_seconds)

    scroll_point = prepare_scroll_target(window_title)
    if scroll_point is None:
        print("无法定位亲小禾窗口，请确认小程序已打开且窗口标题包含「亲小禾」。", file=sys.stderr)
        return 1

    x, y = scroll_point
    print(f"已定位窗口，滚动焦点：({int(x)}, {int(y)})")
    print()

    last_pages = len(pages)
    last_urls = photo_urls
    stagnant_rounds = 0
    ai_ineffective_rounds = 0
    current_pixels = scroll_pixels_amount

    for i in range(1, scroll_count + 1):
        prepare_scroll_target(window_title)
        perform_scroll_step(x, y, scroll_pixels_amount=current_pixels)
        time.sleep(pause)

        if use_ai:
            try:
                from vision import analyze_feed_screenshot, capture_window_screenshot

                screenshot = capture_window_screenshot(window_title)
                if screenshot is not None:
                    result = analyze_feed_screenshot(screenshot)
                    print(
                        f"  AI: 到底={result.at_bottom} 有效={result.scroll_effective} "
                        f"置信度={result.confidence:.2f} | {result.reason}",
                        flush=True,
                    )
                    if result.at_bottom and result.confidence >= 0.6:
                        print("\nAI 判断已滑到列表底部。")
                        break
                    if not result.scroll_effective:
                        ai_ineffective_rounds += 1
                        current_pixels = min(current_pixels + 120, 1200)
                        if ai_ineffective_rounds >= 3:
                            print("  AI: 滚动似乎无效，加大滚动幅度并重新点击列表区域")
                            scroll_point = prepare_scroll_target(window_title)
                            if scroll_point is not None:
                                x, y = scroll_point
                            ai_ineffective_rounds = 0
                    else:
                        ai_ineffective_rounds = 0
            except Exception as exc:
                print(f"  AI 分析跳过: {exc}", flush=True)

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

        if len(pages) == last_pages and photo_urls == last_urls:
            stagnant_rounds += 1
            if stagnant_rounds >= stagnant_limit:
                if target_pages and len(pages) < target_pages:
                    print(
                        f"\n连续 {stagnant_limit} 轮没有新数据，但只加载了 {len(pages)}/{target_pages} 页。"
                        "\n可能滚动仍未生效，请确认亲小禾动态列表在屏幕上可见，然后重试："
                        "\n  python3 browse.py --scrolls 200 --stagnant-limit 40"
                    )
                else:
                    print("\n已连续多轮没有新数据，可能已滑到列表底部。")
                break
        else:
            stagnant_rounds = 0
            last_pages = len(pages)
            last_urls = photo_urls

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
        default=200,
        help="最大滚动次数（默认 200）",
    )
    parser.add_argument(
        "--pixels",
        type=int,
        default=500,
        help="每次滚动的像素量（默认 500）",
    )
    parser.add_argument(
        "--pause",
        type=float,
        default=1.5,
        help="每次滚动后的等待秒数（默认 1.5）",
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
        default=5,
        help="开始滚动前的准备时间秒数（默认 5）",
    )
    parser.add_argument(
        "--stagnant-limit",
        type=int,
        default=30,
        help="连续多少轮无新数据后停止（默认 30）",
    )
    parser.add_argument(
        "--window-title",
        type=str,
        default=DEFAULT_WINDOW_TITLE,
        help="微信窗口标题关键字（默认：亲小禾）",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="只查看当前缓存进度，不滚动",
    )
    parser.add_argument(
        "--ai",
        action="store_true",
        help="启用 AI 视觉模式（需 OPENAI_API_KEY，用截图判断是否到底）",
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
        scroll_pixels_amount=args.pixels,
        pause=args.pause,
        until_pages=args.until_pages,
        prep_seconds=args.prep,
        stagnant_limit=args.stagnant_limit,
        window_title=args.window_title,
        use_ai=args.ai,
    )


if __name__ == "__main__":
    raise SystemExit(main())
