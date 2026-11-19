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
    return result, note, [item["parts"][0]["text"].split("\n", 1)[1] for item in items[1:]]


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
