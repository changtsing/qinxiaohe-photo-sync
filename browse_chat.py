#!/usr/bin/env python3
"""Auto-scroll 亲小禾 group chat to load IM history and share cards into cache."""

from __future__ import annotations

import argparse
import sys
import time

from browse import (
    DEFAULT_WINDOW_TITLE,
    accessibility_trusted,
    click_at,
    countdown,
    find_window_center,
    focus_wechat_window,
    move_mouse,
    press_page_down,
)
from chat_sync import scan_chat_cache


def scroll_pixels_up(amount: int) -> None:
    from Quartz import (
        CGEventCreateScrollWheelEvent,
        CGEventPost,
        kCGHIDEventTap,
        kCGScrollEventUnitPixel,
    )

    event = CGEventCreateScrollWheelEvent(None, kCGScrollEventUnitPixel, 1, amount)
    CGEventPost(kCGHIDEventTap, event)


def press_page_up() -> None:
    from Quartz import CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap

    for pressed in (True, False):
        event = CGEventCreateKeyboardEvent(None, 116, pressed)
        CGEventPost(kCGHIDEventTap, event)


def press_arrow_up(times: int = 5) -> None:
    from Quartz import CGEventCreateKeyboardEvent, CGEventPost, kCGHIDEventTap

    for _ in range(times):
        for pressed in (True, False):
            event = CGEventCreateKeyboardEvent(None, 126, pressed)
            CGEventPost(kCGHIDEventTap, event)
        time.sleep(0.02)


def drag_scroll_down(x: float, y: float, distance: int = 260) -> None:
    """Drag downward inside chat to reveal older messages above."""
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
    end = (x, y + distance)
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


def prepare_chat_target(title: str = DEFAULT_WINDOW_TITLE) -> tuple[float, float] | None:
    if not focus_wechat_window(title):
        print(f"未找到标题包含「{title}」的微信窗口，请确认亲小禾小程序已打开。", file=sys.stderr)
        return None

    center = find_window_center(title)
    if center is None:
        return None

    x, y = center
    chat_y = y + 40
    move_mouse(x, chat_y)
    time.sleep(0.1)
    click_at(x, chat_y)
    time.sleep(0.15)
    return x, chat_y


def perform_chat_scroll_step(x: float, y: float, *, scroll_pixels_amount: int) -> None:
    move_mouse(x, y)
    time.sleep(0.05)
    press_page_up()
    time.sleep(0.1)
    scroll_pixels_up(scroll_pixels_amount)
    time.sleep(0.08)
    scroll_pixels_up(scroll_pixels_amount)
    time.sleep(0.08)
    press_arrow_up(times=4)
    time.sleep(0.08)
    drag_scroll_down(x, y, distance=220)


def run_browse_chat(
    *,
    scroll_count: int,
    scroll_pixels_amount: int,
    pause: float,
    prep_seconds: int,
    stagnant_limit: int,
    window_title: str,
) -> int:
    if not accessibility_trusted():
        print(
            "需要「辅助功能」权限才能自动控制滚动。\n"
            "请到 系统设置 → 隐私与安全性 → 辅助功能，"
            "为 Terminal（或 Cursor）开启权限后重试。",
            file=sys.stderr,
        )
        return 1

    stats = scan_chat_cache()
    print("亲小禾群聊自动浏览")
    print("=" * 40)
    print("请先手动完成以下步骤：")
    print("  1. 打开 Mac 版微信")
    print("  2. 进入「亲小禾」小程序")
    print("  3. 打开底部「消息」→ 进入班级群聊")
    print("  4. 保持聊天窗口可见（脚本会向上滚动加载更早的消息）")
    print()
    print(
        "当前缓存："
        f"TIM 消息 {stats['tim_messages']} 条，"
        f"分享卡片 {stats['share_cards']} 条，"
        f"群成员 {stats['im_members']} 人"
    )
    print()
    countdown(prep_seconds)

    scroll_point = prepare_chat_target(window_title)
    if scroll_point is None:
        print("无法定位亲小禾窗口，请确认小程序已打开且窗口标题包含「亲小禾」。", file=sys.stderr)
        return 1

    x, y = scroll_point
    print(f"已定位窗口，聊天滚动焦点：({int(x)}, {int(y)})")
    print()

    last_total = stats["total_messages"]
    stagnant_rounds = 0
    current_pixels = scroll_pixels_amount

    for i in range(1, scroll_count + 1):
        prepare_chat_target(window_title)
        perform_chat_scroll_step(x, y, scroll_pixels_amount=current_pixels)
        time.sleep(pause)

        stats = scan_chat_cache()
        print(
            f"滚动 {i}/{scroll_count} | "
            f"TIM {stats['tim_messages']} | "
            f"分享卡片 {stats['share_cards']} | "
            f"合计 {stats['total_messages']}",
            flush=True,
        )

        if stats["total_messages"] == last_total:
            stagnant_rounds += 1
            if stagnant_rounds >= stagnant_limit:
                print(f"\n连续 {stagnant_limit} 轮没有新消息，可能已加载完可见历史。")
                break
        else:
            stagnant_rounds = 0
            last_total = stats["total_messages"]
            current_pixels = scroll_pixels_amount

    stats = scan_chat_cache()
    print()
    print("浏览结束。接下来导出群聊消息：")
    print("  python3 scripts/qinxiaohe-photo-sync/sync.py --chat")
    print("或只看老师本周分享：")
    print("  python3 scripts/qinxiaohe-photo-sync/sync.py --chat --teacher-only --keyword 分享")
    print()
    print(
        f"最终缓存：TIM {stats['tim_messages']} 条，"
        f"分享卡片 {stats['share_cards']} 条，"
        f"合计 {stats['total_messages']} 条"
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="自动向上滚动亲小禾班级群聊，加载 IM 历史与老师分享卡片",
    )
    parser.add_argument("--scrolls", type=int, default=200, help="最大滚动次数（默认 200）")
    parser.add_argument("--pixels", type=int, default=500, help="每次向上滚动的像素量（默认 500）")
    parser.add_argument("--pause", type=float, default=1.5, help="每次滚动后的等待秒数（默认 1.5）")
    parser.add_argument("--prep", type=int, default=5, help="开始滚动前的准备时间秒数（默认 5）")
    parser.add_argument(
        "--stagnant-limit",
        type=int,
        default=30,
        help="连续多少轮无新消息后停止（默认 30）",
    )
    parser.add_argument("--window-title", type=str, default=DEFAULT_WINDOW_TITLE)
    parser.add_argument("--status", action="store_true", help="只查看当前群聊缓存进度")
    return parser


def main() -> int:
    args = build_parser().parse_args()

    if args.status:
        stats = scan_chat_cache()
        print("群聊缓存进度:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        return 0

    return run_browse_chat(
        scroll_count=args.scrolls,
        scroll_pixels_amount=args.pixels,
        pause=args.pause,
        prep_seconds=args.prep,
        stagnant_limit=args.stagnant_limit,
        window_title=args.window_title,
    )


if __name__ == "__main__":
    raise SystemExit(main())
