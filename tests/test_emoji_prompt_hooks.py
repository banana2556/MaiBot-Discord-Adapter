from types import SimpleNamespace
import asyncio

from test_typing_indicator import _load_adapter_module, _load_plugin_class


class _Emoji:
    def __init__(self, name: str, emoji_id: int, usable: bool = True) -> None:
        self.name, self.id, self._usable = name, emoji_id, usable

    def is_usable(self) -> bool:
        return self._usable

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


GUILD = SimpleNamespace(emojis=[_Emoji("pepe", 111), _Emoji("gone", 222, usable=False)])


def test_replyer_hook_allows_server_emojis_only_in_discord_sessions() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._discord_session_channels = {"s-guild": 10}
    items = [
        {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "不要输出表情包"}]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "hi"}]},
    ]
    result = asyncio.run(plugin.inject_discord_emoji_replyer_hint(session_id="s-guild", items=items))
    system_parts = result["modified_kwargs"]["items"][0]["parts"]
    assert system_parts[1]["text"] == plugin_cls._REPLYER_EMOJI_HINT
    assert asyncio.run(plugin.inject_discord_emoji_replyer_hint(session_id="qq-session", items=items)) == {
        "action": "continue"
    }


def test_outbound_text_turns_emoji_names_into_native() -> None:
    builder_cls = _load_adapter_module("src.send_handler.message_send_handler").DiscordContentBuilder
    channel = SimpleNamespace(guild=GUILD)
    render = builder_cls._render_emoji_names
    assert render("笑死:pepe:你", channel) == "笑死<:pepe:111>你"
    # 已成形的表情、不可用或不存在的名称、时间格式都不动
    assert render("<:pepe:111> <a:pepe:111>", channel) == "<:pepe:111> <a:pepe:111>"
    assert render(":gone: :nope: 10:30:00", channel) == ":gone: :nope: 10:30:00"
    assert render(":pepe:", SimpleNamespace()) == ":pepe:"
