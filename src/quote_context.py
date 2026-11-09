"""回覆和叫她的訊息：告訴 planner 誰在找她，被回覆的舊訊息補上它和之前的對話。

MaiBot 的 planner 上下文只用 ``quote="msg_id"`` 標出回覆了哪則、不附內容，replyer 也一樣；
有人 @ 她或回覆她時 MaiBot 會強制開一輪 planner，但觸發原因只寫進日誌，planner 不知道這輪是為誰開的，
常常接著聊最新的話題、把找她的人晾著；別人之間的回覆沒主詞時（「開金口很厲害」），她又會以為在跟她說。所以 planner 開始前：

- 每則回覆的正文前面標出回覆誰：``[回覆「小明」：…]``、``[回覆你：…]``；replyer 也告訴它要回的那則在回覆誰；
- 她上次說話之後叫她的訊息（@、回覆她、喊她名字），列出來要她先回應；
- 被回覆的那則早就滑出上下文時，從 MaiBot 的聊天紀錄找出那則和它之前的幾則接在提示詞後面（還在上下文裡就不補）。
"""

from collections import OrderedDict
from datetime import datetime, timedelta
from typing import Any, Dict, List, Mapping

from maibot_sdk import HookHandler

from .emote import display_message_id, planner_messages, tool_names
from .life.common import readable_chat

# 被回覆的那則連同它之前一共給幾則；往前最多找多久
CONTEXT_MESSAGES = 8
CONTEXT_SPAN = timedelta(hours=12)
# 一輪 planner 最多補幾則回覆（從最新的往前）
PLANNER_LIMIT = 2
# 查過的上下文記幾筆（聊天紀錄不會變，查過就不用再查）
CACHED = 50
# 叫她的訊息最多列幾則（取最新的）
CALLING_LIMIT = 3
CALLING_NOTE = (
    "【有人在找你】你上次說話之後，下面這幾則在叫你（@ 你、回覆你的訊息或喊你名字），這輪就是因為它們才開始的。"
    "先回應它們（reply 帶那則的 msg_id）；其他人正在聊的只是背景，別只顧著接最新的話題、把找你的人晾著：\n"
)
PLANNER_NOTE = (
    "【被回覆的舊訊息】下面這幾則回覆了更早、已經不在上面聊天紀錄裡的訊息（quote 指向的那則）。"
    "這是被回覆的那則和它之前的對話，用來理解對方為什麼這樣回：\n"
)
REPLYER_NOTE = "被回覆的那則已經不在聊天紀錄裡，這是它（最後一則）和之前的對話：\n"


class QuoteContextMixin:
    """需要主插件提供 ctx、_append_system_text，和 EmoteMixin 收訊時記下的 _inbound（quote、addressed）與 _latest_inbound。"""

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

    def _calling_note(self, session_id: str, messages: List[Dict[str, Any]], remembered: Mapping[str, Any]) -> str:
        """她上次說話之後叫她的訊息；私訊每則都在跟她說話，不用特別提。"""
        if not self._latest_inbound.get(session_id, {}).get("group_id"):
            return ""
        last_self = max((index for index, message in enumerate(messages) if message["self"]), default=-1)
        lines = []
        for message in messages[last_self + 1 :]:
            entry = remembered.get(message["msg_id"])
            if entry and entry["addressed"]:
                label = f"[{entry['quote_label']}]" if entry.get("quote_label") else ""
                lines.append(f"- msg_id={message['msg_id']}（{message['user']}）：{label}{message['text']}")
        return CALLING_NOTE + "\n".join(lines[-CALLING_LIMIT:]) if lines else ""

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_quote_context",
        mode="blocking",
        order="late",
        timeout_ms=3000,
    )
    async def add_quote_context_to_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """每則回覆標出回覆誰；列出叫她的訊息（這輪能回覆時）；被回覆那則已經不在上下文的，補上它和之前的對話。"""
        session_id = str(kwargs.get("session_id") or "")
        remembered = self._inbound.get(session_id)
        if not remembered:
            return {"action": "continue"}
        messages = planner_messages(kwargs.get("items"))
        labeled = False
        for message in messages:
            entry = remembered.get(message["msg_id"])
            if entry and entry.get("quote_label"):
                part, end = message["head"]
                part["text"] = f"{part['text'][:end]}[{entry['quote_label']}]{part['text'][end:]}"
                labeled = True
        # 睡覺時段只剩 stop，不用叫她回
        notes = [self._calling_note(session_id, messages, remembered)] if "reply" in tool_names(kwargs) else []
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
        if blocks:
            notes.append(PLANNER_NOTE + "\n\n".join(reversed(blocks)))
        notes = [note for note in notes if note]
        if notes:
            self._append_system_text(kwargs, "\n\n".join(notes))
        return {"action": "continue", "modified_kwargs": kwargs} if notes or labeled else {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="discord_quote_context_reply",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def add_quote_context_to_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """replyer 也只看得到 quote 的 ID：要回的那則是回覆時，告訴它在回覆誰；被回覆那則是舊訊息時，把 planner 那輪查到的對話一起給它。"""
        remembered = self._inbound.get(str(kwargs.get("session_id") or ""), {})
        entry = remembered.get(display_message_id(str(kwargs.get("reply_message_id") or "")))
        if not entry or not entry.get("quote_label"):
            return {"action": "continue"}
        note = f"你要回覆的這則訊息是在{entry['quote_label']}。"
        if context := self._quote_contexts.get(entry["quote"]):
            note += f"\n{REPLYER_NOTE}{context}"
        kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{note}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}
