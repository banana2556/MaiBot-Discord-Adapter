"""聯想：分完話題後，用焦點那串查群記憶與參與者的記憶，決策模型判斷看不看得懂、哪筆有關，看不懂才上網，整理給 planner 和 replyer。"""

import asyncio

from test_emote import FUNNY, LATEST, LAUGH, _env, _message_item, _topics
from test_typing_indicator import _load_adapter_module

recall = _load_adapter_module("src.recall")
topics = _load_adapter_module("src.topics")
alias = _load_adapter_module("src.emote").display_message_id

# 她說的先自己一串（t1）；小明、阿華、小美（回覆阿華）同一串（t2）
SAME_THREAD = _topics(topic_1="none", topic_2="m1", topic_3="m2")
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
    del env.plugin._run_recall  # 用真的聯想
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


def _run(env, planner=None):
    """跑一輪 planner 的兩個 hook（分析、只留焦點那串）；回傳最後的 kwargs。"""
    kwargs = planner or _planner()

    async def run():
        await env.plugin.analyze_in_planner(**kwargs)
        return (await env.plugin.keep_focus_thread(**kwargs))["modified_kwargs"]

    return asyncio.run(run())


def test_pieces() -> None:
    assert recall.tidy("[2026-08-28 05:30] hinana: GTA6鎖30FPS<a:lol:5> [2026-08-28 05:40] LZ: https://ptt.cc/x ...") == (
        "2026-08-28 hinana：GTA6鎖30FPS／LZ：（連結）"
    ), "日期只留一個、拿掉表情 ID、網址換掉、截斷的 ... 拿掉"
    assert recall.tidy("x" * 200, 10) == "x" * 9 + "…"
    assert recall.parse_hits("你知道这些知识: 1. A 常玩 R6\n2. 上週在排位") == ["A 常玩 R6", "上週在排位"]
    assert recall.parse_hits("你不太了解有关x的知识") == [] and recall.parse_hits({"success": False}) == []
    window = [
        {"msg_id": "a", "user": "「A」", "self": False, "text": "x"},
        {"msg_id": "b", "user": "「B」", "self": False, "text": "x"},
        {"msg_id": "me", "user": "麥麥", "self": True, "text": "x"},
        {"msg_id": "c", "user": "「C」", "self": False, "text": "x"},
        {"msg_id": "d", "user": "「D」", "self": False, "text": "x"},
    ]
    remembered = {
        "a": {"user_id": "1", "platform": "discord", "addressed": True},
        "b": {"user_id": "2", "platform": "discord"},
        "c": {"user_id": "3", "platform": "discord", "addressed": True},
        "d": {"user_id": "4", "platform": "discord", "mentioned": {"bot": "麥麥", "5": "E"}},
    }
    assert topics.focus_message(window, remembered)["msg_id"] == "c", "她說話之後叫她的優先"
    assert topics.focus_message(window, {})["msg_id"] == "d", "沒人叫她就是最新一則"
    people = recall.participants(window, window[4], ["a", "b"], remembered, "bot")
    assert [person["name"] for person in people] == ["D", "E", "B"], "發話人、@ 的人、前文（新的先）；不含她自己，最多 3 個"

RECALL_NO_M1 = {
    "recall_understand": {"choice": "no", "probabilities": {"no": 0.7, "yes": 0.3}},
    "recall_pick": {"choice": "m1", "probabilities": {"m1": 0.6, "none": 0.3}},
}


def test_recall_reads_memories_asks_decision_looks_up_and_tells_planner(monkeypatch) -> None:
    env, calls = _setup(monkeypatch, [SAME_THREAD, RECALL_NO_M1], {"lookup_enabled": True})
    result = _run(env)

    # 第一次只分話題；分完用焦點那串聯想，第二次才問聯想
    assert len(env.asked) == 2 and not any(name.startswith("recall_") for name in env.asked[0][1])
    state, questions = env.asked[1]
    assert (
        "【聯想】#3 是你要回的那則，它和這幾則在聊同一件事：\n"
        "#1 「小明」：今天好累\n#2 「阿華」：晚上要打R6嗎\n#3 「小美」：羚羊拿MP5打盾牌\n"
    ) in state
    assert "m1. 群裡常打虹彩六號：圍攻" in state and "m3. （和「阿華」有關）阿華主玩防守方" in state, "群記憶和人的記憶一起當候選"
    assert list(questions) == ["recall_understand", "recall_pick"]
    assert list(questions["recall_pick"]["criteria"]) == ["m1", "m2", "m3", "none"]
    lookup = next(kwargs for name, kwargs in calls if name == "api.call")
    assert lookup["api_name"] == "luca.linux-computer.lookup"
    assert lookup["args"] == {
        "chat": "「小明」：今天好累\n「阿華」：晚上要打R6嗎\n「小美」：羚羊拿MP5打盾牌",
        "hint": "群裡常打虹彩六號：圍攻",
        "stream_id": "s1",
    }, "焦點那串"
    keywords = next(kwargs for name, kwargs in calls if name == "llm.generate")
    assert "「小美」：羚羊拿MP5打盾牌" in keywords["prompt"] and keywords["task_name"] == "utils"
    searches = [kwargs for name, kwargs in calls if name == "knowledge.search"]
    assert all(kwargs["group_id"] == "10" and kwargs["chat_id"] == "s1" for kwargs in searches), "和 query_memory 一樣帶群號"
    searched = [(kwargs["person_id"], kwargs["query"]) for kwargs in searches]
    assert ("", "R6 羚羊 MP5 盾牌") in searched, "群記憶用小模型寫的關鍵字查，不用整段原文"
    assert ("p-u2", "阿華 R6 羚羊 MP5 盾牌") in searched, "整體印象：名字＋關鍵字"
    assert ("p-u2", "群裡常打虹彩六號：圍攻") not in searched, "不再用選中的那筆重查一次人（查回來的就是同一筆）"

    note = result["items"][0]["parts"][-1]["text"]
    assert note == (
        "【聯想】這段話題讓你想起的（可能不對；和眼前的聊天衝突時以聊天為準，別逐字說出來）：\n"
        "- 群裡以前聊過（把握 0.60）：群裡常打虹彩六號：圍攻\n"
        "- 上網查到：在聊《虹彩六號：圍攻》（來源：https://r6.example）"
    ), "只給選中的那筆和上網查到的，人一個個列出來的那幾行不給"
    summary = "看不懂 0.70・想到 m1（0.60）・人 1/3・上網「羚羊 MP5 盾牌」"
    assert env.plugin._decision_stats.state["decisions"][0]["recall"] == summary
    row = asyncio.run(env.plugin.webui_decision())["decisions"][0]
    assert row["recall"] == summary
    assert "#3 「小美」：羚羊拿MP5打盾牌" in row["context"], "詳情看得到 # 編號對應的訊息"
    assert row["recall_detail"].startswith(
        "往回找話題：\n→ #3 「小美」：羚羊拿MP5打盾牌（焦點，話題串 2）\n"
        "← #2 「阿華」：晚上要打R6嗎（同一串）\n← #1 「小明」：今天好累（同一串）\n"
        "關鍵字：R6 羚羊 MP5 盾牌\n想起的記憶：\nm1. 群裡常打虹彩六號：圍攻\nm2. 上週在聊顯卡\nm3. （「阿華」）阿華主玩防守方"
    )
    assert "「小美」：沒查到" in row["recall_detail"] and row["recall_detail"].endswith(note)

    # 同一回合後面的請求（不再問決策模型）也帶著；replyer 回的是這串（或沒指定回哪則）才給
    later = _run(env)
    assert later["items"][0]["parts"][-1]["text"] == note and len(env.asked) == 2
    replied = asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=FUNNY, extra_prompt="前面"))
    assert replied["modified_kwargs"]["extra_prompt"] == f"前面\n{note}"
    assert asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=""))["modified_kwargs"]
    assert asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id="mother")) == {"action": "continue"}


def test_unsure_recall_asks_her_to_hedge_and_lookup_only_when_enabled(monkeypatch) -> None:
    unsure = {
        "recall_understand": {"choice": "no", "probabilities": {"no": 0.8, "yes": 0.2}},
        "recall_pick": {"choice": "none", "probabilities": {"none": 0.7}},
    }
    env, calls = _setup(monkeypatch, [SAME_THREAD, unsure], {})
    note = _run(env)["items"][0]["parts"][-1]["text"]
    assert note.endswith(recall.UNSURE)
    assert "群裡常打" not in note and "阿華主玩防守方" not in note and "沒有相關的記憶" not in note, (
        "選了「都無關」：記憶和用同一組關鍵字查到的印象都不給"
    )
    assert not any(name == "api.call" for name, _ in calls), "沒開上網查"

    # 寫不出關鍵字時改用原文查
    failing, failing_calls = _setup(monkeypatch, [SAME_THREAD, unsure], {})
    original = failing.ctx.call_capability

    async def no_keywords(name, **kwargs):
        if name == "llm.generate":
            failing_calls.append((name, kwargs))
            return {"success": False, "error": "沒有模型"}
        return await original(name, **kwargs)

    failing.ctx.call_capability = no_keywords
    _run(failing)
    queries = [kwargs["query"] for name, kwargs in failing_calls if name == "knowledge.search" and not kwargs["person_id"]]
    assert queries[0] == "今天好累 晚上要打R6嗎 羚羊拿MP5打盾牌"

    # 沒開決策模型就不分話題、不聯想
    off, off_calls = _setup(monkeypatch, [], {"enabled": False})
    _run(off)
    assert not off.asked and not off_calls


def test_timed_out_searches_are_not_reported_as_empty(monkeypatch) -> None:
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [SAME_THREAD, understood], {})
    original = env.ctx.call_capability

    async def slow(name, **kwargs):
        if name == "knowledge.search" and kwargs["person_id"] in ("", "p-u1"):
            calls.append((name, kwargs))
            return {"success": False, "error": "TimeoutError()"}
        return await original(name, **kwargs)

    env.ctx.call_capability = slow
    result = _run(env)
    record = env.plugin._decision_stats.state["decisions"][0]
    assert record["recall"] == "看得懂 0.90・群記憶沒等到（逾時或失敗）・人 1/3（1 人沒等到）"
    assert "想起的記憶：\nm1. （「阿華」）阿華主玩防守方" in record["recall_detail"], "群記憶沒等到，人的照樣當候選"
    assert "「小明」：沒等到（逾時或失敗）" in record["recall_detail"]
    assert "【聯想】" not in result["items"][0]["parts"][-1]["text"], "沒選中、也有把握：不加聯想"


def test_keyword_model_saying_nothing_falls_back_to_the_thread_text(monkeypatch) -> None:
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [SAME_THREAD, understood], {})
    original = env.ctx.call_capability

    async def nothing(name, **kwargs):
        if name == "llm.generate":
            calls.append((name, kwargs))
            return {"success": True, "response": "無"}
        return await original(name, **kwargs)

    env.ctx.call_capability = nothing
    _run(env)
    queries = [kwargs["query"] for name, kwargs in calls if name == "knowledge.search" and not kwargs["person_id"]]
    assert queries[0] == "今天好累 晚上要打R6嗎 羚羊拿MP5打盾牌", "看不出在聊什麼就用這串的原文查"
