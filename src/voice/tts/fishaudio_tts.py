"""Fish Audio TTS 提供商实现。"""

import asyncio
import wave
from io import BytesIO
from typing import Any, Optional

import aiohttp

from ..base import TTSProvider


class FishAudioTTSProvider(TTSProvider):
    """Fish Audio TTS 提供商。

    通过 POST /v1/tts 接口合成语音，模型经请求头 ``model`` 指定，音色由 ``reference_id`` 指定。
    """

    def __init__(self, config: Any, logger: Any) -> None:
        """从配置读取 Fish Audio API、模型、音色与音频参数并记录初始化日志。

        Args:
            config: 含 ``api_key``、``api_base``、``model``、``reference_id``、``response_format``、``sample_rate`` 等属性的配置对象。
            logger: 日志器。

        Returns:
            None
        """
        self._logger = logger
        self._last_error: str = ""
        self._api_key: str = str(getattr(config, "api_key", "") or "").strip()
        self._api_base: str = str(getattr(config, "api_base", "") or "https://api.fish.audio").strip().rstrip("/")
        self._model: str = str(getattr(config, "model", "") or "s2.1-pro").strip()
        self._reference_id: str = str(getattr(config, "reference_id", "") or "").strip()
        self._response_format: str = str(getattr(config, "response_format", "wav") or "wav").strip().lower()
        self._sample_rate: int = int(getattr(config, "sample_rate", 44100))
        self._speed: float = float(getattr(config, "speed", 1.0))
        self._volume: float = float(getattr(config, "volume", 0.0))
        self._temperature: float = float(getattr(config, "temperature", 0.7))
        self._top_p: float = float(getattr(config, "top_p", 0.7))
        self._latency: str = str(getattr(config, "latency", "normal") or "normal").strip().lower()
        self._normalize: bool = bool(getattr(config, "normalize", True))
        self._normalize_audio_options()
        self._logger.info(
            "Fish Audio TTS 初始化完成 "
            f"[模型: {self._model}, 音色: {self._reference_id or '<默认>'}, "
            f"格式: {self._response_format}, 采样率: {self._sample_rate}]"
        )

    def _normalize_audio_options(self) -> None:
        """按官方约束修正输出格式、采样率与数值参数。"""
        # opus 返回的是 Ogg 容器，播放端的格式探测不认，这里只放行 wav/mp3/pcm
        if self._response_format not in {"wav", "mp3", "pcm"}:
            self._logger.warning(
                f"Fish Audio TTS 音频格式不受支持: {self._response_format!r}，改用 wav"
            )
            self._response_format = "wav"

        valid_sample_rates = {
            "mp3": {32000, 44100},
            "wav": {8000, 16000, 24000, 32000, 44100},
            "pcm": {8000, 16000, 24000, 32000, 44100},
        }
        allowed = valid_sample_rates[self._response_format]
        if self._sample_rate not in allowed:
            self._logger.warning(
                "Fish Audio TTS 采样率与输出格式不匹配，已自动调整: "
                f"format={self._response_format}, sample_rate={self._sample_rate} -> 44100"
            )
            self._sample_rate = 44100

        if self._latency not in {"low", "normal", "balanced"}:
            self._logger.warning(f"Fish Audio TTS 延迟模式不受支持: {self._latency!r}，改用 normal")
            self._latency = "normal"

        self._speed = min(max(self._speed, 0.5), 2.0)
        self._temperature = min(max(self._temperature, 0.0), 1.0)
        self._top_p = min(max(self._top_p, 0.0), 1.0)

    def _wrap_pcm_as_wav(self, data: bytes) -> bytes:
        """将裸 PCM 字节包装为 WAV，避免播放阶段丢失采样率信息。"""
        wav_buffer = BytesIO()
        with wave.open(wav_buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(self._sample_rate)
            wav_file.writeframes(data)
        return wav_buffer.getvalue()

    def get_last_error(self) -> str:
        """返回最近一次合成失败的原因，供上层拼接错误提示。"""
        return self._last_error

    def _fail(self, message: str) -> None:
        self._last_error = message
        self._logger.error(f"Fish Audio TTS {message}")

    async def synthesize(self, text: str) -> Optional[BytesIO]:
        """调用 Fish Audio ``POST /v1/tts``，将返回的音频字节封装为 ``BytesIO``。

        Args:
            text: 待合成文本。

        Returns:
            成功时返回音频字节的 ``BytesIO``；缺密钥、空文本、HTTP 或网络错误时返回 None。
        """
        if not text:
            return None
        if not self._api_key:
            self._fail("API 密钥未配置")
            return None

        url = f"{self._api_base}/v1/tts"
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "model": self._model,
        }
        payload: dict[str, Any] = {
            "text": text,
            "format": self._response_format,
            "sample_rate": self._sample_rate,
            "latency": self._latency,
            "normalize": self._normalize,
            "temperature": self._temperature,
            "top_p": self._top_p,
            "prosody": {"speed": self._speed, "volume": self._volume},
        }
        if self._response_format == "mp3":
            payload["mp3_bitrate"] = 128
        if self._reference_id:
            payload["reference_id"] = self._reference_id

        try:
            timeout = aiohttp.ClientTimeout(total=60)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.post(url, json=payload, headers=headers) as resp:
                    if resp.status == 200:
                        data = await resp.read()
                        if not data:
                            self._fail("响应中缺少音频数据")
                            return None
                        if self._response_format == "pcm":
                            data = self._wrap_pcm_as_wav(data)
                        self._last_error = ""
                        self._logger.debug(
                            f"Fish Audio TTS 合成成功: {text[:50]}... ({len(data)} bytes)"
                        )
                        return BytesIO(data)
                    text_err = await resp.text()
                    hint = {401: "（API Key 无效）", 402: "（账户余额或额度不足）", 503: "（服务端繁忙）"}.get(
                        resp.status, ""
                    )
                    self._fail(f"请求失败: {resp.status}{hint} {text_err}")
                    return None
        except asyncio.TimeoutError:
            self._fail("请求超时")
            return None
        except aiohttp.ClientError as exc:
            self._fail(f"网络错误: {exc}")
            return None
        except Exception as exc:
            self._fail(f"异常: {exc}")
            return None

    async def close(self) -> None:
        """关闭提供商（当前实现仅打日志，无持久连接需释放）。

        Returns:
            None
        """
        self._logger.info("Fish Audio TTS 提供商已关闭")
