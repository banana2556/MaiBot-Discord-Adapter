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


def _item(msg_id, user, text, quote=""):
    attrs = f'msg_id="{msg_id}"' + (f' quote="{quote}"' if quote else "")
    head = f'<message {attrs} time="15:11:30" user="{user}">\n'
    return {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": head + text}]}


def _replyer_items():
    return [
        {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
        _item("100", "小明", "週末去爬山嗎"),
        _item("101", "老王", "顯卡又漲了"),
        _item("102", "阿強", "真的貴", quote="101"),
        _item("103", "阿花", "好啊", quote="100"),
        {"item_type": "AssistantMessageItem", "meta": {}, "parts": [{"type": "text", "text": "我也想去"}]},
        _item("104", "老王", "4090 要三萬"),
        _item("105", "阿強", "瘋了"),
        _item("106", "老王", "等明年吧"),
        _item("107", "阿強", "也是"),
        _item("108", "老王", "嗯"),
        {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "你想要回复的消息是…"}]},
    ]


def test_replyer_only_sees_the_targets_context_and_recent_messages(monkeypatch) -> None:
    env = _env(monkeypatch, decision={"reply_context_enabled": True})
    env.plugin._remember_inbound("s1", _inbound("103"))
    kwargs = {"items": _replyer_items(), "session_id": "s1", "reply_message_id": "103"}
    result = asyncio.run(env.plugin.keep_reply_context(**kwargs))
    items = result["modified_kwargs"]["items"]
    kept = [item["parts"][0]["text"].split('"')[1] for item in items if item["parts"][0]["text"].startswith("<message")]
    assert kept == ["100", "103", "104", "105", "106", "107", "108"], "顯卡那段較早的兩則略過"
    assert items[3]["item_type"] == "AssistantMessageItem", "她說過的話留著"
    assert items[-1]["parts"][0]["text"].startswith("你想要回复"), "不是聊天訊息的指示留著"
    assert items[0]["parts"][-1]["text"] == (
        "【回覆前文】你要回的 msg_id=103（阿花）在接：\n- msg_id=100（小明）：週末去爬山嗎\n"
        "群裡同時在聊的其他話題（2 則）這次先略過，回覆只接這段。"
    )

    # 要回的那則不在上下文、沒開回覆前文：都不動
    assert asyncio.run(env.plugin.keep_reply_context(**{**kwargs, "items": _replyer_items(), "reply_message_id": "999"})) == {
        "action": "continue"
    }
    off = _env(monkeypatch)
    off.plugin._remember_inbound("s1", _inbound("103"))
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
