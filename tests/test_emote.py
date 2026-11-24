"""話題分析的入口（分話題、選焦點）和 discord_emote 工具：向量挑表情送出，不經過 LLM。"""

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
        self.id, self.name, self.url = emoji_id, name, f"https://e/{emoji_id}.png"

    def is_usable(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


class FakeCtx:
    """MaiBot Host 的假上下文：表情庫三張、embedding 照關鍵字給向量、送出記下來。

    前兩張是收過的 Discord 表情與貼圖（kekw、hug），第三張是聊天裡收來的一般圖片，送出去會變成圖片附件。
    """

    def __init__(self) -> None:
        self.library = [
            {"base64": _b64(b"laugh-cat"), "description": "笑到翻過去的貓", "emotion": "好笑"},
            {"base64": _b64(b"hug"), "description": "抱抱安慰", "emotion": "心疼"},
            {"base64": _b64(b"stray-dog"), "description": "心疼心疼的狗狗", "emotion": "心疼"},
        ]
        self.embedded: list = []
        self.embed_fail = False
        self.sent: list = []
        self.logs: list = []
        self.logger = SimpleNamespace(
            info=self.logs.append, warning=self.logs.append, debug=lambda *a: None, exception=self.logs.append
        )
        self.sent_options: list = []
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
            self.sent_options.append(kwargs)
            return True

        return send

    async def _by_description(self, description, limit=5):
        return self.library[1] if description == "心疼" else None

    async def _streams(self, platform="all_platforms"):
        return [{"stream_id": "s1", "group_name": "聊天大廳", "user_cardname": None, "user_nickname": "x"}]


def _env(monkeypatch, answers=None, decision=None, chat=None):
    """搭一個能跑表情功能的插件：伺服器有兩個自訂表情、兩張貼圖；ask_decision 依序回 answers。"""
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
    stickers = [
        SimpleNamespace(
            id=70, name="wave", description="揮手打招呼", available=True, format=discord.StickerFormatType.png, url="https://s/70.png"
        ),
        SimpleNamespace(id=71, name="hug", description="", available=True, format=discord.StickerFormatType.png, url="https://s/71.png"),
    ]
    guild = SimpleNamespace(emojis=emojis, stickers=stickers)
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
    plugin._native_emoji.remember(ctx.library[1]["base64"], "sticker", 71)  # 第二張是貼圖 hug
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
    recalled: list = []

    async def fake_recall(decision, session_id, window, remembered, numbers, state, focus):
        recalled.append(focus["msg_id"])
        return "看得懂 0.90", "經過", {"topic": "串"}, {"understood": {"verdict": "yes", "p": 0.9}, "terms": []}

    # 聯想另外在 test_recall.py 測；這裡只確認分完話題後用焦點跑
    plugin._run_recall = fake_recall
    return SimpleNamespace(
        plugin=plugin,
        ctx=ctx,
        module=emote_module,
        reactions=reactions,
        asked=asked,
        emojis=emojis,
        guild=guild,
        recalled=recalled,
    )


def _topics(**choices):
    """決策模型對話題題目的回答：題目 ID -> 選項。"""
    return {question: {"type": "choice", "choice": choice} for question, choice in choices.items()}


# 她說的「大家在幹嘛」先自己開一串（t1）；#1、#2 同一串（t2），#3 另開（t3）
SPLIT = _topics(topic_1="none", topic_2="m1", topic_3="none")


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
    result = await env.plugin.analyze_in_planner(**kwargs)
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
    part, end = parsed[0].pop("head")
    assert parsed == [
        {"msg_id": "mwjoitk", "user": "A&B", "self": False, "text": "第一行 第二行 [圖片]", "quote": "", "time": None}
    ]
    assert part is item["parts"][0] and part["text"][end:] == "第一行\n第二行", "head 指向原本的 part，接在訊息頭後面"


# ===== 向量表情庫 =====


def test_library_keeps_only_discord_emotes_and_ranks_by_similarity() -> None:
    library_module = _load_adapter_module("src.emote_library")
    native_module = _load_adapter_module("src.native_emoji")
    ctx = FakeCtx()
    registry = native_module.NativeEmojiRegistry()
    registry.remember(ctx.library[0]["base64"], "emoji", 1)
    registry.remember(ctx.library[1]["base64"], "sticker", 71)
    library = library_module.EmoteLibrary(ctx, None, lambda: registry)

    async def run():
        await library.refresh(force=True)
        assert [entry["key"] for entry in library.entries] == ["emoji:1", "sticker:71"], "一般圖片送出去會變成附件，不留"
        assert library.text_for("emoji:1") == "笑到翻過去的貓，好笑" and library.image_for("sticker:71") == ctx.library[1]["base64"]
        assert ctx.embedded == [], "向量照「名字：描述」在挑圖時算，重抓表情庫不算"
        items = [("emoji:1", "kekw：笑到翻過去的貓，好笑"), ("sticker:71", "hug：抱抱安慰，心疼")]
        ranked = await library.rank("心疼：好可憐", items, top=1)
        assert ranked[0][0] == "sticker:71"
        embedded = len(ctx.embedded)
        await library.rank("心疼：好可憐", items, top=1)
        assert len(ctx.embedded) == embedded + 1, "文字沒變就不重算，只算查詢"
        ctx.embed_fail = True
        assert await library.rank("好笑", [("x", "新的描述")]) is None, "embedding 掛了回 None，呼叫端改用別的方法"

    asyncio.run(run())


def test_background_warm_up_embeds_server_emotes_before_the_first_pick(monkeypatch) -> None:
    env = _env(monkeypatch)

    async def run():
        await env.plugin.warm_emote_vectors()
        assert {"thumbsup", "wave：揮手打招呼", "kekw：笑到翻過去的貓，好笑", "hug：抱抱安慰，心疼"} <= set(env.ctx.embedded), (
            "名字放在描述前面，叫名字也找得到"
        )
        assert not any("狗狗" in text for text in env.ctx.embedded), "一般圖片不算向量"
        before = len(env.ctx.embedded)
        await env.plugin._pick_custom_emoji(SimpleNamespace(emojis=env.emojis, stickers=[]), "讚同：好", "s1")
        assert env.ctx.embedded[before:] == ["讚同：好"], "挑的時候只算這次的查詢"

    asyncio.run(run())


# ===== 話題分析：新訊息後第一次進 planner 時分話題、選焦點 =====


def test_without_decision_model_planner_keeps_tool_and_random_nudge(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False}, chat={"emote_chance": 1})
    kwargs = _planner(env.module)
    asyncio.run(env.plugin.analyze_in_planner(**kwargs))
    assert "discord_emote" in kwargs["items"][0]["parts"][-1]["text"]
    assert asyncio.run(env.plugin.analyze_in_planner(**_planner(env.module, tools=("reply", "stop")))) == {
        "action": "continue"
    }, "不是 Discord 聊天（或睡覺只剩 stop）不管"


def test_analysis_runs_once_per_new_message_and_never_touches_planner_tools(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[SPLIT])
    alias = env.module.display_message_id

    async def scenario():
        _receive_all(env.plugin)
        first = await _decide(env)
        second = await _decide(env)  # 同一回合後面的請求不再分析
        return first, second

    first, second = asyncio.run(scenario())
    # 拿掉工具時 planner 會把回覆直接寫進自己的正文，打亂 reply / replyer 的流程
    assert first[0] == second[0] == KEPT_TOOLS
    assert first[1] == "人設", "表情、文字都由 planner 自己決定，不再加情境提示"
    assert len(env.asked) == 1, "每次有新訊息只分析一次"
    state, questions = env.asked[0]
    assert "你是「麥麥」" in state and "（你）：大家在幹嘛" in state and "#1 「小明」：我剛剛跌倒了哈哈哈" in state
    assert set(questions) == {"topic_1", "topic_2", "topic_3"}, "只問話題，不再問情境、對象和情緒"
    store = env.plugin._topics["s1"]
    assert store["of"][alias(FUNNY)] == store["of"][alias(LAUGH)] != store["of"][alias(LATEST)]
    assert env.plugin._focus["s1"] == alias(LATEST), "沒人叫她：焦點是最新一則"
    assert env.recalled == [alias(LATEST)], "每輪都聯想，不管這輪會不會說話"
    record = env.plugin._decision_stats.state["decisions"][0]
    assert record["message"] == "「小美」：真的假的" and "#2→串2" in record["topics"] and not record["failed"]


def test_focus_thread_is_all_planner_selector_and_replyer_see(monkeypatch) -> None:
    """阿華在叫她：焦點是阿華那則所在的串（小明、阿華），小美另開的話題和她之前說的都不給看。"""
    env = _env(monkeypatch, answers=[SPLIT])
    alias = env.module.display_message_id
    for message_id in (FUNNY, LAUGH, LATEST):
        env.plugin._remember_inbound("s1", _inbound(message_id, mentions_bot=message_id == LAUGH))
    kwargs = _planner(env.module)
    kwargs["items"][4]["meta"] = {"item_id": "x3"}
    # linux 沙盒電腦插件替含連結的訊息插的提醒，跟著那則一起拿掉
    kwargs["items"].append({"item_type": "UserMessageItem", "meta": {"item_id": "link-reminder-x3"}, "parts": [{"type": "text", "text": "提醒"}]})

    async def scenario():
        await env.plugin.analyze_in_planner(**kwargs)
        planner = await env.plugin.keep_focus_thread(**kwargs)
        selector = await env.plugin.keep_focus_thread(**_planner(env.module, tools=()))
        no_target = await env.plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id="", reply_tool_args={"x": 1})
        other = await env.plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=LATEST)
        return planner["modified_kwargs"], selector["modified_kwargs"], no_target["modified_kwargs"], other["modified_kwargs"]

    planner, selector, no_target, other = asyncio.run(scenario())
    assert env.plugin._focus["s1"] == alias(LAUGH), "叫她的優先"
    assert [m["msg_id"] for m in env.module.planner_messages(planner["items"])] == [alias(FUNNY), alias(LAUGH)]
    assert all(item["meta"].get("item_id") != "link-reminder-x3" for item in planner["items"])
    assert "【有人在找你】" not in planner["items"][0]["parts"][-1]["text"], "焦點訊息已在聊天紀錄裡，勿重複提示"
    assert [m["msg_id"] for m in env.module.planner_messages(selector["items"])] == [alias(FUNNY), alias(LAUGH)], (
        "表達方式選擇也只看這串"
    )
    assert selector["items"][0]["parts"][-1]["text"] == "人設", "表達方式選擇沒有工具，不加提示"
    assert no_target["reply_tool_args"] == {"x": 1, "_keep_message_ids": [alias(FUNNY), alias(LAUGH)]}, "沒指定回哪則：焦點那串"
    assert other["reply_tool_args"]["_keep_message_ids"] == [alias(LATEST)], "回別串的那則：給那一串"

    # 她送出的話加進焦點那串；之後才收到、還沒分的訊息也留著
    env.plugin._join_focus({"session_id": "s1", "message_id": "her-1", "raw_message": [{"type": "text", "data": "笑死我"}]})
    env.plugin._remember_inbound("s1", _inbound("1567799999999999999"))
    keep = env.plugin._focus_keep("s1")
    assert keep == [alias(FUNNY), alias(LAUGH), "her-1"], "後到而未分配的訊息留給下一輪"
    assert env.plugin._topics["s1"]["topics"][2]["lines"][-1].endswith("（你）：笑死我")


def test_failed_topic_request_keeps_only_unassigned_messages(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[None])

    async def scenario():
        _receive_all(env.plugin)
        await _decide(env)
        kwargs = _planner(env.module)
        return await env.plugin.keep_focus_thread(**kwargs)

    result = asyncio.run(scenario())
    ids = [m["msg_id"] for m in env.module.planner_messages(result["modified_kwargs"]["items"])]
    alias_id = env.module.display_message_id
    assert ids == [alias_id(LATEST)], "模型失敗時只看待判定的焦點，不混入其他串"
    assert env.plugin._decision_stats.state["decisions"][0]["failed"] is True


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
    assert env.plugin._native_emoji.lookup(env.ctx.sent[1][1]) is not None, "embedding 掛了隨機挑一個原生的，不挑一般圖片"


# ===== 「DC生活 → 話題分析」頁面 =====


def test_topic_page_shows_analysis_and_sends(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[SPLIT])

    async def scenario():
        _receive_all(env.plugin)
        await _decide(env)
        await env.plugin.handle_discord_emote(msg_id=FUNNY, group_id="10", stream_id="s1", feeling="好笑")
        return await env.plugin.webui_decision()

    page = asyncio.run(scenario())
    assert page["notice"] == "" and page["today_calls"] == 1 and page["today_sends"] == "送出 1・沒送成 0"
    row = page["decisions"][0]
    assert row["chat"] == "聊天大廳" and row["message"] == "「小美」：真的假的" and "#1→串2" in row["topics"]
    assert "（你）：大家在幹嘛" in row["context"] and row["recall"] == "看得懂 0.90" and row["topic"] == "串"
    assert page["sends"][0]["kind"] == "planner 工具" and page["daily"][0]["sends"] == 1


def test_threads_page_shows_live_threads_as_bubbles_and_recent_analyses(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[SPLIT])
    alias = env.module.display_message_id

    async def scenario():
        _receive_all(env.plugin)
        await _decide(env)
        env.plugin._join_focus({"session_id": "s1", "message_id": "her-1", "raw_message": [{"type": "text", "data": "真的啦 https://x.com/a"}]})
        return await env.plugin.webui_threads()

    page = asyncio.run(scenario())
    chat = page["chats"][0]
    assert chat["name"] == "聊天大廳" and [thread["id"] for thread in chat["threads"]] == [3, 2, 1], "焦點那串在前，其餘新的在前"
    focus = chat["threads"][0]
    assert focus["focus"] and focus["count"] == 2 and focus["title"] == "真的假的"
    assert focus["messages"] == [
        {"who": "小美", "self": False, "text": "真的假的", "time": "--:--"},
        {"who": "你", "self": True, "text": "真的啦 （連結）", "time": focus["messages"][1]["time"]},
    ], "她送出的話也進這串，網址換成（連結）"
    assert chat["threads"][1]["messages"][0]["who"] == "小明" and not chat["threads"][1]["focus"]
    analysis = page["analyses"][0]
    assert analysis["message"] == "「小美」：真的假的" and analysis["understood"] == {"verdict": "yes", "p": 0.9}
    assert analysis["assigned"].startswith("#1→串2") and env.plugin._focus["s1"] == alias(LATEST)


def test_decision_page_lists_only_recent_records_and_reads_old_ones(monkeypatch) -> None:
    env = _env(monkeypatch)
    for index in range(15):
        env.plugin._decision_stats.record_decision(datetime.now(), "s1", f"第 {index} 則", 100)
    # 上一版的紀錄：有 scene、links，沒有 topics、failed
    env.plugin._decision_stats.state["decisions"].insert(
        0, {"time": "2026-11-18T10:00:00", "chat": "s1", "message": "舊", "scene": "failed", "latency_ms": 5, "links": "#1→串1"}
    )
    page = asyncio.run(env.plugin.webui_decision())
    assert len(page["decisions"]) == 10 and page["decisions"][1]["message"] == "第 14 則", "只列最近 10 筆，新的在前"
    assert page["decisions"][0]["result"] == "分話題失敗" and page["decisions"][0]["topics"] == "#1→串1"
    assert page["today_calls"] == 15, "統計照算全部"


def test_settings_edited_on_the_page_are_used(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_topics(topic_3="none")])
    plugin = env.plugin
    plugin._page_settings = {}
    plugin._page_settings_path = None
    asyncio.run(plugin.webui_decision_save(context_messages="3", model="  "))

    async def scenario():
        _receive_all(plugin)
        await _decide(env)

    asyncio.run(scenario())
    state, _ = env.asked[0]
    assert "大家在幹嘛" not in state and "#3" in state, "只看最後 3 則，她自己說的那句被擠掉"
    page = asyncio.run(plugin.webui_decision())
    assert page["current"]["context_messages"] == "3"
    assert plugin._load_settings().decision.model == "vllm-sr/d3-flash", "留空不改"


def test_decision_test_button_reports_the_failure_reason(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[_topics(topic_2="m1")])
    result = asyncio.run(env.plugin.webui_decision_test())
    assert result["ok"] and "在接 #1" in result["detail"]

    async def unauthorized(config, state, questions, logger, **kwargs):
        logger.warning("決策模型回傳 HTTP 401：invalid key")
        return None

    monkeypatch.setattr(env.module, "ask_decision", unauthorized)
    with pytest.raises(ValueError, match="401"):
        asyncio.run(env.plugin.webui_decision_test())
    assert env.plugin._decision_stats.state["last_test"]["ok"] is False


def test_recent_picks_are_skipped_so_stickers_do_not_repeat(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False})
    funny = [
        SimpleNamespace(
            id=80 + index,
            name=f"funny{index}",
            description=f"好笑的貼圖 {index}",
            available=True,
            format=discord.StickerFormatType.png,
            url=f"https://s/{80 + index}.png",
        )
        for index in range(5)
    ]
    guild = SimpleNamespace(emojis=[], stickers=funny)

    async def run():
        picks = [await env.plugin._pick_image(guild, "好笑：哈哈", "s1") for _ in range(3)]
        other_chat = await env.plugin._pick_image(guild, "好笑：哈哈", "s2")
        return [image for _, image, _ in picks], other_chat[1]

    images, other_chat = asyncio.run(run())
    assert len(set(images)) == 3, "同一個聊天最近發過的先跳過"
    assert other_chat == images[0], "別的聊天不受影響"


def test_calling_her_by_alias_counts_like_maibots_own_mention_check(monkeypatch) -> None:
    env = _env(monkeypatch, answers=[SPLIT])
    alias = env.module.display_message_id

    async def scenario():
        await env.plugin.warm_emote_vectors()  # 背景輪詢時讀到 MaiBot 的暱稱和別名
        env.plugin._remember_inbound("s1", _inbound(FUNNY))
        env.plugin._remember_inbound("s1", _inbound(LAUGH, text="C醬做點reaction"))  # 沒有 @，只叫別名
        env.plugin._remember_inbound("s1", _inbound(LATEST))
        await _decide(env)

    asyncio.run(scenario())
    assert env.plugin._bot_names == ["麥麥", "C醬", "小C"]
    assert env.plugin._focus["s1"] == alias(LAUGH), "叫她別名也算在叫她，焦點是那則"
    questions = env.asked[0][1]
    assert "在叫你" in questions["topic_2"]["instructions"] and "在叫你" not in questions["topic_3"]["instructions"], (
        "叫她的訊息提醒決策模型：常常在接她上次說話的那串"
    )


def test_send_emoji_is_hidden_wherever_discord_emote_exists(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"enabled": False})
    result = asyncio.run(env.plugin.analyze_in_planner(**_planner(env.module)))
    names = [tool["function"]["name"] for tool in result["modified_kwargs"]["tool_definitions"]]
    assert names == KEPT_TOOLS, "決策模型沒開也一樣：發表情包只走 discord_emote"
    other_platform = _planner(env.module, tools=("reply", "send_emoji", "stop"))
    assert asyncio.run(env.plugin.analyze_in_planner(**other_platform)) == {"action": "continue"}, "別的平台不動"


def test_custom_emoji_ids_and_their_images_are_hidden_from_the_models(monkeypatch) -> None:
    """表情 ID 和表情圖對判斷話題沒幫助：planner 和決策模型都看不到；貼圖留名字和說明；真的圖片留著。"""
    image = {"type": "image", "image_format": "png", "data": "x"}
    items = [
        {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設 <:keep:1>"}]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [
            {"type": "text", "text": '<message msg_id="m1" user="「A」">\n不太一樣 '},
            {"type": "text", "text": "<a:CFthinkge:1507125146987659506>"}, dict(image),
        ]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [
            {"type": "text", "text": '<message msg_id="m2" user="「B」">\n<:RenaWhy:827792040212299796>'}, dict(image),
        ]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [
            {"type": "text", "text": '<message msg_id="m3" user="「C」">\n[貼圖：wave（揮手）]'}, dict(image),
        ]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [
            {"type": "text", "text": '<message msg_id="m4" user="「D」">\n看這張 <:lol:5>'}, dict(image), dict(image),
        ]},
    ]
    emote_module = _load_adapter_module("src.emote")
    emote_module.strip_emotes(items)
    assert items[0]["parts"][0]["text"] == "人設 <:keep:1>", "只動聊天訊息"
    assert [part.get("text") for part in items[1]["parts"]] == ['<message msg_id="m1" user="「A」">\n不太一樣 ']
    assert [part.get("text") for part in items[2]["parts"]] == ['<message msg_id="m2" user="「B」">\n（表情）']
    assert [part.get("text") for part in items[3]["parts"]] == ['<message msg_id="m3" user="「C」">\n[貼圖：wave（揮手）]']
    assert [part["type"] for part in items[4]["parts"]] == ["text", "image"], "表情的圖拿掉，真的圖片留著"
    texts = [message["text"] for message in emote_module.planner_messages(items)]
    assert texts == ["不太一樣", "（表情）", "[貼圖：wave（揮手）]", "看這張 [圖片]"]

    # 決策模型那邊：只有表情的不分話題，用回覆功能回的訊息標出在回誰
    env = _env(monkeypatch, answers=[_topics(topic_1="none", topic_3="m1")])
    _receive_all(env.plugin)
    alias = emote_module.display_message_id
    env.plugin._inbound["s1"][alias(LATEST)]["quote_label"] = "回覆「阿華」：笑死"
    kwargs = _planner(env.module)
    kwargs["items"][3]["parts"] = [
        {"type": "text", "text": kwargs["items"][3]["parts"][0]["text"].split("\n")[0] + "\n<:lol:5>"}, dict(image),
    ]
    asyncio.run(env.plugin.analyze_in_planner(**kwargs))
    state, questions = env.asked[0]
    assert "#2 「阿華」：（表情）" in state and "<:lol:5>" not in state
    assert "#3 「小美」：[回覆「阿華」：笑死]真的假的" in state
    assert "topic_2" not in questions, "只有表情的不分話題"
