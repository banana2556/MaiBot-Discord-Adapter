"""决策模型（System One 格式）：Jev（TypeSafe）和 vLLM-SR Decision 都用同一个 ``POST /v1/systemone``。

只回每个选项的概率、不生成文字，适合「值不值得」这类快速判断。
请求 ``{"model", "state": 文字, "questions": {问题ID: {"type", "instructions", "criteria"}}}``，
回应 ``{"answers": {问题ID: {"type": "choice", "choice", "probabilities", "confidence"}}}``。
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Optional

import aiohttp

from .life.common import load_state, save_state

TIMEOUT_SECONDS = 2.5
# 统计页保留几天的每日计数、最近几次的明细
HISTORY_DAYS = 14
RECENT_LIMIT = 50
# 提醒后这么久内麦麦在同一个聊天按了表情或发了贴图，算她采纳了这次提醒
ADOPT_WINDOW = timedelta(minutes=3)
# reaction / sticker / skip（没过门槛）/ failed（请求失败，退回随机）是判断结果，adopted 是采纳次数
DAY_COUNTERS = ("calls", "reaction", "sticker", "skip", "failed", "adopted", "latency_ms")


async def ask(config: Any, state: str, questions: Dict[str, Any], logger: Any) -> Optional[Dict[str, Any]]:
    """问决策模型。

    Args:
        config: ``DecisionConfig``，用到 base_url、api_key、model。
        state: 问题针对的内容；vLLM-SR 只收字符串。
        questions: 问题 ID -> 问题定义。
        logger: 日志器。

    Returns:
        按问题 ID 的 answers；请求失败或回应格式不对时返回 None，调用方退回原本的做法。
    """
    body = {"model": config.model, "state": state, "questions": questions}
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)) as session:
            async with session.post(
                f"{config.base_url.strip().rstrip('/')}/v1/systemone",
                json=body,
                headers={"Authorization": f"Bearer {config.api_key.strip()}"},
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    logger.warning(f"决策模型返回 HTTP {response.status}：{data}")
                    return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(f"决策模型请求失败：{exc!r}")
        return None
    answers = data.get("answers") if isinstance(data, dict) else None
    return answers if isinstance(answers, dict) else None


class DecisionStats:
    """决策模型的判断纪录：每天的计数和最近几十次的明细，供「DC生活 → 決策模型」页面显示。

    ``state = {"days": {日期: {计数}}, "recent": [新的在前], "last_test": {...}}``；path 为 None 时只放内存。
    """

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self.state: Dict[str, Any] = load_state(path) if path else {}

    def record(
        self,
        now: datetime,
        chat: str,
        message: str,
        result: str,
        latency_ms: int,
        reaction: float = 0.0,
        sticker: float = 0.0,
    ) -> None:
        """记一次判断；失败的请求不算进平均延迟（多半是超时，会把平均拉歪）。"""
        day = self._day(now.date().isoformat())
        day["calls"] += 1
        day[result] += 1
        if result != "failed":
            day["latency_ms"] += latency_ms
        recent = self.state.setdefault("recent", [])
        recent.insert(
            0,
            {
                "time": now.isoformat(timespec="seconds"),
                "chat": chat,
                "message": message,
                "result": result,
                "reaction": round(reaction, 3),
                "sticker": round(sticker, 3),
                "latency_ms": latency_ms,
                "adopted": "",
            },
        )
        del recent[RECENT_LIMIT:]
        self.state["days"] = dict(sorted(self.state["days"].items())[-HISTORY_DAYS:])
        self._save()

    def adopt(self, now: datetime, chat: str, form: str) -> None:
        """麦麦用了 discord_emote：这个聊天最近一次判断如果是刚提醒过的，记为采纳。"""
        for item in self.state.get("recent", []):
            if item["chat"] != chat:
                continue
            when = datetime.fromisoformat(item["time"])
            if item["result"] in ("reaction", "sticker") and not item["adopted"] and now - when <= ADOPT_WINDOW:
                item["adopted"] = form
                self._day(when.date().isoformat())["adopted"] += 1
                self._save()
            return

    def set_last_test(self, now: datetime, ok: bool, detail: str) -> None:
        self.state["last_test"] = {"time": now.isoformat(timespec="seconds"), "ok": ok, "detail": detail}
        self._save()

    def reset(self) -> None:
        self.state = {}
        self._save()

    def _day(self, date: str) -> Dict[str, int]:
        day = self.state.setdefault("days", {}).setdefault(date, {})
        for key in DAY_COUNTERS:
            day.setdefault(key, 0)
        return day

    def _save(self) -> None:
        if self._path:
            save_state(self._path, self.state)
