"""表情：決策模型判斷情境，向量挑表情送出，不經過 LLM。"""

from types import SimpleNamespace
import asyncio
import base64

import discord
import pytest

from test_typing_indicator import _load_adapter_module, _load_plugin_class

# 假的 embedding：每個關鍵字一個維度，文字含這個詞那一維就是 1
KEYWORDS = ("好笑", "心疼", "讚同", "揮手")


def _vector(text: str) -> list:
    return [1.0 if word in text else 0.0 for word in KEYWORDS] + [0.01]


def _b64(data: bytes) -> str:
    return base64.b64encode(data).decode()


class _Emoji:
    def __init__(self, emoji_id: int, name: str) -> None:
        self.id, self.name = emoji_id, name

    def is_usable(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


class FakeCtx:
    """MaiBot Host 的假上下文：表情庫兩張、embedding 照關鍵字給向量、送出記下來。"""

    def __init__(self) -> None:
        self.library = [
            {"base64": _b64(b"laugh-cat"), "description": "笑到翻過去的貓", "emotion": "好笑"},
            {"base64": _b64(b"hug"), "description": "抱抱安慰", "emotion": "心疼"},
        ]
        self.embedded: list = []
        self.embed_fail = False
        self.sent: list = []
        self.logs: list = []
        self.logger = SimpleNamespace(
            info=self.logs.append, warning=self.logs.append, debug=lambda *a: None, exception=self.logs.append
        )
        self.message = SimpleNamespace(build_readable=self._build_readable)
        self.send = SimpleNamespace(emoji=self._sender("emoji"), text=self._sender("text"))
        self.emoji = SimpleNamespace(get_by_description=self._by_description)
        self.chat = SimpleNamespace(get_all_streams=self._streams)

    async def call_capability(self, name, **kwargs):
        if name == "emoji.get_count":
            return len(self.library)
        if name == "emoji.get_all":
            return self.library  # SDK 會把 {"emojis": [...]} 拆開
        assert name == "llm.embed"
        self.embedded.extend(kwargs["texts"])
        if self.embed_fail:
            return {"success": False, "error": "沒有 embedding 模型"}
        return {"success": True, "results": [{"embedding": _vector(text)} for text in kwargs["texts"]]}

    async def _build_readable(self, messages, **kwargs):
        return "[12:00] 「小明」说：我剛剛跌倒了哈哈哈"

    def _sender(self, kind):
        async def send(data, stream_id, **kwargs):
            self.sent.append((kind, data, stream_id))
            return True

        return send

    async def _by_description(self, description, limit=5):
        return self.library[1] if description == "心疼" else None

    async def _streams(self, platform="all_platforms"):
        return [{"stream_id": "s1", "group_name": "聊天大廳", "user_cardname": None, "user_nickname": "x"}]


def _env(monkeypatch, answers=None, decision=None, chat=None):
    """搭一個能跑表情功能的插件：伺服器有兩個自訂表情、一張貼圖；ask_decision 依序回 answers。"""
    emote_module = _load_adapter_module("src.emote")
    native_module = _load_adapter_module("src.native_emoji")
    library_module = _load_adapter_module("src.emote_library")
    plugin = _load_plugin_class()()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0"},
            "chat": chat or {"emote_chance": 0},
            "decision": {"enabled": True, "api_key": "k", **(decision or {})},
        }
    )
    ctx = FakeCtx()
    plugin._ctx = ctx
    emojis = [_Emoji(1, "kekw"), _Emoji(2, "thumbsup")]
    sticker = SimpleNamespace(
        id=70, name="wave", description="揮手打招呼", available=True, format=discord.StickerFormatType.png, url="https://s/70.png"
    )
    guild = SimpleNamespace(emojis=emojis, stickers=[sticker])
    reactions: list = []

    async def add_reaction(emoji):
        reactions.append(emoji)

    target = SimpleNamespace(content="我剛剛跌倒了哈哈哈", guild=guild, add_reaction=add_reaction)

    async def find_message(msg_id, group_id, user_id):
        return target if msg_id == "m1" else None

    async def fetch(url, size=48):
        return _b64(b"sticker-" + url.encode())

    plugin._native_emoji = native_module.NativeEmojiRegistry()
    plugin._native_emoji.remember(ctx.library[0]["base64"], "emoji", 1)  # 表情庫第一張就是伺服器的 kekw
    plugin._emote_library = library_module.EmoteLibrary(ctx, None, lambda: plugin._native_emoji)
    plugin._client_manager = SimpleNamespace(
        client=SimpleNamespace(user=SimpleNamespace(display_name="麥麥"), get_channel=lambda cid: SimpleNamespace(guild=guild)),
        remember_typing_target=lambda session_id, channel_id: None,
    )
    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._message_handler = SimpleNamespace(_fetch_emoji_bytes=fetch)
    queue = list(answers or [])
    asked: list = []

    async def fake_ask(config, state, questions, logger, **kwargs):
        asked.append((state, questions))
        return queue.pop(0) if queue else None

    monkeypatch.setattr(emote_module, "ask_decision", fake_ask)
    monkeypatch.setattr(emote_module.random, "choice", lambda items: items[0])  # 前幾名固定取第一名
    return SimpleNamespace(plugin=plugin, ctx=ctx, module=emote_module, reactions=reactions, asked=asked, emojis=emojis)


def _answer(scene, probability=0.9, emotion="好笑"):
    return {
        "scene": {"type": "choice", "choice": scene, "probabilities": {scene: probability}},
        "emotion": {"type": "choice", "choice": emotion},
    }


def _inbound(mentions_bot=False, group=True, message_id="m1"):
    info = {
        "additional_config": {"platform_io_target_channel_id": "10", **({"discord_mentions_bot": True} if mentions_bot else {})},
        "user_info": {"user_id": "u1"},
    }
    if group:
        info["group_info"] = {"group_id": "10"}
    return {
        "platform": "discord",
        "session_id": "s1",
        "message_id": message_id,
        "message_info": info,
        "processed_plain_text": "我剛剛跌倒了哈哈哈",
    }


def _planner(*tools):
    return {
        "items": [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "人設"}]}],
        "tool_definitions": [{"type": "function", "function": {"name": name}} for name in tools],
        "session_id": "s1",
    }


def _names(kwargs):
    return [tool["function"]["name"] for tool in kwargs["tool_definitions"]]


async def _decide(plugin):
    """跑一次 planner hook，回傳 MaiBot 實際會用的工具名稱與系統提示詞（hook 收到的是 kwargs 的副本）。"""
    kwargs = _planner("reply", "discord_emote", "send_emoji", "stop")
    result = await plugin.decide_emote_in_planner(**kwargs)
    used = result.get("modified_kwargs") or kwargs
    return _names(used), used["items"][0]["parts"][-1]["text"]


async def _drain():
    for _ in range(30):
        await asyncio.sleep(0)


# ===== 向量表情庫 =====


def test_library_maps_discord_emotes_and_ranks_by_similarity() -> None:
    library_module = _load_adapter_module("src.emote_library")
    native_module = _load_adapter_module("src.native_emoji")
    ctx = FakeCtx()
    registry = native_module.NativeEmojiRegistry()
    registry.remember(ctx.library[0]["base64"], "emoji", 1)
    library = library_module.EmoteLibrary(ctx, None, lambda: registry)

    async def run():
        await library.refresh(force=True)
        keys = [entry["key"] for entry in library.entries]
        assert keys[0] == "emoji:1" and keys[1].startswith("img:"), "能對回 Discord 的用 emoji:<id>，其餘用圖片雜湊"
        assert library.text_for("emoji:1") == "笑到翻過去的貓，好笑"
        embedded = len(ctx.embedded)
        await library.refresh(force=True)
        assert len(ctx.embedded) == embedded, "表情庫沒變、描述沒變就不重算"
        ranked = await library.rank("心疼：好可憐", [(entry["key"], entry["text"]) for entry in library.entries], top=1)
        assert ranked[0][0] == keys[1]
        ctx.embed_fail = True
        assert await library.rank("好笑", [("x", "新的描述")]) is None, "embedding 掛了回 None，呼叫端改用別的方法"

    asyncio.run(run())


# ===== planner 入口的情境判斷 =====


def test_without_decision_model_planner_keeps_tool_and_random_nudge(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False}, chat={"emote_chance": 1})
    kwargs = _planner("reply", "discord_emote", "stop")
    result = asyncio.run(env.plugin.decide_emote_in_planner(**kwargs))
    assert "discord_emote" in _names(kwargs) and "discord_emote" in kwargs["items"][0]["parts"][-1]["text"]
    assert result["action"] == "continue"
    assert asyncio.run(env.plugin.decide_emote_in_planner(**_planner("reply", "stop"))) == {"action": "continue"}, (
        "不是 Discord 聊天（或睡覺只剩 stop）不管"
    )


def test_text_scene_hides_tool_for_the_turn_and_failure_keeps_it(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("text", 0.8), None])

    async def scenario():
        env.plugin._remember_inbound("s1", _inbound())
        first, _ = await _decide(env.plugin)
        second, _ = await _decide(env.plugin)  # 同一回合後面的請求不再問
        env.plugin._remember_inbound("s1", _inbound())
        failed, _ = await _decide(env.plugin)
        return first, second, failed

    first, second, failed = asyncio.run(scenario())
    assert first == ["reply", "stop"] and second == ["reply", "stop"]
    assert len(env.asked) == 2, "每則新訊息只問一次"
    assert {"discord_emote", "send_emoji"} <= set(failed), "請求失敗那回合 planner 照舊自己決定"
    state = env.asked[0][0]
    assert "你是「麥麥」" in state and "沒有 @ 你" in state and "跌倒了" in state


def test_quick_reaction_reacts_by_vector_and_planner_only_stops(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", 0.9, "好笑")])

    async def scenario():
        env.plugin._remember_inbound("s1", _inbound())
        decided = await _decide(env.plugin)
        await _drain()
        return decided

    names, prompt = asyncio.run(scenario())
    assert names == ["stop"] and "你已經用表情回應了" in prompt
    assert env.reactions == [env.emojis[0]], "kekw 在表情庫的描述是好笑，向量最接近"
    stats = env.plugin._decision_stats.state
    assert stats["decisions"][0]["scene"] == "quick_reaction" and stats["sends"][0]["kind"] == "reaction"


def test_quick_response_is_downgraded_when_she_is_addressed_or_unsure(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_sticker", 0.9, "心疼"), _answer("quick_reaction", 0.3)])

    async def scenario():
        env.plugin._remember_inbound("s1", _inbound(mentions_bot=True))
        addressed, _ = await _decide(env.plugin)
        env.plugin._remember_inbound("s1", _inbound())
        await _decide(env.plugin)
        return addressed

    addressed = asyncio.run(scenario())
    assert addressed == ["reply", "stop"], "@ 她的訊息不只發圖不說話"
    scenes = [item["scene"] for item in env.plugin._decision_stats.state["decisions"]]
    assert scenes == ["text", "text_emote"], "新的在前：第二則沒過門檻當成只要文字；第一則降成文字＋附圖"
    assert env.reactions == [] and env.ctx.sent == []


def test_text_emote_appends_a_picture_after_her_whole_reply(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("text_emote", 0.8, "心疼")])
    monkeypatch.setattr(env.module, "APPEND_DELAY_SECONDS", 0)

    def sent_message(*segments):
        return {"session_id": "s1", "raw_message": list(segments)}

    async def scenario():
        env.plugin._remember_inbound("s1", _inbound())
        await _decide(env.plugin)
        env.plugin._schedule_append(sent_message({"type": "emoji", "data": ""}))  # 圖本身送出不算回覆
        assert "s1" not in env.plugin._append_tasks
        env.plugin._schedule_append(sent_message({"type": "text", "data": "沒事吧"}))
        env.plugin._schedule_append(sent_message({"type": "text", "data": "要不要抱抱"}))  # 回覆拆成兩則，等最後一則
        await _drain()

    asyncio.run(scenario())
    assert env.ctx.sent == [("emoji", env.ctx.library[1]["base64"], "s1")], "照「心疼＋她的回覆」挑到抱抱那張"
    assert env.plugin._decision_stats.state["sends"][0]["kind"] == "append"
    assert "s1" not in env.plugin._pending_append


# ===== planner 自己用的 discord_emote 工具（決策模型沒開時）=====


def test_tool_picks_by_vector_without_any_llm_and_falls_back_when_embedding_fails(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False})

    async def emote(**kwargs):
        return await env.plugin.handle_discord_emote(msg_id="m1", group_id="10", stream_id="s1", **kwargs)

    async def scenario():
        assert (await emote(feeling="讚同", form="reaction"))["success"] is True
        assert (await emote(feeling="揮手", form="sticker"))["success"] is True
        env.ctx.embed_fail = True
        assert (await emote(feeling="心疼", form="sticker"))["success"] is True
        assert "form 只能是" in (await emote(form="gif"))["error"]

    asyncio.run(scenario())
    assert env.reactions == [env.emojis[1]], "讚同和沒收進表情庫的 thumbsup 最接近（只能比名稱）"
    wave = env.ctx.sent[0]
    assert wave[0] == "emoji" and base64.b64decode(wave[1]).startswith(b"sticker-https://s/70.png"), "沒進表情庫的貼圖照說明比，選中才下載"
    assert env.plugin._native_emoji.lookup(wave[1]) == ("sticker", 70), "下載後登記，送出時換回原生貼圖"
    assert env.ctx.sent[1][1] == env.ctx.library[1]["base64"], "embedding 掛了改用 MaiBot 照情緒標籤挑"


# ===== 「DC生活 → 決策模型」頁面 =====


def test_decision_page_shows_scenes_and_sends(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", 0.9)])

    async def scenario():
        env.plugin._remember_inbound("s1", _inbound())
        await _decide(env.plugin)
        await _drain()
        return await env.plugin.webui_decision()

    page = asyncio.run(scenario())
    assert page["notice"] == "" and page["today_calls"] == 1
    assert "只按表情 1" in page["today_scenes"] and page["today_sends"] == "送出 1・沒送成 0"
    assert page["decisions"][0]["chat"] == "聊天大廳" and page["decisions"][0]["emotion"] == "好笑"
    assert page["sends"][0]["kind"] == "按表情" and page["sends"][0]["result"] == "送出"
    assert page["daily"][0]["emotes"] == 1 and page["daily"][0]["sends"] == 1


def test_decision_test_button_reports_the_failure_reason(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_sticker")])
    assert asyncio.run(env.plugin.webui_decision_test())["ok"]

    async def unauthorized(config, state, questions, logger, **kwargs):
        logger.warning("決策模型回傳 HTTP 401：invalid key")
        return None

    monkeypatch.setattr(env.module, "ask_decision", unauthorized)
    with pytest.raises(ValueError, match="401"):
        asyncio.run(env.plugin.webui_decision_test())
    assert env.plugin._decision_stats.state["last_test"]["ok"] is False
