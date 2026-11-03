from types import SimpleNamespace
import asyncio

from test_typing_indicator import _load_adapter_module, _load_plugin_class

BOT = SimpleNamespace(id=1)


def _plugin(target: object, voice_manager: object = None, channels: dict | None = None):
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)

    async def find_message(msg_id, group_id, user_id):
        return target if msg_id == "555" else None

    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(user=BOT, get_channel=(channels or {}).get))
    plugin._voice_manager = voice_manager
    return plugin


def test_unreact_removes_only_own_reactions() -> None:
    removed: list[tuple[str, object]] = []

    async def remove_reaction(emoji, member):
        removed.append((emoji, member))

    reactions = [SimpleNamespace(emoji="👍", me=True), SimpleNamespace(emoji="😂", me=False)]
    target = SimpleNamespace(reactions=reactions, remove_reaction=remove_reaction)
    plugin = _plugin(target)

    result = asyncio.run(plugin.handle_discord_unreact(msg_id="555", group_id="10"))
    assert result["success"] is True and removed == [("👍", BOT)]

    reactions[0].me = False
    assert "没有自己加的" in asyncio.run(plugin.handle_discord_unreact(msg_id="555"))["error"]
    assert "找不到" in asyncio.run(plugin.handle_discord_unreact(msg_id="404"))["error"]


class _FakeVoice:
    def __init__(self, mode: str = "auto", allowed: tuple[int, ...] = (), connected: int | None = None) -> None:
        self.voice_mode, self._allowed, self._connected = mode, allowed, connected
        self.calls: list[object] = []

    def can_join(self, channel_id: int) -> bool:
        return channel_id in self._allowed

    async def connect(self, channel_id: int) -> bool:
        self.calls.append(("connect", channel_id))
        return True

    def get_connected_channel_id(self):
        return self._connected

    async def disconnect(self) -> None:
        self.calls.append("disconnect")


def test_voice_join_follows_requester_into_allowed_channel() -> None:
    lounge = SimpleNamespace(id=77, name="lounge")
    author = SimpleNamespace(voice=SimpleNamespace(channel=lounge))
    target = SimpleNamespace(author=author)

    assert "未启用" in asyncio.run(_plugin(target).handle_discord_voice_join(msg_id="555"))["error"]

    blocked = _FakeVoice(allowed=())
    assert "不在设置允许" in asyncio.run(_plugin(target, blocked).handle_discord_voice_join(msg_id="555"))["error"]
    assert blocked.calls == []

    voice = _FakeVoice(allowed=(77,))
    result = asyncio.run(_plugin(target, voice).handle_discord_voice_join(msg_id="555"))
    assert result["success"] is True and voice.calls == [("connect", 77)]

    # 私聊作者是 User，没有 voice；不在语音里也一样
    for nobody in (SimpleNamespace(), SimpleNamespace(voice=None)):
        message = SimpleNamespace(author=nobody)
        assert "不在语音频道" in asyncio.run(_plugin(message, voice).handle_discord_voice_join(msg_id="555"))["error"]


def test_voice_leave_only_from_same_guild_and_not_in_fixed_mode() -> None:
    guild, other = SimpleNamespace(id=1), SimpleNamespace(id=2)
    channels = {
        10: SimpleNamespace(guild=guild),
        20: SimpleNamespace(guild=other),
        77: SimpleNamespace(guild=guild, name="lounge"),
    }

    def leave(voice, group_id):
        return asyncio.run(_plugin(None, voice, channels).handle_discord_voice_leave(group_id=group_id))

    assert "不在语音频道" in leave(_FakeVoice(connected=None), "10")["error"]
    assert "固定频道" in leave(_FakeVoice(mode="fixed", connected=77), "10")["error"]
    for group_id in ("20", ""):
        voice = _FakeVoice(connected=77)
        assert "同一个服务器" in leave(voice, group_id)["error"] and voice.calls == []

    voice = _FakeVoice(connected=77)
    assert leave(voice, "10")["success"] is True and voice.calls == ["disconnect"]


def test_voice_manager_can_join_respects_configured_channels() -> None:
    manager_cls = _load_adapter_module("src.voice.voice_manager").VoiceManager
    logger = SimpleNamespace(info=lambda *_: None, warning=lambda *_: None)

    fixed = manager_cls(bot=None, logger=logger, voice_mode="fixed", fixed_channel_id=" 77 ")
    assert fixed.can_join(77) and not fixed.can_join(78)
    auto = manager_cls(bot=None, logger=logger, voice_mode="auto", auto_channel_list=["77", "x"])
    assert auto.can_join(77) and not auto.can_join(78)
