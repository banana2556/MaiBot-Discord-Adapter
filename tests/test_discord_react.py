from types import SimpleNamespace
import asyncio
import base64
import io

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
    animated = plugin_cls._resolve_reaction_emoji("<a:x:1339569745908731994>", guild, client)
    assert animated._as_reaction() == "x:1339569745908731994"
    assert plugin_cls._resolve_reaction_emoji(":pepe:", guild, client) is local
    assert plugin_cls._resolve_reaction_emoji("wave", guild, client) is remote
    assert plugin_cls._resolve_reaction_emoji(":thumbsup:", guild, client) is None


def _png(color: str) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def test_discord_react_tool_picks_emoji_from_grid_like_send_emoji() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    added: list[object] = []

    async def add_reaction(emoji: object) -> None:
        added.append(emoji)

    emojis = [SimpleNamespace(id=i, name=f"e{i}", is_usable=lambda: True) for i in range(3)]
    guild = SimpleNamespace(emojis=[*emojis, SimpleNamespace(id=9, name="locked", is_usable=lambda: False)])
    target = SimpleNamespace(content="笑死", guild=guild, add_reaction=add_reaction)
    requests: list[tuple[str, list]] = []
    replies: list[dict] = []

    async def find_message(msg_id, group_id, user_id):
        return target

    async def fetch(url, size=48):
        return _png("red")

    async def generate(prompt, task_name=""):
        requests.append((task_name, prompt))
        return replies.pop(0)

    async def config_get(key, default=None):
        assert key == "emoji.emoji_send_num"
        return 25

    async def get_recent(chat_id, limit=10):
        assert (chat_id, limit) == ("stream-1", 12)
        return [{"id": "m"}]

    async def build_readable(messages):
        return "A: 笑死"

    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(emojis=[]))
    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._message_handler = SimpleNamespace(_fetch_emoji_bytes=fetch)
    plugin._ctx = SimpleNamespace(
        llm=SimpleNamespace(generate=generate),
        config=SimpleNamespace(get=config_get),
        message=SimpleNamespace(get_recent=get_recent, build_readable=build_readable),
        logger=SimpleNamespace(warning=print, debug=print),
    )

    async def react(**kwargs):
        return await plugin.handle_discord_react(msg_id="555", group_id="10", stream_id="stream-1", **kwargs)

    async def run() -> None:
        # 指定了表情就不问模型
        assert (await react(emoji=":e0:"))["success"] is True
        assert added == [emojis[0]] and requests == []

        # 不指定：emoji 任务没配好就退到 vlm，看拼图和聊天记录选序号
        replies[:] = [{"success": False, "error": "未找到名为 '' 的模型"}, {"success": True, "response": '{"emoji_index":2,"reason":"笑"}'}]
        result = await react(feeling="笑翻了")
        assert result["success"] is True
        assert [task for task, _ in requests] == ["emoji", "vlm"]
        system, user = requests[-1][1]
        assert "3 个位置" in system["content"] and "emoji_index" in system["content"]
        text, image = user["content"]
        assert "A: 笑死" in text["text"] and "笑翻了" in text["text"] and "候选总数: 3" in text["text"]
        assert base64.b64decode(image["image_base64"]).startswith(b"\x89PNG")
        picked = added[-1]
        assert picked in emojis

        # 回复不是 JSON 或序号越界：照 MaiBot 回退到第 1 张
        for response in ("看不懂", '{"emoji_index": 99}'):
            replies[:] = [{"success": True, "response": response}]
            assert (await react())["success"] is True

        # 两个任务都失败：让 planner 改填 Unicode
        replies[:] = [{"success": False}, {"success": False}]
        assert "Unicode" in (await react())["error"]

    asyncio.run(run())


def test_discord_react_does_not_need_unregistered_config_capability() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    selected = []

    async def add_reaction(emoji):
        selected.append(emoji)

    async def forbidden_config_get(*args, **kwargs):
        raise RuntimeError("[E_CAPABILITY_DENIED] 未注册能力令牌")

    async def fetch(url, size=48):
        return _png("green")

    async def generate(prompt, task_name=""):
        return {"success": True, "response": '{"emoji_index":1}'}

    emoji = SimpleNamespace(id=42, is_usable=lambda: True)
    target = SimpleNamespace(content="好耶", guild=SimpleNamespace(emojis=[emoji]), add_reaction=add_reaction)

    async def find_message(*args):
        return target

    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(emojis=[]))
    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._message_handler = SimpleNamespace(_fetch_emoji_bytes=fetch)
    plugin._ctx = SimpleNamespace(
        config=SimpleNamespace(get=forbidden_config_get),
        llm=SimpleNamespace(generate=generate),
        message=SimpleNamespace(get_recent=lambda **kwargs: None),
        logger=SimpleNamespace(warning=lambda *args: None, debug=lambda *args: None),
    )
    result = asyncio.run(plugin.handle_discord_react(msg_id="55", group_id="10"))
    assert result["success"] is True and selected == [emoji]


def test_unicode_hint_prefers_guild_custom_emoji() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    sent = []
    picked = []
    custom = SimpleNamespace(id=42, is_usable=lambda: True)

    async def add_reaction(value):
        sent.append(value)

    async def find_message(*args):
        return SimpleNamespace(guild=SimpleNamespace(emojis=[custom]), add_reaction=add_reaction)

    async def pick(target, feeling, stream_id):
        picked.append(feeling)
        return custom

    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(emojis=[]))
    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._pick_reaction_emoji = pick
    result = asyncio.run(plugin.handle_discord_react(msg_id="55", group_id="10", emoji="🐟"))
    assert result["success"] is True
    assert sent == [custom], "伺服器有可用自訂表情時不應直接送 Unicode"
    assert picked == ["🐟"], "Unicode 應作為情緒提示給選圖路徑"


def test_unicode_hint_falls_back_when_custom_selection_fails() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    sent = []
    custom = SimpleNamespace(id=42, is_usable=lambda: True)

    async def add_reaction(value):
        sent.append(value)

    async def find_message(*args):
        return SimpleNamespace(guild=SimpleNamespace(emojis=[custom]), add_reaction=add_reaction)

    async def pick(*args):
        return None

    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(emojis=[]))
    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._pick_reaction_emoji = pick
    result = asyncio.run(plugin.handle_discord_react(msg_id="55", group_id="10", emoji="🐟"))
    assert result["success"] is True and sent == ["🐟"]


def test_emoji_grid_numbers_every_candidate() -> None:
    grid_module = _load_adapter_module("src.emoji_grid")
    from PIL import Image

    assert grid_module.grid_shape(25) == (5, 5) and grid_module.grid_shape(3) == (2, 2)
    grid = grid_module.build_numbered_grid([base64.b64decode(_png("red"))] * 3 + [b"broken"])
    with Image.open(io.BytesIO(grid)) as image:
        assert image.size == (2 * 136 - 8, 2 * 136 - 8)
