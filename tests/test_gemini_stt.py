from types import SimpleNamespace
import asyncio
import base64
import json
import logging

import aiohttp

from test_typing_indicator import _load_adapter_module


class FakeWs:
    """按顺序回放服务器消息的假 Live WebSocket；送出的消息记在 sent。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.sent: list = []

    async def send_json(self, data):
        self.sent.append(data)

    async def receive(self, timeout=None):
        if not self.replies:
            raise asyncio.TimeoutError
        return SimpleNamespace(type=aiohttp.WSMsgType.BINARY, data=json.dumps(self.replies.pop(0)).encode(), extra=None)


def _provider(**config):
    module = _load_adapter_module("src.voice.stt.gemini_stt")
    return module, module.GeminiSTTProvider(
        SimpleNamespace(api_key="k", **config), logging.getLogger("test_gemini_stt")
    )


def test_live_session_sends_chunks_and_joins_final_transcripts() -> None:
    module, provider = _provider(model="gemini-3.5-transcribe-live", mode="SMART")
    ws = FakeWs(
        [
            {"setupComplete": {}},
            {"serverContent": {"interimInputTranscription": {"text": "今天"}}},
            {"serverContent": {"inputTranscription": {"text": "今天晚上"}}},
            {"serverContent": {"inputTranscription": {"text": "一起打遊戲嗎？"}}},
            {"serverContent": {"turnComplete": True}},
            {"serverContent": {"inputTranscription": {"text": "不该读到这里"}}},
        ]
    )
    audio = b"\x01\x00" * (module.CHUNK_BYTES // 2 + 5)  # 一整块再多 10 字节

    assert asyncio.run(provider._transcribe(ws, audio)) == "今天晚上一起打遊戲嗎？"
    setup = ws.sent[0]["setup"]
    assert setup["model"] == "models/gemini-3.5-transcribe-live"
    assert setup["inputAudioTranscription"]["mode"] == "SMART"
    chunks = [m["realtimeInput"]["audio"] for m in ws.sent[1:-1]]
    assert len(chunks) == 2 and all(c["mimeType"] == "audio/pcm;rate=16000" for c in chunks)
    assert b"".join(base64.b64decode(c["data"]) for c in chunks) == audio
    assert ws.sent[-1] == {"realtimeInput": {"audioStreamEnd": True}}


def test_live_session_ends_on_silence_without_turn_complete() -> None:
    _, provider = _provider(model="models/gemini-3.5-transcribe-live", mode="VERBATIM")
    ws = FakeWs([{"setupComplete": {}}, {"serverContent": {"inputTranscription": {"text": " hello "}}}])

    assert asyncio.run(provider._transcribe(ws, b"\x00\x00" * 100)) == "hello"
    assert ws.sent[0]["setup"]["model"] == "models/gemini-3.5-transcribe-live", "不重复加 models/ 前缀"
