"""聯想：分完話題後讀懂焦點那串：參與者的人物畫像、這串的黑話、相關的群記憶；看不懂時提醒她先查，整理給 planner 和 replyer。"""

import asyncio

from test_emote import FUNNY, LATEST, LAUGH, _env, _message_item, _topics
from test_typing_indicator import _load_adapter_module

recall = _load_adapter_module("src.recall")
topics = _load_adapter_module("src.topics")
alias = _load_adapter_module("src.emote").display_message_id

# 她說的先自己一串（t1）；小明、阿華、小美（回覆阿華）同一串（t2）
SAME_THREAD = _topics(topic_1="none", topic_2="m1", topic_3="m2")
MEMORY = "你知道这些知识: 1. [2026-08-28 05:30] 阿華: 晚上R6 <a:lol:5> [2026-08-28 05:40] 小明: https://x.com/a ...\n2. 上週在聊顯卡"
PROFILES = {
    "p-u2": {"success": True, "summary": "阿華主玩 R6 防守方", "traits": ["鑽石牌位"]},
    "p-u1": {"success": True, "summary": "", "traits": []},
    "p-u3": {"success": False, "error": "TimeoutError()"},
}
# 黑話是字串包含：「P5」也會在「MP5」裡比對到
JARGONS = {"success": True, "jargons": [
    {"content": "MP5", "meaning": "一把衝鋒槍"}, {"content": "P5", "meaning": "女神異聞錄5"}, {"content": "羚羊", "meaning": "R6 幹員 Thermite"},
]}
NOTE_HEAD = "【這串的理解】（聯想，可能不對；和眼前的聊天衝突時以聊天為準，別逐字說出來）"


def _inbound(message_id, user_id, at=None):
    raw = [{"type": "at", "data": {"target_user_id": at[0], "target_user_cardname": at[1]}}] if at else []
    info = {"additional_config": {}, "user_info": {"user_id": user_id}, "group_info": {"group_id": "10"}}
    return {"platform": "discord", "session_id": "s1", "message_id": message_id, "message_info": info, "raw_message": raw}


def _item(msg_id, user, text, quote=""):
    attrs = f'msg_id="{msg_id}"' + (f' quote="{quote}"' if quote else "")
    head = f'<message {attrs} time="15:11:30" user="{user}" group_card="「{user}」">\n'
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": head + text}]}


def _planner():
    """小明說累、阿華約打 R6、小美回覆阿華並 @ 小明：「羚羊拿MP5打盾牌」；MaiBot 自己也插了一段整段上下文的黑話。"""
    return {
        "items": [
            {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
            _message_item("mself", "麥麥", "大家在幹嘛", self_message=True),
            _item(alias(FUNNY), "小明", "今天好累"),
            _item(alias(LAUGH), "阿華", "晚上要打R6嗎"),
            _item(alias(LATEST), "小美", "羚羊拿MP5打盾牌", quote=alias(LAUGH)),
            {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "<jargon>\nFF：開拓動漫祭"}]},
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
        if name == "person.get_profile":
            return PROFILES[kwargs["person_id"]]
        if name == "jargon.match":
            return JARGONS
        if name == "llm.generate":
            return {"success": True, "response": "R6 羚羊 MP5 盾牌\n（多的行不要）"}
        if name == "knowledge.search":
            return MEMORY
        return await original(name, **kwargs)

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
    assert recall.parse_hits("你知道这些知识: 1. A 常玩 R6\n2. 上週在排位") == ["A 常玩 R6", "上週在排位"]
    assert recall.parse_hits("你不太了解有关x的知识") == [] and recall.parse_hits({"success": False}) == []
    assert recall.tidy("[2026-08-28 05:30] hinana: GTA6鎖30FPS<a:lol:5> [2026-08-28 05:40] LZ: https://ptt.cc/x ...") == (
        "2026-08-28 hinana：GTA6鎖30FPS／LZ：（連結）"
    ), "日期只留一個、拿掉表情 ID、網址換掉、截斷的 ... 拿掉"
    assert recall.tidy("x" * 200, 10) == "x" * 9 + "…"
    terms = recall.whole_terms(JARGONS["jargons"] + [{"content": "FF", "meaning": "開拓動漫祭"}], ["羚羊拿MP5打盾牌", "FF14 好玩"])
    assert [term["content"] for term in terms] == ["MP5", "羚羊"], "英數詞要整個詞對上：MP5 裡的 P5、FF14 裡的 FF 不算"
    long = [{"content": "T1", "meaning": "「T1」指韓國英雄聯盟戰隊。上下文在討論選手與陣容，例如宙斯、Oner。"}, {"content": "五星", "meaning": "某人的暱稱"}]
    terms = recall.whole_terms(long, ["T1 又換人", "五星說的"], ["五星爛卡"])
    assert terms == [{"content": "T1", "meaning": "「T1」指韓國英雄聯盟戰隊。"}], "意思只留第一句；參與者的暱稱不當黑話"
    profile = (
        "# 人物画像\n人物ID: 01e0bd\n主称呼: 糖奶\n别名: 「糖奶」、糖奶\n\n## 身份设定\n- 糖奶从事优化工程方法相关工作。\n\n"
        "## 关系设定\n- 糖奶要求资讯科全员通过其面试。\n\n## 稳定了解\n- 暂无\n\n## 相处偏好\n- 暂无\n\n"
        "## 近期互动\n- 一\n- 二\n- 三\n\n## 不确定信息\n- 可能是学生\n\n## 维护备注\n- 自动画像仅供内部参考。"
    )
    assert recall.profile_brief(profile) == "糖奶从事优化工程方法相关工作。；糖奶要求资讯科全员通过其面试。；一；二", (
        "不要開頭的人物 ID 和別名，跳過暂无、不確定和維護備註，近期互動只取兩筆"
    )
    assert recall.profile_brief("# 人物画像\n人物ID: x\n## 身份设定\n- 暂无") == "", "全部暂无就當沒有畫像"
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
    assert [person["name"] for person in people] == ["D", "E", "B", "A"], "發話人、@ 的人、這串其他人（新的先）；不含她自己"


def test_planner_gets_the_threads_people_terms_and_memory(monkeypatch) -> None:
    picked = {
        "recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9, "no": 0.1}},
        "recall_pick": {"choice": "m1", "probabilities": {"m1": 0.7, "none": 0.2}},
    }
    env, calls = _setup(monkeypatch, [SAME_THREAD, picked], {})
    result = _run(env)

    # 第一次只分話題；分完用焦點那串聯想，第二次才問看不看得懂、哪筆記憶有關
    assert len(env.asked) == 2 and not any(name.startswith("recall_") for name in env.asked[0][1])
    state, questions = env.asked[1]
    assert (
        "【聯想】#3 是你要回的那則，它和這幾則在聊同一件事：\n"
        "#1 「小明」：今天好累\n#2 「阿華」：晚上要打R6嗎\n#3 「小美」：羚羊拿MP5打盾牌\n"
        "人物：\n- 阿華：阿華主玩 R6 防守方（鑽石牌位）\n詞彙：\n- MP5：一把衝鋒槍\n- 羚羊：R6 幹員 Thermite\n"
    ) in state, "決策模型判斷看不看得懂時也知道這些人和詞"
    assert "m1. 2026-08-28 阿華：晚上R6／小明：（連結）" in state
    assert list(questions["recall_pick"]["criteria"]) == ["m1", "m2", "none"]

    jargon = next(kwargs for name, kwargs in calls if name == "jargon.match")
    assert jargon["session_id"] == "s1" and jargon["texts"] == ["今天好累", "晚上要打R6嗎", "羚羊拿MP5打盾牌"], "只比對這串"
    profiles = [kwargs for name, kwargs in calls if name == "person.get_profile"]
    assert {kwargs["person_id"] for kwargs in profiles} == {"p-u1", "p-u2", "p-u3"} and profiles[0]["chat_id"] == "s1"
    searches = [kwargs for name, kwargs in calls if name == "knowledge.search"]
    assert [(kwargs["query"], kwargs["group_id"], "person_id" in kwargs) for kwargs in searches] == [("R6 羚羊 MP5 盾牌", "10", False)], (
        "只用關鍵字查一次群記憶，不再一個個人去撈聊天原文"
    )
    assert not any(name == "api.call" for name, _ in calls), "要不要上網查交給 planner"

    note = result["items"][0]["parts"][-1]["text"]
    assert note == (
        f"{NOTE_HEAD}\n"
        "人物：\n- 阿華：阿華主玩 R6 防守方（鑽石牌位）\n"
        "詞彙：\n- MP5：一把衝鋒槍\n- 羚羊：R6 幹員 Thermite\n"
        "記憶：\n- 群裡以前聊過（把握 0.70）：2026-08-28 阿華：晚上R6／小明：（連結）"
    )
    assert not any("<jargon>" in str(item) for item in result["items"]), "MaiBot 整段上下文比對的黑話拿掉"

    summary = "看得懂 0.90・想到 m1（0.70）・人 1/3・詞 2"
    record = env.plugin._decision_stats.state["decisions"][0]
    assert record["recall"] == summary and record["columns"]["memory"] == "2 筆・人 1/3・詞 2"
    detail = record["recall_detail"]
    assert detail.startswith("【話題串】串 2「今天好累」・3 則\n→ #3 「小美」：羚羊拿MP5打盾牌（焦點，話題串 2）\n"), "分段給人看"
    assert "\n\n【人物】\n- 小美：沒等到（逾時或失敗）\n- 小明：沒有畫像\n- 阿華：阿華主玩 R6 防守方（鑽石牌位）\n" in detail
    assert "\n\n【詞彙】MP5、羚羊\n" in detail and "\n\n【記憶】關鍵字：R6 羚羊 MP5 盾牌\n- m1. " in detail
    assert detail.endswith(f"【給 planner 的】\n{note}")

    # 同一回合後面的請求（不再問決策模型）也帶著；replyer 回的是這串（或沒指定回哪則）才給
    later = _run(env)
    assert later["items"][0]["parts"][-1]["text"] == note and len(env.asked) == 2
    replied = asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id=FUNNY, extra_prompt="前面"))
    assert replied["modified_kwargs"]["extra_prompt"] == f"前面\n{note}"
    assert asyncio.run(env.plugin.add_recall_to_replyer(session_id="s1", reply_message_id="mother")) == {"action": "continue"}

    # 人物畫像變得慢：快取期間不再查
    before = len(calls)
    asyncio.run(env.plugin._profile("s1", person := {"person_id": "p-u2"}))
    assert len(calls) == before and person["profile"] == "阿華主玩 R6 防守方（鑽石牌位）"


def test_not_understanding_asks_her_to_look_it_up_herself(monkeypatch) -> None:
    lost = {
        "recall_understand": {"choice": "no", "probabilities": {"no": 0.8, "yes": 0.2}},
        "recall_pick": {"choice": "none", "probabilities": {"none": 0.7}},
    }
    env, calls = _setup(monkeypatch, [SAME_THREAD, lost], {})
    note = _run(env)["items"][0]["parts"][-1]["text"]
    assert note.endswith("沒把握：" + recall.UNSURE.format(query="R6 羚羊 MP5 盾牌")), "給她可以查的關鍵字，查不查她自己決定"
    assert "記憶：" not in note and "人物：" in note, "選了「都無關」不給記憶；人和詞照樣給"
    assert not any(name == "api.call" for name, _ in calls)

    # 寫不出關鍵字時改用原文查
    failing, failing_calls = _setup(monkeypatch, [SAME_THREAD, lost], {})
    original = failing.ctx.call_capability

    async def no_keywords(name, **kwargs):
        if name == "llm.generate":
            failing_calls.append((name, kwargs))
            return {"success": False, "error": "沒有模型"}
        return await original(name, **kwargs)

    failing.ctx.call_capability = no_keywords
    _run(failing)
    queries = [kwargs["query"] for name, kwargs in failing_calls if name == "knowledge.search"]
    assert queries[0] == "今天好累 晚上要打R6嗎 羚羊拿MP5打盾牌"

    # 沒開決策模型就不分話題、不聯想，MaiBot 自己的 <jargon> 也留著
    off, off_calls = _setup(monkeypatch, [], {"enabled": False})
    kept = _run(off)
    assert not off.asked and not off_calls and any("<jargon>" in str(item) for item in kept["items"])


def test_missing_capabilities_and_timeouts_leave_their_part_out(monkeypatch) -> None:
    """原版 MaiBot 沒有 person.get_profile、jargon.match；記憶檢索逾時。三段都缺時只剩把握的判斷。"""
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [SAME_THREAD, understood], {})
    original = env.ctx.call_capability

    async def bare(name, **kwargs):
        if name in ("person.get_profile", "jargon.match", "knowledge.search"):
            calls.append((name, kwargs))
            return {"success": False, "error": f"沒有 {name}"}
        return await original(name, **kwargs)

    env.ctx.call_capability = bare
    result = _run(env)
    record = env.plugin._decision_stats.state["decisions"][0]
    assert record["recall"] == "看得懂 0.90・群記憶沒等到（逾時或失敗）・人 0/3・詞 0"
    assert "【記憶】關鍵字：R6 羚羊 MP5 盾牌\n- 沒等到（逾時或失敗）" in record["recall_detail"]
    assert "【這串的理解】" not in result["items"][0]["parts"][-1]["text"], "看得懂又沒東西可補：不加"


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
    queries = [kwargs["query"] for name, kwargs in calls if name == "knowledge.search"]
    assert queries[0] == "今天好累 晚上要打R6嗎 羚羊拿MP5打盾牌", "看不出在聊什麼就用這串的原文查"


def test_a_bare_link_is_not_searched_or_suggested_as_a_query(monkeypatch) -> None:
    """正式機那次：焦點只有影片連結和卡片，小模型寫不出關鍵字，結果拿整串網址去查記憶、又叫她去查那串網址。"""
    lost = {"recall_understand": {"choice": "no", "probabilities": {"no": 0.84}}}
    env, calls = _setup(monkeypatch, [SAME_THREAD, lost], {})
    original = env.ctx.call_capability

    async def nothing(name, **kwargs):
        if name == "llm.generate":
            return {"success": True, "response": "無"}
        return await original(name, **kwargs)

    env.ctx.call_capability = nothing
    planner = _planner()
    planner["items"][4] = _item(alias(LATEST), "小美", "https://fxtwitter.com/a/status/1/video/1?a=x&s=12[卡片: 椿ちゃ]", quote=alias(LAUGH))
    note = _run(env, planner)["items"][0]["parts"][-1]["text"]
    query = next(kwargs["query"] for name, kwargs in calls if name == "knowledge.search")
    assert "http" not in query and "[卡片: 椿ちゃ]" in query, "網址拿掉、卡片留著"
    assert note.endswith("沒把握：" + recall.UNSURE_NO_QUERY), "原文不當成建議她查的關鍵字"
    record = env.plugin._decision_stats.state["decisions"][0]
    assert "http" not in record["recall_detail"] and "http" not in record["message"], "頁面上的網址都換成（連結）"
    assert "（小模型沒寫出關鍵字，改用這串的原文）" in record["recall_detail"]
