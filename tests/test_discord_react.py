from types import SimpleNamespace
import asyncio

import discord

from test_typing_indicator import _load_adapter_module, _load_plugin_class


def _not_found() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "Unknown Message")


class _FakeChannel:
    def __init__(self, channel_id: int, messages: dict[int, object]) -> None:
        self.id = channel_id
        self._messages = messages

    async def fetch_message(self, message_id: int) -> object:
        if message_id not in self._messages:
            raise _not_found()
        return self._messages[message_id]


def test_find_message_checks_group_channel_then_dm() -> None:
    routing_cls = _load_adapter_module("src.send_handler.thread_send_handler").ThreadRoutingManager
    group_message, dm_message = object(), object()
    channels = {10: _FakeChannel(10, {1: group_message})}
    dm_channel = _FakeChannel(20, {2: dm_message})
    user = SimpleNamespace(dm_channel=dm_channel)

    routing = routing_cls(logger=SimpleNamespace(), chat_config=SimpleNamespace())
    routing.bind_client(SimpleNamespace(get_channel=channels.get, get_user=lambda _user_id: user))

    async def run() -> None:
        assert await routing.find_message("1", group_id="10") is group_message
        assert await routing.find_message("2", user_id="99") is dm_message
        # 不在会话频道、也不在其子区（非 TextChannel 时不扫描子区）→ None
        assert await routing.find_message("3", group_id="10") is None
        assert await routing.find_message("not-a-number", group_id="10") is None

    asyncio.run(run())


def test_resolve_reaction_emoji_prefers_local_guild() -> None:
    plugin_cls = _load_plugin_class()
    local, remote = SimpleNamespace(name="pepe"), SimpleNamespace(name="wave")
    guild = SimpleNamespace(emojis=[local])
    client = SimpleNamespace(emojis=[SimpleNamespace(name="pepe"), remote])

    assert plugin_cls._resolve_reaction_emoji("👍", guild, client) == "👍"
    assert plugin_cls._resolve_reaction_emoji("<:x:123>", guild, client) == "<:x:123>"
    assert plugin_cls._resolve_reaction_emoji(":pepe:", guild, client) is local
    assert plugin_cls._resolve_reaction_emoji("wave", guild, client) is remote
    assert plugin_cls._resolve_reaction_emoji(":thumbsup:", guild, client) is None


def test_discord_react_tool_adds_reaction_or_lists_emojis() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    added: list[object] = []

    async def add_reaction(emoji: object) -> None:
        added.append(emoji)

    pepe = SimpleNamespace(name="pepe")
    target = SimpleNamespace(guild=SimpleNamespace(emojis=[pepe]), add_reaction=add_reaction)
    lookups: list[tuple] = []

    async def find_message(msg_id, group_id, user_id):
        lookups.append((msg_id, group_id, user_id))
        return target

    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(emojis=[]))
    plugin._thread_routing = SimpleNamespace(find_message=find_message)

    async def run() -> None:
        result = await plugin.handle_discord_react(msg_id="555", emoji=":pepe:", group_id="10", stream_id="s")
        assert result["success"] is True
        assert added == [pepe]
        assert lookups == [("555", "10", "")]

        result = await plugin.handle_discord_react(msg_id="555", emoji=":thumbsup:", group_id="10")
        assert result["success"] is False
        assert ":pepe:" in result["error"]

    asyncio.run(run())
