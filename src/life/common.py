"""日常生活与主动话题共用的 Host 调用：读聊天记录、调 LLM、解析 JSON、读写状态文件。"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import re

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


async def list_group_streams(ctx: Any, platform: str) -> List[Dict[str, Any]]:
    """返回该平台的群聊会话（Discord 频道 / 子区）；读取失败时记日志并返回空列表。"""
    streams = await ctx.chat.get_group_streams(platform=platform)
    if isinstance(streams, dict):
        streams = streams.get("streams") if streams.get("success") else None
    if not isinstance(streams, list):
        ctx.logger.warning(f"读取 {platform} 群聊会话失败，日常功能这次跳过聊天记录")
        return []
    return [stream for stream in streams if isinstance(stream, dict) and stream.get("stream_id")]


async def readable_chat(ctx: Any, stream_id: str, since: datetime, until: datetime, limit: int) -> str:
    """把一段时间内的聊天记录转成可读文本，没有消息或读取失败时返回空字符串。"""
    result = await ctx.message.build_readable(
        None,
        chat_id=stream_id,
        start_time=since.timestamp(),
        end_time=until.timestamp(),
        limit=limit,
        timestamp_mode="normal",
        truncate=True,
    )
    if not isinstance(result, dict) or not result.get("success"):
        return ""
    return str(result.get("text") or "").strip()


async def persona_text(ctx: Any) -> str:
    """当前人设的文字描述；读不到时返回空字符串，由调用方只靠配置里的生活设定。"""
    try:
        current = await ctx.personality.get_current()
    except Exception as exc:
        ctx.logger.debug(f"读取当前人设失败：{exc}")
        return ""
    if not isinstance(current, dict):
        return ""
    lines = [f"{key}: {value}" for key, value in current.items() if isinstance(value, str) and value.strip()]
    return "\n".join(lines)


async def generate_json(ctx: Any, prompt: str, model: str) -> Optional[Any]:
    """调 LLM 并把回复解析成 JSON；失败或解析不了时记日志返回 None。"""
    result = await ctx.llm.generate(prompt, model=model)
    if not isinstance(result, dict) or not result.get("success"):
        ctx.logger.warning(f"日常功能 LLM 调用失败：{(result or {}).get('error') if isinstance(result, dict) else result}")
        return None
    return parse_json(str(result.get("response") or ""), ctx.logger)


def parse_json(text: str, logger: Any = None) -> Optional[Any]:
    """从模型回复里取出 JSON：优先 ```json 代码块，否则取第一个 { 或 [ 到最后一个 } 或 ]。"""
    match = _JSON_BLOCK.search(text)
    candidate = match.group(1) if match else text
    starts = [index for index in (candidate.find("{"), candidate.find("[")) if index >= 0]
    if starts:
        start = min(starts)
        end = max(candidate.rfind("}"), candidate.rfind("]"))
        candidate = candidate[start : end + 1]
    try:
        return json.loads(candidate)
    except ValueError:
        if logger is not None:
            logger.warning(f"日常功能无法解析模型回复的 JSON：{text[:200]!r}")
        return None


def load_state(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def save_state(path: Path, data: Dict[str, Any]) -> None:
    # 先写临时文件再替换，写到一半崩溃也不会损坏已有状态
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)
