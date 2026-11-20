"""planner、表達方式選擇和 replyer 看到的聊天：只留焦點那串，標出回覆誰、誰在找她。

話題分析（emote._analyze）選好焦點之後：

- planner 和表達方式選擇（MaiBot 的子請求，也走 planner.before_request）的聊天訊息只留焦點那串，
  加上分完話題之後才出現的（新進來的、她剛說的）；其他話題的訊息和它們的連結提醒都拿掉，
  MaiBot 拿整段上下文比對的 <jargon> 也拿掉（這串的詞彙在聯想裡）。
  提示詞叫模型「別混在一起」常常沒用，直接不給它看才混不進去。要不要說話、回哪一則由 planner 自己決定。
- replyer 的聊天紀錄照同一串過濾：把要留的 msg_id 放進 ``reply_tool_args["_keep_message_ids"]``，
  MaiBot（banana2556/MaiBot 的 custom 分支）在組提示詞、挑表達和分圖片名額之前照它過濾；原版 MaiBot 會忽略。
- 她送出的話直接加進焦點那串，同一回合後面的請求和 replyer 都看得到自己剛說過什麼。
- MaiBot 的上下文只用 ``quote="msg_id"`` 標出回覆了哪則、不附內容：每則回覆的正文前面標出回覆誰
  （``[回覆「小明」：…]``、``[回覆你：…]``），replyer 也告訴它要回的那則在回覆誰。
- 有人 @ 她或回覆她時 MaiBot 會強制開一輪 planner，但觸發原因只寫進日誌：她上次說話之後叫她的訊息列出來要她先回應。
  她說的話在 MaiBot 上下文裡只是 reply 的工具結果，所以「上次說話」用她最後一次送出的時間算，回過的不再列。
- MaiBot 壓縮上下文時較早的訊息換成摘要，摘要常漏掉這串開頭在問什麼；重啟恢復的上下文也可能不在這串：
  這串已經不在上下文的前文從話題串（存在 threads.json）補回來，放在聊天紀錄最前面。
- MaiBot 在每批訊息前插的「时间：…」不送：每則訊息開頭已經有時間（不是今天的加上日期），只留最後一條當作現在時間。
  過濾掉別串後它們只剩空殼；焦點過濾時 MaiBot 的「[上下文恢复]」說明也拿掉（它說的那些恢復的訊息多半不在這串）。
- 焦點以外她還沒做完的事（別串有人叫她、子代理查完了…，agenda.py）各給 planner 一行。
- 快照：replyer 只看 planner 做決定那時已經到的訊息。replyer 寫得慢時新訊息會進來，它會順手回到 planner 沒看過的事
  （「搜完了啦」）；之後才到的留給下一輪，「她看過的截到哪」也照 planner 開始的時間算，下一輪還會提醒她。
- 送出間隔：同一個聊天兩次回話至少隔 REPLY_GAP_SECONDS，不會上一則剛送出下一則又跟著噴出來。
"""

import asyncio
import re
import time
from datetime import date, datetime
from typing import Any, Dict, List, Mapping, Sequence, Set, Tuple

from maibot_sdk import HookHandler

from .emote import display_message_id, planner_messages, strip_emotes, tool_names
from .life.common import sent_text
from .topics import add_member, clock, earlier_items, focus_ids, unanswered

# 同一個聊天兩次回話（不是同一則回覆拆成幾段）之間至少隔幾秒
REPLY_GAP_SECONDS = 8
# 叫她的訊息最多列幾則（取最新的）
CALLING_LIMIT = 3
CALLING_NOTE = (
    "【有人在找你】你上次說話之後，下面這幾則在叫你（@ 你、回覆你的訊息或喊你名字），這輪就是因為它們才開始的。"
    "先回應它們（reply 帶那則的 msg_id）：\n"
)
# linux 沙盒電腦插件在含連結的訊息後面插的提醒（item_id 是「這個前綴＋那則訊息的 item_id」）
LINK_REMINDER = "link-reminder-"
EARLIER_NOTE = "【這串前面的對話】（比下面的紀錄更早、已經不在紀錄裡的部分，照時間排；這幾則不能用 msg_id 回覆）\n"
EARLIER_ITEM_ID = "discord-thread-earlier"
RESTORE_NOTE = "[上下文恢复]"
_TIME_MARK = re.compile(r"时间：\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}")
_HEAD_TIME = re.compile(r'time="(\d{2}:\d{2}(?::\d{2})?)"')


def is_jargon(item: Mapping[str, Any]) -> bool:
    """MaiBot 每輪 planner 前插的黑話參考（src/maisaka/jargon_context_matcher.py，開頭是 <jargon>）。"""
    parts = item.get("parts") if isinstance(item.get("parts"), list) else []
    first = parts[0] if parts and isinstance(parts[0], Mapping) else {}
    return item.get("item_type") == "UserMessageItem" and str(first.get("text") or "").lstrip().startswith("<jargon>")


def first_text(item: Mapping[str, Any]) -> str:
    """只有一段文字的 item 的那段文字；其他是空字串。"""
    parts = item.get("parts") if isinstance(item.get("parts"), list) else []
    return str(parts[0].get("text") or "") if len(parts) == 1 and isinstance(parts[0], Mapping) else ""


def drop_time_marks(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """拿掉 MaiBot 的「时间：…」，只留最後一條（現在時間）。"""
    marks = [
        index
        for index, item in enumerate(items)
        if item.get("item_type") == "UserMessageItem" and _TIME_MARK.fullmatch(first_text(item).strip())
    ]
    drop = set(marks[:-1])
    return [item for index, item in enumerate(items) if index not in drop]


def stamp(at: float, today: date) -> str:
    """訊息時間：今天的只寫 HH:MM，其他天的加上月日。"""
    moment = datetime.fromtimestamp(at)
    return moment.strftime("%H:%M" if moment.date() == today else "%m-%d %H:%M")


def date_heads(messages: Sequence[Dict[str, Any]], today: date) -> None:
    """不是今天的訊息，開頭的 time="HH:MM:SS" 加上月日（時間標記拿掉了，跨日要看得出來）。"""
    for message in messages:
        if not message.get("time") or datetime.fromtimestamp(message["time"]).date() == today:
            continue
        part, end = message["head"]
        day = datetime.fromtimestamp(message["time"]).strftime("%m-%d")
        head = _HEAD_TIME.sub(rf'time="{day} \1"', part["text"][:end], count=1)
        part["text"] = head + part["text"][end:]
        message["head"] = (part, len(head))


def insert_earlier(items: List[Dict[str, Any]], text: str, now: datetime) -> List[Dict[str, Any]]:
    """前文放在第一則聊天訊息前面；沒有聊天訊息時放在最後一條時間標記（或最後一個 item）前面。"""
    item = {
        "item_type": "UserMessageItem",
        "meta": {"item_id": EARLIER_ITEM_ID, "logical_turn_id": None, "timestamp": now.isoformat()},
        "parts": [{"type": "text", "text": text}],
    }
    chat = [index for index, other in enumerate(items) if planner_messages([other])]
    marks = [index for index, other in enumerate(items) if _TIME_MARK.fullmatch(first_text(other).strip())]
    index = chat[0] if chat else marks[-1] if marks else max(len(items) - 1, 0)
    return [*items[:index], item, *items[index:]]


def keep_focus_items(items: List[Dict[str, Any]], keep: Set[str], assigned: Set[str]) -> Tuple[List[Dict[str, Any]], int]:
    """聊天訊息只留 keep 裡的，和最後一則分過話題的之後才出現的；其他 item（系統、工具呼叫與結果、時間標記…）照留。

    Returns:
        (留下的 items, 拿掉幾則聊天訊息)。
    """
    chat = [(index, found[0]["msg_id"]) for index, item in enumerate(items) if (found := planner_messages([item]))]
    last = max((index for index, msg_id in chat if msg_id in assigned), default=-1)
    gone = {index for index, msg_id in chat if index <= last and msg_id not in keep}
    def item_id(item: Mapping[str, Any]) -> str:
        return str((item.get("meta") or {}).get("item_id") or "")

    gone_items = {item_id(items[index]) for index in gone} - {""}
    kept = [
        item
        for index, item in enumerate(items)
        if index not in gone
        and not (item_id(item).startswith(LINK_REMINDER) and item_id(item)[len(LINK_REMINDER) :] in gone_items)
    ]
    return kept, len(gone)


class QuoteContextMixin:
    """需要主插件提供 ctx、_append_system_text、_add_recall_note，和 EmoteMixin 的 _inbound、_latest_inbound、_topics、_focus。"""

    def _focus_keep(self, session_id: str, msg_id: str = "") -> List[str]:
        """要留的 msg_id：msg_id 所在那串（沒給或沒分到串就用這輪的焦點），加上之後才收到、還沒分的；沒有焦點時是空清單。"""
        store = self._topics.get(session_id)
        focus = self._focus.get(session_id, "")
        if not store or not focus:
            return []
        anchor = msg_id if msg_id in store["of"] else focus
        return focus_ids(store, anchor, list(self._inbound.get(session_id, {})))

    def _kept_topics(self, session_id: str, msg_id: str = "") -> List[Dict[str, Any]]:
        """要給模型看的話題串：msg_id（沒給就是焦點）所在那串。"""
        store = self._topics.get(session_id)
        focus = self._focus.get(session_id, "")
        if not store or not focus:
            return []
        topic = store["topics"].get(store["of"].get(msg_id if msg_id in store["of"] else focus))
        return [topic] if topic else []

    def _earlier_note(self, session_id: str, messages: Sequence[Mapping[str, Any]], msg_id: str = "") -> str:
        """要看的話題串裡，已經被 MaiBot 壓縮掉或重啟後不在上下文的前文（幾串的照時間合在一起）。"""
        present = [message["msg_id"] for message in messages]
        before = min((message["time"] for message in messages if message.get("time")), default=None)
        found = sorted(
            (item for topic in self._kept_topics(session_id, msg_id) for item in earlier_items(topic, present, before)),
            key=lambda item: item.get("at", 0),
        )
        today = date.today()
        lines = [
            f"[{stamp(item['at'], today) if item.get('at') else item['time']}] "
            f"{'（你）' if item['self'] else '「' + item['who'] + '」'}：{item['text']}"
            for item in found
        ]
        return EARLIER_NOTE + "\n".join(lines) if lines else ""

    def _join_focus(self, message: Mapping[str, Any]) -> None:
        """她送出的話加進焦點那串（send_service.after_send）；同一回合後面的請求和 replyer 才看得到她剛說過什麼。"""
        session_id = str(message.get("session_id") or "")
        store = self._topics.get(session_id)
        focus = self._focus.get(session_id, "")
        msg_id = display_message_id(str(message.get("message_id") or ""))
        now = time.time()
        if session_id:
            self._last_sent[session_id] = now
            # replyer 只看了 planner 開始前到的（快照），之後才到的還沒回
            self._seen_until[session_id] = self._planner_at.get(session_id, now)
        if not store or focus not in store["of"] or not msg_id or msg_id in store["of"]:
            return
        line = f"[{clock(now)}] （你）：{sent_text(message)[:120]}"
        add_member(store, store["of"][focus], {"msg_id": msg_id, "user": "", "self": True, "time": now}, line)
        self._save_threads()

    def _calling_note(self, session_id: str, messages: Sequence[Mapping[str, Any]], remembered: Mapping[str, Any]) -> str:
        """她上次說話之後叫她的訊息；私訊每則都在跟她說話，不用特別提。"""
        if not self._latest_inbound.get(session_id, {}).get("group_id"):
            return ""
        lines = []
        for message in unanswered(messages, self._seen_until.get(session_id, 0.0)):
            entry = remembered.get(message["msg_id"])
            if entry and entry["addressed"]:
                label = f"[{entry['quote_label']}]" if entry.get("quote_label") else ""
                lines.append(f"- msg_id={message['msg_id']}（{message['user']}）：{label}{message['text']}")
        return CALLING_NOTE + "\n".join(lines[-CALLING_LIMIT:]) if lines else ""

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_focus_thread",
        mode="blocking",
        # 在話題分析（normal）之後：照它選的焦點過濾
        order="late",
        timeout_ms=3000,
    )
    async def keep_focus_thread(self, **kwargs: Any) -> Dict[str, Any]:
        """planner 和表達方式選擇只看焦點那串；每則回覆標出回覆誰；planner 再加上叫她的訊息和聯想。"""
        session_id = str(kwargs.get("session_id") or "")
        remembered = self._inbound.get(session_id)
        items = kwargs.get("items")
        if not remembered or not isinstance(items, list):
            return {"action": "continue"}
        # 表達方式選擇不經過話題分析那個 hook，表情 ID 和表情圖在這裡拿掉
        strip_emotes(items)
        self._track_subagents(session_id, items)
        if keep := self._focus_keep(session_id):
            store = self._topics[session_id]
            items, dropped = keep_focus_items(items, set(keep), set(store["of"]))
            # MaiBot 拿整段上下文比對的黑話（<jargon>）拿掉：這串用得到的詞已經在聯想的「這串的理解」裡
            items = [item for item in items if not is_jargon(item) and not first_text(item).startswith(RESTORE_NOTE)]
            if dropped:
                self.ctx.logger.debug(f"焦點話題串 [{session_id}]：拿掉其他話題 {dropped} 則")
        items = drop_time_marks(items)
        messages = planner_messages(items)
        for message in messages:
            entry = remembered.get(message["msg_id"])
            if entry and entry.get("quote_label"):
                part, end = message["head"]
                part["text"] = f"{part['text'][:end]}[{entry['quote_label']}]{part['text'][end:]}"
        date_heads(messages, date.today())
        # replyer 拿的是同一份壓縮過的上下文，前文也要給它（add_quote_context_to_replyer）
        self._earlier_notes[session_id] = note = self._earlier_note(session_id, messages) if keep else ""
        if note:
            items = insert_earlier(items, note, datetime.now())
        kwargs["items"] = items
        # 有 reply 才是 planner 本身（表達方式選擇沒有工具；睡覺時段只剩 stop，不用叫她回）
        if "reply" in tool_names(kwargs):
            self._planner_at[session_id] = time.time()
            if note := self._calling_note(session_id, messages, remembered):
                self._append_system_text(kwargs, note)
            if note := self._agenda_note(session_id):
                self._append_system_text(kwargs, note)
            self._add_recall_note(kwargs, session_id)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="discord_quote_context_reply",
        mode="blocking",
        order="late",
        # 等送出間隔（REPLY_GAP_SECONDS）再多留一點
        timeout_ms=(REPLY_GAP_SECONDS + 2) * 1000,
    )
    async def add_quote_context_to_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """replyer 只看焦點那串、planner 做決定前到的（_keep_message_ids）；要回的那則是回覆時，告訴它在回覆誰。
        上一則剛送出時先等到滿 REPLY_GAP_SECONDS 再寫。"""
        session_id = str(kwargs.get("session_id") or "")
        target = display_message_id(str(kwargs.get("reply_message_id") or ""))
        if (wait := REPLY_GAP_SECONDS - (time.time() - self._last_sent.get(session_id, 0.0))) > 0:
            await asyncio.sleep(wait)
        changed = False
        if keep := self._focus_keep(session_id, target):
            decided = self._planner_at.get(session_id, time.time())
            remembered = self._inbound.get(session_id, {})
            late = [msg_id for msg_id in keep if (remembered.get(msg_id) or {}).get("at", 0.0) > decided and msg_id != target]
            keep = [msg_id for msg_id in keep if msg_id not in late]
            kwargs["reply_tool_args"] = {**(kwargs.get("reply_tool_args") or {}), "_keep_message_ids": keep}
            self.ctx.logger.info(
                f"回覆只看焦點那串 [{session_id}]：{len(keep)} 則" + (f"，planner 決定後才到的 {len(late)} 則留給下一輪" if late else "")
            )
            changed = True
            if note := self._earlier_notes.get(session_id):
                kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{note}".strip()
        entry = self._inbound.get(session_id, {}).get(target)
        if entry and entry.get("quote_label"):
            kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n你要回覆的這則訊息是在{entry['quote_label']}。".strip()
            changed = True
        return {"action": "continue", "modified_kwargs": kwargs} if changed else {"action": "continue"}
