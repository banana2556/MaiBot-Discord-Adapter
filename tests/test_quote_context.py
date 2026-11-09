"""回覆舊訊息：被回覆的那則不在 planner 上下文時，補上它和之前的對話給 planner 和 replyer。"""

import asyncio
from types import SimpleNamespace

from test_emote import FUNNY, _env, _inbound, _message_item

# 兩小時前她說的話（早就不在 planner 上下文），和剛剛有人回覆那則的訊息
OLD, REPLY = "1567700000000000001", "1567799999999999999"
OLD_TIME = 1_790_000_000.0
FOUND = {"message_id": OLD, "timestamp": str(OLD_TIME)}


def _reply_inbound(quote=OLD):
    message = _inbound(REPLY, mentions_bot=True)
    message["raw_message"] = [
        {
            "type": "reply",
            "data": {
                "target_message_id": quote,
                "target_message_content": "好啊，明天見",
                "target_message_sender_nickname": "「麥麥」",
            },
        },
        {"type": "text", "data": "真的嗎"},
    ]
    return message


def _setup(monkeypatch, found=FOUND):
    env = _env(monkeypatch)
    calls = {"get_by_id": [], "readable": []}

    async def get_by_id(message_id, chat_id=""):
        calls["get_by_id"].append((message_id, chat_id))
        return found

    async def build_readable(messages, **kwargs):
        calls["readable"].append(kwargs)
        return "[昨天 20:00] 「小明」說：明天要不要去看展\n[昨天 20:01] 「麥麥」說：好啊，明天見"

    env.ctx.message = SimpleNamespace(get_by_id=get_by_id, build_readable=build_readable)
    env.plugin._remember_inbound("s1", _inbound(FUNNY))
    env.plugin._remember_inbound("s1", _reply_inbound())
    return env, calls


def _kwargs(env, *extra_items):
    alias = env.module.display_message_id
    return {
        "items": [
            {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "人設"}]},
            *extra_items,
            _message_item(alias(FUNNY), "小明", "我剛剛跌倒了哈哈哈"),
            _message_item(alias(REPLY), "小美", "真的嗎"),
        ],
        "session_id": "s1",
    }


def test_planner_and_replyer_get_the_conversation_before_an_old_quoted_message(monkeypatch) -> None:
    env, calls = _setup(monkeypatch)
    result = asyncio.run(env.plugin.add_quote_context_to_planner(**_kwargs(env)))
    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    alias = env.module.display_message_id
    assert note.startswith("【被回覆的舊訊息】") and f"msg_id={alias(REPLY)}（「小美」）" in note
    assert "明天要不要去看展" in note, "planner 看得到被回覆那則之前在聊什麼"
    assert calls["get_by_id"] == [(OLD, "s1")]
    assert calls["readable"][0]["end_time"] == OLD_TIME and calls["readable"][0]["chat_id"] == "s1"

    # 同一輪後面的請求不再查
    asyncio.run(env.plugin.add_quote_context_to_planner(**_kwargs(env)))
    assert len(calls["get_by_id"]) == 1

    # replyer 回這則時也拿得到；回別則不加
    reply = asyncio.run(
        env.plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=REPLY, extra_prompt="原本的要求")
    )
    prompt = reply["modified_kwargs"]["extra_prompt"]
    assert prompt.startswith("原本的要求\n") and "明天要不要去看展" in prompt
    other = asyncio.run(env.plugin.add_quote_context_to_replyer(session_id="s1", reply_message_id=FUNNY))
    assert other == {"action": "continue"}


def test_quoted_message_still_in_context_is_not_repeated(monkeypatch) -> None:
    env, calls = _setup(monkeypatch)
    visible = _message_item(env.module.display_message_id(OLD), "麥麥", "好啊，明天見", self_message=True)
    assert asyncio.run(env.plugin.add_quote_context_to_planner(**_kwargs(env, visible))) == {"action": "continue"}
    assert calls["get_by_id"] == []


def test_quote_maibot_never_saw_falls_back_to_the_quoted_text(monkeypatch) -> None:
    # 例如別的 bot 說的話：MaiBot 沒收過（SDK 回 None），只能給收訊時帶的那一則
    env, calls = _setup(monkeypatch, found=None)
    result = asyncio.run(env.plugin.add_quote_context_to_planner(**_kwargs(env)))
    note = result["modified_kwargs"]["items"][0]["parts"][-1]["text"]
    assert "「麥麥」說：好啊，明天見" in note and calls["readable"] == []


def test_failed_lookup_adds_nothing_and_retries_later(monkeypatch) -> None:
    env, _ = _setup(monkeypatch, found={"success": False, "error": "資料庫忙"})
    assert asyncio.run(env.plugin.add_quote_context_to_planner(**_kwargs(env))) == {"action": "continue"}
    assert any("資料庫忙" in str(line) for line in env.ctx.logs)
    assert env.plugin._quote_contexts == {}, "失敗不記，下一輪再查"

