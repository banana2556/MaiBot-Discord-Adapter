"""聯想：回一則訊息前查群記憶與參與者的記憶，決策模型順便判斷看不看得懂、哪筆有關，看不懂才上網，整理給 planner 和 replyer。"""

import asyncio

from test_emote import FUNNY, LATEST, LAUGH, _answer, _env, _message_item
from test_typing_indicator import _load_adapter_module

recall = _load_adapter_module("src.recall")
alias = _load_adapter_module("src.emote").display_message_id

LOOKUP = {"success": True, "query": "羚羊 MP5 盾牌", "summary": "在聊《虹彩六號：圍攻》", "sources": ["https://r6.example"]}


def _inbound(message_id, user_id, at=None):
    raw = [{"type": "at", "data": {"target_user_id": at[0], "target_user_cardname": at[1]}}] if at else []
    info = {"additional_config": {}, "user_info": {"user_id": user_id}, "group_info": {"group_id": "10"}}
    return {"platform": "discord", "session_id": "s1", "message_id": message_id, "message_info": info, "raw_message": raw}


def _item(msg_id, user, text, quote=""):
    attrs = f'msg_id="{msg_id}"' + (f' quote="{quote}"' if quote else "")
    head = f'<message {attrs} time="15:11:30" user="{user}" group_card="「{user}」">\n'
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": head + text}]}


def _planner():
    """小明說累、阿華約打 R6、小美回覆阿華並 @ 小明：「羚羊拿MP5打盾牌」。"""
    return {
        "items": [
            {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
            _message_item("mself", "麥麥", "大家在幹嘛", self_message=True),
            _item(alias(FUNNY), "小明", "今天好累"),
            _item(alias(LAUGH), "阿華", "晚上要打R6嗎"),
            _item(alias(LATEST), "小美", "羚羊拿MP5打盾牌", quote=alias(LAUGH)),
        ],
        "tool_definitions": [{"type": "function", "function": {"name": name}} for name in ("reply", "discord_emote", "stop")],
        "session_id": "s1",
    }


def _setup(monkeypatch, answers, decision):
    env = _env(monkeypatch, answers=answers, decision=decision)
    for message_id, user_id, at in ((FUNNY, "u1", None), (LAUGH, "u2", None), (LATEST, "u3", ("u1", "小明"))):
        env.plugin._remember_inbound("s1", _inbound(message_id, user_id, at))
    calls = []
    original = env.ctx.call_capability

    async def call(name, **kwargs):
        calls.append((name, kwargs))
        if name == "person.get_id":
            return f"p-{kwargs['user_id']}"
        if name == "api.call":
            return LOOKUP
        if name == "llm.generate":
            return {"success": True, "response": "R6 羚羊 MP5 盾牌\n（多的行不要）"}
        if name != "knowledge.search":
            return await original(name, **kwargs)
        if not kwargs["person_id"]:
            return "你知道这些知识: 1. 群裡常打虹彩六號：圍攻\n2. 上週在聊顯卡"
        if kwargs["person_id"] == "p-u2":
            # 先用「名字＋這段文字」查整體印象，有了話題再用話題查
            return "你知道这些知识: 1. " + ("阿華主玩防守方" if kwargs["query"].startswith("阿華") else "阿華 R6 鑽石牌位")
        return f"你不太了解有关{kwargs['query']}的知识"

    env.ctx.call_capability = call
    return env, calls


def test_pieces() -> None:
    assert recall.parse_hits("你知道这些知识: 1. A 常玩 R6\n2. 上週在排位") == ["A 常玩 R6", "上週在排位"]
    assert recall.parse_hits("你不太了解有关x的知识") == [] and recall.parse_hits({"success": False}) == []
    window = [
        {"msg_id": "a", "user": "「A」", "self": False},
        {"msg_id": "b", "user": "「B」", "self": False},
        {"msg_id": "me", "user": "麥麥", "self": True},
        {"msg_id": "c", "user": "「C」", "self": False},
        {"msg_id": "d", "user": "「D」", "self": False},
    ]
    remembered = {
        "a": {"user_id": "1", "platform": "discord", "addressed": True},
        "b": {"user_id": "2", "platform": "discord"},
        "c": {"user_id": "3", "platform": "discord", "addressed": True},
        "d": {"user_id": "4", "platform": "discord", "mentioned": {"bot": "麥麥", "5": "E"}},
    }
    assert recall.focus_message(window, remembered)["msg_id"] == "c", "她說話之後叫她的優先"
    assert recall.focus_message(window, {})["msg_id"] == "d", "沒人叫她就是最新一則"
    people = recall.participants(window, window[4], ["a", "b"], remembered, "bot")
    assert [person["name"] for person in people] == ["D", "B", "A", "E"], "發話人、前文（新的先）、@ 的人；不含她自己"


def test_recall_reads_memories_asks_decision_looks_up_and_tells_planner(monkeypatch) -> None:
    answers = {
        **_answer("text", 0.8),
        "recall_understand": {"choice": "no", "probabilities": {"no": 0.7, "yes": 0.3}},
        "recall_pick": {"choice": "m1", "probabilities": {"m1": 0.6, "none": 0.3}},
    }
    env, calls = _setup(monkeypatch, [answers], {"recall_enabled": True, "lookup_enabled": True})
    result = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))

    state, questions = env.asked[0]
    assert "【聯想】#3 是你要回的那則" in state and "m1. 群裡常打虹彩六號：圍攻" in state
    assert "對「阿華」的印象：阿華主玩防守方" in state
    assert list(questions["recall_pick"]["criteria"]) == ["m1", "m2", "none"] and "recall_understand" in questions
    lookup = next(kwargs for name, kwargs in calls if name == "api.call")
    assert lookup["api_name"] == "luca.linux-computer.lookup"
    assert lookup["args"] == {
        "chat": "「小明」：今天好累\n「阿華」：晚上要打R6嗎\n「小美」：羚羊拿MP5打盾牌",
        "hint": "群裡常打虹彩六號：圍攻",
        "stream_id": "s1",
    }, "前文：回覆鏈和 @ 的人剛說的"
    keywords = next(kwargs for name, kwargs in calls if name == "llm.generate")
    assert "「小美」：羚羊拿MP5打盾牌" in keywords["prompt"] and keywords["task_name"] == "utils"
    searches = [kwargs for name, kwargs in calls if name == "knowledge.search"]
    assert all(kwargs["group_id"] == "10" and kwargs["chat_id"] == "s1" for kwargs in searches), "和 query_memory 一樣帶群號"
    searched = [(kwargs["person_id"], kwargs["query"]) for kwargs in searches]
    assert ("", "R6 羚羊 MP5 盾牌") in searched, "群記憶用小模型寫的關鍵字查，不用整段原文"
    assert ("p-u2", "阿華 R6 羚羊 MP5 盾牌") in searched, "整體印象：名字＋關鍵字"
    assert ("p-u2", "群裡常打虹彩六號：圍攻") in searched, "有了話題再用話題查"

    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    assert note == (
        f"【聯想】回 msg_id={alias(LATEST)}（小美）之前，你想起這些。這是聯想，不一定對；和眼前的聊天衝突時以聊天為準，也別逐字說出來：\n"
        "- 上網查到：在聊《虹彩六號：圍攻》（來源：https://r6.example）\n"
        "- 可能在聊：群裡常打虹彩六號：圍攻（把握 0.60）\n"
        "- 「小美」：沒有相關的記憶\n"
        "- 「阿華」：阿華 R6 鑽石牌位；阿華主玩防守方\n"
        "- 「小明」：沒有相關的記憶"
    )
    summary = "看不懂 0.70・想到 m1（0.60）・人 1/3・上網「羚羊 MP5 盾牌」"
    assert env.plugin._decision_stats.state["decisions"][0]["recall"] == summary
    row = asyncio.run(env.plugin.webui_decision())["decisions"][0]
    assert row["recall"] == summary
    assert "#3 「小美」：羚羊拿MP5打盾牌" in row["context"], "詳情看得到 # 編號對應的訊息"
    assert row["recall_detail"].startswith("關鍵字：R6 羚羊 MP5 盾牌\n群記憶：\nm1. 群裡常打虹彩六號：圍攻")
    assert "「小美」：沒查到" in row["recall_detail"] and row["recall_detail"].endswith(note)

    # 同一回合後面的請求（不再問決策模型）也帶著；replyer 回的是那則才給
    later = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))
    assert later["modified_kwargs"]["items"][0]["parts"][-1]["text"] == note and len(env.asked) == 1
    replied = asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=LATEST, extra_prompt="前面"))
    assert replied["modified_kwargs"]["extra_prompt"] == f"前面\n{note}"
    assert asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=FUNNY)) == {"action": "continue"}


def test_unsure_recall_asks_her_to_hedge_and_lookup_only_when_enabled(monkeypatch) -> None:
    answers = {
        **_answer("text", 0.8),
        "recall_understand": {"choice": "no", "probabilities": {"no": 0.8, "yes": 0.2}},
        "recall_pick": {"choice": "none", "probabilities": {"none": 0.7}},
    }
    env, calls = _setup(monkeypatch, [answers], {"recall_enabled": True})
    result = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))
    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    assert "看不出是哪一筆：群裡常打虹彩六號：圍攻；上週在聊顯卡" in note and note.endswith(recall.UNSURE)
    assert not any(name == "api.call" for name, _ in calls), "沒開上網查"

    # 寫不出關鍵字時改用原文查
    failing, failing_calls = _setup(monkeypatch, [answers], {"recall_enabled": True})
    original = failing.ctx.call_capability

    async def no_keywords(name, **kwargs):
        if name == "llm.generate":
            failing_calls.append((name, kwargs))
            return {"success": False, "error": "沒有模型"}
        return await original(name, **kwargs)

    failing.ctx.call_capability = no_keywords
    asyncio.run(failing.plugin.decide_emote_in_planner(**_planner()))
    queries = [kwargs["query"] for name, kwargs in failing_calls if name == "knowledge.search" and not kwargs["person_id"]]
    assert queries[0] == "今天好累 晚上要打R6嗎 羚羊拿MP5打盾牌"

    off, off_calls = _setup(monkeypatch, [_answer("text", 0.8)], {})
    asyncio.run(off.plugin.decide_emote_in_planner(**_planner()))
    assert not any(name.startswith("recall_") for name in off.asked[0][1]) and not off_calls, "沒開聯想就不查也不問"
