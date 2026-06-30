#!/usr/bin/env python3
"""Sync 亲小禾 group chat messages and teacher share cards from Mac WeChat cache."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import unquote, urlparse

from sync import (
    APP_NAME,
    WECHAT_ROOT,
    download_photo,
    ext_from_url,
    filename_from_url,
    log,
    normalize_media_url,
    sha256_bytes,
    unique_output_path,
)

MESSAGES_DIR_NAME = "messages"
SHARES_DIR_NAME = "shares"
STATE_SAVE_EVERY = 20

TIM_TEXT_RE = re.compile(
    r'\{[^{}]*"type"\s*:\s*"TIMTextElem"[^{}]*"payload"\s*:\s*\{[^{}]*"text"\s*:\s*"([^"\\]*(?:\\.[^"\\]*)*)"',
)
TIM_IMAGE_RE = re.compile(
    r'\{[^{}]*"type"\s*:\s*"TIMImageElem"[^{}]*"payload"\s*:\s*\{[^{}]*"imageInfoArray"\s*:\s*\[[^\]]*"url"\s*:\s*"(https?://[^"]+)"',
)
TIM_CUSTOM_RE = re.compile(
    r'\{[^{}]*"type"\s*:\s*"TIMCustomElem"[^{}]*"payload"\s*:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})',
)
TIM_MESSAGE_BLOCK_RE = re.compile(
    r'\{"ID":"[^"]+","type":"TIM(?:Text|Image|Custom|Sound|File|Video)Elem"[^}]*(?:\{[^}]*\}[^}]*)*\}',
)
API_SUCCESS_RE = re.compile(r'\{"success":true,"code":10000,"message":"成功","data":')
TEACHER_NAME_RE = re.compile(r"老师|保健医|大夫")


@dataclass
class ChatMessage:
    message_id: str
    kind: str
    sender: str
    time: str | int | None
    text: str = ""
    images: list[str] = field(default_factory=list)
    share: dict[str, Any] | None = None
    source: str = ""
    raw: dict[str, Any] | None = None

    def matches_filter(self, *, teacher_only: bool, keyword: str | None) -> bool:
        if teacher_only and not TEACHER_NAME_RE.search(self.sender):
            if not (self.share and TEACHER_NAME_RE.search(str(self.share.get("accountName", "")))):
                return False
        if keyword:
            haystack = " ".join(
                part
                for part in (
                    self.text,
                    self.sender,
                    json.dumps(self.share or {}, ensure_ascii=False),
                )
                if part
            )
            if keyword not in haystack:
                return False
        return True


def discover_cache_dirs() -> list[Path]:
    web_profiles = WECHAT_ROOT / "web" / "profiles"
    if not web_profiles.is_dir():
        return []
    return sorted(p for p in web_profiles.glob("webview_*/Cache/Cache_Data") if p.is_dir())


def discover_indexeddb_dirs() -> list[Path]:
    web_profiles = WECHAT_ROOT / "web" / "profiles"
    if not web_profiles.is_dir():
        return []
    dirs: list[Path] = []
    for profile in web_profiles.glob("webview_*"):
        indexeddb = profile / "IndexedDB" / "https_servicewechat.com_0.indexeddb.leveldb"
        if indexeddb.is_dir():
            dirs.append(indexeddb)
    return dirs


def decode_json_string(value: str) -> str:
    try:
        return json.loads(f'"{value}"')
    except json.JSONDecodeError:
        return value.replace("\\n", "\n").replace('\\"', '"')


def load_json_object(chunk: str) -> dict[str, Any] | None:
    depth = 0
    end: int | None = None
    for index, char in enumerate(chunk):
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                end = index + 1
                break
    if end is None or end < 20:
        return None
    try:
        obj = json.loads(chunk[:end])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def iter_api_objects(cache_dirs: list[Path], api_marker: str) -> Iterator[tuple[str, dict[str, Any]]]:
    for cache_dir in cache_dirs:
        for entry in cache_dir.iterdir():
            if not entry.is_file() or entry.name in {"index", "the-real-index"}:
                continue
            try:
                text = entry.read_bytes().decode("utf-8", errors="ignore")
            except OSError:
                continue
            if api_marker not in text:
                continue
            for match in API_SUCCESS_RE.finditer(text):
                obj = load_json_object(text[match.start() : match.start() + 200_000])
                if obj is not None:
                    yield f"{api_marker}:{entry.name}", obj


def extract_im_member_map(cache_dirs: list[Path]) -> dict[str, dict[str, Any]]:
    members: dict[str, dict[str, Any]] = {}
    for source, obj in iter_api_objects(cache_dirs, "im/group/"):
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        member_list = data.get("groupMemberList")
        if not isinstance(member_list, list):
            continue
        for member in member_list:
            if not isinstance(member, dict):
                continue
            user_id = member.get("imUserId")
            if isinstance(user_id, str) and user_id:
                members[user_id] = member
    return members


def resolve_sender(raw: dict[str, Any], member_map: dict[str, dict[str, Any]]) -> str:
    for key in ("nick", "name", "from", "sender", "accountName"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    user_id = raw.get("from") or raw.get("userID") or raw.get("senderID")
    if isinstance(user_id, str) and user_id in member_map:
        return str(member_map[user_id].get("imName") or user_id)
    return str(user_id or "未知发送者")


def parse_tim_payload_text(payload: dict[str, Any]) -> str:
    text = payload.get("text")
    if isinstance(text, str):
        return text
    data = payload.get("data")
    if isinstance(data, str):
        try:
            nested = json.loads(data)
        except json.JSONDecodeError:
            return data
        if isinstance(nested, dict):
            for key in ("text", "title", "content", "description", "shareTitle"):
                value = nested.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
            return json.dumps(nested, ensure_ascii=False)
    description = payload.get("description")
    if isinstance(description, str):
        return description
    extension = payload.get("extension")
    if isinstance(extension, str):
        return extension
    return ""


def parse_tim_custom_share(payload: dict[str, Any]) -> dict[str, Any] | None:
    candidates: list[Any] = [payload]
    data = payload.get("data")
    if isinstance(data, str):
        try:
            candidates.append(json.loads(data))
        except json.JSONDecodeError:
            pass
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in ("publishObjectId", "shareSourceId", "sourceId", "objectId"):
            value = candidate.get(key)
            if isinstance(value, str) and value:
                return candidate
    return None


def message_from_tim_dict(raw: dict[str, Any], member_map: dict[str, dict[str, Any]], source: str) -> ChatMessage | None:
    message_id = raw.get("ID") or raw.get("id") or raw.get("msgID")
    if not isinstance(message_id, str) or not message_id:
        return None

    elem_type = str(raw.get("type") or "")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}

    sender = resolve_sender(raw, member_map)
    timestamp = raw.get("time") or raw.get("timestamp") or raw.get("clientTime")
    text = ""
    images: list[str] = []
    share: dict[str, Any] | None = None
    kind = "message"

    if "TIMTextElem" in elem_type:
        kind = "text"
        text = parse_tim_payload_text(payload)
    elif "TIMImageElem" in elem_type:
        kind = "image"
        image_info = payload.get("imageInfoArray")
        if isinstance(image_info, list):
            for item in image_info:
                if isinstance(item, dict):
                    url = item.get("url") or item.get("imageUrl")
                    if isinstance(url, str):
                        images.append(normalize_media_url(url))
        text = "[图片]"
    elif "TIMCustomElem" in elem_type:
        kind = "custom"
        text = parse_tim_payload_text(payload)
        share = parse_tim_custom_share(payload)
        if share is not None:
            kind = "share"
    else:
        text = parse_tim_payload_text(payload) or json.dumps(raw, ensure_ascii=False)[:500]

    return ChatMessage(
        message_id=message_id,
        kind=kind,
        sender=sender,
        time=timestamp,
        text=text,
        images=images,
        share=share,
        source=source,
        raw=raw,
    )


def extract_tim_messages_from_text(
    text: str,
    *,
    member_map: dict[str, dict[str, Any]],
    source: str,
) -> list[ChatMessage]:
    found: dict[str, ChatMessage] = {}

    for match in TIM_MESSAGE_BLOCK_RE.finditer(text):
        raw = load_json_object(match.group(0))
        if raw is None:
            continue
        message = message_from_tim_dict(raw, member_map, source)
        if message is not None:
            found[message.message_id] = message

    for match in TIM_TEXT_RE.finditer(text):
        digest = sha256_bytes(match.group(0).encode("utf-8"))[:16]
        message_id = f"text-{digest}"
        if message_id in found:
            continue
        found[message_id] = ChatMessage(
            message_id=message_id,
            kind="text",
            sender="",
            time=None,
            text=decode_json_string(match.group(1)),
            source=source,
        )

    for match in TIM_IMAGE_RE.finditer(text):
        digest = sha256_bytes(match.group(0).encode("utf-8"))[:16]
        message_id = f"image-{digest}"
        if message_id in found:
            continue
        found[message_id] = ChatMessage(
            message_id=message_id,
            kind="image",
            sender="",
            time=None,
            text="[图片]",
            images=[normalize_media_url(match.group(1))],
            source=source,
        )

    for match in TIM_CUSTOM_RE.finditer(text):
        payload = load_json_object(match.group(1))
        if payload is None:
            continue
        digest = sha256_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"))[:16]
        message_id = f"custom-{digest}"
        if message_id in found:
            continue
        share = parse_tim_custom_share(payload)
        found[message_id] = ChatMessage(
            message_id=message_id,
            kind="share" if share else "custom",
            sender="",
            time=None,
            text=parse_tim_payload_text(payload),
            share=share,
            source=source,
            raw={"payload": payload},
        )

    return list(found.values())


def extract_tim_messages(
    cache_dirs: list[Path],
    indexeddb_dirs: list[Path],
    member_map: dict[str, dict[str, Any]],
) -> list[ChatMessage]:
    found: dict[str, ChatMessage] = {}

    scan_paths: list[tuple[Path, str]] = []
    for cache_dir in cache_dirs:
        for entry in cache_dir.iterdir():
            if entry.is_file() and entry.name not in {"index", "the-real-index"}:
                scan_paths.append((entry, f"cache:{entry.name}"))
    for ldb_dir in indexeddb_dirs:
        for entry in ldb_dir.glob("*.ldb"):
            scan_paths.append((entry, f"indexeddb:{entry.name}"))
        for entry in ldb_dir.glob("*.log"):
            if entry.stat().st_size > 0:
                scan_paths.append((entry, f"indexeddb-log:{entry.name}"))

    for path, source in scan_paths:
        try:
            text = path.read_bytes().decode("utf-8", errors="ignore")
        except OSError:
            continue
        if "TIM" not in text and "msgID" not in text and "conversationID" not in text:
            continue
        for message in extract_tim_messages_from_text(text, member_map=member_map, source=source):
            found.setdefault(message.message_id, message)

    return list(found.values())


def share_from_album_detail(data: dict[str, Any], source: str) -> ChatMessage | None:
    publish_object_id = data.get("publishObjectId")
    if not isinstance(publish_object_id, str) or not publish_object_id:
        return None

    content_list = data.get("contentList")
    text_parts: list[str] = []
    images: list[str] = []
    if isinstance(content_list, list):
        for item in content_list:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("contentType") or "")
            content = item.get("content")
            if not isinstance(content, str):
                continue
            if content_type == "txt":
                text_parts.append(content)
            elif content_type in {"img", "image"} and content.startswith("http"):
                images.append(normalize_media_url(content))

    return ChatMessage(
        message_id=f"share-{publish_object_id}",
        kind="share",
        sender=str(data.get("accountName") or ""),
        time=data.get("publishTime"),
        text="\n".join(text_parts),
        images=images,
        share=data,
        source=source,
    )


def extract_share_cards(cache_dirs: list[Path]) -> list[ChatMessage]:
    found: dict[str, ChatMessage] = {}

    def upsert(message: ChatMessage) -> None:
        existing = found.get(message.message_id)
        if existing is None:
            found[message.message_id] = message
            return
        if not existing.sender and message.sender:
            existing.sender = message.sender
        if not existing.text and message.text:
            existing.text = message.text
        if not existing.images and message.images:
            existing.images = message.images
        if existing.share is None and message.share is not None:
            existing.share = message.share
        elif isinstance(existing.share, dict) and isinstance(message.share, dict):
            if len(message.share.get("contentList") or []) > len(existing.share.get("contentList") or []):
                existing.share = message.share
        if existing.time is None and message.time is not None:
            existing.time = message.time

    for source, obj in iter_api_objects(cache_dirs, "albumContent/detail"):
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        message = share_from_album_detail(data, source)
        if message is not None:
            upsert(message)

    for source, obj in iter_api_objects(cache_dirs, "share/page"):
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        items = data.get("list")
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            publish_object_id = (
                item.get("publishObjectId")
                or item.get("shareSourceId")
                or item.get("sourceId")
            )
            if not isinstance(publish_object_id, str) or not publish_object_id:
                continue
            message = share_from_album_detail(item, source)
            if message is None:
                message = ChatMessage(
                    message_id=f"share-{publish_object_id}",
                    kind="share",
                    sender=str(item.get("accountName") or item.get("publishUserName") or ""),
                    time=item.get("publishTime") or item.get("recordTime"),
                    text=str(item.get("content") or item.get("title") or ""),
                    share=item,
                    source=source,
                )
            upsert(message)

    for source, obj in iter_api_objects(cache_dirs, "growFile/parentPick"):
        data = obj.get("data")
        if not isinstance(data, dict):
            continue
        source_id = data.get("sourceId")
        if not isinstance(source_id, str) or not source_id:
            continue
        images: list[str] = []
        text = str(data.get("content") or data.get("title") or "")
        content_list = data.get("contentList")
        if isinstance(content_list, list):
            for item in content_list:
                if not isinstance(item, dict):
                    continue
                if str(item.get("contentType")) in {"img", "image"}:
                    content = item.get("content")
                    if isinstance(content, str) and content.startswith("http"):
                        images.append(normalize_media_url(content))
        upsert(
            ChatMessage(
                message_id=f"share-{source_id}",
                kind="share",
                sender=str(data.get("publishManName") or ""),
                time=data.get("recordTime"),
                text=text,
                images=images,
                share=data,
                source=source,
            )
        )

    return list(found.values())


def merge_messages(*groups: list[ChatMessage]) -> list[ChatMessage]:
    merged: dict[str, ChatMessage] = {}
    for group in groups:
        for message in group:
            existing = merged.get(message.message_id)
            if existing is None:
                merged[message.message_id] = message
                continue
            if not existing.sender and message.sender:
                existing.sender = message.sender
            if not existing.text and message.text:
                existing.text = message.text
            if not existing.images and message.images:
                existing.images = message.images
            if existing.share is None and message.share is not None:
                existing.share = message.share
            if existing.time is None and message.time is not None:
                existing.time = message.time
    return list(merged.values())


def load_chat_state(state_path: Path) -> dict[str, Any]:
    if not state_path.exists():
        return {"version": 1, "messages": {}, "images": {}}
    try:
        state = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "messages": {}, "images": {}}
    state.setdefault("messages", {})
    state.setdefault("images", {})
    return state


def save_chat_state(state_path: Path, state: dict[str, Any]) -> None:
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def format_message_markdown(message: ChatMessage) -> str:
    lines = [
        f"## {message.sender or '未知发送者'}",
        "",
    ]
    if message.time is not None:
        lines.append(f"- 时间: {message.time}")
    lines.append(f"- 类型: {message.kind}")
    if message.text:
        lines.append("")
        lines.append(message.text)
    if message.images:
        lines.append("")
        lines.append("图片:")
        for url in message.images:
            lines.append(f"- {url}")
    if message.share:
        share_title = message.share.get("title") or message.share.get("shareTitle")
        if isinstance(share_title, str) and share_title.strip():
            lines.append("")
            lines.append(f"分享标题: {share_title}")
    lines.append("")
    return "\n".join(lines)


def download_message_images(
    message: ChatMessage,
    images_dir: Path,
    images_state: dict[str, Any],
    *,
    dry_run: bool,
) -> int:
    saved = 0
    for index, url in enumerate(message.images, start=1):
        if not url.startswith("http"):
            continue
        if url in images_state and not dry_run:
            continue
        ext = ext_from_url(url)
        publish_time = str(message.time) if message.time is not None else None
        filename = filename_from_url(url, ext, publish_time)
        if dry_run:
            log(f"  [dry-run] 图片: {filename}")
            saved += 1
            continue
        downloaded = download_photo(url)
        if downloaded is None:
            log(f"  └─ 图片下载失败: {url}")
            continue
        data, actual_ext = downloaded
        if not filename.lower().endswith(f".{actual_ext}"):
            filename = f"{Path(filename).stem}.{actual_ext}"
        target = unique_output_path(images_dir, filename, sha256_bytes(data))
        target.write_bytes(data)
        images_state[url] = {
            "filename": target.name,
            "sha256": sha256_bytes(data),
            "message_id": message.message_id,
            "publish_time": publish_time,
        }
        saved += 1
    return saved


def export_messages(
    messages: list[ChatMessage],
    output_dir: Path,
    *,
    dry_run: bool,
) -> None:
    messages_dir = output_dir / MESSAGES_DIR_NAME
    if dry_run:
        log(f"[dry-run] 将写入 {messages_dir}/messages.json 与 messages.md")
        return

    messages_dir.mkdir(parents=True, exist_ok=True)
    serializable = []
    for message in sorted(messages, key=lambda item: str(item.time or "")):
        serializable.append(
            {
                "id": message.message_id,
                "kind": message.kind,
                "sender": message.sender,
                "time": message.time,
                "text": message.text,
                "images": message.images,
                "share": message.share,
                "source": message.source,
            }
        )
    (messages_dir / "messages.json").write_text(
        json.dumps(serializable, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    markdown_lines = [f"# {APP_NAME} 群聊消息", ""]
    for message in sorted(messages, key=lambda item: str(item.time or "")):
        markdown_lines.append(format_message_markdown(message))
    (messages_dir / "messages.md").write_text("\n".join(markdown_lines), encoding="utf-8")


def scan_chat_cache() -> dict[str, int]:
    cache_dirs = discover_cache_dirs()
    indexeddb_dirs = discover_indexeddb_dirs()
    member_map = extract_im_member_map(cache_dirs)
    tim_messages = extract_tim_messages(cache_dirs, indexeddb_dirs, member_map)
    share_cards = extract_share_cards(cache_dirs)
    messages = merge_messages(tim_messages, share_cards)
    teacher_messages = [m for m in messages if TEACHER_NAME_RE.search(m.sender or "")]
    weekly_messages = [m for m in messages if "本周" in (m.text or "") or "分享" in (m.text or "")]
    return {
        "tim_messages": len(tim_messages),
        "share_cards": len(share_cards),
        "total_messages": len(messages),
        "teacher_messages": len(teacher_messages),
        "weekly_keyword_messages": len(weekly_messages),
        "im_members": len(member_map),
    }


def sync_chat_messages(
    output_dir: Path,
    state_path: Path,
    *,
    dry_run: bool = False,
    teacher_only: bool = False,
    keyword: str | None = None,
    download_images: bool = True,
) -> tuple[int, int, int]:
    cache_dirs = discover_cache_dirs()
    indexeddb_dirs = discover_indexeddb_dirs()
    if not cache_dirs and not indexeddb_dirs:
        log("未找到 Mac 微信缓存目录，请确认已用 Mac 版微信打开过亲小禾。")
        return 0, 0, 0

    member_map = extract_im_member_map(cache_dirs)
    tim_messages = extract_tim_messages(cache_dirs, indexeddb_dirs, member_map)
    share_cards = extract_share_cards(cache_dirs)
    messages = merge_messages(tim_messages, share_cards)

    if teacher_only or keyword:
        messages = [
            message
            for message in messages
            if message.matches_filter(teacher_only=teacher_only, keyword=keyword)
        ]

    if not messages:
        log("未发现可导出的群聊消息或老师分享卡片。")
        log(
            "请先在亲小禾打开「消息」→ 班级群聊，向上滚动加载历史记录；"
            "老师发的「本周分享」卡片可点开一次，再运行："
            "\n  python3 scripts/qinxiaohe-photo-sync/browse_chat.py"
            "\n  python3 scripts/qinxiaohe-photo-sync/sync.py --chat"
        )
        return 0, 0, 0

    log(
        f"发现 {len(messages)} 条消息"
        f"（TIM {len(tim_messages)}，分享卡片 {len(share_cards)}，群成员映射 {len(member_map)}）"
    )

    state = load_chat_state(state_path)
    messages_state: dict[str, Any] = state.setdefault("messages", {})
    images_state: dict[str, Any] = state.setdefault("images", {})

    new_count = 0
    skipped = 0
    images_saved = 0
    images_dir = output_dir / MESSAGES_DIR_NAME / SHARES_DIR_NAME

    for index, message in enumerate(sorted(messages, key=lambda item: str(item.time or "")), start=1):
        if message.message_id in messages_state:
            log(f"[{index}/{len(messages)}] 跳过已同步: {message.sender or message.kind}")
            skipped += 1
            continue

        preview = (message.text or message.kind).replace("\n", " ")[:60]
        log(f"[{index}/{len(messages)}] 保存消息: {message.sender or '未知'} | {preview}")

        if not dry_run:
            messages_state[message.message_id] = {
                "sender": message.sender,
                "kind": message.kind,
                "time": message.time,
                "text": message.text,
                "image_count": len(message.images),
                "source": message.source,
            }

        if download_images and message.images:
            images_saved += download_message_images(
                message,
                images_dir,
                images_state,
                dry_run=dry_run,
            )

        new_count += 1
        if not dry_run and new_count % STATE_SAVE_EVERY == 0:
            save_chat_state(state_path, state)

    export_messages(messages, output_dir, dry_run=dry_run)

    if not dry_run and new_count:
        state["last_sync_at"] = datetime.now(timezone.utc).isoformat()
        save_chat_state(state_path, state)

    log(
        f"消息阶段：新增 {new_count}，跳过 {skipped}，图片 {images_saved} 张；"
        f"输出目录 {output_dir / MESSAGES_DIR_NAME}"
    )
    return new_count, skipped, len(messages)


def build_parser() -> argparse.ArgumentParser:
    default_output = Path.home() / "Pictures" / APP_NAME
    default_state = default_output / MESSAGES_DIR_NAME / ".chat-state.json"

    parser = argparse.ArgumentParser(
        description=f"从 Mac 微信缓存同步{APP_NAME}群聊消息与老师分享卡片",
    )
    parser.add_argument("--output", type=Path, default=default_output)
    parser.add_argument("--state", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--teacher-only", action="store_true", help="只保留老师/保健医消息")
    parser.add_argument(
        "--keyword",
        type=str,
        default=None,
        help="只保留包含关键字的分享（例如：本周分享）",
    )
    parser.add_argument("--no-images", action="store_true", help="不下载消息图片")
    parser.add_argument("--status", action="store_true", help="查看当前缓存中的消息数量")
    return parser


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(line_buffering=True)

    args = build_parser().parse_args()
    output_dir = args.output.expanduser()
    state_path = (args.state or output_dir / MESSAGES_DIR_NAME / ".chat-state.json").expanduser()

    if args.status:
        stats = scan_chat_cache()
        print("群聊缓存扫描结果:")
        for key, value in stats.items():
            print(f"  {key}: {value}")
        return 0

    sync_chat_messages(
        output_dir,
        state_path,
        dry_run=args.dry_run,
        teacher_only=args.teacher_only,
        keyword=args.keyword,
        download_images=not args.no_images,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
