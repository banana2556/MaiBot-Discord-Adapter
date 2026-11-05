from pathlib import Path
from types import SimpleNamespace
import asyncio
import base64
import io
import json
import re

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


def test_rebinding_client_drops_channels_from_the_old_client() -> None:
    routing_cls = _load_adapter_module("src.send_handler.thread_send_handler").ThreadRoutingManager
    old_message, new_message = object(), object()

    async def fetch_old(channel_id):
        return _FakeChannel(channel_id, {1: old_message})

    routing = routing_cls(logger=SimpleNamespace(), chat_config=SimpleNamespace())
    routing.bind_client(SimpleNamespace(get_channel=lambda _id: None, fetch_channel=fetch_old))
    new_client = SimpleNamespace(get_channel={10: _FakeChannel(10, {1: new_message})}.get)

    async def run() -> None:
        assert await routing.find_message("1", group_id="10") is old_message  # 旧客户端拉来的频道进了缓存
        routing.bind_client(new_client)  # 改设定或重连后重建客户端
        assert await routing.find_message("1", group_id="10") is new_message, "不能再用连着已关闭 session 的旧频道"

    asyncio.run(run())


def _png(color: str) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_discord_emote_schema_only_takes_message_feeling_and_form() -> None:
    cls = _load_plugin_class()
    parameters = {item.name: item for item in cls.handle_discord_emote.__maibot_component_info__.parameters}
    assert set(parameters) == {"msg_id", "feeling", "form"}
    assert parameters["form"].enum_values == ["reaction", "emoji", "sticker"]


def test_manifest_declares_every_host_capability_the_plugin_calls() -> None:
    # 没声明的能力会被 Host 拒绝（未注册能力令牌），挑表情和单发都会静默失败
    root = Path(__file__).resolve().parents[1]
    used = {
        match.group(1)
        for path in [root / "plugin.py", *(root / "src").rglob("*.py")]
        for pattern in (
            r"ctx\.((?:llm|message|send|config|emoji|chat|frequency)\.\w+)\(",
            r"call_capability\(\s*\"([a-z_]+\.[a-z_.]+)\"",
        )
        for match in re.finditer(pattern, path.read_text(encoding="utf-8"))
    }
    declared = set(json.loads((root / "_manifest.json").read_text(encoding="utf-8"))["capabilities"])
    assert used and used <= declared, used - declared


def test_emoji_grid_numbers_every_candidate() -> None:
    grid_module = _load_adapter_module("src.emoji_grid")
    from PIL import Image

    assert grid_module.grid_shape(25) == (5, 5) and grid_module.grid_shape(3) == (2, 2)
    grid = grid_module.build_numbered_grid([base64.b64decode(_png("red"))] * 3 + [b"broken"])
    with Image.open(io.BytesIO(grid)) as image:
        assert image.size == (2 * 168 - 8, 2 * 168 - 8), "每格 160px 加 8px 間距"
        # 16px 的小圖放大填滿整格，不是中間一小點
        assert image.convert("RGB").getpixel((150, 150)) == (255, 0, 0)
