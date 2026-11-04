"""A decoded Discord audio frame arrives on voice_recv's packet-router thread."""
import asyncio
import logging
import struct
from types import SimpleNamespace

from test_typing_indicator import _load_adapter_module


def test_vad_from_audio_thread_finishes_utterance_on_event_loop():
    mod = _load_adapter_module("src.voice.voice_manager")
    mod._frame_db = lambda pcm: -100.0 if not any(pcm) else -10.0
    member = SimpleNamespace(id=123, bot=False)
    calls = []
    vm = mod.VoiceManager(
        bot=SimpleNamespace(), logger=logging.getLogger("test_voice_vad_thread"),
        stt_provider=object(), vad_deactivation_delay_ms=20,
    )
    vm.voice_client = SimpleNamespace(channel=SimpleNamespace(
        guild=SimpleNamespace(get_member=lambda uid: member if uid == member.id else None)))

    async def process_audio(found_member):
        calls.append(found_member.id)

    vm._process_user_audio = process_audio
    vm._voice_sink = mod.VoiceDataSink(vm)
    loud = struct.pack("<h", 10000) * 1920
    quiet = b"\x00\x00" * 1920

    async def run():
        await vm.start()
        await asyncio.to_thread(lambda: (
            vm._voice_sink.write(member, SimpleNamespace(pcm=loud)),
            vm._voice_sink.write(member, SimpleNamespace(pcm=quiet)),
        ))
        await asyncio.sleep(0.1)
        assert calls == [member.id]
        assert vm._vad_user_talking[member.id] is False

    asyncio.run(run())
