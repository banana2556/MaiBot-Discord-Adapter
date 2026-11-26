"""話題分析的入口和 discord_emote 工具。

- 群聊收到新訊息後第一次進 planner 時，拿 planner 這輪看到的同一份上下文：
  1. 問決策模型把還沒分配的訊息分到話題串（topics.py）；
  2. 選焦點：她上次說話之後叫她的最新一則，沒有就是最新一則別人說的；
  3. 聯想（recall.py）。
  之後 planner、表達方式選擇和 replyer 都只看焦點那串（quote_context.py 過濾）；要不要說話、按不按表情、
  回哪一則都由 planner 自己決定。
- planner 的上下文拿掉自訂表情的 ID 和圖，也拿掉 MaiBot 原生的 send_emoji（discord_emote 也能發貼圖，兩個都留就是兩套決定）。
- discord_emote 工具照情緒用向量挑伺服器的表情或貼圖（EmoteLibrary），不經過 LLM：排除這個聊天最近用過的，
  再從前 PICK_POOL 名照相似度加權抽一張，免得每次都發同一張。
- 決策模型沒開時偶爾照 chat.emote_chance 提醒 planner 可以用 discord_emote。
"""

import base64
import hashlib
import html
import math
import random
import re
import time
from collections import OrderedDict, deque
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import discord
from maibot_sdk import API, HookHandler, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from ..config import PAGE_FIELDS
from ..constants import DEFAULT_PLATFORM_NAME
from .agenda import mark_handled
from .decision import TIMEOUT_SECONDS
from .decision import ask as ask_decision
from .life.common import load_state, save_state, short_time
from .reply_links import EMOTE_ONLY, plain
from .topics import (
    apply_topics,
    dump_stores,
    focus_message,
    hide_urls,
    load_stores,
    new_store,
    numbered,
    topic_questions,
    with_card,
)

# 決策模型沒開時，chat.emote_chance 命中就把這段接在 planner 系統提示詞後面
EMOTE_NUDGE = (
    "【表情】這一輪如果有值得表態的訊息（好笑、好消息、附和、驚訝、無語），"
    "可以用 discord_emote 在那則訊息按個 reaction；可以和 reply 同一輪一起呼叫，也可以只按表情不說話。沒有合適的就略過。"
)
# 插件替她送的表情和文字同步進 planner 歷史時標成她自己說的；不給的話主程式記成 plugin_send，
# planner 上下文裡就沒有 is_self_message，會當成一個也叫她名字的群友在說話
SELF_SENT = "guided_reply"
SEND_KIND_LABELS = {"tool": "planner 工具"}
# 頁面「測試連線」送的範例聊天和題目
DECISION_TEST_STATE = "最近的群聊（越下面越新，# 是訊息編號）：\n#1 「小明」：我終於抽到限定角色了！！！\n#2 「阿華」：笑死 歐皇"
DECISION_TEST_QUESTIONS = {
    "topic_2": {
        "type": "choice",
        "instructions": "#2 這則在接哪一串話題？",
        "criteria": {"m1": "跟 #1 同一串：小明抽到限定角色", "none": "開了新話題"},
    }
}
# 頁面只列最近幾筆判斷與送出，全部的紀錄還是照 decision.py 的上限保存
PAGE_RECENT = 10
# 「話題串」頁每個群聊最多列幾串
PAGE_THREADS = 8
# MaiBot 的暱稱與別名（bot.nickname / bot.alias_names）多久重讀一次，用來判斷訊息是不是在叫她
BOT_NAMES_REFRESH_SECONDS = 600
# 每個聊天記住最近幾則收到的訊息（planner 上下文裡只有 msg_id 別名，靠這裡換回 Discord 訊息 ID）
INBOUND_REMEMBERED = 60
# 挑表情：向量排序取前 PICK_POOL 名，越接近越容易抽中（溫度越小越偏向最接近的）；
# 同一個聊天最近 RECENT_PICKS 次用過的先排除，剩不到 MIN_FRESH 個才放回來
PICK_POOL = 8
PICK_TEMPERATURE = 0.05
RECENT_PICKS = 10
MIN_FRESH = 3

_MESSAGE_HEAD = re.compile(r'<message ((?:[\w-]+="[^"]*"\s*)*)>\n?')
_ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')
# 收訊時（recv_handler/message_handler.py）自訂表情是「<:名:ID>」文字＋緊接一張圖，貼圖是「[貼圖：名（說明）]」＋一張圖
_CUSTOM_EMOJI = re.compile(r"<a?:\w+:\d+>")
_STICKER_LABEL = re.compile(r"\[貼圖：[^\]]*\]")


def strip_emotes(items: Any) -> None:
    """planner 上下文的聊天訊息拿掉自訂表情（<:名:ID> 和它的圖）和貼圖的圖；貼圖的名字和說明留著。

    表情 ID 和表情圖對判斷話題沒有幫助，只會被當成內容。圖在收訊時照送，MaiBot 的表情庫要收（discord_emote 靠它）；
    這裡只改這一輪請求看到的內容。整則只剩表情時寫成 reply_links.EMOTE_ONLY。
    """
    for item in items if isinstance(items, list) else []:
        parts = item.get("parts") if isinstance(item, dict) else None
        if not isinstance(parts, list) or not parts or not isinstance(parts[0], dict):
            continue
        head = _MESSAGE_HEAD.match(str(parts[0].get("text") or "")) if parts[0].get("type") == "text" else None
        if head is None:
            continue
        # ponytail: 圖照順序配給前面的表情和貼圖；讀不到圖的表情、Lottie 貼圖沒有圖，會多吃掉同一則後面的一張真圖片
        pending = 0
        kept: List[Dict[str, Any]] = []
        for index, part in enumerate(parts):
            if part.get("type") == "text":
                text = str(part.get("text") or "")
                pending += len(_CUSTOM_EMOJI.findall(text)) + len(_STICKER_LABEL.findall(text))
                part["text"] = _CUSTOM_EMOJI.sub("", text)
                if index == 0 or part["text"].strip():
                    kept.append(part)
            elif part.get("type") == "image" and pending:
                pending -= 1
            else:
                kept.append(part)
        rest = str(kept[0]["text"])[head.end() :] + "".join(str(part.get("text") or "") for part in kept[1:] if part.get("type") == "text")
        if not rest.strip() and len(kept) == 1:
            kept[0]["text"] = str(kept[0]["text"]).rstrip("\n") + "\n" + EMOTE_ONLY
        item["parts"] = kept


def named(name: str, description: Optional[str]) -> str:
    """向量比對用的文字：名字放前面，才找得到「叫名字」的貼圖或表情。"""
    return f"{name}：{description}" if description else name


def display_message_id(message_id: str) -> str:
    """MaiBot planner 上下文裡顯示的 msg_id：超過 12 字元的 ID 換成 ``m`` + 6 位 base32 雜湊。

    照抄 MaiBot ``maisaka/context/message_id_alias.py``（2026-10-02 起）；改了的話這裡也要跟著改，有測試釘住。
    """
    if len(message_id) <= 12:
        return message_id
    return "m" + base64.b32encode(hashlib.sha1(message_id.encode("utf-8")).digest()).decode("ascii").lower()[:6]


def planner_messages(items: Any) -> List[Dict[str, Any]]:
    """從 planner hook 的 items 取出聊天訊息：每則一個 item，第一段文字開頭是 ``<message msg_id=… user=…>``。

    ``head`` 是那段文字的 part（可以改，改了會送進這輪請求）和訊息頭的長度，要在正文前面加字時用。
    ``quote`` 是它回覆的那則的 msg_id（沒回覆時是空字串），``time`` 是訊息時間（epoch 秒，item 沒帶時間時是 None）。
    """
    messages: List[Dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        parts = item.get("parts") if isinstance(item, dict) else None
        parts = parts if isinstance(parts, list) else []
        text_parts = [part for part in parts if isinstance(part, dict) and part.get("type") == "text"]
        texts = [part.get("text") or "" for part in text_parts]
        head = _MESSAGE_HEAD.match(texts[0]) if texts else None
        if head is None:
            continue
        attributes = {key: html.unescape(value) for key, value in _ATTRIBUTE.findall(head.group(1))}
        images = sum(1 for part in parts if isinstance(part, dict) and part.get("type") == "image")
        content = (texts[0][head.end() :] + "".join(texts[1:])).strip() + " [圖片]" * images
        stamp = (item.get("meta") or {}).get("timestamp")
        messages.append(
            {
                "msg_id": attributes.get("msg_id", ""),
                "user": plain(attributes.get("group_card") or attributes.get("user", "")),
                "self": attributes.get("is_self_message") == "true",
                "text": " ".join(content.split())[:120],
                "quote": attributes.get("quote", "").split(",")[0],
                "time": datetime.fromisoformat(stamp).timestamp() if stamp else None,
                "head": (text_parts[0], head.end()),
            }
        )
    return messages


# 附圖給決策模型時整次請求的秒數上限；圖太大就不附（base64 字數）
IMAGE_TIMEOUT_SECONDS = 8
IMAGE_MAX_CHARS = 1_500_000


def first_image(message: Mapping[str, Any]) -> str:
    """入站訊息的第一張圖片（base64）；沒有或太大時是空字串。表情包不算（type 是 emoji）。"""
    raw = message.get("raw_message")
    for segment in raw if isinstance(raw, list) else []:
        if isinstance(segment, Mapping) and segment.get("type") == "image":
            data = str(segment.get("binary_data_base64") or segment.get("data") or "")
            return data if 100 < len(data) <= IMAGE_MAX_CHARS else ""
    return ""


def image_sha256(message: Mapping[str, Any]) -> str:
    """入站訊息第一張圖片的 sha256（MaiBot 存圖片描述用的鍵）；沒有圖片時是空字串。不限大小。"""
    raw = message.get("raw_message")
    for segment in raw if isinstance(raw, list) else []:
        if isinstance(segment, Mapping) and segment.get("type") == "image":
            try:
                data = base64.b64decode(str(segment.get("binary_data_base64") or segment.get("data") or ""), validate=True)
            except ValueError:
                return ""
            return hashlib.sha256(data).hexdigest() if data else ""
    return ""


def quoted(message: Mapping[str, Any]) -> str:
    """入站訊息回覆了誰：「回覆「名字」：內容開頭」（回覆她時寫「回覆你」）；沒有回覆時是空字串。

    標在這則正文前面：planner 上下文只用 quote 屬性標出回覆哪則，沒主詞的回覆（「開金口很厲害」）她會以為在跟她說。
    """
    info = message.get("message_info") if isinstance(message.get("message_info"), Mapping) else {}
    extra = info.get("additional_config") if isinstance(info.get("additional_config"), Mapping) else {}
    raw = message.get("raw_message")
    for segment in raw if isinstance(raw, list) else []:
        data = segment.get("data") if isinstance(segment, Mapping) and segment.get("type") == "reply" else None
        if isinstance(data, Mapping) and data.get("target_message_id"):
            name = data.get("target_message_sender_cardname") or data.get("target_message_sender_nickname") or ""
            content = " ".join(str(data.get("target_message_content") or "").split())
            who = "你" if extra.get("discord_replies_to_bot") else name or "一則訊息"
            label = f"回覆{who}：{content[:30]}{'…' if len(content) > 30 else ''}" if content else f"回覆{who}"
            return label
    return ""


def _at_segments(message: Mapping[str, Any]) -> List[Mapping[str, Any]]:
    raw = message.get("raw_message")
    return [
        segment["data"]
        for segment in (raw if isinstance(raw, list) else [])
        if isinstance(segment, Mapping) and segment.get("type") == "at" and isinstance(segment.get("data"), Mapping)
    ]


def mentioned_people(message: Mapping[str, Any]) -> Dict[str, str]:
    """入站訊息 @ 的人：{user_id: 伺服器暱稱或名字}；聯想時查他們的記憶。"""
    return {
        str(data["target_user_id"]): str(data.get("target_user_cardname") or data.get("target_user_nickname") or "")
        for data in _at_segments(message)
        if data.get("target_user_id")
    }


def tool_names(kwargs: Mapping[str, Any]) -> set[str]:
    """planner Hook 載荷裡本輪候選工具的名稱（OpenAI function 結構）。"""
    tools = kwargs.get("tool_definitions")
    return {
        str((tool.get("function") or {}).get("name") or "")
        for tool in (tools if isinstance(tools, list) else [])
        if isinstance(tool, dict)
    }


def _avg(total: int, count: int) -> int:
    return round(total / count) if count > 0 else 0


class EmoteMixin:
    """話題分析與 discord_emote；需要主插件提供 ctx、_load_settings、_append_system_text、_thread_routing、
    _client_manager、_message_handler、_native_emoji、_decision_stats、_emote_library。"""

    def _init_emote_state(self) -> None:
        # 有新的 Discord 訊息、還沒分析過話題的聊天；一回合 planner 會請求好幾次，只有第一次分析
        self._unanalyzed: set[str] = set()
        # 每個聊天最近收到的訊息（照收到的順序）：msg_id（planner 上下文顯示的別名）-> 原始 ID、有沒有在跟她說話、能不能按表情
        self._inbound: Dict[str, "OrderedDict[str, Dict[str, Any]]"] = {}
        # 每個聊天最新一則的頻道與發話人，送出時找頻道和訊息用
        self._latest_inbound: Dict[str, Dict[str, Any]] = {}
        # 每個聊天的話題串（topics.py）；存在資料目錄的 threads.json（_save_threads），插件重啟後讀回
        self._topics: Dict[str, Dict[str, Any]] = {}
        self._threads_path: Optional[Path] = None
        # 每個聊天這輪的焦點 msg_id：planner、表達方式選擇和 replyer 只看它那串
        self._focus: Dict[str, str] = {}
        # 每個聊天最近挑過的表情 key，下次先排除
        self._recent_picks: Dict[str, "deque[str]"] = {}
        # MaiBot 設定的暱稱與別名，和它的「被提及必回覆」用同一份
        self._bot_names: List[str] = []
        self._bot_names_at = 0.0
        # 每個聊天她上次說話時看過的訊息截到哪（那輪 planner 開始的時間，epoch 秒）：在那之前叫她的，她已經回過了
        self._seen_until: Dict[str, float] = {}
        # 每個聊天她最後一次送出的時間，和最近一次會回話的 planner 請求開始的時間（replyer 的快照照它切）
        self._last_sent: Dict[str, float] = {}
        self._planner_at: Dict[str, float] = {}
        # 每個聊天上一次 planner 補上的「這串前面說過的」，replyer 也用
        self._earlier_notes: Dict[str, str] = {}

    def _load_threads(self, path: Path) -> None:
        """讀回話題串：重啟後 MaiBot 恢復的上下文、被壓縮掉的前文，都還對得上是哪一串。"""
        self._threads_path = path
        self._topics, self._seen_until = load_stores(load_state(path))

    def _save_threads(self) -> None:
        if self._threads_path is not None:
            save_state(self._threads_path, dump_stores(self._topics, self._seen_until))

    # ===== 收訊與 planner =====

    def _is_calling_her(self, message: Mapping[str, Any]) -> bool:
        """這則是不是在叫她；和 MaiBot 判斷「被提及」一樣：@ 她、回覆她的訊息、私訊，
        或提到 bot.nickname / bot.alias_names 裡的名字。睡覺被吵醒和選焦點共用這一個判斷。"""
        info = message.get("message_info") if isinstance(message.get("message_info"), Mapping) else {}
        extra = info.get("additional_config") if isinstance(info.get("additional_config"), Mapping) else {}
        text = str(message.get("processed_plain_text") or "")
        return (
            bool(extra.get("discord_mentions_bot"))
            or not info.get("group_info")
            or any(name in text for name in self._bot_names)
        )

    def _remember_inbound(self, session_id: str, message: Mapping[str, Any]) -> None:
        """chat.receive 時記下這則：之後在 planner 上下文裡看到它的 msg_id，才換得回 Discord 訊息。"""
        info = message.get("message_info") if isinstance(message.get("message_info"), Mapping) else {}
        group = info.get("group_info") if isinstance(info.get("group_info"), Mapping) else {}
        user = info.get("user_info") if isinstance(info.get("user_info"), Mapping) else {}
        extra = (info or {}).get("additional_config") or {}
        extra = extra if isinstance(extra, Mapping) else {}
        message_id = str(message.get("message_id") or "")
        self._unanalyzed.add(session_id)
        if message_id:
            self._reminder_delivery.pop(session_id, None)
        self._latest_inbound[session_id] = {
            "group_id": str(group.get("group_id") or ""),
            "user_id": str(user.get("user_id") or ""),
            "msg_id": display_message_id(message_id) if message_id else "",
        }
        if not message_id:
            return
        entry = {
            # 「回覆「名字」：…」，標在 planner 上下文裡這則的正文前面
            "quote_label": quoted(message),
            "addressed": self._is_calling_her(message),
            # 發話人和 @ 的人的 ID：聯想時換成 MaiBot 的人物 ID 查他們的記憶
            "platform": str(message.get("platform") or DEFAULT_PLATFORM_NAME),
            "user_id": str(user.get("user_id") or ""),
            "mentioned": mentioned_people(message),
            "preview_pending": bool(extra.get("discord_preview_pending")),
            # 第一張圖片（base64）：分話題時直接附給決策模型看，不等主程式寫圖片描述
            "image": first_image(message),
            # 圖片的 sha256：向 MaiBot 拿識圖寫好的描述（image.get_description）
            "image_hash": image_sha256(message),
            # 收到的時間：planner 做完決定之後才到的，replyer 不看（quote_context 的快照）
            "at": time.time(),
        }
        remembered = self._inbound.setdefault(session_id, OrderedDict())
        # 用 planner 上下文顯示的 msg_id（長 ID 是別名）當鍵
        key = display_message_id(message_id)
        remembered[key] = entry
        remembered.move_to_end(key)
        while len(remembered) > INBOUND_REMEMBERED:
            remembered.popitem(last=False)

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_topic_analysis",
        mode="blocking",
        # 比 quote_context（late）早：那邊照這裡選的焦點過濾聊天
        order="normal",
        # 決策模型兩次、寫關鍵字、記憶檢索和人物畫像（recall.SEARCH_TIMEOUT_SECONDS）
        timeout_ms=150000,
    )
    async def analyze_in_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """Discord 聊天拿掉 MaiBot 原生的 send_emoji 和聊天裡的表情 ID、表情圖；群聊有新訊息後第一次進 planner 時
        分話題、選焦點、聯想（_analyze）。"""
        # 沒有這個工具：不是 Discord 聊天、睡覺時段只剩 stop，或是表達方式選擇這類沒有工具的子請求
        if "discord_emote" not in tool_names(kwargs):
            return {"action": "continue"}
        # discord_emote 也能發貼圖，兩個都留就是兩套「要不要發表情包」的決定；
        # 只拿掉 send_emoji，planner 手上還有 discord_emote，不會因為沒工具而把表情寫進正文
        kwargs["tool_definitions"] = [
            tool for tool in kwargs["tool_definitions"] if tool_names({"tool_definitions": [tool]}) != {"send_emoji"}
        ]
        strip_emotes(kwargs.get("items"))
        result = {"action": "continue", "modified_kwargs": kwargs}
        settings = self._load_settings()
        decision = settings.decision
        session_id = str(kwargs.get("session_id") or "")
        if not (decision.enabled and decision.api_key.strip() and session_id):
            self._focus.pop(session_id, None)
            if random.random() < settings.chat.emote_chance:
                self._append_system_text(kwargs, EMOTE_NUDGE)
            return result
        # 子代理派出、結論送回記成話題串的待辦（agenda），新一輪選焦點時用得到
        self._track_subagents(session_id, kwargs.get("items") or [])
        new_round = session_id in self._round_over
        self._round_over.discard(session_id)
        is_group = bool(self._latest_inbound.get(session_id, {}).get("group_id"))
        # 有新訊息：分話題、照待辦選焦點；沒有新訊息的新一輪（子代理結論送回、叫醒她做下一件）照待辦換焦點；
        # 同一輪後面的請求照這輪的焦點
        if session_id in self._unanalyzed:
            self._unanalyzed.discard(session_id)
            if is_group:
                await self._analyze(decision, session_id, planner_messages(kwargs.get("items")))
        elif new_round and is_group:
            self._new_round_focus(session_id)
        return result

    async def _analyze(self, decision: Any, session_id: str, messages: Sequence[Mapping[str, Any]]) -> None:
        """分話題、選焦點、聯想；焦點記在 _focus，聯想記在 _recalls，給 quote_context 和 recall 的 hook 用。"""
        now = datetime.now()
        window = list(messages)[-decision.context_messages :]
        # 焦點只有網址、卡片還沒到時等一下（最多 recall.FOCUS_WAIT_SECONDS）；其他的照常，晚點再分話題
        await self._wait_for_focus_content(session_id, window)
        remembered = self._inbound.get(session_id, {})
        # 卡片晚到時插件補記的文字接在正文後面；在叫她的記下來（話題串的待辦用）
        window = [
            {
                **message,
                "text": with_card(message, remembered.get(message["msg_id"]) or {}),
                "addressed": bool((remembered.get(message["msg_id"]) or {}).get("addressed")),
            }
            for message in window
        ]
        # 用回覆功能回的，正文前面標「[回覆「名字」：…]」：planner 看到的回覆標籤要等 quote_context 那個 late hook 才加，
        # 這裡不補的話決策模型和聯想不知道它在回誰
        window = [
            {**message, "text": f"[{label}]{message['text']}"}
            if (label := (remembered.get(message["msg_id"]) or {}).get("quote_label"))
            else message
            for message in window
        ]
        focus = focus_message(window, remembered, self._seen_until.get(session_id, 0.0))
        if focus is None:
            self._focus.pop(session_id, None)
            return
        # 別人的訊息都編號，她自己的標「（你）」
        numbers = {id(message): str(index) for index, message in enumerate((m for m in window if not m["self"]), 1)}
        lines = [numbered(message, numbers) for message in window]
        state = f"你是「{self._bot_name()}」，在 Discord 群組聊天。\n\n最近的群聊（越下面越新，# 是訊息編號）：\n" + "\n".join(lines)
        store = self._topics.setdefault(session_id, new_store())
        questions, keys, images = topic_questions(store, window, numbers, remembered, time.time())
        started = time.monotonic()
        answers = (
            await ask_decision(
                decision,
                state,
                questions,
                self.ctx.logger,
                images=images or None,
                timeout=IMAGE_TIMEOUT_SECONDS if images else TIMEOUT_SECONDS,
            )
            if questions
            else {}
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        failed = answers is None
        topic_summary = apply_topics(store, keys, answers)
        # 待辦優先（叫她還沒回的、子代理查完還沒回的）；沒有就是上面挑的（叫她的、最新一則別人說的）
        if item := self._agenda_focus(session_id, [message["msg_id"] for message in window if not message["self"]]):
            focus = next(message for message in window if message["msg_id"] == item["msg_id"])
        else:
            mark_handled(store, store["of"].get(focus["msg_id"]), time.time())
        self._save_threads()
        self._focus[session_id] = focus["msg_id"]
        recall_summary, recall_detail, columns, view = await self._run_recall(
            decision, session_id, window, remembered, numbers, state, focus
        )
        self.ctx.logger.info(
            f"話題分析 [{session_id}]：焦點 #{numbers[id(focus)]}「{focus['user']}」{focus['text'][:30]}（{latency_ms} ms）"
            + (f"；話題 {topic_summary}" if topic_summary else "")
            + ("；分話題的請求失敗" if failed else "")
            + (f"；聯想 {recall_summary}" if recall_summary else "")
        )
        self._decision_stats.record_decision(
            now,
            session_id,
            hide_urls(f"「{focus['user']}」：{focus['text']}"),
            latency_ms,
            failed=failed,
            topics=topic_summary,
            recall=recall_summary,
            context=hide_urls("\n".join(lines)),
            recall_detail=recall_detail,
            columns=columns,
            view=view,
        )

    def _record_send(self, session_id: str, kind: str, started: float, picked: str, score: float, ok: bool) -> None:
        latency_ms = round((time.monotonic() - started) * 1000)
        if ok:
            self.ctx.logger.info(f"{SEND_KIND_LABELS[kind]} [{session_id}]：{picked}（相似度 {score:.2f}，{latency_ms} ms）")
        else:
            self.ctx.logger.warning(f"{SEND_KIND_LABELS[kind]} [{session_id}]：沒有送出")
        self._decision_stats.record_send(datetime.now(), session_id, kind, latency_ms, picked=picked, score=score, ok=ok)

    # ===== 挑表情（向量，不經過 LLM）=====

    def _emoji_items(self, guild: Any) -> Dict[str, Tuple[Any, str]]:
        """伺服器能用的自訂表情：key -> (表情, 比對用文字「名字：表情庫的描述」)；沒收進表情庫的只有名字。"""
        return {
            f"emoji:{emoji.id}": (emoji, named(emoji.name, self._emote_library.text_for(f"emoji:{emoji.id}")))
            for emoji in (guild.emojis if guild is not None else [])
            if emoji.is_usable()
        }

    def _sticker_items(self, guild: Any) -> Dict[str, Tuple[Any, str]]:
        """伺服器能發的貼圖：key -> (貼圖, 比對用文字「名字：描述」)；描述用表情庫的，沒收過的用貼圖自己的說明。"""
        return {
            f"sticker:{sticker.id}": (
                sticker,
                named(sticker.name, self._emote_library.text_for(f"sticker:{sticker.id}") or sticker.description),
            )
            for sticker in (guild.stickers if guild is not None else [])
            if sticker.available and sticker.format != discord.StickerFormatType.lottie
        }

    async def warm_emote_vectors(self) -> None:
        """背景先算好表情庫和各伺服器表情、貼圖的向量，第一次挑圖不用現算（refresh 自己限制頻率）；順便重讀她的名字。"""
        await self._refresh_bot_names()
        await self._emote_library.refresh()
        for guild in getattr(getattr(self._client_manager, "client", None), "guilds", None) or []:
            items = {**self._emoji_items(guild), **self._sticker_items(guild)}
            await self._emote_library.ensure_vectors([(key, text) for key, (_, text) in items.items()])

    async def _rank_fresh(
        self, session_id: str, query: str, items: Sequence[Tuple[str, str]]
    ) -> Optional[Tuple[str, float]]:
        """排除這個聊天最近用過的（剩不到 MIN_FRESH 個時才放回來），向量排序後照相似度加權抽一個。

        Returns:
            ``(key, 相似度)``；embedding 失敗時回傳 None。
        """
        recent = self._recent_picks.setdefault(session_id, deque(maxlen=RECENT_PICKS))
        fresh = [item for item in items if item[0] not in recent]
        ranked = await self._emote_library.rank(query, fresh if len(fresh) >= MIN_FRESH else items, top=PICK_POOL)
        if not ranked:
            return None
        best = ranked[0][1]
        weights = [math.exp((score - best) / PICK_TEMPERATURE) for _, score in ranked]
        key, score = random.choices(ranked, weights=weights, k=1)[0]
        recent.append(key)
        return key, score

    async def _pick_custom_emoji(self, guild: Any, query: str, session_id: str) -> Optional[Tuple[Any, float]]:
        """從伺服器能用的自訂表情挑一個。"""
        await self._emote_library.refresh()
        usable = self._emoji_items(guild)
        if not usable:
            return None
        picked = await self._rank_fresh(session_id, query, [(key, text) for key, (_, text) in usable.items()])
        if picked is None:
            self.ctx.logger.warning("表情向量排序失敗，改成隨機挑一個伺服器表情")
            return random.choice([emoji for emoji, _ in usable.values()]), 0.0
        key, score = picked
        return usable[key][0], score

    async def _pick_image(self, guild: Any, query: str, session_id: str) -> Optional[Tuple[str, str, float]]:
        """從這個伺服器能直接發的貼圖和自訂表情挑一個，回傳 ``(名字, 圖片 base64, 相似度)``。

        只挑 Discord 原生的：MaiBot 表情庫裡的一般圖片送出去會變成圖片附件，不挑。
        圖片用表情庫裡那份（送出時 DiscordContentBuilder 換回原生貼圖或表情）；沒收過的選中時才下載並登記。
        """
        await self._emote_library.refresh()
        items = {**self._sticker_items(guild), **self._emoji_items(guild)}
        if not items:
            return None
        picked = await self._rank_fresh(session_id, query, [(key, text) for key, (_, text) in items.items()])
        if picked is None:
            self.ctx.logger.warning("表情向量排序失敗，改成隨機挑一個伺服器貼圖或表情")
            picked = (random.choice(list(items)), 0.0)
        key, score = picked
        item = items[key][0]
        image_b64 = self._emote_library.image_for(key)
        if not image_b64:
            kind = key.split(":", 1)[0]
            image_b64 = await self._message_handler._fetch_emoji_bytes(str(item.url), size=256 if kind == "sticker" else 96)
            if not image_b64:
                return None
            # 發送鏈路只收圖片：先登記這張圖，出站時 DiscordContentBuilder 會換回原生的
            self._native_emoji.remember(image_b64, kind, item.id)
        return item.name, image_b64, score

    async def _refresh_bot_names(self) -> None:
        """讀 MaiBot 的 bot.nickname 和 bot.alias_names；讀不到就沿用上次的。"""
        if self._bot_names and time.monotonic() - self._bot_names_at < BOT_NAMES_REFRESH_SECONDS:
            return
        self._bot_names_at = time.monotonic()
        names: List[str] = []
        for key in ("bot.nickname", "bot.alias_names"):
            value = await self.ctx.config.get(key)
            # SDK 成功時把 {"value": …} 拆開；失敗回 dict
            value = value.get("value") if isinstance(value, dict) else value
            names += [value] if isinstance(value, str) else list(value or []) if isinstance(value, list) else []
        names = [name.strip() for name in names if isinstance(name, str) and name.strip()]
        if names:
            self._bot_names = names

    def _guild_of(self, channel_id: Any) -> Any:
        client = getattr(self._client_manager, "client", None)
        try:
            channel = client.get_channel(int(channel_id)) if client is not None and channel_id else None
        except (TypeError, ValueError):
            return None
        return getattr(channel, "guild", None)

    async def _ask_decision(self, decision: Any, state: str, questions: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """給聯想（recall.py）用的決策模型請求，和話題分析同一個設定。"""
        return await ask_decision(decision, state, questions, self.ctx.logger)

    def _bot_name(self) -> str:
        user = getattr(getattr(self._client_manager, "client", None), "user", None)
        return str(getattr(user, "display_name", "") or "麥麥")

    # ===== planner 工具 =====

    _EMOTE_FORMS = {"reaction": "表情回應", "emoji": "表情", "sticker": "貼圖"}

    # ponytail: platforms 寫死預設平台名，platform_name 改成別的名字時工具不會出現
    @Tool(
        "discord_emote",
        description=(
            "用表情回應：按在訊息上（reaction／react）、單發一個伺服器表情，或發一張貼圖（sticker，"
            "從這個伺服器的貼圖和自訂表情挑；要發表情包也用這個）。"
            "可以和 reply 在同一輪一起呼叫（邊回話邊按表情），也可以只按表情不說話。"
            "適合好笑、好消息、附和、按讚、表示收到、驚訝、無語、心疼的時候，像真人聊天一樣偶爾用。"
            "只傳目標訊息、想表達的情緒與形式，工具會照情緒挑伺服器的貼圖或表情，不使用普通 Unicode emoji。"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id", param_type=ToolParamType.STRING, description="要回應的訊息 msg_id。", required=True
            ),
            ToolParameterInfo(
                name="feeling",
                param_type=ToolParamType.STRING,
                description="想表達的情緒或態度，如笑翻、認同、無語、心疼；只描述情緒，不指定表情符號。",
                required=False,
            ),
            ToolParameterInfo(
                name="form",
                param_type=ToolParamType.STRING,
                description="送出形式：reaction 按在該訊息上（最輕），emoji 單獨發一個表情，sticker 發一張貼圖。預設 reaction。",
                required=False,
                enum_values=list(_EMOTE_FORMS),
            ),
        ],
        platforms=DEFAULT_PLATFORM_NAME,
        visibility="visible",
    )
    async def handle_discord_emote(
        self,
        msg_id: str = "",
        feeling: str = "",
        form: str = "reaction",
        group_id: str = "",
        user_id: str = "",
        stream_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """planner 工具：照情緒用向量挑一個伺服器表情或貼圖，按 form 加成 reaction 或單獨發出。"""
        del kwargs
        if self._thread_routing is None or self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "Discord 客戶端未就緒"}
        form = str(form or "reaction").strip()
        if form not in self._EMOTE_FORMS:
            return {"success": False, "error": f"form 只能是 {'、'.join(self._EMOTE_FORMS)}"}
        stream_id = str(stream_id or "").strip()
        if form != "reaction" and not stream_id:
            return {"success": False, "error": "缺少聊天流，無法單獨發送"}
        target = await self._thread_routing.find_message(str(msg_id or "").strip(), group_id, user_id)
        if target is None:
            return {"success": False, "error": f"找不到訊息 {msg_id}"}

        started = time.monotonic()
        label = self._EMOTE_FORMS[form]
        query = f"{str(feeling or '').strip() or '開心'}：{(target.content or '')[:200]}"
        if form == "sticker":
            found_image = await self._pick_image(target.guild, query, stream_id)
            if not found_image:
                return {"success": False, "error": f"沒有可用的{label}，沒有送出"}
            picked, image_b64, score = found_image
            sent = await self.ctx.send.emoji(image_b64, stream_id, sync_to_maisaka_history=True, maisaka_source_kind=SELF_SENT)
        else:
            found = await self._pick_custom_emoji(target.guild, query, stream_id)
            if not found:
                return {"success": False, "error": f"本伺服器沒有可用的{label}，沒有送出"}
            emoji, score = found
            picked = str(emoji)
            if form == "reaction":
                try:
                    await target.add_reaction(emoji)
                except discord.HTTPException as exc:
                    return {"success": False, "error": f"添加表情回應 {emoji} 失敗：{exc}"}
                sent = True
            else:
                # 走 Host 發送鏈路，訊息才會進聊天紀錄
                sent = await self.ctx.send.text(picked, stream_id, sync_to_maisaka_history=True, maisaka_source_kind=SELF_SENT)
        self._record_send(stream_id or "", "tool", started, picked, score, bool(sent))
        if not sent:
            return {"success": False, "error": f"發送{label}失敗"}
        return {"success": True, "content": f"已用{label} {picked} 回應訊息 {msg_id}"}

    # ===== 「DC生活 → 話題分析」頁面 =====

    @API("webui_decision", description="讀取話題分析的設定與紀錄", version="1")
    async def webui_decision(self) -> Dict[str, Any]:
        """話題分析頁：今天的分析與送出的表情、近 14 天走勢、最近的紀錄；沒啟用或沒填 Key 時用 notice 說明。"""
        config = self._load_settings().decision
        state = self._decision_stats.state
        days = state.get("days") or {}
        today = days.get(datetime.now().date().isoformat()) or {}
        if not config.enabled:
            notice = "決策模型未啟用，在下方「設定」開啟；沒開時 planner、replyer 看完整的聊天。"
        elif not config.api_key:
            notice = "還沒填 API Key（在插件設定「聊天與過濾 → 決策模型」），現在 planner、replyer 看完整的聊天。"
        else:
            notice = ""
        last_test = state.get("last_test")
        decisions = (state.get("decisions") or [])[:PAGE_RECENT]
        sends = (state.get("sends") or [])[:PAGE_RECENT]
        names = await self._webui_chat_names() if decisions or sends else {}

        def judged(counts: Mapping[str, int]) -> int:
            return counts.get("calls", 0) - counts.get("failed", 0)

        return {
            "notice": notice,
            "provider": config.base_url,
            "model": config.model,
            # 設定分頁每一列旁邊顯示的目前的值
            "current": {
                field: row["value"]
                for field, row in zip(PAGE_FIELDS["decision"], self._setting_rows("decision", PAGE_FIELDS["decision"]), strict=True)
            },
            "last_test": (
                f"{short_time(last_test['time'])} {'✅' if last_test['ok'] else '❌'} {last_test['detail']}"
                if last_test
                else "還沒測試"
            ),
            "today_calls": today.get("calls", 0),
            "today_latency": f"{_avg(today.get('latency_ms', 0), judged(today))} ms" if judged(today) else "—",
            "today_failed": today.get("failed", 0),
            "today_sends": f"送出 {today.get('sends', 0)}・沒送成 {today.get('send_failed', 0)}",
            "today_send_latency": (
                f"{_avg(today.get('send_latency_ms', 0), today.get('sends', 0))} ms" if today.get("sends") else "—"
            ),
            "daily": [
                {
                    "date": day[5:],
                    "calls": counts.get("calls", 0),
                    "sends": counts.get("sends", 0),
                    "latency": _avg(counts.get("latency_ms", 0), judged(counts)),
                }
                for day, counts in sorted(days.items())
            ],
            "decisions": [
                {
                    "time": short_time(item["time"]),
                    "chat": names.get(item["chat"], item["chat"]),
                    "latency": f"{item['latency_ms']} ms",
                    "message": item["message"],
                    "result": "分話題失敗" if item.get("failed") or item.get("scene") == "failed" else "—",
                    # 舊紀錄的欄位名不同或沒有
                    "topics": item.get("topics") or item.get("links") or "—",
                    "recall": item.get("recall") or "—",
                    "context": item.get("context") or "—",
                    "recall_detail": item.get("recall_detail") or "—",
                    "topic": (item.get("columns") or {}).get("topic") or "—",
                    "understood": (item.get("columns") or {}).get("understood") or "—",
                    "memory": (item.get("columns") or {}).get("memory") or "—",
                }
                for item in decisions
            ],
            "sends": [
                {
                    "time": short_time(item["time"]),
                    "chat": names.get(item["chat"], item["chat"]),
                    "kind": SEND_KIND_LABELS.get(item["kind"], item["kind"]),
                    "picked": item["picked"] or "—",
                    "score": f"{item['score']:.2f}" if item["score"] else "—",
                    "latency": f"{item['latency_ms']} ms",
                    "result": "送出" if item["ok"] else "沒送成",
                }
                for item in sends
            ],
        }

    @API("webui_threads", description="讀取各群聊現在的話題串和最近的話題分析（「話題串」頁）", version="1")
    async def webui_threads(self) -> Dict[str, Any]:
        """每個群聊開著的話題串（這輪的焦點那串在前，其餘新的在前），和最近幾次分析當時的對話與理解。"""
        names = await self._webui_chat_names() if self._topics or self._decision_stats.state.get("decisions") else {}
        now = time.time()
        chats = []
        for session_id, store in self._topics.items():
            focus_topic = store["of"].get(self._focus.get(session_id, ""))
            threads = [
                {
                    "id": topic_id,
                    "title": topic["items"][0]["text"][:40],
                    "count": len(topic["members"]),
                    "minutes": max(0, round((now - topic["last"]) / 60)),
                    "focus": topic_id == focus_topic,
                    "messages": [{key: item.get(key) for key in ("who", "self", "text", "time")} for item in topic["items"]],
                }
                for topic_id, topic in reversed(store["topics"].items())
                if topic.get("items")
            ]
            threads.sort(key=lambda thread: not thread["focus"])  # 穩定排序：焦點那串在前，其餘照新的在前
            if threads:
                chats.append({"id": session_id, "name": names.get(session_id, session_id), "threads": threads[:PAGE_THREADS]})
        analyses = [
            {
                "time": short_time(item["time"]),
                "chat": names.get(item["chat"], item["chat"]),
                "message": item["message"],
                "latency": item["latency_ms"],
                "failed": bool(item.get("failed") or item.get("scene") == "failed"),
                "assigned": item.get("topics") or "",
                "summary": item.get("recall") or "",
                **(item.get("view") or {}),
            }
            for item in (self._decision_stats.state.get("decisions") or [])[:PAGE_RECENT]
        ]
        return {"chats": chats, "analyses": analyses}

    @API("webui_decision_test", description="用一段範例聊天測試決策模型連不連得上", version="1")
    async def webui_decision_test(self) -> Dict[str, Any]:
        """沒啟用也能測，方便先確認 Key 和網址；結果記在頁面的「上次測試」。"""
        config = self._load_settings().decision
        if not config.api_key.strip():
            raise ValueError("還沒填 API Key")
        errors: List[str] = []
        started = time.monotonic()
        answers = await ask_decision(config, DECISION_TEST_STATE, DECISION_TEST_QUESTIONS, SimpleNamespace(warning=errors.append))
        latency_ms = round((time.monotonic() - started) * 1000)
        choice = str(((answers or {}).get("topic_2") or {}).get("choice") or "")
        if choice:
            detail = f"{latency_ms} ms，範例判斷 #2 " + ("在接 #1" if choice == "m1" else "是新話題")
            self._decision_stats.set_last_test(datetime.now(), True, detail)
            return {"ok": True, "detail": detail}
        detail = errors[-1] if errors else "回應裡沒有判斷結果，檢查模型名稱"
        self._decision_stats.set_last_test(datetime.now(), False, detail)
        raise ValueError(f"決策模型測試失敗：{detail}")

    @API("webui_decision_save", description="儲存話題分析的設定", version="1")
    async def webui_decision_save(self, **fields: Optional[str]) -> Dict[str, Any]:
        """只改有填的項目；留空不改。"""
        return {"saved": self._save_page_fields("decision", fields)}

    @API("webui_decision_reset", description="清除決策模型的判斷統計", version="1")
    async def webui_decision_reset(self) -> Dict[str, Any]:
        self._decision_stats.reset()
        return {"cleared": True}
