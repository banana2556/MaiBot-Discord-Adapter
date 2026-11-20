"""planner 看到的聊天：標出每則回覆在回誰、列出叫她的訊息；只留焦點那串（keep_focus_items）。"""

import asyncio
from types import SimpleNamespace

from test_emote import FUNNY, _env, _inbound, _message_item
from test_typing_indicator import _load_adapter_module

# 兩小時前她說的話（早就不在 planner 上下文），和剛剛有人回覆那則的訊息
OLD, REPLY = "1567700000000000001", "1567799999999999999"
# 小明說的話，和阿華回覆小明的訊息
MING, PRAISE = "1567700000000000002", "1567799999999999998"
OLD_TIME = 1_790_000_000.0
FOUND = {"message_id": OLD, "timestamp": str(OLD_TIME)}


def _reply(message_id, quote, name, content, text, to_her):
    message = _inbound(message_id, mentions_bot=to_her)
    if to_her:
        message["message_info"]["additional_config"]["discord_replies_to_bot"] = True
    message["raw_message"] = [
        {
            "type": "reply",
            "data": {
                "target_message_id": quote,
                "target_message_content": content,
                "target_message_sender_nickname": name,
            },
        },
        {"type": "text", "data": text},
    ]
    return message


def _setup(monkeypatch, found=FOUND):
    env = _env(monkeypatch)
    calls = {"get_by_id": [], "readable": []}

    async def get_by_id(message_id, chat_id=""):
        calls["get_by_id"].append((message_id, chat_id))
        return found

    async def build_readable(messages, **kwargs):
        calls["readable"].append(kwargs)
        return "[昨天 20:00] 「小明」說：明天要不要去看展\n[昨天 20:01] 「麥麥」說：好啊，明天見"

    env.ctx.message = SimpleNamespace(get_by_id=get_by_id, build_readable=build_readable)
    env.plugin._remember_inbound("s1", _inbound(FUNNY))
    # 小美回覆她兩小時前說的話
    env.plugin._remember_inbound("s1", _reply(REPLY, OLD, "「麥麥」", "好啊，明天見", "真的嗎", to_her=True))
    return env, calls


def _planner(env, *chat, tools=()):
    """跑一次 planner hook；回傳 (結果, 系統提示詞後面加的字, 改過的聊天訊息文字)。"""
    alias = env.module.display_message_id
    chat = chat or (
        _message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈"),
        _message_item(alias(REPLY), "小美", "真的嗎"),
    )
    kwargs = {
        "items": [{"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]}, *chat],
        "tool_definitions": [{"type": "function", "function": {"name": name}} for name in tools],
        "session_id": "s1",
    }
    result = asyncio.run(env.plugin.keep_focus_thread(**kwargs))
    items = result.get("modified_kwargs", {}).get("items") or kwargs["items"]
    note = "".join(part["text"] for part in items[0]["parts"][1:])
    return result, note, [item["parts"][0]["text"].split("\n", 1)[-1] for item in items[1:] if "parts" in item]


def test_reply_between_others_is_labelled_so_she_does_not_take_it_as_hers(monkeypatch) -> None:
    # 阿華回覆小明「開金口很厲害」，沒主詞：她要看得出這是在跟小明說，不是在叫她
    env, calls = _setup(monkeypatch)
    env.plugin._remember_inbound(
        "s1", _reply(PRAISE, MING, "「小明」", "今天終於把報告交出去了", "開金口很厲害", False)
    )
    alias = env.module.display_message_id
    ming = _message_item(alias(MING), "小明", "今天終於把報告交出去了")
    praise = _message_item(alias(PRAISE), "阿華", "開金口很厲害")
    _, note, texts = _planner(env, ming, praise, tools=("reply", "stop"))
    assert texts == ["今天終於把報告交出去了", "[回覆「小明」：今天終於把報告交出去了]開金口很厲害"]
    assert note == "" and calls["get_by_id"] == [], "被回覆那則還在上下文、也沒人叫她：不加系統提示"

    reply = asyncio.run(env.plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=PRAISE))
    assert reply["modified_kwargs"]["extra_prompt"] == "你要回覆的這則訊息是在回覆「小明」：今天終於把報告交出去了。"


def test_planner_is_told_who_is_calling_her_until_she_answers(monkeypatch) -> None:
    # 小美回覆了她，之後小明又在聊別的：planner 要知道這輪是為小美開的，別只接最新的話題
    env, calls = _setup(monkeypatch)
    alias = env.module.display_message_id
    mine = _message_item(alias(OLD), "麥麥", "好啊，明天見", self_message=True)
    replying = _message_item(alias(REPLY), "小美", "真的嗎")
    chatting = _message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈")

    _, note, _ = _planner(env, mine, replying, chatting, tools=("reply", "stop"))
    assert note.startswith("【有人在找你】")
    assert f"msg_id={alias(REPLY)}（小美）：[回覆你：好啊，明天見]真的嗎" in note
    assert "跌倒" not in note and "【被回覆的舊訊息】" not in note and calls["get_by_id"] == [], "被回覆的舊訊息不再另外補"

    # 她回過話之後就不再提；睡覺時段只剩 stop 也不提
    answered = _message_item("mself", "麥麥", "對啊", self_message=True)
    assert _planner(env, mine, replying, answered, chatting, tools=("reply", "stop"))[1] == ""
    assert _planner(env, mine, replying, chatting, tools=("stop",))[1] == ""


def test_nothing_remembered_leaves_the_request_alone(monkeypatch) -> None:
    env, _ = _setup(monkeypatch)
    env.plugin._inbound.clear()
    assert _planner(env)[0] == {"action": "continue"}


def test_keep_focus_items_drops_other_topics_but_not_newer_messages_or_tool_calls(monkeypatch) -> None:
    quote = _load_adapter_module("src.quote_context")
    system = {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]}
    a, b, c, d = (_message_item(f"m{n}", "x", "t") for n in "abcd")
    b["meta"] = {"item_id": "ib"}
    reminder = {"item_type": "UserMessageItem", "meta": {"item_id": "link-reminder-ib"}, "parts": [{"type": "text", "text": "提醒"}]}
    call = {"item_type": "FunctionCallItem", "meta": {}, "tool_call": {}}
    kept, dropped = quote.keep_focus_items([system, a, b, reminder, call, c, d], {"ma"}, {"ma", "mb", "mc"})
    assert kept == [system, a, call, d] and dropped == 2, "其他話題的 b、c 和 b 的連結提醒拿掉；分完話題之後才出現的 d 留著"


def _items(env, *chat, tools=("reply",)):
    """跑一次 planner hook，回傳送出去的 items。"""
    kwargs = {
        "items": [{"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]}, *chat],
        "tool_definitions": [{"type": "function", "function": {"name": name}} for name in tools],
        "session_id": "s1",
    }
    result = asyncio.run(env.plugin.keep_focus_thread(**kwargs))
    return result.get("modified_kwargs", {}).get("items") or kwargs["items"]


def _mark(text):
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": text}]}


def test_time_marks_dropped_except_now_and_older_messages_get_a_date(monkeypatch) -> None:
    # 2026-11-20 重啟後的 ping：前面恢復的訊息都不在這串，只剩一串「时间：…」空殼
    env, _ = _setup(monkeypatch)
    alias = env.module.display_message_id
    yesterday = _stamp(_message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈"), __import__("time").time() - 86400 * 2)
    items = _items(env, _mark("时间：2026-11-15 19:04:48"), _mark("时间：2026-11-18 12:44:06"), yesterday,
                   _mark("时间：2026-11-20 16:12:38"), _mark("请分析后决定下一步"))
    texts = [item["parts"][0]["text"] for item in items[1:]]
    assert [text for text in texts if text.startswith("时间：")] == ["时间：2026-11-20 16:12:38"], "只留現在時間"
    day = __import__("datetime").date.fromtimestamp(__import__("time").time() - 86400 * 2).strftime("%m-%d")
    assert f'time="{day} 15:11:30"' in texts[0], "不是今天的訊息開頭加上月日"


def test_threads_survive_a_restart(monkeypatch, tmp_path) -> None:
    env, _ = _setup(monkeypatch)
    topics = _load_adapter_module("src.topics")
    store = topics.new_store()
    tid = topics.add_member(store, None, {"msg_id": "mq", "user": "CiKa", "self": False, "time": 1.0}, "[08:00] 「CiKa」：嗨")
    env.plugin._topics["s1"], env.plugin._seen_until["s1"] = store, 5.0
    env.plugin._threads_path = tmp_path / "threads.json"
    env.plugin._save_threads()
    env.plugin._topics, env.plugin._seen_until = {}, {}
    env.plugin._load_threads(tmp_path / "threads.json")
    loaded = env.plugin._topics["s1"]
    assert loaded["of"] == {"mq": tid} and list(loaded["topics"]) == [tid] and env.plugin._seen_until == {"s1": 5.0}
    assert loaded["topics"][tid]["items"][0]["text"] == "嗨"


def _stamp(item, at):
    item["meta"] = {"timestamp": __import__("datetime").datetime.fromtimestamp(at).isoformat()}
    return item


def test_compacted_start_of_thread_subagent_thread_and_answered_calls(monkeypatch) -> None:
    # 2026-11-20 15:44 有人問 Irys、她說「我搜搜」並派子代理；後來 MaiBot 把那段壓成摘要，焦點又換到別串
    env, _ = _setup(monkeypatch)
    topics = _load_adapter_module("src.topics")
    alias = env.module.display_message_id
    plugin, store = env.plugin, topics.new_store()
    plugin._topics["s1"] = store
    t0 = 1_795_000_000.0
    irys = topics.add_member(store, None, {"msg_id": "mq", "user": "CiKa", "self": False, "time": t0}, "[15:44] 「CiKa」：C醬你知道Irys是誰嗎")
    topics.add_member(store, irys, {"msg_id": "ms", "user": "", "self": True, "time": t0 + 60}, "[15:45] （你）：我搜搜")
    other = topics.add_member(store, None, {"msg_id": alias(FUNNY), "user": "小明", "self": False, "time": t0 + 120}, "[15:46] 「小明」：我剛剛跌倒了哈哈哈")
    plugin._focus["s1"] = "mq"

    # 派子代理那輪記下焦點那串；之後焦點換到小明那串
    asyncio.run(plugin.remember_subagent_topic(session_id="s1", output_items=[{"tool_call": {"call_id": "c1", "func_name": "subagent"}}]))
    plugin._focus["s1"] = alias(FUNNY)
    funny = _stamp(_message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈"), t0 + 120)
    _, note, texts = _planner(env, funny, tools=("reply",))
    assert "Irys" not in note, "子代理還沒回來：只看焦點那串"

    # 結論送回：Irys 那串（已經被壓縮掉）補成前文，replyer 也拿得到
    output = {"item_type": "FunctionCallOutputItem", "call_id": "c1", "output": "子代理已在后台开始工作，任务 id：subagent-1。"}
    result = {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": '<subagent_result id="subagent-1" status="completed">'}]}
    items = _items(env, output, funny, result)
    texts = [part.get("text", "") for item in items for part in item.get("parts", [])]
    earlier = next(index for index, text in enumerate(texts) if text.startswith("【這串前面的對話】"))
    assert "「CiKa」：C醬你知道Irys是誰嗎" in texts[earlier] and "（你）：我搜搜" in texts[earlier]
    assert texts.index(next(t for t in texts if "我剛剛跌倒了" in t)) > earlier, "前文放在聊天紀錄最前面，不是系統提示詞"
    assert set(plugin._focus_keep("s1")) >= {"mq", "ms", alias(FUNNY)} and plugin._extra_topic["s1"] == irys != other
    reply = asyncio.run(plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=""))
    assert "C醬你知道Irys是誰嗎" in reply["modified_kwargs"]["extra_prompt"]

    # 叫她的訊息在她送出之前就到了：她送出時 replyer 已經看過，不再列成「有人在找你」
    calling = _stamp(_message_item(alias(REPLY), "小美", "真的嗎"), t0 + 200)
    assert "【有人在找你】" in _planner(env, calling, tools=("reply",))[1]
    plugin._seen_until["s1"] = t0 + 230
    assert "【有人在找你】" not in _planner(env, calling, tools=("reply",))[1]


def test_replyer_snapshot_leaves_late_messages_for_next_round_and_keeps_a_gap(monkeypatch) -> None:
    # 2026-11-20 15:50：replyer 寫了 44 秒，期間到的「搜好沒」被它順手回成「搜完了啦」
    env, _ = _setup(monkeypatch)
    topics = _load_adapter_module("src.topics")
    quote = _load_adapter_module("src.quote_context")
    alias = env.module.display_message_id
    plugin, store = env.plugin, topics.new_store()
    plugin._topics["s1"] = store
    tid = topics.add_member(store, None, {"msg_id": alias(FUNNY), "user": "小明", "self": False, "time": 1.0}, "「小明」：跌倒")
    topics.add_member(store, tid, {"msg_id": alias(REPLY), "user": "小美", "self": False, "time": 2.0}, "「小美」：真的嗎")
    plugin._focus["s1"] = alias(FUNNY)
    plugin._inbound["s1"][alias(REPLY)]["at"] = plugin._inbound["s1"][alias(FUNNY)]["at"] + 30
    plugin._planner_at["s1"] = plugin._inbound["s1"][alias(FUNNY)]["at"] + 1
    slept = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(quote.asyncio, "sleep", fake_sleep)
    reply = asyncio.run(plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=FUNNY))
    assert reply["modified_kwargs"]["reply_tool_args"]["_keep_message_ids"] == [alias(FUNNY)], "planner 決定後才到的不給 replyer"
    assert slept == [], "之前沒說過話，不用等"

    # 送出後：她看過的截到 planner 開始時，晚到的那則下一輪還算沒回；下一則回覆要等滿間隔
    plugin._join_focus({"session_id": "s1", "message_id": "her-1", "raw_message": [{"type": "text", "data": "哈哈"}]})
    assert plugin._seen_until["s1"] == plugin._planner_at["s1"]
    asyncio.run(plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=FUNNY))
    assert len(slept) == 1 and 0 < slept[0] <= quote.REPLY_GAP_SECONDS
