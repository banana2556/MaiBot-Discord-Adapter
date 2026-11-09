"""回覆舊訊息時，補上被回覆的那則和它之前的對話。

MaiBot 的 planner 上下文只用 ``quote="msg_id"`` 標出回覆了哪則、不附內容，replyer 也一樣；
被回覆的訊息早就滑出上下文時，她只知道有人回覆了某則，不知道對方在回什麼。
planner 和 replyer 開始前，從 MaiBot 的聊天紀錄找出那則和它之前的幾則接在提示詞後面；那則還在上下文裡就不補。
"""

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping

from maibot_sdk import HookHandler

from .emote import display_message_id, planner_messages
from .life.common import readable_chat

# 被回覆的那則連同它之前一共給幾則；往前最多找多久
CONTEXT_MESSAGES = 8
CONTEXT_SPAN = timedelta(hours=12)
# 一輪 planner 最多補幾則回覆（從最新的往前）
PLANNER_LIMIT = 2
# 查過的上下文記幾筆（聊天紀錄不會變，查過就不用再查）
CACHED = 50
PLANNER_NOTE = (
    "【被回覆的舊訊息】下面這幾則回覆了更早、已經不在上面聊天紀錄裡的訊息（quote 指向的那則）。"
    "這是被回覆的那則和它之前的對話，用來理解對方為什麼這樣回：\n"
)
REPLYER_NOTE = "你要回覆的這則訊息是在回覆更早的對話，這是被回覆的那則（最後一則）和它之前的對話：\n"


class QuoteContextMixin:
    """需要主插件提供 ctx、_append_system_text，和 EmoteMixin 收訊時記下的 _inbound（裡面有 quote）。"""

    def _init_quote_state(self) -> None:
        self._quote_contexts: "OrderedDict[str, str]" = OrderedDict()

    async def _quote_context(self, session_id: str, entry: Mapping[str, Any]) -> str:
        """被回覆的那則和它之前的對話；MaiBot 沒有那則時只給收訊時帶的那一則，讀取失敗回傳空字串。"""
        quote = entry["quote"]
        if quote in self._quote_contexts:
            return self._quote_contexts[quote]
        found = await self.ctx.message.get_by_id(quote, chat_id=session_id)
        # SDK 成功時直接回訊息（找不到是 None），失敗才回 {"success": False, "error": ...}
        if isinstance(found, Mapping) and not found.get("timestamp"):
            self.ctx.logger.warning(f"讀取被回覆的訊息失敗：{found.get('error')}")
            return ""
        text = ""
        if isinstance(found, Mapping):
            at = datetime.fromtimestamp(float(found["timestamp"]))
            text = await readable_chat(self.ctx, session_id, at - CONTEXT_SPAN, at, CONTEXT_MESSAGES)
        text = text or entry.get("quote_text") or ""
        if text:
            self._quote_contexts[quote] = text
            while len(self._quote_contexts) > CACHED:
                self._quote_contexts.popitem(last=False)
        return text

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_quote_context",
        mode="blocking",
        order="late",
        timeout_ms=3000,
    )
    async def add_quote_context_to_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """這輪 planner 看得到的回覆裡，被回覆那則已經不在上下文的，補上它和之前的對話。"""
        session_id = str(kwargs.get("session_id") or "")
        remembered = self._inbound.get(session_id)
        if not remembered:
            return {"action": "continue"}
        messages = planner_messages(kwargs.get("items"))
        visible = {message["msg_id"] for message in messages}
        blocks = []
        for message in reversed(messages):
            entry = remembered.get(message["msg_id"])
            if not entry or not entry.get("quote") or display_message_id(entry["quote"]) in visible:
                continue
            context = await self._quote_context(session_id, entry)
            if context:
                blocks.append(f"msg_id={message['msg_id']}（{message['user']}）回覆的是這段的最後一則：\n{context}")
            if len(blocks) >= PLANNER_LIMIT:
                break
        if not blocks:
            return {"action": "continue"}
        return self._append_system_text(kwargs, PLANNER_NOTE + "\n\n".join(reversed(blocks)))

    @HookHandler(
        "maisaka.replyer.before_request",
        name="discord_quote_context_reply",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def add_quote_context_to_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """replyer 也只看得到 quote 的 ID：要回的那則在回覆舊訊息時，把 planner 那輪查到的對話一起給它。"""
        remembered = self._inbound.get(str(kwargs.get("session_id") or ""), {})
        entry = remembered.get(display_message_id(str(kwargs.get("reply_message_id") or "")))
        context = self._quote_contexts.get(entry["quote"]) if entry and entry.get("quote") else None
        if not context:
            return {"action": "continue"}
        kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{REPLYER_NOTE}{context}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}
