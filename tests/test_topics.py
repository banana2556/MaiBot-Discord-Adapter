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
    assert topics.focus_ids(store, "m9", ["m7", "m8", "m9", "x", "y"]) == ["m8", "m9", "x", "y"], "之後才收到、還沒分的也留"
    assert topics.focus_ids(store, "m7", ["m8"]) == [], "沒分到串就不過濾"


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
    assert texts == ["我顯卡燒了", "風扇有轉但沒畫面"], "planner 也只看這串"


def test_late_link_card_is_remembered_and_shown(monkeypatch) -> None:
    env, _ = _setup(monkeypatch, [], {})
    env.plugin._remember_card(LATEST, "[卡片: Suica 新角色是兔子]")
    assert env.plugin._inbound["s1"][alias(LATEST)]["card"] == "[卡片: Suica 新角色是兔子]"
    window = [_message_item(alias(LATEST), "小美", "https://x.com/abc")]
    messages = _load_adapter_module("src.emote").planner_messages(window)
    asyncio.run(env.plugin._wait_for_focus_content("s1", messages))  # 卡片已到，不用等


def test_topic_page_shows_thread_understanding_memory_and_current_settings(monkeypatch) -> None:
    split = _topics(topic_1="none", topic_2="none", topic_3="m1")
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
