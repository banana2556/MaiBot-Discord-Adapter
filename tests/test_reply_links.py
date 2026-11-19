"""回覆前文：明確連結、決策模型順便判斷的「在接哪一則」、有人在找你的標註、replyer 只看前文、舊頁面設定的清理。"""

import asyncio
import json

from test_daily_features import _plugin
from test_emote import FUNNY, LATEST, _answer, _env, _inbound, _message_item, _receive_all
from test_emote import _planner as _emote_planner
from test_typing_indicator import _load_adapter_module

links = _load_adapter_module("src.reply_links")


def _m(msg_id, user, text, time, quote="", self_message=False):
    return {"msg_id": msg_id, "user": user, "text": text, "time": time, "quote": quote, "self": self_message}


# 兩個話題交錯：爬山（小明、阿花、她、小美）和顯卡（老王、阿強）
CHAT = [
    _m("m1", "小明", "週末去爬山嗎", 0),
    _m("m2", "阿花", "好啊", 10, quote="m1"),
    _m("m3", "老王", "顯卡又漲了", 20),
    _m("m4", "阿強", "真的貴", 30, quote="m3"),
    _m("m5", "小明", "要早點出發", 40),
    _m("m6", "阿花", "你買哪張", 50),
    _m("m7", "麥麥", "我也想去", 60, quote="m2", self_message=True),
    _m("m8", "小美", "你也要去？", 70),
    _m("m9", "小明", "那就八點", 400),
]


def test_explicit_links_reply_chain_mentions_her_and_same_speaker() -> None:
    def context(target, inbound=None, judged=()):
        return links.context_ids(CHAT, target, inbound or {}, list(judged))

    assert context("m4") == ["m3"]
    assert context("m7") == ["m1", "m2"], "沿回覆鏈往上"
    assert context("m5") == ["m1"], "同一人兩分鐘內說的"
    assert context("m9") == [], "隔太久的不算連發"
    # @ 的人剛說的；阿花 40 秒前說的也算連發（她換了話題時這條會多帶一句，只是多給 replyer 看）
    assert context("m6", {"mentions": ["「老王」"]}) == ["m2", "m3"]
    assert context("m8", {"addressed": True}) == ["m7"], "叫她的接她上一句"
    assert context("m5", judged=["m2"]) == ["m1", "m2"], "加上決策模型判斷的"
    assert context("nope") == []


def test_link_questions_ask_unlinked_messages_and_answers_need_enough_probability() -> None:
    numbers = {id(m): str(n) for n, m in enumerate((m for m in CHAT if not m["self"]), 1)}
    questions, keys = links.link_questions(CHAT[:8], numbers, {"m8": {"addressed": True}})
    # 叫她的先問，再補最近的；有回覆的（m2、m4）不問；最多 4 題
    assert list(questions) == ["link_3", "link_5", "link_6", "link_7"]
    assert keys["link_7"]["target"] == "m8"
    assert list(questions["link_7"]["criteria"]) == ["1", "2", "3", "4", "5", "6", "you", "none"]
    assert questions["link_7"]["criteria"]["you"] == "（你）：我也想去"
    assert questions["link_3"]["instructions"].startswith("#3「老王」：顯卡又漲了")

    answers = {
        "link_7": {"choice": "you", "probabilities": {"you": 0.8, "6": 0.25, "none": 0.1}},
        "link_5": {"choice": "none", "probabilities": {"none": 0.7, "1": 0.2}},
        "link_3": {"choice": "1", "confidence": 0.5},  # 沒給機率時信它選的那個
    }
    judged, summary = links.parse_links(answers, keys, 0.3)
    assert judged == {"m3": ["m1"], "m5": [], "m8": ["m7"]}, "沒回答的那題（link_6）不記"
    assert summary == "#3→#1（0.50）；#5→沒在接誰（0.70）；#7→你（0.80）"
    assert links.parse_links(None, keys, 0.3) == ({}, "")


def test_decision_request_also_asks_what_each_message_continues(monkeypatch) -> None:
    alias = _load_adapter_module("src.emote").display_message_id
    answer = {**_answer("text", 0.8), "link_3": {"choice": "1", "probabilities": {"1": 0.6, "none": 0.2}}}
    env = _env(monkeypatch, answers=[answer], decision={"reply_context_enabled": True})
    _receive_all(env.plugin)
    asyncio.run(env.plugin.decide_emote_in_planner(**_emote_planner(env.module)))
    state, questions = env.asked[0]
    assert "# 是訊息編號" in state and "#1 「小明」" in state
    assert set(questions) >= {"scene", "target", "emotion", "link_1", "link_2", "link_3"}, "同一次請求一起問"
    assert list(questions["link_1"]["criteria"]) == ["you", "none"], "前面只有她說的那句"
    assert env.plugin._reply_links["s1"][alias(LATEST)] == [alias(FUNNY)]
    assert env.plugin._decision_stats.state["decisions"][0]["links"] == "#3→#1（0.60）", "沒回答的題目不列"
    assert asyncio.run(env.plugin.webui_decision())["decisions"][0]["links"] == "#3→#1（0.60）"

    plain = _env(monkeypatch, answers=[_answer("text", 0.8)])
    _receive_all(plain.plugin)
    asyncio.run(plain.plugin.decide_emote_in_planner(**_emote_planner(plain.module)))
    assert not any(name.startswith("link_") for name in plain.asked[0][1]), "沒開回覆前文就不問"


def test_calling_note_marks_what_each_message_continues(monkeypatch) -> None:
    alias = _load_adapter_module("src.emote").display_message_id
    env = _env(monkeypatch, decision={"reply_context_enabled": True})
    env.plugin._remember_inbound("s1", _inbound(FUNNY))
    env.plugin._remember_inbound("s1", _inbound(LATEST, mentions_bot=True))
    env.plugin._reply_links["s1"] = {alias(LATEST): [alias(FUNNY)]}
    kwargs = {
        "items": [
            {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
            _message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈"),
            _message_item(alias(LATEST), "小美", "真的假的"),
        ],
        "tool_definitions": [{"type": "function", "function": {"name": "reply"}}],
        "session_id": "s1",
    }
    result = asyncio.run(env.plugin.add_quote_context_to_planner(**kwargs))
    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    assert f"msg_id={alias(LATEST)}（小美）：真的假的（在接 msg_id={alias(FUNNY)}（小明））" in note


topics = _load_adapter_module("src.topics")


def _at(clock):
    from datetime import datetime

    return datetime.fromisoformat(f"2026-11-19T{clock}.218").timestamp()


def _replyer_items():
    """MaiBot 1.4.6 的 replyer 請求：系統提示加一則 user 訊息，聊天紀錄整段在裡面，圖片跟在那行後面。"""
    image = {"type": "image", "image_format": "gif", "image_base64": "R0lG"}
    return [
        {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
        {
            "item_type": "UserMessageItem",
            "meta": {},
            "parts": [
                {"type": "text", "text": "\n".join([HEAD, PS5, "[09:00:28] 「老王」说：<a:dissolve:1>[表情包]"])},
                image,
                {"type": "text", "text": "\n" + "\n".join([LINK, CARD, "[09:19:21] 「Clover」说：<a:pet:2>[表情包]"])},
                image,
                {"type": "text", "text": "\n" + "\n".join([TARGET, MINE]) + TAIL},
            ],
        },
    ]


HEAD = "你正在群里聊天，下面是群里正在聊的内容:\n当前时间：2026-11-19 09:25:42"
PS5 = "[08:59:12] 「阿強」说：為什麼PS5不是每個人都有的東西"
LINK = "[09:06:36] 「海苔」说：https://www.plurk.com/p/x"
CARD = "[09:06:36] 「ermiana」[回复了「海苔」的消息: https://www.plurk.com/p/x] 说：[卡片: 要入坑FF14\n\n買房要打高難嗎]"
TARGET = "[09:23:33] 「ODST」说：國際服用定型話就能溝通了"
MINE = "[09:24:26] Claude醬(你)说：買房不用打高難"
TAIL = "\n\n现在「ODST」说的：國際服用定型話就能溝通了 引起了你的注意。\n现在，你说："


def test_replyer_only_sees_the_targets_topic_thread(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"reply_context_enabled": True})
    env.plugin._remember_inbound("s1", _inbound("103"))
    store = topics.new_store()
    thread = None
    for msg_id, user, clock, me in (("101", "海苔", "09:06:36", False), ("102", "ermiana", "09:06:36", False),
                                    ("103", "ODST", "09:23:33", False), ("104", "Claude醬", "09:24:26", True)):
        thread = topics.add_member(store, thread, {"msg_id": msg_id, "user": user, "time": _at(clock), "self": me}, "")
    topics.add_member(store, None, {"msg_id": "100", "user": "阿強", "time": _at("08:59:12"), "self": False}, "")
    env.plugin._topics = {"s1": store}
    kwargs = {"items": _replyer_items(), "session_id": "s1", "reply_message_id": "103"}
    result = asyncio.run(env.plugin.keep_reply_context(**kwargs))
    parts = result["modified_kwargs"]["items"][1]["parts"]
    assert parts == [{"type": "text", "text": "\n".join([HEAD, LINK, CARD, TARGET, MINE]) + TAIL}], (
        "別的話題和只有表情的行連同圖片都拿掉，她在這串說的話留著"
    )

    # 要回的那則沒分到話題串、沒開回覆前文：都不動
    assert asyncio.run(env.plugin.keep_reply_context(**{**kwargs, "items": _replyer_items(), "reply_message_id": "999"})) == {
        "action": "continue"
    }
    off = _env(monkeypatch)
    off.plugin._remember_inbound("s1", _inbound("103"))
    off.plugin._topics = {"s1": store}
    assert asyncio.run(off.plugin.keep_reply_context(**{**kwargs, "items": _replyer_items()})) == {"action": "continue"}


def test_old_page_settings_from_removed_features_are_dropped(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path)
    path = tmp_path / "daily_settings.json"
    # 上一版「話題切分」頁存的設定，和已經不歸頁面管的欄位
    path.write_text(
        json.dumps({"threads": {"enabled": True, "ledger_every": 15}, "decision": {"enabled": True, "gone": 1}}),
        encoding="utf-8",
    )
    plugin._load_page_settings(path)
    settings = plugin._load_settings()
    assert settings.decision.enabled is True and settings.decision.reply_context_enabled is False
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "threads" not in saved and "gone" not in saved["decision"]
