"""話題串：每則訊息分配到話題串（回覆、她自己說的用規則；其餘問決策模型），只有網址的先等卡片，圖片直接附給決策模型。"""

import asyncio

from test_emote import FUNNY, LAUGH, LATEST, _answer, _message_item
from test_recall import _item, _planner, _setup
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

    assert store["of"]["a"] == store["of"]["me"] == store["of"]["e"], "第一則直接開串；她回叫她的那則；回覆跟被回覆的同一串"
    assert list(questions) == ["topic_2", "topic_4"], "只有網址的先等卡片，不問"
    assert "最後一則是 0 分鐘前" in questions["topic_2"]["criteria"]["t1"] and "none" in questions["topic_2"]["criteria"]
    assert "m2" in questions["topic_4"]["criteria"], "這輪前面剛問的那則也能選"
    assert images == ["data:image/png;base64," + "Q" * 200] and "附圖第 1 張" in questions["topic_4"]["instructions"]

    summary = topics.apply_topics(store, keys, {"topic_2": {"choice": "none"}, "topic_4": {"choice": "m2"}})
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


def test_recall_reads_the_topic_thread_instead_of_walking_back(monkeypatch) -> None:
    """表情決策那次請求順便分好話題，聯想直接拿要回那則所在的串，不用再一則一則往回問。"""
    scene = {**_answer("text", 0.8), "topic_2": {"choice": "none"}, "topic_3": {"choice": "t1"}, "topic_4": {"choice": "m2"}}
    understood = {"recall_understand": {"choice": "yes", "probabilities": {"yes": 0.9}}}
    env, calls = _setup(monkeypatch, [scene, understood], {"recall_enabled": True})
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
    asyncio.run(env.plugin.decide_emote_in_planner(**planner))

    assert len(env.asked) == 2 and "recall_same_1" not in env.asked[1][1], "分好話題就不用再問「是不是同一件事」"
    assert set(env.asked[0][1]) >= {"topic_2", "topic_3", "topic_4"}
    detail = env.plugin._decision_stats.state["decisions"][0]["recall_detail"]
    assert "（要回的這則，話題串 2）" in detail and "← #2 「阿傑」：我顯卡燒了（同一串）" in detail and "火鍋" not in detail
    prompt = next(kwargs["prompt"] for name, kwargs in calls if name == "llm.generate")
    assert "顯卡燒了" in prompt and "火鍋" not in prompt


def test_late_link_card_is_remembered_and_shown(monkeypatch) -> None:
    env, _ = _setup(monkeypatch, [_answer("text", 0.8)], {})
    env.plugin._remember_card(LATEST, "[卡片: Suica 新角色是兔子]")
    assert env.plugin._inbound["s1"][alias(LATEST)]["card"] == "[卡片: Suica 新角色是兔子]"
    window = [_message_item(alias(LATEST), "小美", "https://x.com/abc")]
    messages = _load_adapter_module("src.emote").planner_messages(window)
    asyncio.run(env.plugin._wait_for_focus_card("s1", messages))  # 卡片已到，不用等
