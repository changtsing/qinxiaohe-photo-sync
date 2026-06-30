#!/usr/bin/env python3
"""Vision helpers for AI-guided 亲小禾 feed browsing."""

from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

FEED_ANALYSIS_PROMPT = """你是 Mac 微信「亲小禾」小程序「成长空间/班级动态」列表界面的分析助手。
请根据截图判断滚动状态，只返回 JSON，不要其他文字：

{
  "at_bottom": true/false,
  "scroll_effective": true/false,
  "visible_posts_estimate": 整数,
  "confidence": 0.0-1.0,
  "reason": "简短中文说明"
}

判断标准：
- at_bottom=true：列表已到底，常见「没有更多了」「暂无数据」、或连续重复内容且无法继续下滑
- scroll_effective=false：界面与上一轮相比几乎没变化（仍在同一位置）
- 若看到老师发布的图文动态卡片、照片网格，说明在正确页面
"""


@dataclass(frozen=True)
class FeedVisionResult:
    at_bottom: bool
    scroll_effective: bool
    visible_posts_estimate: int
    confidence: float
    reason: str
    raw: str


def get_window_id(title: str = "亲小禾") -> int | None:
    safe_title = title.replace('"', '\\"')
    script = f'''
tell application "System Events"
    tell process "WeChat"
        repeat with w in windows
            if name of w contains "{safe_title}" then
                return id of w
            end if
        end repeat
    end tell
end tell
'''
    result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
    if result.returncode != 0 or not result.stdout.strip().isdigit():
        return None
    return int(result.stdout.strip())


def capture_window_screenshot(title: str = "亲小禾") -> Path | None:
    window_id = get_window_id(title)
    if window_id is None:
        return None

    output = Path(tempfile.gettempdir()) / f"qinxiaohe-feed-{window_id}.png"
    result = subprocess.run(
        ["screencapture", "-x", f"-l{window_id}", str(output)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0 or not output.exists() or output.stat().st_size < 1024:
        return None
    return output


def _parse_json_response(text: str) -> dict:
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise ValueError(f"模型未返回 JSON: {text[:200]}")
        return json.loads(match.group(0))


def analyze_with_openai(image_path: Path, *, api_key: str, model: str) -> FeedVisionResult:
    image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "model": model,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": FEED_ANALYSIS_PROMPT},
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/png;base64,{image_b64}"},
                    },
                ],
            }
        ],
        "max_tokens": 300,
        "temperature": 0,
    }
    request = urllib.request.Request(
        "https://api.openai.com/v1/chat/completions",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="ignore")
        raise RuntimeError(f"OpenAI API 错误 ({exc.code}): {detail[:300]}") from exc

    content = body["choices"][0]["message"]["content"]
    parsed = _parse_json_response(content)
    return FeedVisionResult(
        at_bottom=bool(parsed.get("at_bottom", False)),
        scroll_effective=bool(parsed.get("scroll_effective", True)),
        visible_posts_estimate=int(parsed.get("visible_posts_estimate", 0) or 0),
        confidence=float(parsed.get("confidence", 0.5) or 0.5),
        reason=str(parsed.get("reason", "")),
        raw=content,
    )


def load_dotenv() -> None:
    env_path = Path(__file__).with_name(".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        os.environ.setdefault(key, value)


def analyze_feed_screenshot(
    image_path: Path,
    *,
    provider: str | None = None,
    model: str | None = None,
) -> FeedVisionResult:
    selected = (provider or os.environ.get("VISION_PROVIDER", "openai")).lower()
    if selected != "openai":
        raise ValueError(f"暂不支持的视觉提供方: {selected}（目前仅支持 openai）")

    load_dotenv()
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("请设置环境变量 OPENAI_API_KEY")

    selected_model = model or os.environ.get("VISION_MODEL", "gpt-4o-mini")
    return analyze_with_openai(image_path, api_key=api_key, model=selected_model)
