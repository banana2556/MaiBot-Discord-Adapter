"""話題切分：規則級聯、嵌入相似度、LLM 整理，planner 插話題線索、replyer 說明話題，DC生活頁面。"""

from datetime import datetime
from types import SimpleNamespace
import asyncio
import json
import logging
import time

from test_typing_indicator import _load_adapter_module, _load_plugin_class

# 頁面照現在時間算哪些話題還活躍：最後一則（+2000 秒）在 100 秒前，前面那些已經冷掉
BASE = time.time() - 2100
# 被 @ 的那則用 Discord 的長 ID，planner 和 replyer 看到的是別名
CALLING = "1567799999999999106"
TOPICS = {"山": [1.0, 0.0, 0.0], "爬": [1.0, 0.0, 0.0], "卡": [0.0, 1.0, 0.0], "買": [0.0, 1.0, 0.0], "貴": [0.0, 1.0, 0.0]}


class FakeCtx:
    def __init__(self) -> None:
        self.embedded: list = []
        self.embed_fail = False
        self.ledger_reply = ""
        self.prompts: list = []
        self.logs: list = []
        self.logger = logging.getLogger("test_topic_threads")
        self.logger.warning = self.logs.append
        self.logger.info = self.logs.append
        self.llm = SimpleNamespace(generate=self._generate)
        self.chat = SimpleNamespace(get_all_streams=self._streams, get_stream_by_group_id=self._stream_by_group)

    async def call_capability(self, name, **kwargs):
        assert name == "llm.embed"
        self.embedded.extend(kwargs["texts"])
        if self.embed_fail:
            return {"success": False, "error": "沒有 embedding 模型"}
        vectors = [next((v for k, v in TOPICS.items() if k in text), [0.0, 0.0, 1.0]) for text in kwargs["texts"]]
        return {"success": True, "results": [{"embedding": vector} for vector in vectors]}

    async def _generate(self, prompt, task_name):
        assert task_name == "utils"
        self.prompts.append(prompt)
        return {"success": True, "response": self.ledger_reply}

    async def _streams(self, platform):
        return [{"stream_id": "s1", "group_name": "閒聊", "user_cardname": None, "user_nickname": "x"}]

    async def _stream_by_group(self, group_id, platform):
        # 頻道 10 是聊天 s1；頻道 20 還沒有人說過話（SDK 回 None）
        return {"stream_id": "s1"} if group_id == "10" else None


def _plugin(**threads):
    plugin = _load_plugin_class()()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0"},
            "threads": {"enabled": True, "ledger_enabled": False, "channel_list": ["10"], **threads},
        }
    )
    plugin._ctx = FakeCtx()
    return plugin


def _inbound(message_id, user, mention=""):
    raw = [{"type": "text", "data": "…"}]
    if mention:
        raw.insert(0, {"type": "at", "data": {"target_user_id": "u3", "target_user_cardname": mention}})
    return {
        "platform": "discord",
        "session_id": "s1",
        "message_id": message_id,
        "message_info": {"group_info": {"group_id": "10"}, "user_info": {"user_id": user}},
        "raw_message": raw,
    }


def _item(msg_id, user, text, offset, quote="", self_message=False):
    alias = _load_adapter_module("src.emote").display_message_id
    attrs = f'msg_id="{alias(msg_id)}"' + (f' quote="{alias(quote)}"' if quote else "")
    flag = ' is_self_message="true"' if self_message else ""
    head = f'<message {attrs} time="20:00:00" user="{user}"{flag}>\n'
    meta = {"item_id": msg_id, "logical_turn_id": None, "timestamp": datetime.fromtimestamp(BASE + offset).isoformat()}
    return {"item_type": "UserMessageItem", "meta": meta, "parts": [{"type": "text", "text": head + text}]}


CHAT = [
    ("101", "小明", "週末要不要去爬山", 0, ""),
    ("102", "阿花", "好啊去哪座山", 10, ""),
    ("103", "老王", "新顯卡價格又漲了", 20, ""),
    ("104", "阿強", "真的貴", 30, "103"),
    ("105", "小明", "哈哈", 40, ""),
    (CALLING, "阿花", "你要買哪張", 50, ""),
    ("107", "老王", "4090吧", 60, ""),
    ("108", "小明", "爬山的話要早點出發", 2000, ""),  # 冷場超過 15 分鐘
]


def _kwargs(rows=CHAT):
    items = [
        {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
        *(_item(*row) for row in rows),
        {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "現在請決定下一步"}]},
    ]
    return {"items": items, "tool_definitions": [{"function": {"name": "reply"}}], "session_id": "s1"}


def _planner(plugin, rows=CHAT):
    kwargs = _kwargs(rows)
    return asyncio.run(plugin.add_topic_threads_to_planner(**kwargs)), kwargs["items"]


def _setup(**threads):
    plugin = _plugin(**threads)
    plugin._remember_inbound("s1", _inbound("101", "u1"))
    plugin._remember_inbound("s1", _inbound(CALLING, "u2", mention="老王"))
    return plugin


def test_rules_split_interleaved_chat_and_planner_gets_the_threads() -> None:
    plugin = _setup()
    alias = _load_adapter_module("src.emote").display_message_id
    result, items = _planner(plugin)
    state = plugin._threads["s1"]
    rules = {msg_id: entry["rule"] for msg_id, entry in state.entries.items()}
    assert rules == {
        "101": "new", "102": "embed", "103": "new", "104": "quote",
        "105": "filler", alias(CALLING): "at", "107": "speaker", "108": "new",
    }, rules
    thread = {msg_id: entry["thread"] for msg_id, entry in state.entries.items()}
    assert thread["102"] == thread["101"] and thread["104"] == thread["107"] == thread["103"]
    assert thread["108"] not in (thread["101"], thread["103"]), "冷場後同樣的內容開新話題"
    assert "哈哈" not in plugin.ctx.embedded and len(plugin.ctx.embedded) == 7, "附和不算向量，一輪只算一次"

    # 線索插在最後那條提醒前面，最近活躍的話題在前
    assert result["action"] == "continue" and items[-1]["parts"][0]["text"] == "現在請決定下一步"
    note = items[-2]["parts"][0]["text"]
    assert note.startswith("【話題線索】") and items[-2]["meta"]["timestamp"]
    lines = note.split("\n")[1:]
    assert lines[0].startswith(f"- [{thread['108']}] 爬山的話要早點出發｜小明｜msg_id：108")
    assert f"｜老王、阿強、小明、阿花｜msg_id：103 104 105 {alias(CALLING)} 107" in note
    assert lines[-1].startswith(f"- [{thread['101']}] 週末要不要去爬山｜小明、阿花｜msg_id：101 102")

    # 同一輪後面的請求沿用，不重算
    _planner(plugin)
    assert len(plugin.ctx.embedded) == 7

    # replyer：要回的那則（長 ID）屬於哪個話題
    reply = asyncio.run(
        plugin.add_topic_thread_to_replyer(session_id="s1", reply_message_id=CALLING, extra_prompt="原本的要求")
    )
    prompt = reply["modified_kwargs"]["extra_prompt"]
    assert prompt.startswith("原本的要求\n你要回覆的這則屬於話題「新顯卡價格又漲了」（在聊的人：老王、阿強、小明、阿花）。")
    assert asyncio.run(plugin.add_topic_thread_to_replyer(session_id="s1", reply_message_id="nope")) == {"action": "continue"}


def test_her_own_messages_and_people_calling_her_join_the_right_thread() -> None:
    plugin = _plugin()
    plugin._remember_inbound("s1", _inbound("101", "u1"))
    calling = _inbound("103", "u2")
    calling["message_info"]["additional_config"] = {"discord_mentions_bot": True}
    plugin._remember_inbound("s1", calling)
    rows = [
        ("101", "小明", "週末要不要去爬山", 0, ""),
        ("102", "麥麥", "好啊我也想去", 10, "101"),
        ("103", "阿花", "你要買哪張顯卡", 20, ""),
    ]
    items = [_item(*row, self_message=row[1] == "麥麥") for row in rows]
    asyncio.run(plugin.add_topic_threads_to_planner(items=items, session_id="s1"))
    entries = plugin._threads["s1"].entries
    assert entries["102"]["rule"] == "quote" and entries["102"]["thread"] == entries["101"]["thread"]
    assert entries["103"]["rule"] == "at", "叫她的訊息接到她最近說話的話題"


def test_ledger_relabels_and_names_threads_and_survives_bad_replies() -> None:
    plugin = _setup(ledger_enabled=True, ledger_every=8)
    _planner(plugin, CHAT[:7])
    state = plugin._threads["s1"]
    assert state.ledger_task is None, "還不到 8 則"
    hiking, gpu = state.entries["101"]["thread"], state.entries["103"]["thread"]
    plugin.ctx.ledger_reply = "```json\n" + json.dumps(
        {
            "assign": {"1": hiking, "2": hiking, "3": gpu, "4": gpu, "5": hiking, "6": gpu, "7": gpu, "8": "new1"},
            "threads": {
                hiking: {"title": "週末爬山", "summary": "小明約阿花週末爬山"},
                gpu: {"title": "顯卡漲價", "summary": "老王和阿強在抱怨顯卡價格"},
                "new1": {"title": "出發時間", "summary": "小明提議早點出發"},
            },
        },
        ensure_ascii=False,
    ) + "\n```"

    async def eighth_message_starts_the_ledger():
        await plugin.add_topic_threads_to_planner(**_kwargs())
        assert state.pending == 0 and state.ledger_task is not None
        await state.ledger_task

    asyncio.run(eighth_message_starts_the_ledger())
    prompt = plugin.ctx.prompts[-1]
    assert "#4 [" in prompt and "→#3" in prompt and "阿強" in prompt
    assert state.entries["105"]["thread"] == hiking and state.entries["105"]["rule"] == "llm"
    assert state.entries["108"]["thread"] not in (hiking, gpu)
    assert state.threads[hiking]["title"] == "週末爬山" and state.ledger_at is not None

    _, items = _planner(plugin)
    note = items[-2]["parts"][0]["text"]
    assert "週末爬山" in note and "顯卡漲價" in note and "老王和阿強在抱怨顯卡價格" in note

    before = {msg_id: entry["thread"] for msg_id, entry in state.entries.items()}
    plugin.ctx.ledger_reply = "我不知道"
    window = _load_adapter_module("src.emote").planner_messages([_item(*row) for row in CHAT])
    asyncio.run(plugin._run_ledger("s1", state, [{k: v for k, v in m.items() if k != "head"} for m in window]))
    assert before == {msg_id: entry["thread"] for msg_id, entry in state.entries.items()}


def test_without_embeddings_rules_still_work_and_warn_once() -> None:
    plugin = _setup()
    plugin.ctx.embed_fail = True
    _planner(plugin, CHAT[:3])
    _planner(plugin, CHAT[:4])
    entries = plugin._threads["s1"].entries
    assert entries["102"]["rule"] == "new" and entries["104"]["rule"] == "quote"
    assert sum("沒有 embedding 模型" in str(line) for line in plugin.ctx.logs) == 1
    assert "算不出向量" in asyncio.run(plugin.webui_threads())["notice"]


def test_disabled_unlisted_private_or_single_thread_leaves_the_request_alone() -> None:
    plugin = _setup(enabled=False)
    assert _planner(plugin)[0] == {"action": "continue"} and plugin._threads == {}

    plugin = _setup(channel_list=["20"])
    assert _planner(plugin)[0] == {"action": "continue"} and plugin._threads == {}, "沒列進頻道列表的不切"

    plugin = _plugin()
    private = _inbound("101", "u1")
    del private["message_info"]["group_info"]
    plugin._remember_inbound("s1", private)
    assert _planner(plugin)[0] == {"action": "continue"}

    plugin = _setup()
    result, items = _planner(plugin, CHAT[:2])
    assert result == {"action": "continue"} and len(items) == 4, "只有一個話題不插線索"


def test_page_lists_threads_and_recent_assignments_and_saves_settings() -> None:
    plugin = _setup()
    _planner(plugin)
    page = asyncio.run(plugin.webui_threads())
    assert page["notice"] == "" and page["channel_count"] == 1 and page["threshold"] == "0.60"
    assert page["channels"] == [{"channel_id": "10", "chat": "閒聊", "status": "切分中：1 個活躍話題，記了 8 則"}]
    assert all(row["item"] != "頻道列表" for row in page["settings"]), "頻道另外列，不放在設定表格"
    assert [row["title"] for row in page["threads"]] == ["爬山的話要早點出發"], "冷場前的話題已經不活躍"
    newest = page["recent"][0]
    assert newest["chat"] == "閒聊" and newest["rule"] == "新話題" and newest["text"] == "爬山的話要早點出發"
    assert newest["score"] == "—", "冷場後沒有活躍話題可比"
    embedded = next(row for row in page["recent"] if row["msg_id"] == "102")
    assert embedded["rule"] == "語意相近" and embedded["score"] == "1.000"
    assert any(row["item"] == "相似度門檻" and row["value"] == "0.6" for row in page["settings"])

    saved = asyncio.run(plugin.webui_threads_save(similarity_threshold="0.75", ledger_every=""))
    assert saved == {"saved": ["similarity_threshold"]}
    assert plugin._load_settings().threads.similarity_threshold == 0.75


def test_channels_are_added_and_removed_on_the_page_with_their_status() -> None:
    plugin = _setup(channel_list=[])
    page = asyncio.run(plugin.webui_threads())
    assert page["notice"].startswith("還沒設定頻道") and page["channels"] == []
    assert _planner(plugin)[0] == {"action": "continue"}, "沒有頻道就不切"

    for bad, reason in (("abc", "一串數字"), ("10", None)):
        if reason is None:
            asyncio.run(plugin.webui_threads_add_channel(new_channel_id=" 10 "))
        try:
            asyncio.run(plugin.webui_threads_add_channel(new_channel_id=bad))
        except ValueError as exc:
            assert (reason or "已經在列表裡") in str(exc)
        else:
            raise AssertionError(bad)
    asyncio.run(plugin.webui_threads_add_channel(new_channel_id="20"))
    statuses = {row["channel_id"]: row["status"] for row in asyncio.run(plugin.webui_threads())["channels"]}
    assert statuses == {"10": "等待新訊息：有人說話、麥麥開始思考時就會切", "20": "還沒有人說話，MaiBot 不認得這個頻道"}

    _planner(plugin)
    assert "s1" in plugin._threads
    asyncio.run(plugin.webui_threads_remove_channel(channel_id="10"))
    assert plugin._load_settings().threads.channel_list == ["20"] and "s1" not in plugin._threads
    reply = asyncio.run(plugin.add_topic_thread_to_replyer(session_id="s1", reply_message_id=CALLING))
    assert reply == {"action": "continue"}, "移除後 replyer 也不再收到話題提示"
