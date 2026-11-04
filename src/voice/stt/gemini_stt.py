"""Google Gemini STT 提供商：经 Live API（WebSocket）调用 gemini-3.5-transcribe-live。"""

import asyncio
import base64
import json
from typing import Any, Dict, Optional

import aiohttp

from ..base import STTProvider

LIVE_URL = (
    "wss://generativelanguage.googleapis.com/ws/"
    "google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
)
# 16kHz / 单声道 / s16le 下 100ms 一块，与官方建议的分块大小一致
CHUNK_BYTES = 3200
# 拿到定稿文字后这么久没有新消息就视为整段识别完；没有 turnComplete 时靠它收尾
SETTLE_SECONDS = 1.5
# 送完音频后等第一段定稿文字的上限，以及整次请求的上限
FIRST_RESULT_SECONDS = 15
TOTAL_SECONDS = 40


class GeminiSTTProvider(STTProvider):
    """Gemini Live 转写。

    每次识别开一条 Live 会话，把整段 16kHz 单声道 PCM 按 100ms 分块送出后发 ``audioStreamEnd``，
    收集 ``inputTranscription`` 定稿文字。
    """

    # ponytail: 关麦后才整段送出，延迟约等于一次 Live 会话往返；要边说边转写得把 VoiceDataSink 改成串流

    def __init__(self, config: Any, logger: Any) -> None:
        """从配置读取 API Key、模型与转写模式。

        Args:
            config: 含 ``api_key``、``model``、``mode`` 属性的配置对象。
            logger: 日志器。
        """
        self._logger = logger
        self._api_key: str = getattr(config, "api_key", "")
        self._model: str = getattr(config, "model", "") or "gemini-3.5-transcribe-live"
        self._mode: str = getattr(config, "mode", "SMART")
        self._logger.info(f"Gemini STT 初始化完成 [模型: {self._model}, 模式: {self._mode}]")

    async def recognize(self, audio_data: bytes) -> Optional[str]:
        """把 16kHz 单声道 PCM 送进 Gemini Live 会话转写。

        Args:
            audio_data: 裸 PCM 字节（16kHz/mono/s16le）。

        Returns:
            识别文字；无数据、缺密钥、无结果或请求失败时返回 None。
        """
        if not audio_data:
            return None
        if not self._api_key:
            self._logger.error("Gemini API 密钥未配置")
            return None
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(LIVE_URL, params={"key": self._api_key}, heartbeat=20) as ws:
                    text = await asyncio.wait_for(self._transcribe(ws, audio_data), TOTAL_SECONDS)
        except asyncio.TimeoutError:
            self._logger.error("Gemini STT 请求超时")
            return None
        except aiohttp.ClientError as exc:
            self._logger.error(f"Gemini STT 网络错误: {exc}")
            return None
        except Exception as exc:
            self._logger.error(f"Gemini STT 异常: {exc}")
            return None
        if text:
            self._logger.debug(f"识别结果: {text}")
            return text
        self._logger.debug("未识别到语音")
        return None

    async def _transcribe(self, ws: Any, audio_data: bytes) -> str:
        """在已连上的 Live WebSocket 上完成一次转写：setup → 送音频 → 收定稿文字。"""
        await ws.send_json(
            {
                "setup": {
                    "model": f"models/{self._model.removeprefix('models/')}",
                    "generationConfig": {"responseModalities": ["TEXT"]},
                    "inputAudioTranscription": {"languageCodes": [], "mode": self._mode},
                }
            }
        )
        # 官方要求等 setupComplete 后才能送其他消息
        while "setupComplete" not in await self._receive(ws, FIRST_RESULT_SECONDS):
            pass

        for start in range(0, len(audio_data), CHUNK_BYTES):
            chunk = base64.b64encode(audio_data[start : start + CHUNK_BYTES]).decode("ascii")
            await ws.send_json({"realtimeInput": {"audio": {"data": chunk, "mimeType": "audio/pcm;rate=16000"}}})
        await ws.send_json({"realtimeInput": {"audioStreamEnd": True}})

        parts: list[str] = []
        interim = ""
        while True:
            try:
                message = await self._receive(ws, SETTLE_SECONDS if parts else FIRST_RESULT_SECONDS)
            except asyncio.TimeoutError:
                break
            content = message.get("serverContent") or {}
            text = (content.get("inputTranscription") or {}).get("text")
            if text:
                parts.append(text)
                interim = ""
            elif (content.get("interimInputTranscription") or {}).get("text"):
                # Interim messages revise the same phrase; only the latest is useful.
                interim = content["interimInputTranscription"]["text"]
            if content.get("turnComplete"):
                break
        return ("".join(parts) + interim).strip()

    @staticmethod
    async def _receive(ws: Any, timeout: float) -> Dict[str, Any]:
        """读一则服务器 JSON 消息（Live API 用 binary 帧传 JSON）；连接关闭或报错时抛异常。"""
        msg = await ws.receive(timeout=timeout)
        if msg.type in (aiohttp.WSMsgType.TEXT, aiohttp.WSMsgType.BINARY):
            data = json.loads(msg.data)
            if isinstance(data, dict):
                if "error" in data:
                    raise RuntimeError(f"Gemini 返回错误: {data['error']}")
                return data
            return {}
        raise aiohttp.ClientError(f"Live 连接已关闭: type={msg.type}, extra={msg.extra}")

    async def close(self) -> None:
        """每次识别用完即关连接，这里只打日志。"""
        self._logger.info("Gemini STT 提供商已关闭")
