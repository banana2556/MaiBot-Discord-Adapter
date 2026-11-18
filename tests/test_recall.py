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
    assert recall.focus_message(window, remembered)["msg_id"] == "c", "她說話之後叫她的優先"
    assert recall.focus_message(window, {})["msg_id"] == "d", "沒人叫她就是最新一則"
    people = recall.participants(window, window[4], ["a", "b"], remembered, "bot")
    assert [person["name"] for person in people] == ["D", "E", "B"], "發話人、@ 的人、前文（新的先）；不含她自己，最多 3 個"

RECALL_NO_M1 = {
    "recall_understand": {"choice": "no", "probabilities": {"no": 0.7, "yes": 0.3}},
    "recall_pick": {"choice": "m1", "probabilities": {"m1": 0.6, "none": 0.3}},
}


def test_recall_reads_memories_asks_decision_looks_up_and_tells_planner(monkeypatch) -> None:
    env, calls = _setup(monkeypatch, [_answer("text", 0.8), RECALL_NO_M1], {"recall_enabled": True, "lookup_enabled": True})
    result = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))

    # 表情決策那次不問聯想；前文（回覆、@）就是整串，前面沒有別的訊息，不用問「同一件事」；第二次才問聯想
    assert len(env.asked) == 2 and not any(name.startswith("recall_") for name in env.asked[0][1])
    state, questions = env.asked[1]
    assert (
        "【聯想】#3 是你要回的那則，它和這幾則在聊同一件事：\n"
        "#1 「小明」：今天好累\n#2 「阿華」：晚上要打R6嗎\n#3 「小美」：羚羊拿MP5打盾牌\n"
    ) in state
    assert "m1. 群裡常打虹彩六號：圍攻" in state and "對「阿華」的印象：阿華主玩防守方" in state
    assert list(questions) == ["recall_understand", "recall_pick"]
    assert list(questions["recall_pick"]["criteria"]) == ["m1", "m2", "none"]
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
        "- 「小明」：沒有相關的記憶\n"
        "- 「阿華」：阿華 R6 鑽石牌位；阿華主玩防守方"
    )
    summary = "看不懂 0.70・想到 m1（0.60）・人 1/3・上網「羚羊 MP5 盾牌」"
    assert env.plugin._decision_stats.state["decisions"][0]["recall"] == summary
    row = asyncio.run(env.plugin.webui_decision())["decisions"][0]
    assert row["recall"] == summary
    assert "#3 「小美」：羚羊拿MP5打盾牌" in row["context"], "詳情看得到 # 編號對應的訊息"
    assert row["recall_detail"].startswith(
        "往回找話題：\n→ #3 「小美」：羚羊拿MP5打盾牌（要回的這則）\n"
        "← #2 「阿華」：晚上要打R6嗎（明確前文）\n← #1 「小明」：今天好累（明確前文）\n"
        "關鍵字：R6 羚羊 MP5 盾牌\n群記憶：\nm1. 群裡常打虹彩六號：圍攻"
    )
    assert "「小美」：沒查到" in row["recall_detail"] and row["recall_detail"].endswith(note)

    # 同一回合後面的請求（不再問決策模型）也帶著；replyer 回的是那則才給
    later = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))
    assert later["modified_kwargs"]["items"][0]["parts"][-1]["text"] == note and len(env.asked) == 2
    replied = asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=LATEST, extra_prompt="前面"))
    assert replied["modified_kwargs"]["extra_prompt"] == f"前面\n{note}"
    assert asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=FUNNY)) == {"action": "continue"}


def test_unsure_recall_asks_her_to_hedge_and_lookup_only_when_enabled(monkeypatch) -> None:
    unsure = {
        "recall_understand": {"choice": "no", "probabilities": {"no": 0.8, "yes": 0.2}},
        "recall_pick": {"choice": "none", "probabilities": {"none": 0.7}},
    }
    env, calls = _setup(monkeypatch, [_answer("text", 0.8), unsure], {"recall_enabled": True})
    result = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))
    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    assert "看不出是哪一筆：群裡常打虹彩六號：圍攻；上週在聊顯卡" in note and note.endswith(recall.UNSURE)
    assert not any(name == "api.call" for name, _ in calls), "沒開上網查"

    # 寫不出關鍵字時改用原文查
    failing, failing_calls = _setup(monkeypatch, [_answer("text", 0.8), unsure], {"recall_enabled": True})
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

    # 只按表情（她這輪不說話）不跑聯想；沒開聯想也不跑
    quiet, quiet_calls = _setup(monkeypatch, [_answer("quick_reaction", 0.9)], {"recall_enabled": True})
    asyncio.run(quiet.plugin.decide_emote_in_planner(**_planner()))
    assert len(quiet.asked) == 1 and not any(name == "knowledge.search" for name, _ in quiet_calls)
    off, off_calls = _setup(monkeypatch, [_answer("text", 0.8)], {})
    asyncio.run(off.plugin.decide_emote_in_planner(**_planner()))
    assert len(off.asked) == 1 and not off_calls, "沒開聯想就不查也不問"


def test_timed_out_searches_are_not_reported_as_empty(monkeypatch) -> None:
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [_answer("text", 0.8), understood], {"recall_enabled": True})
    original = env.ctx.call_capability

    async def slow(name, **kwargs):
        if name == "knowledge.search" and kwargs["person_id"] in ("", "p-u1"):
            calls.append((name, kwargs))
            return {"success": False, "error": "TimeoutError()"}
        return await original(name, **kwargs)

    env.ctx.call_capability = slow
    result = asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))
    record = env.plugin._decision_stats.state["decisions"][0]
    assert record["recall"] == "看得懂 0.90・群記憶沒等到（逾時或失敗）・人 1/3（1 人沒等到）"
    assert "群記憶：沒等到（逾時或失敗）" in record["recall_detail"] and "「小明」：沒等到（逾時或失敗）" in record["recall_detail"]
    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    assert "「阿華」：阿華主玩防守方" in note and "小明" not in note.split("\n", 1)[1], "沒等到的人不說成沒有記憶"


OLD = "1567700000000000001"


def test_walks_back_from_a_short_reply_to_the_topic_it_belongs_to(monkeypatch) -> None:
    """正式機那次：#4「打」在接她答應打絕本那句，往回是 #3 @ 她問要不要打絕本、#2 問要不要打絕歐；#1 是戀愛話題，不算。"""
    links = {"link_4": {"choice": "you", "probabilities": {"you": 0.57, "3": 0.37, "none": 0.01}}}
    same = {
        "recall_same_2": {"choice": "yes", "probabilities": {"yes": 1.0, "no": 0.0}},
        "recall_same_1": {"choice": "no", "probabilities": {"yes": 0.08, "no": 0.92}},
    }
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(
        monkeypatch, [{**_answer("text", 0.8), **links}, same, understood], {"recall_enabled": True, "reply_context_enabled": True}
    )
    env.plugin._remember_inbound("s1", _inbound(OLD, "u4"))
    env.plugin._inbound["s1"][alias(LAUGH)]["addressed"] = True  # #3 @ 了她
    bot = env.plugin._bot_name()
    planner = _planner()
    planner["items"][2:] = [
        _item(alias(OLD), "海苔", "你沒談過噢"),
        _item(alias(FUNNY), "海苔", "欸刻主Bo你要不要打絕歐"),
        _item(alias(LAUGH), "Ahh shit. here we go again", f"@「{bot}」 要不要打絕本"),
        _message_item("mself2", bot, "好呀，但我只會躺地板", self_message=True),
        _item(alias(LATEST), "Clover", "打"),
    ]
    asyncio.run(env.plugin.decide_emote_in_planner(**planner))

    # 第二次請求：前面兩則各問一次是不是跟「#3、她、#4」這串同一件事
    state, questions = env.asked[1]
    assert sorted(questions) == ["recall_same_1", "recall_same_2"]
    thread = f"#3 「Ahh shit. here we go again」：@「{bot}」 要不要打絕本\n（你）：好呀，但我只會躺地板\n#4 「Clover」：打"
    assert thread in questions["recall_same_2"]["instructions"] and "#2 和這段在聊同一件事嗎？" in questions["recall_same_2"]["instructions"]
    # 第三次才問聯想；這串是 #2、#3、她、#4，#1 不在裡面
    recall_state = env.asked[2][0]
    assert f"它和這幾則在聊同一件事：\n#2 「海苔」：欸刻主Bo你要不要打絕歐\n{thread}\n" in recall_state

    prompt = next(kwargs["prompt"] for name, kwargs in calls if name == "llm.generate")
    assert "「海苔」：欸刻主Bo你要不要打絕歐\n「Ahh shit. here we go again」：@你 要不要打絕本\n（你）：好呀，但我只會躺地板\n「Clover」：打" in prompt
    assert "你沒談過噢" not in prompt, "不相關的不拿去寫關鍵字"
    detail = env.plugin._decision_stats.state["decisions"][0]["recall_detail"]
    assert detail.startswith(
        "往回找話題：\n→ #4 「Clover」：打（要回的這則）\n"
        "← （你）：好呀，但我只會躺地板（明確前文）\n"
        f"← #3 「Ahh shit. here we go again」：@「{bot}」 要不要打絕本（明確前文）\n"
        "← #2 「海苔」：欸刻主Bo你要不要打絕歐（同一件事 1.00）\n"
        "✕ #1 「海苔」：你沒談過噢（同一件事 0.08，不算）\n"
    ), detail


def test_keyword_model_saying_nothing_falls_back_to_the_thread_text(monkeypatch) -> None:
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [_answer("text", 0.8), understood], {"recall_enabled": True})
    original = env.ctx.call_capability

    async def nothing(name, **kwargs):
        if name == "llm.generate":
            calls.append((name, kwargs))
            return {"success": True, "response": "無"}
        return await original(name, **kwargs)

    env.ctx.call_capability = nothing
    asyncio.run(env.plugin.decide_emote_in_planner(**_planner()))
    queries = [kwargs["query"] for name, kwargs in calls if name == "knowledge.search" and not kwargs["person_id"]]
    assert queries[0] == "今天好累 晚上要打R6嗎 羚羊拿MP5打盾牌", "看不出在聊什麼就用這串的原文查"
