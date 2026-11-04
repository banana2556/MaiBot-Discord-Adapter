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


def _png(color: str) -> str:
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color).save(buffer, format="PNG")
    return base64.b64encode(buffer.getvalue()).decode()


def _emote_plugin(target: object, replies: list[dict], requests: list | None = None):
    """搭一个只够跑 discord_emote 的插件：图片下载固定回红色小图，模型按 replies 依次回答。"""
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    sent: list[tuple[str, str, dict]] = []

    async def find_message(msg_id, group_id, user_id):
        return target

    async def fetch(url, size=48):
        return _png("red")

    async def generate(prompt, task_name=""):
        if requests is not None:
            requests.append((task_name, prompt))
        return replies.pop(0)

    async def get_recent(chat_id, limit=10):
        assert (chat_id, limit) == ("stream-1", 12)
        return [{"id": "m"}]

    async def build_readable(messages):
        return "A: 笑死"

    def sender(kind):
        async def send(data, stream_id, **kwargs):
            sent.append((kind, data, kwargs))
            return True

        return send

    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(emojis=[]))
    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._message_handler = SimpleNamespace(_fetch_emoji_bytes=fetch)
    plugin._native_emoji = _load_adapter_module("src.native_emoji").NativeEmojiRegistry()
    plugin._ctx = SimpleNamespace(
        llm=SimpleNamespace(generate=generate),
        message=SimpleNamespace(get_recent=get_recent, build_readable=build_readable),
        send=SimpleNamespace(text=sender("text"), emoji=sender("emoji")),
        logger=SimpleNamespace(warning=lambda *args: None, debug=lambda *args: None),
    )
    return plugin, sent


def _guild_target(added: list) -> SimpleNamespace:
    async def add_reaction(emoji: object) -> None:
        added.append(emoji)

    emojis = [SimpleNamespace(id=i, name=f"e{i}", is_usable=lambda: True) for i in range(3)]
    locked = SimpleNamespace(id=9, name="locked", is_usable=lambda: False)
    stickers = [
        SimpleNamespace(id=70, name="wave", available=True, format=discord.StickerFormatType.png, url="https://s/70.png"),
        SimpleNamespace(id=71, name="lottie", available=True, format=discord.StickerFormatType.lottie, url="x"),
        SimpleNamespace(id=72, name="gone", available=False, format=discord.StickerFormatType.png, url="x"),
    ]
    guild = SimpleNamespace(emojis=[*emojis, locked], stickers=stickers)
    return SimpleNamespace(content="笑死", guild=guild, add_reaction=add_reaction)


def test_discord_emote_reaction_picks_emoji_from_grid_like_send_emoji() -> None:
    added: list[object] = []
    requests: list[tuple[str, list]] = []
    replies: list[dict] = []
    target = _guild_target(added)
    plugin, sent = _emote_plugin(target, replies, requests)

    async def emote(**kwargs):
        return await plugin.handle_discord_emote(msg_id="555", group_id="10", stream_id="stream-1", **kwargs)

    async def run() -> None:
        # 只传情绪，由视觉模型从服务器自定义表情拼图挑图
        # emoji 任务没配好就退到 vlm，看拼图和聊天记录选序号
        replies[:] = [{"success": False, "error": "未找到名为 '' 的模型"}, {"success": True, "response": '{"emoji_index":2,"reason":"笑"}'}]
        result = await emote(feeling="笑翻了")
        assert result["success"] is True
        assert [task for task, _ in requests] == ["emoji", "vlm"]
        system, user = requests[-1][1]
        assert "3 个位置" in system["content"] and "emoji_index" in system["content"]
        text, image = user["content"]
        assert "A: 笑死" in text["text"] and "笑翻了" in text["text"] and "候选总数: 3" in text["text"]
        assert base64.b64decode(image["image_base64"]).startswith(b"\x89PNG")
        assert added[-1] in target.guild.emojis and sent == []

        # 回覆不是 JSON 或序號越界：不可假裝選了第 1 張
        for response in ("看不懂", '{"emoji_index": 99}'):
            replies[:] = [{"success": True, "response": response}]
            assert (await emote())["success"] is False
        # 視覺選圖失敗：不送出，也不退回 Unicode
        replies[:] = [{"success": False}, {"success": False}]
        assert (await emote())["success"] is False
        assert len(added) == 1 and sent == []

        assert "form 只能是" in (await emote(form="gif"))["error"]

    asyncio.run(run())


def test_discord_emote_sends_emoji_and_sticker_through_host() -> None:
    added: list[object] = []
    target = _guild_target(added)
    replies = [{"success": True, "response": '{"emoji_index":1}'}, {"success": True, "response": '{"emoji_index":1}'}]
    plugin, sent = _emote_plugin(target, replies)

    async def run() -> None:
        # 单发表情：原生 <:名称:id> 文本经 Host 发出，进聊天记录
        result = await plugin.handle_discord_emote(msg_id="555", stream_id="stream-1", form="emoji")
        assert result["success"] is True and sent[0][0] == "text"
        assert sent[0][1] in {str(emoji) for emoji in target.guild.emojis[:3]}  # 候选随机抽样，不固定第几张
        assert sent[0][2] == {"sync_to_maisaka_history": True}

        # 发贴图：只从可用的图片贴图里挑；先登记哈希，出站时还原成原生贴图
        result = await plugin.handle_discord_emote(msg_id="555", stream_id="stream-1", form="sticker")
        assert result["success"] is True and "wave" in result["content"]
        kind, image_b64, _ = sent[1]
        assert kind == "emoji" and plugin._native_emoji.lookup(image_b64) == ("sticker", 70)
        assert added == []

        # 单发必须有聊天流
        assert "缺少聊天流" in (await plugin.handle_discord_emote(msg_id="555", form="sticker"))["error"]

    asyncio.run(run())


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
        assert image.size == (2 * 136 - 8, 2 * 136 - 8)
