"""決策模型（System One 格式）：Jev（TypeSafe）和 vLLM-SR Decision 都用同一個 ``POST /v1/systemone``。

只回每個選項的機率、不生成文字，適合「值不值得」「挑哪一個」這類快速判斷。
請求 ``{"model", "state": 文字, "questions": {問題ID: {"type", "instructions", "criteria"}}, "images": [data URL]}``，
回應 ``{"answers": {問題ID: {"type": "choice", "choice", "probabilities", "confidence"}}}``。
"""

import asyncio
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import aiohttp

from .life.common import load_state, save_state

TIMEOUT_SECONDS = 2.5
# 統計頁保留幾天的每日計數、最近幾次的明細
HISTORY_DAYS = 14
RECENT_LIMIT = 50
# 判斷：calls 次數、failed 請求失敗，其餘是最後採用的情境；送出：sends 成功、send_failed 沒送成
DAY_COUNTERS = (
    "calls",
    "failed",
    "text",
    "text_emote",
    "quick_reaction",
    "quick_sticker",
    "latency_ms",
    "sends",
    "send_failed",
    "send_latency_ms",
)


async def ask(
    config: Any,
    state: str,
    questions: Dict[str, Any],
    logger: Any,
    *,
    images: Optional[List[str]] = None,
    timeout: float = TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    """問決策模型。

    Args:
        config: ``DecisionConfig``，用到 base_url、api_key、model。
        state: 問題針對的內容；vLLM-SR 只收字串。
        questions: 問題 ID -> 問題定義。
        logger: 日誌器。
        images: 附帶的圖片 data URL（PNG / JPEG / WebP），問題可以照順序指稱它們。
        timeout: 整次請求的秒數上限；帶圖時要給長一點。

    Returns:
        按問題 ID 的 answers；請求失敗或回應格式不對時回傳 None，呼叫端退回原本的做法。
    """
    body: Dict[str, Any] = {"model": config.model, "state": state, "questions": questions}
    if images:
        body["images"] = images
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout)) as session:
            async with session.post(
                f"{config.base_url.strip().rstrip('/')}/v1/systemone",
                json=body,
                headers={"Authorization": f"Bearer {config.api_key.strip()}"},
            ) as response:
                data = await response.json(content_type=None)
                if response.status != 200:
                    logger.warning(f"決策模型回傳 HTTP {response.status}：{data}")
                    return None
    except (aiohttp.ClientError, asyncio.TimeoutError, ValueError) as exc:
        logger.warning(f"決策模型請求失敗：{exc!r}")
        return None
    answers = data.get("answers") if isinstance(data, dict) else None
    return answers if isinstance(answers, dict) else None


class DecisionStats:
    """決策模型的紀錄：每天的計數、最近的判斷與送出，供「DC生活 → 決策模型」頁面顯示。

    ``state = {"days": {日期: {計數}}, "decisions": [新的在前], "sends": [新的在前], "last_test": {...}}``；
    path 為 None 時只放記憶體。
    """

    def __init__(self, path: Optional[Path]) -> None:
        self._path = path
        self.state: Dict[str, Any] = load_state(path) if path else {}

    def record_decision(
        self,
        now: datetime,
        chat: str,
        message: str,
        scene: str,
        latency_ms: int,
        *,
        requested: str = "",
        emotion: str = "",
        probability: float = 0.0,
    ) -> None:
        """記一次情境判斷；失敗的請求不算進平均延遲（多半是逾時，會把平均拉歪）。

        Args:
            scene: 最後採用的情境（text / text_emote / quick_reaction / quick_sticker），請求失敗是 failed。
            requested: 模型原本選的情境；被安全條件降級時和 scene 不同。
            emotion: 模型選的情緒。
            probability: 模型原本選的那個情境的機率。
        """
        day = self._day(now.date().isoformat())
        day["calls"] += 1
        day[scene] += 1
        if scene != "failed":
            day["latency_ms"] += latency_ms
        self._push(
            "decisions",
            {
                "time": now.isoformat(timespec="seconds"),
                "chat": chat,
                "message": message,
                "scene": scene,
                "requested": requested,
                "emotion": emotion,
                "probability": round(probability, 3),
                "latency_ms": latency_ms,
            },
        )

    def record_send(
        self, now: datetime, chat: str, kind: str, latency_ms: int, *, picked: str = "", score: float = 0.0, ok: bool = True
    ) -> None:
        """記一次挑表情並送出。

        Args:
            kind: reaction（按表情）、sticker（只發圖）、append（回覆後附圖）或 tool（planner 自己用工具）。
            picked: 選中的表情或貼圖名稱。
            score: 向量相似度；沒用向量挑時為 0。
            ok: 有沒有送出去。
        """
        day = self._day(now.date().isoformat())
        if ok:
            day["sends"] += 1
            day["send_latency_ms"] += latency_ms
        else:
            day["send_failed"] += 1
        self._push(
            "sends",
            {
                "time": now.isoformat(timespec="seconds"),
                "chat": chat,
                "kind": kind,
                "picked": picked,
                "score": round(score, 3),
                "latency_ms": latency_ms,
                "ok": ok,
            },
        )

    def set_last_test(self, now: datetime, ok: bool, detail: str) -> None:
        self.state["last_test"] = {"time": now.isoformat(timespec="seconds"), "ok": ok, "detail": detail}
        self._save()

    def reset(self) -> None:
        self.state = {}
        self._save()

    def _push(self, key: str, item: Dict[str, Any]) -> None:
        items = self.state.setdefault(key, [])
        items.insert(0, item)
        del items[RECENT_LIMIT:]
        self.state["days"] = dict(sorted(self.state["days"].items())[-HISTORY_DAYS:])
        self._save()

    def _day(self, date: str) -> Dict[str, int]:
        day = self.state.setdefault("days", {}).setdefault(date, {})
        for key in DAY_COUNTERS:
            day.setdefault(key, 0)
        return day

    def _save(self) -> None:
        if self._path:
            save_state(self._path, self.state)
