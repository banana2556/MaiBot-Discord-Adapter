"""表情：planner 啟動時決策模型判斷情境與表態對象，向量挑表情送出，不經過 LLM。"""

from datetime import datetime
from types import SimpleNamespace
import asyncio
import base64

import discord
import pytest

from test_typing_indicator import _load_adapter_module, _load_plugin_class

# 假的 embedding：每個關鍵字一個維度，文字含這個詞那一維就是 1
KEYWORDS = ("好笑", "心疼", "讚同", "揮手")
# planner 這輪原本有的工具；MaiBot 原生的 send_emoji 會被拿掉（discord_emote 已經能發表情包），其餘一個都不能少
ALL_TOOLS = ["reply", "discord_emote", "send_emoji", "stop"]
KEPT_TOOLS = ["reply", "discord_emote", "stop"]
# 三則別人的訊息（Discord 訊息 ID），planner 上下文裡顯示的是 MaiBot 的別名
FUNNY, LAUGH, LATEST = "1567778765570965517", "1567798499012247615", "1567798613130739872"


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
        self.send = SimpleNamespace(emoji=self._sender("emoji"), text=self._sender("text"))
        self.emoji = SimpleNamespace(get_by_description=self._by_description)
        self.chat = SimpleNamespace(get_all_streams=self._streams)
        # MaiBot 的 bot.nickname / bot.alias_names（SDK 會把 {"value": …} 拆開）
        self.config = SimpleNamespace(get=self._config_get)

    async def _config_get(self, key, default=None):
        return {"bot.nickname": "麥麥", "bot.alias_names": ["C醬", "小C"]}.get(key, default)

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
    messages = {}
    for message_id, text in ((FUNNY, "我剛剛跌倒了哈哈哈"), (LAUGH, "笑死"), (LATEST, "真的假的")):

        async def add_reaction(emoji, message_id=message_id):
            reactions.append((message_id, emoji))

        messages[message_id] = SimpleNamespace(content=text, guild=guild, add_reaction=add_reaction)

    async def find_message(msg_id, group_id, user_id):
        return messages.get(msg_id)

    async def fetch(url, size=48):
        return _b64(b"sticker-" + url.encode())

    plugin._native_emoji = native_module.NativeEmojiRegistry()
    plugin._native_emoji.remember(ctx.library[0]["base64"], "emoji", 1)  # 表情庫第一張就是伺服器的 kekw
    plugin._emote_library = library_module.EmoteLibrary(ctx, None, lambda: plugin._native_emoji)
    cached = [SimpleNamespace(id=int(FUNNY), reactions=[SimpleNamespace(emoji="😂", count=3, me=False)])]
    plugin._client_manager = SimpleNamespace(
        client=SimpleNamespace(
            user=SimpleNamespace(display_name="麥麥"),
            get_channel=lambda cid: SimpleNamespace(guild=guild),
            guilds=[guild],
            cached_messages=cached,
        ),
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
    # 抽籤固定取第一名（最接近的），結果才穩定
    monkeypatch.setattr(emote_module.random, "choice", lambda items: items[0])
    monkeypatch.setattr(emote_module.random, "choices", lambda population, weights=None, k=1: [population[0]])
    return SimpleNamespace(plugin=plugin, ctx=ctx, module=emote_module, reactions=reactions, asked=asked, emojis=emojis)


def _answer(scene, probability=0.9, emotion="好笑", target="1"):
    return {
        "scene": {"type": "choice", "choice": scene, "probabilities": {scene: probability}},
        "target": {"type": "choice", "choice": target},
        "emotion": {"type": "choice", "choice": emotion},
    }


def _inbound(message_id, mentions_bot=False, group=True, text=""):
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
        "processed_plain_text": text,
    }


def _receive_all(plugin, mentions_bot=False):
    """群裡依序有人說了三句：好笑的、接話的、最新的。"""
    for message_id in (FUNNY, LAUGH, LATEST):
        plugin._remember_inbound("s1", _inbound(message_id, mentions_bot=mentions_bot and message_id == LATEST))


def _message_item(msg_id, user, text, self_message=False):
    flag = ' is_self_message="true"' if self_message else ""
    head = f'<message msg_id="{msg_id}" time="15:11:30" user="{user}" group_card="「{user}」"{flag}>\n'
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": head + text}]}


def _planner(emote_module, tools=("reply", "discord_emote", "send_emoji", "stop")):
    """planner 這輪看到的上下文：系統提示詞，加上她說過的一句和三則別人的訊息。"""
    alias = emote_module.display_message_id
    return {
        "items": [
            {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
            _message_item("mself", "麥麥", "大家在幹嘛", self_message=True),
            _message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈"),
            _message_item(alias(LAUGH), "阿華", "笑死"),
            _message_item(alias(LATEST), "小美", "真的假的"),
        ],
        "tool_definitions": [{"type": "function", "function": {"name": name}} for name in tools],
        "session_id": "s1",
    }


async def _decide(env):
    """跑一次 planner hook，回傳 MaiBot 實際會用的工具名稱與系統提示詞（hook 收到的是 kwargs 的副本）。"""
    kwargs = _planner(env.module)
    result = await env.plugin.decide_emote_in_planner(**kwargs)
    used = result.get("modified_kwargs") or kwargs
    return [tool["function"]["name"] for tool in used["tool_definitions"]], used["items"][0]["parts"][-1]["text"]


async def _drain():
    for _ in range(30):
        await asyncio.sleep(0)


# ===== planner 上下文 =====


def test_message_ids_match_maibot_aliases_and_planner_items_are_parsed() -> None:
    emote = _load_adapter_module("src.emote")
    # 用 MaiBot maisaka/context/message_id_alias.py 算出來的值釘住
    assert emote.display_message_id(FUNNY) == "mwjoitk" and emote.display_message_id(LAUGH) == "mhnn2mf"
    assert emote.display_message_id("m1") == "m1", "12 字元以內原樣顯示"
    item = _message_item("mwjoitk", "A&amp;B", "第一行\n第二行")
    item["parts"].append({"type": "image", "image_base64": "", "image_format": "png"})
    parsed = emote.planner_messages([{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "人設"}]}, item])
    assert parsed == [{"msg_id": "mwjoitk", "user": "「A&B」", "self": False, "text": "第一行 第二行 [圖片]"}]


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


def test_background_warm_up_embeds_server_emotes_before_the_first_pick(monkeypatch) -> None:
    env = _env(monkeypatch)

    async def run():
        await env.plugin.warm_emote_vectors()
        assert {"thumbsup", "wave：揮手打招呼", "笑到翻過去的貓，好笑"} <= set(env.ctx.embedded)
        before = len(env.ctx.embedded)
        await env.plugin._pick_custom_emoji(SimpleNamespace(emojis=env.emojis, stickers=[]), "讚同：好", "s1")
        assert env.ctx.embedded[before:] == ["讚同：好"], "挑的時候只算這次的查詢"

    asyncio.run(run())


# ===== planner 啟動時的情境判斷 =====


def test_without_decision_model_planner_keeps_tool_and_random_nudge(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False}, chat={"emote_chance": 1})
    kwargs = _planner(env.module)
    asyncio.run(env.plugin.decide_emote_in_planner(**kwargs))
    assert "discord_emote" in kwargs["items"][0]["parts"][-1]["text"]
    assert asyncio.run(env.plugin.decide_emote_in_planner(**_planner(env.module, tools=("reply", "stop")))) == {
        "action": "continue"
    }, "不是 Discord 聊天（或睡覺只剩 stop）不管"


def test_decision_runs_once_per_new_message_and_never_touches_planner_tools(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("text", 0.8), None])

    async def scenario():
        _receive_all(env.plugin)
        first = await _decide(env)
        second = await _decide(env)  # 同一回合後面的請求不再問
        _receive_all(env.plugin)
        failed = await _decide(env)
        return first, second, failed

    first, second, failed = asyncio.run(scenario())
    # 拿掉工具時 planner 會把回覆直接寫進自己的正文，打亂 reply / replyer 的流程
    assert first[0] == second[0] == failed[0] == KEPT_TOOLS
    assert first[1] == "人設", "只要文字：不加提示"
    assert len(env.asked) == 2, "每次 planner 啟動只問一次"
    state, questions = env.asked[0]
    assert "你是「麥麥」" in state and "沒有人 @ 你" in state and "（你）：大家在幹嘛" in state
    assert "#1 「「小明」」：我剛剛跌倒了哈哈哈（已有人按 😂×3）" in state, "別人已經按的表情是線索"
    assert set(questions["target"]["criteria"]) == {"1", "2", "3"}, "她自己說的話不能當表態對象"


def test_quick_reaction_goes_to_the_funny_message_not_the_latest(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", 0.9, "好笑", target="1")])

    async def scenario():
        _receive_all(env.plugin)
        decided = await _decide(env)
        await _drain()
        return decided

    names, prompt = asyncio.run(scenario())
    assert names == KEPT_TOOLS and "回應了「「小明」」那則訊息" in prompt and "直接呼叫 stop() 結束" in prompt
    assert env.reactions == [(FUNNY, env.emojis[0])], "按在被大家笑的那則，kekw 的描述是好笑"
    stats = env.plugin._decision_stats.state
    assert stats["decisions"][0]["scene"] == "quick_reaction" and "跌倒" in stats["decisions"][0]["message"]


def test_sticker_for_an_older_message_becomes_a_reaction_on_it(monkeypatch) -> None:
    env = _env(
        monkeypatch,
        answers=[_answer("quick_sticker", 0.9, "好笑", target="1"), _answer("quick_sticker", 0.9, "揮手", target="3")],
    )

    async def scenario():
        _receive_all(env.plugin)
        older, _ = await _decide(env)
        await _drain()
        _receive_all(env.plugin)
        recent, _ = await _decide(env)
        await _drain()
        return older, recent

    older, recent = asyncio.run(scenario())
    assert older == recent == KEPT_TOOLS
    assert env.reactions == [(FUNNY, env.emojis[0])], "發圖不能指定回哪一則，對象太早就改按表情"
    assert len(env.ctx.sent) == 1 and env.ctx.sent[0][0] == "emoji", "對象是最近的，直接發圖"


def test_asked_to_react_she_reacts_and_can_still_talk(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", 0.9, "讚同", target="3"), _answer("quick_reaction", 0.3)])

    async def scenario():
        _receive_all(env.plugin, mentions_bot=True)  # 「@麥麥 幫這則按個讚」
        addressed = await _decide(env)
        await _drain()
        _receive_all(env.plugin)
        await _decide(env)
        await _drain()
        return addressed

    (names, prompt) = asyncio.run(scenario())
    assert names == KEPT_TOOLS and "有人在跟你說話，需要的話照常回覆" in prompt and "stop()" not in prompt, (
        "表情照按，planner 照常可以說話"
    )
    assert env.reactions == [(LATEST, env.emojis[1])]
    scenes = [item["scene"] for item in env.plugin._decision_stats.state["decisions"]]
    assert scenes == ["text", "quick_reaction"], "新的在前：第二次沒過門檻當成只要文字"


def test_planner_decides_alone_when_no_message_can_be_traced_back(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction")])
    env.plugin._emote_unjudged.add("s1")  # 有新訊息，但都是插件啟動前收到的，換不回 Discord 訊息
    names, _ = asyncio.run(_decide(env))
    assert "discord_emote" in names and env.asked == []


def test_single_candidate_skips_the_target_question(monkeypatch) -> None:
    answer = _answer("quick_reaction", 0.9, "讚同")
    del answer["target"]  # 沒問就不會有這個答案
    env = _env(monkeypatch, answers=[answer])

    async def scenario():
        env.plugin._remember_inbound("s1", _inbound(LATEST))  # 剛重啟：只記得啟動後收到的這一則
        await _decide(env)
        await _drain()

    asyncio.run(scenario())
    assert set(env.asked[0][1]) == {"scene", "emotion"}, "只有一個選項的選擇題會被決策模型回 422"
    assert env.reactions == [(LATEST, env.emojis[1])], "表態對象就是唯一那則"


def test_text_emote_appends_a_picture_after_her_whole_reply(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("text_emote", 0.8, "心疼")])
    monkeypatch.setattr(env.module, "APPEND_DELAY_SECONDS", 0)

    def sent_message(*segments):
        return {"session_id": "s1", "raw_message": list(segments)}

    async def scenario():
        _receive_all(env.plugin)
        names, prompt = await _decide(env)
        assert names == KEPT_TOOLS and "會自動附上一張" in prompt
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
        return await env.plugin.handle_discord_emote(msg_id=FUNNY, group_id="10", stream_id="s1", **kwargs)

    async def scenario():
        assert (await emote(feeling="讚同", form="reaction"))["success"] is True
        assert (await emote(feeling="揮手", form="sticker"))["success"] is True
        env.ctx.embed_fail = True
        assert (await emote(feeling="心疼", form="sticker"))["success"] is True
        assert "form 只能是" in (await emote(form="gif"))["error"]

    asyncio.run(scenario())
    assert env.reactions == [(FUNNY, env.emojis[1])], "讚同和沒收進表情庫的 thumbsup 最接近（只能比名稱）"
    wave = env.ctx.sent[0]
    assert wave[0] == "emoji" and base64.b64decode(wave[1]).startswith(b"sticker-https://s/70.png"), "沒進表情庫的貼圖照說明比，選中才下載"
    assert env.plugin._native_emoji.lookup(wave[1]) == ("sticker", 70), "下載後登記，送出時換回原生貼圖"
    assert env.ctx.sent[1][1] == env.ctx.library[1]["base64"], "embedding 掛了改用 MaiBot 照情緒標籤挑"


# ===== 「DC生活 → 決策模型」頁面 =====


def test_decision_page_shows_scenes_and_sends(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", 0.9)])

    async def scenario():
        _receive_all(env.plugin)
        await _decide(env)
        await _drain()
        return await env.plugin.webui_decision()

    page = asyncio.run(scenario())
    assert page["notice"] == "" and page["today_calls"] == 1
    assert "只按表情 1" in page["today_scenes"] and page["today_sends"] == "送出 1・沒送成 0"
    assert page["decisions"][0]["chat"] == "聊天大廳" and page["decisions"][0]["emotion"] == "好笑"
    assert page["sends"][0]["kind"] == "按表情" and page["sends"][0]["result"] == "送出"
    assert page["daily"][0]["emotes"] == 1 and page["daily"][0]["sends"] == 1


def test_decision_page_lists_only_recent_records(monkeypatch) -> None:
    env = _env(monkeypatch)
    for index in range(15):
        env.plugin._decision_stats.record_decision(datetime.now(), "s1", f"第 {index} 則", "text", 100)
    page = asyncio.run(env.plugin.webui_decision())
    assert len(page["decisions"]) == 10 and page["decisions"][0]["message"] == "第 14 則", "只列最近 10 筆，新的在前"
    assert page["today_calls"] == 15, "統計照算全部"


def test_prompts_and_emotions_edited_on_the_page_go_to_the_decision_model(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("text", emotion="傻眼")])
    plugin = env.plugin
    plugin._page_settings = {}
    plugin._page_settings_path = None

    asyncio.run(plugin.webui_decision_save(scene_instructions=" 你要怎麼回？ ", scene_text="   ", context_messages="3"))
    asyncio.run(plugin.webui_decision_save_emotion("傻眼", "傻眼、無言"))
    asyncio.run(plugin.webui_decision_save_emotion("好笑", "笑到流淚"))
    asyncio.run(plugin.webui_decision_remove_emotion("開心"))

    async def scenario():
        _receive_all(plugin)
        await _decide(env)
        await _drain()

    asyncio.run(scenario())
    state, questions = env.asked[0]
    assert questions["scene"]["instructions"] == "你要怎麼回？"
    assert questions["scene"]["criteria"]["text"].startswith("用文字參與"), "留空的不會清掉選項說明"
    emotions = questions["emotion"]["criteria"]
    assert emotions["好笑"] == "笑到流淚" and emotions["傻眼"] == "傻眼、無言" and "開心" not in emotions
    assert list(emotions)[-1] == "傻眼", "新的加在最後"
    assert "大家在幹嘛" not in state and "#3" in state, "只送最後 3 則，她自己說的那句被擠掉"
    assert plugin._decision_stats.state["decisions"][0]["emotion"] == "傻眼"

    page = asyncio.run(plugin.webui_decision())
    assert {"item": "情境題提示詞", "value": "你要怎麼回？"} in page["prompts"]
    assert {"item": "送出的聊天則數", "value": "3"} in page["settings"]
    assert {"name": "傻眼", "description": "傻眼、無言"} in page["emotions"]

    for name in list(plugin._load_settings().decision.emotion_options())[:-2]:
        asyncio.run(plugin.webui_decision_remove_emotion(name))
    with pytest.raises(ValueError, match="至少要留 2 個"):
        asyncio.run(plugin.webui_decision_remove_emotion("傻眼"))
    with pytest.raises(ValueError):
        asyncio.run(plugin.webui_decision_save_emotion("好：笑"))

    asyncio.run(plugin.webui_decision_reset_prompts())
    decision = plugin._load_settings().decision
    assert len(decision.emotion_options()) == 12 and decision.context_messages == 15
    assert decision.scene_instructions.startswith("看最近的群聊")


def test_decision_test_button_reports_the_failure_reason(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_sticker", target="2")])
    result = asyncio.run(env.plugin.webui_decision_test())
    assert result["ok"] and "對象 #2" in result["detail"]

    async def unauthorized(config, state, questions, logger, **kwargs):
        logger.warning("決策模型回傳 HTTP 401：invalid key")
        return None

    monkeypatch.setattr(env.module, "ask_decision", unauthorized)
    with pytest.raises(ValueError, match="401"):
        asyncio.run(env.plugin.webui_decision_test())
    assert env.plugin._decision_stats.state["last_test"]["ok"] is False


def test_recent_picks_are_skipped_so_stickers_do_not_repeat(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False})
    env.ctx.library = [
        {"base64": _b64(f"funny-{index}".encode()), "description": f"好笑的圖 {index}", "emotion": "好笑"} for index in range(5)
    ]

    async def run():
        await env.plugin._emote_library.refresh(force=True)
        picks = [await env.plugin._pick_image(None, "好笑：哈哈", "好笑", "s1") for _ in range(3)]
        other_chat = await env.plugin._pick_image(None, "好笑：哈哈", "好笑", "s2")
        return [image for _, image, _ in picks], other_chat[1]

    images, other_chat = asyncio.run(run())
    assert len(set(images)) == 3, "同一個聊天最近發過的先跳過"
    assert other_chat == images[0], "別的聊天不受影響"


def test_calling_her_by_alias_counts_like_maibots_own_mention_check(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", 0.9, "開心", target="3")])

    async def scenario():
        await env.plugin.warm_emote_vectors()  # 背景輪詢時讀到 MaiBot 的暱稱和別名
        for message_id in (FUNNY, LAUGH):
            env.plugin._remember_inbound("s1", _inbound(message_id))
        env.plugin._remember_inbound("s1", _inbound(LATEST, text="C醬做點reaction"))  # 沒有 @，只叫別名
        decided = await _decide(env)
        await _drain()
        return decided

    names, prompt = asyncio.run(scenario())
    assert env.plugin._bot_names == ["麥麥", "C醬", "小C"]
    assert "上次之後有人 @ 你、回覆你或私訊你" in env.asked[0][0]
    assert names == KEPT_TOOLS and "有人在跟你說話，需要的話照常回覆" in prompt, "叫她別名也算在跟她說話，照常可以回"
    assert [message_id for message_id, _ in env.reactions] == [LATEST], "表情照樣按在她被叫的那則"


def test_only_real_discord_messages_can_be_reaction_targets(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_answer("quick_reaction", target="1")])
    reaction_event = "reaction_1567798613130739872_42_reaction_add_1730000000000"

    async def scenario():
        _receive_all(env.plugin)
        env.plugin._remember_inbound("s1", _inbound(reaction_event))  # 別人按表情也會變成一則進 MaiBot
        kwargs = _planner(env.module)
        kwargs["items"].append(_message_item(env.module.display_message_id(reaction_event), "阿華", "添加了 😂 表情"))
        await env.plugin.decide_emote_in_planner(**kwargs)
        await _drain()

    asyncio.run(scenario())
    criteria = env.asked[0][1]["target"]["criteria"]
    assert len(criteria) == 3 and all("添加了" not in text for text in criteria.values()), "reaction 事件不是訊息，按不了表情"


def test_send_emoji_is_hidden_wherever_discord_emote_exists(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False})
    result = asyncio.run(env.plugin.decide_emote_in_planner(**_planner(env.module)))
    names = [tool["function"]["name"] for tool in result["modified_kwargs"]["tool_definitions"]]
    assert names == KEPT_TOOLS, "決策模型沒開也一樣：發表情包只走 discord_emote"
    other_platform = _planner(env.module, tools=("reply", "send_emoji", "stop"))
    assert asyncio.run(env.plugin.decide_emote_in_planner(**other_platform)) == {"action": "continue"}, "別的平台不動"
