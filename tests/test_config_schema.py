from types import SimpleNamespace
import asyncio

from test_typing_indicator import _load_adapter_module, _load_plugin_class

VOICE_PROVIDER_SECTIONS = {
    "siliconflow_tts",
    "gptsovits_tts",
    "minimax_tts",
    "fishaudio_tts",
    "siliconflow_stt",
    "aliyun_stt",
    "tencent_stt",
}


def _schema_for(voice_overrides: dict) -> dict:
    plugin_cls = _load_plugin_class()
    config_module = _load_adapter_module("config")
    plugin = plugin_cls.__new__(plugin_cls)
    snapshot = config_module.DiscordPluginSettings().model_dump()
    snapshot["voice"].update(voice_overrides)
    plugin._load_schema_config_snapshot = lambda: snapshot
    schema = {"sections": {name: {"fields": {}} for name in config_module.DiscordPluginSettings.model_fields}}
    plugin._prune_inactive_config_sections(schema)
    plugin._apply_config_tabs(schema)
    return schema


def test_voice_disabled_hides_all_provider_sections() -> None:
    schema = _schema_for({"enabled": False})

    assert not VOICE_PROVIDER_SECTIONS & set(schema["sections"])
    assert "voice" in schema["sections"]


def test_only_selected_providers_are_shown() -> None:
    schema = _schema_for({"enabled": True, "tts_provider": "fishaudio", "stt_provider": "tencent"})

    assert VOICE_PROVIDER_SECTIONS & set(schema["sections"]) == {"fishaudio_tts", "tencent_stt"}
    voice_tab = next(tab for tab in schema["layout"]["tabs"] if tab["id"] == "voice")
    assert voice_tab["sections"] == ["voice", "fishaudio_tts", "tencent_stt"]


def test_every_section_belongs_to_a_tab() -> None:
    config_module = _load_adapter_module("config")
    tabbed = {name for _, _, sections in config_module.WEBUI_CONFIG_TABS for name in sections}

    assert tabbed == set(config_module.DiscordPluginSettings.model_fields)


class _FakeResponse:
    status = 200

    async def read(self) -> bytes:
        return b"\x00\x00" * 10

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


class _FakeSession:
    calls: list = []

    def __init__(self, *args, **kwargs) -> None:
        pass

    def post(self, url, json=None, headers=None):
        _FakeSession.calls.append((url, json, headers))
        return _FakeResponse()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> None:
        return None


def test_fishaudio_builds_request_and_wraps_pcm(monkeypatch) -> None:
    tts_module = _load_adapter_module("src.voice.tts.fishaudio_tts")
    config_module = _load_adapter_module("config")
    monkeypatch.setattr(tts_module.aiohttp, "ClientSession", _FakeSession)
    _FakeSession.calls.clear()
    config = config_module.FishAudioTTSConfig(
        api_key="key", reference_id="voice-1", response_format="pcm", sample_rate=24000, model="s1"
    )
    logger = SimpleNamespace(info=lambda *_: None, debug=lambda *_: None, warning=lambda *_: None, error=lambda *_: None)

    audio = asyncio.run(tts_module.FishAudioTTSProvider(config, logger).synthesize("你好"))

    url, payload, headers = _FakeSession.calls[0]
    assert url == "https://api.fish.audio/v1/tts"
    assert headers["model"] == "s1"
    assert headers["Authorization"] == "Bearer key"
    assert payload["reference_id"] == "voice-1"
    assert payload["format"] == "pcm"
    assert payload["sample_rate"] == 24000
    assert audio is not None and audio.getvalue().startswith(b"RIFF")


def test_fishaudio_falls_back_on_unsupported_options() -> None:
    tts_module = _load_adapter_module("src.voice.tts.fishaudio_tts")
    config_module = _load_adapter_module("config")
    config = config_module.FishAudioTTSConfig(response_format="opus", sample_rate=48000, latency="fast")
    logger = SimpleNamespace(info=lambda *_: None, warning=lambda *_: None)

    provider = tts_module.FishAudioTTSProvider(config, logger)

    assert provider._response_format == "wav"
    assert provider._sample_rate == 44100
    assert provider._latency == "normal"
