"""日常生活与主动话题共用的 Host 调用：读聊天记录、调 LLM、解析 JSON、读写状态文件。"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import json
import re
import time

_JSON_BLOCK = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


async def list_group_streams(ctx: Any, platform: str) -> List[Dict[str, Any]]:
    """返回该平台的群聊会话（Discord 频道 / 子区）；读取失败时记日志并返回空列表。"""
    streams = await ctx.chat.get_group_streams(platform=platform)
    if not isinstance(streams, list):  # 成功時 SDK 直接回清單，失敗時回 {"success": False, "error": ...}
        error = streams.get("error") if isinstance(streams, dict) else streams
        ctx.logger.warning(f"讀取 {platform} 群聊會話失敗，日常功能這次跳過聊天紀錄：{error}")
        return []
    return [stream for stream in streams if isinstance(stream, dict) and stream.get("stream_id")]


async def readable_chat(ctx: Any, stream_id: str, since: datetime, until: datetime, limit: int) -> str:
    """把一段時間內的聊天紀錄轉成可讀文字，沒有訊息或讀取失敗時回傳空字串。"""
    result = await ctx.message.build_readable(
        None,
        chat_id=stream_id,
        start_time=since.timestamp(),
        end_time=until.timestamp(),
        limit=limit,
        timestamp_mode="normal",
        truncate=True,
    )
    # 成功時 SDK 把 {"success", "text"} 拆成純文字（maibot_sdk.context._CAPABILITY_RESULT_KEYS），失敗時回原樣的 dict
    if isinstance(result, str):
        return result.strip()
    ctx.logger.warning(f"讀取聊天紀錄失敗：{result.get('error') if isinstance(result, dict) else result}")
    return ""


def short_time(iso: Any) -> str:
    """狀態檔裡的 ISO 時間 -> WebUI 顯示的「MM-DD HH:MM」；沒有或格式不對時顯示「—」。"""
    try:
        return datetime.fromisoformat(str(iso)).strftime("%m-%d %H:%M")
    except ValueError:
        return "—"


# MaiBot 目前人設裡描述角色的欄位；群聊／私聊提示詞是聊天規則，排生活時程用不到
PERSONA_FIELDS = {"personality": "人格", "behavior_style": "行為風格", "reply_style": "說話風格"}


async def persona_text(ctx: Any) -> str:
    """MaiBot 目前人設（personality.get_current）的角色描述；讀不到時記警告並回空字串，由呼叫方只靠生活設定。"""
    try:
        current = await ctx.personality.get_current()
    except Exception as exc:
        current = {"error": str(exc)}
    # 成功時 SDK 直接回人設欄位，失敗時回原樣的 {"success": False, "error": ...}
    if not isinstance(current, dict) or "error" in current or "success" in current:
        error = current.get("error") if isinstance(current, dict) else current
        ctx.logger.warning(f"讀取 MaiBot 目前人設失敗，日常生活時程只參考生活設定：{error}")
        return ""
    return "\n".join(
        f"{label}：{current[field].strip()}"
        for field, label in PERSONA_FIELDS.items()
        if isinstance(current.get(field), str) and current[field].strip()
    )


async def generate_json(ctx: Any, prompt: str, task_name: str) -> Optional[Any]:
    """用 Host 的模型任务（planner / replyer）调 LLM 并把回复解析成 JSON；失败或解析不了时记日志返回 None。"""
    result = await ctx.llm.generate(prompt, task_name=task_name)
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


def save_state(path: Path, data: Any) -> None:
    # 先寫暫存檔再替換，寫到一半當掉也不會弄壞原本的狀態
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    # ponytail: Windows 上防毒或索引程式剛好開著檔案時替換會被拒，最多同步等 0.2 秒再試
    for attempt in range(5):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 4:
                raise
            time.sleep(0.05)
