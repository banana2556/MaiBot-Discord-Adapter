"""待辦：她在各話題串還沒做完的事（agenda.py）決定焦點、給 planner 一行、一輪結束後叫醒她做下一件。"""

import asyncio
import time
from types import SimpleNamespace

from test_emote import _env
from test_typing_indicator import _load_adapter_module

agenda = _load_adapter_module("src.agenda")
topics = _load_adapter_module("src.topics")


def _say(store, topic_id, msg_id, user, text, at, addressed=False):
    message = {"msg_id": msg_id, "user": user, "self": not user, "time": at, "addressed": addressed}
    return topics.add_member(store, topic_id, message, f"[--:--] 「{user}」：{text}" if user else f"[--:--] （你）：{text}")


def test_called_first_then_result_handled_once_and_answering_closes_it() -> None:
    now = time.time()
    store = topics.new_store()
    chat = _say(store, None, "a1", "阿華", "今天好熱", now - 30)
    called = _say(store, None, "m1", "小明", "C醬你覺得呢", now - 300, addressed=True)
    searched = _say(store, None, "q1", "CiKa", "Irys是誰", now - 900, addressed=True)
    _say(store, searched, "s1", "", "我搜搜", now - 890)
    store["topics"][searched]["result_at"] = now - 60

    kinds = {item["topic"]: item["kind"] for item in agenda.open_items(store, now)}
    assert kinds == {called: "called", searched: "result"}, "阿華那串沒在叫她；Irys 那串她說過話之後只剩查完的結論"
    assert agenda.pick(store, now)["topic"] == called, "叫她還沒回的優先"
    assert agenda.pick(store, now, among=["a1"]) is None, "話題分析只能挑這輪看得到的"

    agenda.mark_handled(store, called, now)
    assert agenda.pick(store, now)["topic"] == searched, "當過一次焦點就不再搶，換查完的結論"
    note = agenda.agenda_note(store, now, searched)
    assert "「小明」叫你還沒回（在聊「C醬你覺得呢」，5 分鐘前）" in note and "結論回來了" not in note, "焦點那串不列"

    _say(store, searched, "s2", "", "查到了，是 VTuber", now)
    assert [item["kind"] for item in agenda.open_items(store, now)] == ["called"], "她往那串說了話，結論那件就結束"
    assert chat not in {item["topic"] for item in agenda.open_items(store, now)}


def test_old_calls_expire_and_her_open_question_is_listed() -> None:
    now = time.time()
    store = topics.new_store()
    _say(store, None, "m1", "小明", "C醬在嗎", now - agenda.OPEN_SECONDS - 10, addressed=True)
    asked = _say(store, None, "a1", "阿華", "週末要幹嘛", now - 100)
    _say(store, asked, "s1", "", "你們要去哪？", now - 90)
    assert [(item["topic"], item["kind"]) for item in agenda.open_items(store, now)] == [(asked, "asked")]
    assert "你問了「你們要去哪？」還沒人回" in agenda.agenda_note(store, now, None)


def test_ends_round() -> None:
    def call(name, **args):
        return [{"tool_call": {"func_name": name, "args": args}}]

    assert agenda.ends_round(call("wait")) and agenda.ends_round(call("reply"))
    assert not agenda.ends_round(call("reply", after_reply="continue")) and not agenda.ends_round(call("subagent"))


def test_round_end_wakes_her_once_for_a_call_she_has_not_answered(monkeypatch) -> None:
    env = _env(monkeypatch)
    plugin, now = env.plugin, time.time()
    store = topics.new_store()
    plugin._topics["s1"] = store
    focus = _say(store, None, "a1", "阿華", "今天好熱", now - 30)
    _say(store, None, "m1", "小明", "C醬你覺得呢", now - 20, addressed=True)
    plugin._focus["s1"] = "a1"
    agenda.mark_handled(store, focus, now)
    monkeypatch.setattr(agenda, "NUDGE_DELAY_SECONDS", 0)
    triggered = []

    async def trigger(stream_id, intent, **kwargs):
        triggered.append((stream_id, intent, kwargs.get("reason")))
        return {"success": True}

    env.ctx.maisaka = SimpleNamespace(proactive=SimpleNamespace(trigger=trigger))

    async def scenario():
        await plugin.after_planner_response(session_id="s1", output_items=[{"tool_call": {"func_name": "reply", "args": {}}}])
        await asyncio.sleep(0.01)
        await plugin._nudge_later("s1")  # 同一件不叫第二次

    asyncio.run(scenario())
    assert triggered == [("s1", "「小明」剛剛叫你還沒回：C醬你覺得呢", "話題串的待辦")]
    assert "s1" in plugin._round_over

    # 新的一輪（沒有新訊息）照待辦換到小明那串
    plugin._new_round_focus("s1")
    assert plugin._focus["s1"] == "m1"
