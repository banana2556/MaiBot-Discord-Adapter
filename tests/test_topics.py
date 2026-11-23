"""話題串：每則訊息分配到話題串（回覆、她自己說的用規則；其餘問決策模型），只有網址的先等卡片，圖片直接附給決策模型。"""

import asyncio

from test_emote import FUNNY, LAUGH, LATEST, _message_item, _topics
from test_recall import _item, _planner, _run, _setup
from test_typing_indicator import _load_adapter_module

topics = _load_adapter_module("src.topics")
alias = _load_adapter_module("src.emote").display_message_id


def _msg(msg_id, user, text, time, self_message=False, quote=""):
    return {"msg_id": msg_id, "user": user, "text": text, "self": self_message, "quote": quote, "time": time}


def test_rules_pending_links_images_and_answers() -> None:
    window = [
        _msg("a", "小明", "週六吃火鍋嗎", 1000.0),
        _msg("me", "麥麥", "好啊", 1010.0, self_message=True),
        _msg("b", "阿華", "我顯卡燒了", 1020.0),
        _msg("c", "小美", "https://x.com/abc", 1030.0),
        _msg("d", "阿傑", "[圖片]", 1040.0),
        _msg("e", "小明", "麻辣的", 1050.0, quote="a"),
    ]
    numbers = {id(m): str(n) for n, m in enumerate((m for m in window if not m["self"]), 1)}
    remembered = {"a": {"addressed": True}, "d": {"image": "Q" * 200}}
    store = topics.new_store()
    questions, keys, images = topics.topic_questions(store, window, numbers, remembered, now=1060.0)

    assert store["of"]["a"] == store["of"]["me"]
    assert list(questions) == ["topic_2", "topic_4", "topic_5"], "回覆也需判斷，只有網址先等卡片"
    assert "最後一則是 0 分鐘前" in questions["topic_2"]["criteria"]["t1"] and "none" in questions["topic_2"]["criteria"]
    assert "m2" in questions["topic_4"]["criteria"], "這輪前面剛問的那則也能選"
    assert images == ["data:image/png;base64," + "Q" * 200] and "附圖第 1 張" in questions["topic_4"]["instructions"]

    summary = topics.apply_topics(store, keys, {"topic_2": {"choice": "none"}, "topic_4": {"choice": "m2"}, "topic_5": {"choice": "t1"}})
    assert store["of"]["e"] == store["of"]["a"]
    assert store["of"]["b"] == store["of"]["d"] != store["of"]["a"] and summary.startswith("#2→串")

    # 卡片一直沒到：等過 PENDING_SECONDS 就照網址分
    later, _, _ = topics.topic_questions(store, window, numbers, remembered, now=1060.0 + topics.PENDING_SECONDS)
    assert list(later) == ["topic_3"]
    # 卡片晚到補記後就有內容
    assert not topics.lacks_content(topics.with_card(window[3], {"card": "[卡片: Suica 新角色是兔子]"}))

    # 連結有預覽文字時不附它的預覽圖
    linked = [_msg("x", "五星", "https://perfectpitchtw.vercel.app/ [链接预览: 音痴檢定]", 2000.0)]
    _, _, none = topics.topic_questions(topics.new_store() | {"topics": store["topics"], "of": dict(store["of"])}, linked,
                                        {id(linked[0]): "1"}, {"x": {"image": "P" * 200}}, now=2000.0)
    assert none == []


def test_newest_messages_are_assigned_first_and_focus_ids_keep_later_arrivals() -> None:
    """剛重啟時上下文裡全是沒分過的：一輪只問最新的 ASSIGN_LIMIT 則，焦點（最新的）一定分得到。"""
    window = [_msg(f"m{n}", "小明", f"第 {n} 句", 1000.0 + n) for n in range(10)]
    numbers = {id(m): str(n) for n, m in enumerate(window, 1)}
    store = topics.new_store()
    questions, _, _ = topics.topic_questions(store, window, numbers, {}, now=2000.0)
    assert list(store["of"]) == ["m4"] and list(questions) == [f"topic_{n}" for n in range(6, 11)], (
        "最舊的 4 則不分；第 5 則還沒有任何串，直接開一串"
    )
    store = {"of": {"m8": 1, "m9": 1}, "topics": {1: {"members": ["m8", "m9"], "lines": [], "last": 0.0}}}
    assert topics.focus_ids(store, "m9", ["m7", "m8", "m9", "x", "y"]) == ["m8", "m9"], "未判定的後來訊息不能混入其他串"
    assert topics.focus_ids(store, "m7", ["m8"]) == [], "焦點不在這輪上下文時不過濾"


def test_reply_to_another_subject_must_be_judged_not_inherited() -> None:
    store = topics.new_store()
    original = _msg("t1", "甲", "T1 比賽", 1000.0)
    topics.add_member(store, None, original, topics.topic_line(original, {}))
    reply = _msg("sale", "乙", "遊戲特價", 1010.0, quote="t1")
    questions, keys, _ = topics.topic_questions(store, [reply], {id(reply): "1"}, {}, 1010.0)
    assert "topic_1" in questions and "t1" in questions["topic_1"]["criteria"]
    topics.apply_topics(store, keys, {"topic_1": {"choice": "none"}})
    assert store["of"]["sale"] != store["of"]["t1"]


def test_legacy_giant_thread_is_not_an_active_candidate() -> None:
    store = topics.new_store()
    for i in range(60):
        message = _msg(f"old{i}", "甲", f"不同舊消息 {i}", 1000.0 + i)
        topics.add_member(store, 1 if i else None, message, topics.topic_line(message, {}))
    # 線上資料由舊版本寫入，沒有建立時間；保持原歷史但不讓它繼續吸收新訊息。
    del store["topics"][1]["created"]
    incoming = _msg("new", "乙", "T1 比賽", 1070.0)
    questions, _, _ = topics.topic_questions(store, [incoming], {id(incoming): "1"}, {}, 1070.0)
    assert "t1" not in questions.get("topic_1", {}).get("criteria", {})
    assert store["of"].get("new") != 1


def test_unclassified_focus_is_isolated_not_all_chat() -> None:
    store = topics.new_store()
    old = _msg("old", "甲", "T1 比賽", 1000.0)
    topics.add_member(store, None, old, topics.topic_line(old, {}))
    assert topics.focus_ids(store, "new", ["old", "new"]) == ["new"]


def test_invalid_or_missing_model_answer_does_not_create_a_thread() -> None:
    store = topics.new_store()
    old = _msg("old", "甲", "T1 比賽", 1000.0)
    topics.add_member(store, None, old, topics.topic_line(old, {}))
    newer = _msg("new", "乙", "遊戲特價", 1010.0)
    questions, keys, _ = topics.topic_questions(store, [newer], {id(newer): "1"}, {}, 1010.0)
    assert questions
    assert topics.apply_topics(store, keys, {"topic_1": {"choice": "bad"}}) == ""
    assert "new" not in store["of"]


def test_candidate_count_is_bounded_and_quoted_thread_remains_eligible() -> None:
    store = topics.new_store()
    for i in range(12):
        m = _msg(f"past{i}", "甲", f"話題 {i}", 1000.0 + i)
        topics.add_member(store, None, m, topics.topic_line(m, {}))
    msg = _msg("reply", "乙", "另一個問題", 1040.0, quote="past0")
    questions, _, _ = topics.topic_questions(store, [msg], {id(msg): "1"}, {}, 1040.0)
    candidate_ids = [key for key in questions["topic_1"]["criteria"] if key.startswith("t")]
    assert "t1" in candidate_ids and len(candidate_ids) <= topics.LIVE_TOPICS + 1


def test_sent_reply_does_not_reactivate_legacy_giant(monkeypatch) -> None:
    from test_emote import _env

    env = _env(monkeypatch)
    plugin = env.plugin
    store = topics.new_store()
    msg = _msg("legacy", "甲", "老話題", 1000.0)
    tid = topics.add_member(store, None, msg, topics.topic_line(msg, {}))
    del store["topics"][tid]["created"]
    plugin._topics["s1"] = store
    plugin._focus["s1"] = "legacy"
    plugin._join_focus({"session_id": "s1", "message_id": "her-next", "raw_message": [{"type": "text", "data": "好的"}]})
    assert "her-next" not in store["of"], "舊巨串不應因 Bot 發言又重新活化"


def test_model_failure_retries_unassigned_reply() -> None:
    store = topics.new_store()
    old = _msg("old", "甲", "T1 比賽", 1000.0)
    topics.add_member(store, None, old, topics.topic_line(old, {}))
    reply = _msg("reply", "乙", "遊戲特價", 1040.0, quote="old")
    window = [old, reply]
    numbers = {id(reply): "1", id(old): "0"}
    questions, _, _ = topics.topic_questions(store, window, numbers, {}, 1040.0)
    assert list(questions) == ["topic_1"]
    assert "reply" not in store["of"]
    retry, keys, _ = topics.topic_questions(store, window, numbers, {}, 1050.0)
    assert list(retry) == ["topic_1"]
    topics.apply_topics(store, keys, {"topic_1": {"choice": "none"}})
    assert store["of"]["reply"] != store["of"]["old"]


def test_expired_own_message_starts_new_segment() -> None:
    store = topics.new_store()
    old = _msg("old", "甲", "舊話題", 1000.0)
    tid = topics.add_member(store, None, old, topics.topic_line(old, {}))
    own = _msg("mine", "麥麥", "重新討論", 1000.0 + topics.SEGMENT_SECONDS + 1,
               self_message=True, quote="old")
    topics.topic_questions(store, [old, own], {}, {}, own["time"])
    assert store["of"]["mine"] != tid
    assert store["topics"][store["of"]["mine"]]["members"] == ["mine"]


def test_expired_reply_opens_new_segment_without_extending_parent() -> None:
    store = topics.new_store()
    old = _msg("old", "甲", "舊話題", 1000.0)
    parent = topics.add_member(store, None, old, topics.topic_line(old, {}))
    reply = _msg("reply", "乙", "重新問", 1000.0 + topics.SEGMENT_SECONDS + 1, quote="old")
    questions, _, _ = topics.topic_questions(store, [reply], {id(reply): "1"}, {}, reply["time"])
    assert not questions, "沒有任何活躍候選時明確開新段，不重啟過期巨串"
    assert store["of"]["reply"] != parent and store["topics"][parent]["members"] == ["old"]


def test_legacy_own_message_is_preserved_in_new_segment() -> None:
    store = topics.new_store()
    old = _msg("legacy", "甲", "舊話題", 1000.0)
    parent = topics.add_member(store, None, old, topics.topic_line(old, {}))
    del store["topics"][parent]["created"]
    own = _msg("mine", "麥麥", "回覆", 1010.0, self_message=True, quote="legacy")
    topics.topic_questions(store, [old, own], {}, {}, own["time"])
    assert store["of"]["mine"] != parent and store["topics"][parent]["members"] == ["legacy"]


def test_apply_topics_is_idempotent() -> None:
    store = topics.new_store()
    old = _msg("old", "甲", "第一題", 1000.0)
    topics.add_member(store, None, old, topics.topic_line(old, {}))
    incoming = _msg("second", "乙", "第二題", 1010.0)
    _, keys, _ = topics.topic_questions(store, [incoming], {id(incoming): "1"}, {}, 1010.0)
    answers = {"topic_1": {"choice": "none"}}
    topics.apply_topics(store, keys, answers)
    tid = store["of"]["second"]
    topics.apply_topics(store, keys, answers)
    assert store["of"]["second"] == tid
    assert sum(m == "second" for topic in store["topics"].values() for m in topic["members"]) == 1


def test_recall_reads_the_focus_thread(monkeypatch) -> None:
    """分好話題後聯想直接拿焦點所在的串：火鍋那串不拿去寫關鍵字。"""
    split = _topics(topic_1="none", topic_2="none", topic_3="m1", topic_4="m2")
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [split, understood], {})
    planner = _planner()
    old = "1567700000000000009"
    env.plugin._remember_inbound("s1", {"platform": "discord", "session_id": "s1", "message_id": old, "message_info": {
        "additional_config": {}, "user_info": {"user_id": "u9"}, "group_info": {"group_id": "10"}}, "raw_message": []})
    planner["items"][2:] = [
        _item(alias(old), "海苔", "週六吃火鍋嗎"),
        _item(alias(FUNNY), "阿傑", "我顯卡燒了"),
        _item(alias(LAUGH), "小美", "麻辣的可以嗎"),
        _item(alias(LATEST), "阿凱", "風扇有轉但沒畫面"),
    ]
    result = _run(env, planner)

    assert len(env.asked) == 2 and set(env.asked[0][1]) == {"topic_1", "topic_2", "topic_3", "topic_4"}
    detail = env.plugin._decision_stats.state["decisions"][0]["recall_detail"]
    assert "（焦點，話題串 3）" in detail and "← #2 「阿傑」：我顯卡燒了（同一串）" in detail and "火鍋" not in detail
    prompt = next(kwargs["prompt"] for name, kwargs in calls if name == "llm.generate")
    assert "顯卡燒了" in prompt and "火鍋" not in prompt
    texts = [message["text"] for message in _load_adapter_module("src.emote").planner_messages(result["items"])]
    assert texts == ["我顯卡燒了", "風扇有轉但沒畫面"], "planner 只看焦點所在串"


def test_late_link_card_is_remembered_and_shown(monkeypatch) -> None:
    env, _ = _setup(monkeypatch, [], {})
    env.plugin._remember_card(LATEST, "[卡片: Suica 新角色是兔子]")
    assert env.plugin._inbound["s1"][alias(LATEST)]["card"] == "[卡片: Suica 新角色是兔子]"
    window = [_message_item(alias(LATEST), "小美", "https://x.com/abc")]
    messages = _load_adapter_module("src.emote").planner_messages(window)
    asyncio.run(env.plugin._wait_for_focus_content("s1", messages))  # 卡片已到，不用等


def test_topic_page_shows_thread_understanding_memory_and_current_settings(monkeypatch) -> None:
    split = _topics(topic_1="none", topic_2="none", topic_3="m2")
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, _ = _setup(monkeypatch, [split, understood], {})
    planner = _planner()
    planner["items"][2:] = [_item(alias(FUNNY), "阿傑", "我顯卡燒了"), _item(alias(LAUGH), "海苔", "週六吃火鍋"),
                            _item(alias(LATEST), "阿凱", "風扇有轉但沒畫面")]
    _run(env, planner)
    page = asyncio.run(env.plugin.webui_decision())
    row = page["decisions"][0]
    assert row["topic"].startswith("串 ") and row["topic"].endswith(" 則") and row["understood"] == "懂 0.90"
    assert "筆" in row["memory"]
    assert set(page["current"]) == {"enabled", "base_url", "model", "context_messages"}


def test_image_waits_for_maibot_description_and_the_thread_gets_it(monkeypatch) -> None:
    # 2026-11-20 17:38 只有一張圖的訊息被當成「[圖片]」分進別串：等 MaiBot 識圖寫好描述再分
    env, _ = _setup(monkeypatch, [], {})
    plugin = env.plugin
    entry = plugin._inbound["s1"][alias(LATEST)]
    entry["image_hash"] = "h1"
    store = topics.new_store()
    plugin._topics["s1"] = store
    topics.add_member(store, None, _msg(alias(LATEST), "小美", "[圖片]", 1000.0), "[17:38] 「小美」：[圖片]")
    answers = iter(["", "", "律師們圍著一桌菜，標語寫肚子非常餓"])
    asked = []

    async def call_capability(name, **kwargs):
        asked.append((name, kwargs))
        return {"success": True, "description": next(answers)}

    real_sleep = asyncio.sleep

    async def fast_sleep(seconds):
        await real_sleep(0)

    monkeypatch.setattr(env.ctx, "call_capability", call_capability)
    monkeypatch.setattr(asyncio, "sleep", fast_sleep)
    window = [{**_msg(alias(LATEST), "小美", "[圖片]", 1000.0), "head": None}]
    asyncio.run(plugin._wait_for_focus_content("s1", window))
    assert asked == [("image.get_description", {"image_hash": "h1"})] * 3, "焦點那則等到描述好"
    assert topics.with_card(window[0], entry) == "[圖片：律師們圍著一桌菜，標語寫肚子非常餓]"
    item = store["topics"][1]
    assert item["items"][0]["text"] == "[圖片：律師們圍著一桌菜，標語寫肚子非常餓]" and "肚子非常餓" in item["lines"][0]

    # 描述還沒好的先不分，等 PENDING_SECONDS
    waiting = _msg("p", "阿華", "[圖片]", 1001.0)
    questions, _, _ = topics.topic_questions(store, [waiting], {id(waiting): "1"}, {"p": {"image_hash": "h2"}}, 1001.0)
    assert questions == {} and "p" not in store["of"]
