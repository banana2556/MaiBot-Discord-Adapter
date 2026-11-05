"""决策模型（System One 格式）：Jev（TypeSafe）和 vLLM-SR Decision 都用同一个 ``POST /v1/systemone``。

只回每个选项的概率、不生成文字，适合「值不值得」这类快速判断。
请求 ``{"model", "state": 文字, "questions": {问题ID: {"type", "instructions", "criteria"}}}``，
回应 ``{"answers": {问题ID: {"type": "choice", "choice", "probabilities", "confidence"}}}``。
"""

import asyncio
from typing import Any, Dict, Optional

import aiohttp

TIMEOUT_SECONDS = 2.5


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
