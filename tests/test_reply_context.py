from types import SimpleNamespace
import asyncio

import discord
from maim_message import Seg

from test_native_emoji import _load_adapter_module


def _handler():
    handler_module = _load_adapter_module("src.recv_handler.message_handler")
    return handler_module.DiscordMessageHandler(
        logger=SimpleNamespace(warning=print, error=print, debug=print),
        platform_name="discord",
        chat_config=SimpleNamespace(),
    )


def _referenced():
    author = SimpleNamespace(id=228, display_name="nelu meow", nick=None)
    return SimpleNamespace(id=42, content="原文", attachments=[], author=author)


def test_reply_goes_into_reply_segment_not_text() -> None:
    handler = _handler()
    reply_data = asyncio.run(handler._get_reply_context(None, 42, _referenced()))
    assert reply_data == {
        "target_message_id": "42",
        "target_message_content": "原文",
        "target_message_sender_id": "228",
        "target_message_sender_nickname": "「nelu meow」",
        "target_message_sender_cardname": None,
    }

    tree = Seg(type="seglist", data=[Seg(type="reply", data=reply_data), Seg(type="text", data="但是")])
    raw = handler._serialize_message_segment(tree)
    assert raw == [{"type": "reply", "data": reply_data}, {"type": "text", "data": "但是"}]
    # 正文里不再带引用前缀，存库后再被引用也不会层层叠加
    assert handler._build_processed_plain_text(raw) == "但是"
    assert handler._analyze_segment_tree(tree)["reply_to"] == "42"

    # 拉不到被引用消息时只留 ID，交给 MaiBot 按 ID 查库
    async def fetch_fail(_id):
        raise RuntimeError("gone")

    channel = SimpleNamespace(fetch_message=fetch_fail)
    assert asyncio.run(handler._get_reply_context(channel, 42)) == {"target_message_id": "42"}


def test_reaction_points_at_reacted_message_with_usable_emoji() -> None:
    handler = _handler()
    user = SimpleNamespace(id=7, display_name="Daemi", name="daemi")

    async def resolve_user(_payload, _client):
        return user, None

    handler._resolve_reaction_user = resolve_user
    client = SimpleNamespace(cached_messages=[_referenced()], get_partial_messageable=lambda _id: None)

    for emoji, expected in [
        (discord.PartialEmoji(name="koronekekw", id=123, animated=True), "<a:koronekekw:123>"),
        (discord.PartialEmoji(name="😂"), "😂（笑哭了）"),
    ]:
        payload = SimpleNamespace(user_id=7, message_id=42, channel_id=9, guild_id=None, emoji=emoji)
        result = handler.build_host_message_dict(asyncio.run(handler._convert_reaction_to_maim("reaction_add", payload, client)))
        assert result["reply_to"] == "42"
        assert result["raw_message"][0]["data"]["target_message_content"] == "原文"
        assert result["processed_plain_text"] == f"用户「Daemi」添加了表情回应 {expected}"
