"""話題串聯想：只比對這串的詞彙和群記憶，交決策模型挑選後提供給 planner。

不主動查詢或注入人物畫像；人物資訊由 MaiBot 原生機制按需處理。
"""

import asyncio
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from maibot_sdk import HookHandler

from .emote import display_message_id, planner_messages
from .reply_links import NONE, plain
from .topics import (
    _URL,
    clock,
    describe_member,
    focus_message,
    hide_urls,
    lacks_content,
    numbered,
    topic_members,
    waiting_image,
    with_card,
)

# 群記憶取幾筆、最多給幾個詞
MEMORY_HITS = 3
JARGON_LIMIT = 8
# 黑話的意思最多幾個字
TERM_CHARS = 60
NOTE_MEMORY_CHARS = 160
# 要回的那則只有網址、Discord 卡片還沒補到時最多等幾秒
FOCUS_WAIT_SECONDS = 5
# 焦點那則有圖片時等 MaiBot 識圖最多幾秒（識圖模型通常要好幾秒）
IMAGE_WAIT_SECONDS = 15
# 查詢字數上限（記憶檢索和整理話題用）
QUERY_CHARS = 300
# 正式機的記憶檢索常要好幾秒，4 秒時幾乎每次都逾時；等不到就這輪少一段聯想
SEARCH_TIMEOUT_SECONDS = 15
MISSED = "沒等到（逾時或失敗）"
# 選中的記憶（或看得懂）的機率低於這個值，就要她先查或用問的
CONFIDENT = 0.5
UNDERSTAND = "recall_understand"
PICK = "recall_pick"
NOTE_HEAD = "【這串的理解】（聯想，可能不對；和眼前的聊天衝突時以聊天為準，別逐字說出來）"
# 記憶檢索是向量比對：整段聊天原文太長太雜，分數常過不了門檻，先請小模型寫成關鍵字再查
QUERY_TASK = "utils"
QUERY_TIMEOUT_SECONDS = 10
QUERY_PROMPT = """下面是 Discord 群聊裡的一段對話：
{chat}

每行開頭「」裡是發話人的 Discord 暱稱，可以隨便改、常常像一句話或梗，不是聊天內容；（你）和「@你」是你自己。

寫一行記憶檢索用的關鍵字（3～8 個詞，空格分開）：對話內容裡的遊戲、作品、人物、事件、梗、術語，和這段在聊的主題，不要放暱稱。
看不出在聊什麼就只輸出「無」。只輸出這一行，不要解釋。"""
# 小模型說看不出在聊什麼時的回答
NOTHING = "無"
UNSURE = "你不太確定這串在聊什麼：需要的話先查（可以查：{query}）再回，或用問的，別把猜測當成事實。"
UNSURE_NO_QUERY = "你不太確定這串在聊什麼：需要的話先查再回，或用問的，別把猜測當成事實。"

_HIT = re.compile(r"(?:^|\n)\s*\d+\.\s*")
# 記憶原文：「[2026-08-28 05:30] 名字: 內容 [2026-08-28 05:40] 名字: 內容 ...」
_STAMP = re.compile(r"\[(\d{4}-\d\d-\d\d) \d\d:\d\d\]\s*")
_EMOTE = re.compile(r"<a?:\w+:\d+>")
_ASCII_TERM = re.compile(r"[A-Za-z0-9]+")


def parse_hits(result: Any) -> List[str]:
    """knowledge.search 的結果 -> 每筆記憶；沒找到或失敗時是空清單。

    SDK 成功時把結果拆成純文字「你知道这些知识: 1. … 2. …」，沒找到是「你不太了解有关…的知识」，失敗回原樣的 dict。
    """
    if not isinstance(result, str) or result.startswith("你不太了解"):
        return []
    body = result.split(":", 1)[1] if result.startswith("你知道") else result
    return [hit.strip() for hit in _HIT.split(body) if hit.strip()]


def clip(text: str, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[: limit - 1] + "…"


def tidy(memory: str, limit: int = NOTE_MEMORY_CHARS) -> str:
    """記憶原文整理成一行：日期只留第一個、每句用「／」隔開，拿掉表情 ID，網址換成「（連結）」。"""
    text = hide_urls(_EMOTE.sub("", memory))
    pieces = _STAMP.split(text)
    date = pieces[1] if len(pieces) > 1 else ""
    # 記憶檢索把太長的截斷成「...」
    said = [" ".join(piece.split()).replace(": ", "：", 1).rstrip(" .") for piece in pieces[2::2] or pieces[:1]]
    line = "／".join(piece for piece in said if piece)
    return clip(f"{date} {line}" if date else line, limit)


def whole_terms(jargons: Sequence[Mapping[str, str]], texts: Sequence[str], names: Sequence[str] = ()) -> List[Dict[str, str]]:
    """這串文字中命中的黑話；英數詞須完整匹配，暱稱不當成詞。"""
    joined = "\n".join(texts)
    kept = []
    for item in jargons:
        term = str(item.get("content") or "")
        if _ASCII_TERM.fullmatch(term) and not re.search(rf"(?<![A-Za-z0-9]){re.escape(term)}(?![A-Za-z0-9])", joined, re.I):
            continue
        if any(term in name for name in names):
            continue
        meaning = re.split(r"(?<=[。！？!?])", str(item.get("meaning") or "").strip(), maxsplit=1)[0]
        kept.append({"content": term, "meaning": clip(meaning, TERM_CHARS)})
    return kept


def thread_messages(window: Sequence[Mapping[str, Any]], members: Sequence[str]) -> List[Mapping[str, Any]]:
    """這串的訊息照時間排：成員，加上最早的成員到最後之間她自己說的。"""
    indexes = [index for index, message in enumerate(window) if message["msg_id"] in members]
    if not indexes:
        return []
    # ponytail: 中間她說的都算進來，她同時在聊別的話題時會混進去；要分再問決策模型她那句是不是同一件事
    return [
        message
        for index, message in enumerate(window)
        if indexes[0] <= index <= indexes[-1] and (message["msg_id"] in members or message["self"])
    ]


def probability(answer: Mapping[str, Any], option: str) -> float:
    """決策模型給某個選項的機率；沒給機率時只信它選的那個（用 confidence）。"""
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, Mapping):
        value = probabilities.get(option)
        return float(value) if isinstance(value, (int, float)) else 0.0
    return float(answer.get("confidence") or 1.0) if answer.get("choice") == option else 0.0


def understanding_lines(recall: Mapping[str, Any]) -> List[str]:
    """這串相關的詞彙（planner、replyer 和決策模型都看這一份）。"""
    lines = []
    if recall["terms"]:
        lines += ["詞彙：", *(f"- {term['content']}：{term['meaning']}" for term in recall["terms"])]
    return lines


def recall_state(recall: Mapping[str, Any], number: str) -> str:
    """接在決策模型 state 後面：要回的是哪則、這串、相關詞彙與候選記憶。"""
    lines = [f"\n\n【聯想】#{number} 是你要回的那則，它和這幾則在聊同一件事：\n{recall['thread_text']}", *understanding_lines(recall)]
    if recall["hits"]:
        lines.append("你想起的群聊記憶（可能不對）：" + "".join(f"\nm{index}. {tidy(hit, 300)}" for index, hit in enumerate(recall["hits"], 1)))
    return "\n".join(lines)


def recall_questions(recall: Mapping[str, Any], number: str) -> Dict[str, Dict[str, Any]]:
    """看不看得懂、哪一筆記憶最有關；沒想起記憶時只問第一題（選擇題至少兩個選項）。"""
    questions = {
        UNDERSTAND: {
            "type": "choice",
            "instructions": f"#{number} 和上面跟它在聊同一件事的那幾則，加上你對相關詞彙的了解，你看得懂在聊什麼嗎？",
            "criteria": {
                "yes": "看得懂：靠常識、相關詞彙或想起的記憶就知道在聊什麼",
                "no": "看不懂：有不認識的遊戲、作品、人物、新聞、梗或術語，要先查",
            },
        }
    }
    if recall["hits"]:
        criteria = {f"m{index}": tidy(hit, 300) for index, hit in enumerate(recall["hits"], 1)}
        criteria[NONE] = "都跟這段在聊的無關"
        questions[PICK] = {"type": "choice", "instructions": f"#{number} 那段在聊的，跟哪一筆記憶最有關？", "criteria": criteria}
    return questions


def recall_note(recall: Mapping[str, Any], picked: str, pick_confidence: float, understood: float) -> str:
    """給 planner（和 replyer）的「這串的理解」：詞彙、選中的記憶與不確定提醒。

    Args:
        pick_confidence: 選中那筆記憶的機率。
        understood: 看得懂的機率；低於 CONFIDENT 就提醒她先查或用問的。
    """
    lines = understanding_lines(recall)
    if picked:
        lines += ["記憶：", f"- 群裡以前聊過（把握 {pick_confidence:.2f}）：{tidy(picked)}"]
    if understood < CONFIDENT:
        # 小模型寫出關鍵字才給她當查詢；沒寫出來時那是原文，給了只會害她拿整段去查
        lines.append("沒把握：" + (UNSURE.format(query=recall["query"]) if recall.get("keywords") else UNSURE_NO_QUERY))
    return NOTE_HEAD + "\n" + "\n".join(lines) if lines else ""


def recall_detail(recall: Mapping[str, Any], note: str) -> str:
    """判斷詳情：話題串、詞彙、記憶、最後給 planner 的內容。"""
    terms = "、".join(term["content"] for term in recall["terms"])
    how = "" if recall.get("keywords") else "（小模型沒寫出關鍵字，改用這串的原文）"
    hits = [f"- m{index}. {tidy(hit, 120)}" for index, hit in enumerate(recall["hits"], 1)]
    lines = [
        f"【話題串】{recall.get('topic_label', '')}",
        *(hide_urls(line) for line in recall["walk"]),
        "",
        f"【詞彙】{terms or '沒有比對到'}",
        "",
        f"【記憶】關鍵字：{clip(recall['query'], 60)}{how}",
        *(hits or [f"- {MISSED if recall['missed'] else '沒查到'}"]),
        "",
        "【給 planner 的】",
        note or "沒有（這串沒有要補充的，也不缺把握）",
    ]
    return "\n".join(lines)


def recall_view(
    recall: Mapping[str, Any], picked: str, pick_confidence: float, understand: Mapping[str, Any], note: str
) -> Dict[str, Any]:
    """「話題串」頁泡泡：這串對話、詞彙、選中的記憶與理解程度。"""
    focus = recall["focus"]["msg_id"]
    verdict = str(understand.get("choice") or "")
    return {
        "thread": [
            {
                "who": "你" if message["self"] else plain(message["user"]),
                "self": bool(message["self"]),
                "text": clip(hide_urls(message["text"]), 300),
                "time": clock(message.get("time")),
                "focus": message["msg_id"] == focus,
            }
            for message in recall["said"]
        ],
        "topic": recall.get("topic_label", ""),
        "terms": list(recall["terms"]),
        "memory": {"text": tidy(picked), "p": round(pick_confidence, 2)} if picked else None,
        "understood": {"verdict": verdict, "p": round(probability(understand, verdict), 2) if verdict else None},
        "query": clip(recall["query"], 60),
        "keywords": bool(recall.get("keywords")),
        "note": note,
    }


class RecallMixin:
    """需要主插件提供 ctx、_append_system_text、_client_manager，和 EmoteMixin 的 _topics、_focus、_inbound。"""

    def _init_recall_state(self) -> None:
        # 每個聊天最近一次聯想：{"focus": msg_id, "note": 給 planner 的那段}
        self._recalls: Dict[str, Dict[str, Any]] = {}

    async def _search_memory(self, session_id: str, query: str) -> Optional[List[str]]:
        """查這個群看得到的記憶（和 planner 內建 query_memory 一樣限定這個聊天）。

        Returns:
            每筆記憶；逾時或失敗時是 None，和「查了但沒有」的空清單分開，頁面才不會把沒等到結果寫成 0 筆。
        """
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability(
                    "knowledge.search",
                    query=query[:QUERY_CHARS],
                    limit=MEMORY_HITS,
                    chat_id=session_id,
                    # 和 planner 內建的 query_memory 一樣帶群號：記憶的聊天過濾用群號比對，只給 chat_id 可能整個被濾掉
                    group_id=str(self._latest_inbound.get(session_id, {}).get("group_id") or ""),
                ),
                SEARCH_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # 讀不到記憶這輪就少一段聯想，不影響回覆
            result = {"error": repr(exc)}
        if isinstance(result, Mapping):
            self.ctx.logger.warning(f"聯想：讀取記憶失敗或逾時（最多等 {SEARCH_TIMEOUT_SECONDS} 秒）：{result.get('error')}")
            return None
        return parse_hits(result)

    async def _terms(self, session_id: str, texts: Sequence[str], names: Sequence[str]) -> List[Dict[str, str]]:
        """這串的文字裡出現的、這個群學過的黑話；能力不存在或失敗時是空清單。"""
        try:
            result = await self.ctx.call_capability("jargon.match", session_id=session_id, texts=list(texts), limit=JARGON_LIMIT)
        except Exception as exc:  # 讀不到黑話這輪就少這段
            result = {"success": False, "error": repr(exc)}
        if not isinstance(result, Mapping) or not result.get("success"):
            self.ctx.logger.debug(f"聯想：比對黑話失敗：{result.get('error') if isinstance(result, Mapping) else result}")
            return []
        return whole_terms(result.get("jargons") or [], texts, names)

    async def _run_recall(
        self,
        decision: Any,
        session_id: str,
        window: Sequence[Mapping[str, Any]],
        remembered: Mapping[str, Mapping[str, Any]],
        numbers: Mapping[int, str],
        state: str,
        focus: Mapping[str, Any],
    ) -> tuple[str, str, Dict[str, str], Dict[str, Any]]:
        """分完話題後跑完整個聯想；回傳頁面「聯想」欄的摘要、判斷詳情的經過、頁面欄位和「話題串」頁用的 view。"""
        recall = await self._recall(session_id, window, remembered, numbers, focus)
        number = numbers[id(recall["focus"])]
        answers = await self._ask_decision(decision, state + recall_state(recall, number), recall_questions(recall, number))
        return self._finish_recall(session_id, recall, answers)

    def _thread(
        self, session_id: str, window: Sequence[Mapping[str, Any]], numbers: Mapping[int, str], focus: Mapping[str, Any]
    ) -> tuple[List[Mapping[str, Any]], List[str], str]:
        """焦點所在的話題串（照時間排）、判斷詳情裡的經過、頁面的話題串欄；還沒分到串時只有焦點那一則。"""
        store = getattr(self, "_topics", {}).get(session_id)
        members = topic_members(store, focus["msg_id"]) if store else []
        if not members:
            return [focus], [f"→ {numbered(focus, numbers)}（焦點，還沒分到話題串）"], "沒分到串"
        thread = thread_messages(window, members)
        topic_id = store["of"][focus["msg_id"]]
        walk = [f"→ {numbered(focus, numbers)}（焦點，話題串 {topic_id}）"]
        walk += [f"← {numbered(message, numbers)}（同一串）" for message in reversed(thread) if message is not focus]
        opener = store["topics"][topic_id]["lines"][0].split("：", 1)[-1]
        return thread, walk, f"串 {topic_id}「{opener[:16]}」・{len(members)} 則"

    async def _recall(
        self,
        session_id: str,
        window: Sequence[Mapping[str, Any]],
        remembered: Mapping[str, Mapping[str, Any]],
        numbers: Mapping[int, str],
        focus: Mapping[str, Any],
    ) -> Dict[str, Any]:
        """焦點所在的話題串，檢索這串的黑話和群記憶；人物畫像交由 MaiBot 原生功能按需查。"""
        said, walk, topic_label = self._thread(session_id, window, numbers, focus)
        # 叫她的 @ 換成「@你」：她的 Discord 暱稱會被小模型當成要查的詞
        bot = self._bot_name()
        chat = "\n".join(
            ("（你）" if message["self"] else f"「{plain(message['user'])}」")
            + "："
            + message["text"].replace(f"@「{bot}」", "@你").replace(f"@{bot}", "@你")
            for message in said
        )
        query = await self._recall_query(chat)
        keywords = bool(query)
        # 寫不出關鍵字時用原文查，網址拿掉（向量比對時一長串網址只會變成雜訊）
        query = query or " ".join(_URL.sub(" ", message["text"]).strip() for message in said).strip()
        hits, terms = await asyncio.gather(
            self._search_memory(session_id, query),
            self._terms(session_id, [message["text"] for message in said], []),
        )
        return {
            "focus": focus,
            "said": said,
            "chat": chat,
            "thread_text": "\n".join(numbered(message, numbers) for message in said),
            "walk": walk,
            "topic_label": topic_label,
            "query": query,
            "keywords": keywords,
            "people": [],
            "terms": terms,
            "hits": hits or [],
            "missed": hits is None,
        }

    async def _wait_for_focus_content(self, session_id: str, window: Sequence[Mapping[str, Any]]) -> None:
        """要回的那則預覽或圖片尚未到時稍候，已拿到的描述即時補進話題資料。"""
        remembered = self._inbound.get(session_id, {})
        await self._fetch_image_descriptions(session_id, window)
        focus = focus_message(window, remembered, self._seen_until.get(session_id, 0.0))
        if focus is None:
            return
        loop = asyncio.get_running_loop()
        entry = remembered.get(focus["msg_id"]) or {}
        image = waiting_image(entry)
        deadline = loop.time() + (IMAGE_WAIT_SECONDS if image else FOCUS_WAIT_SECONDS)
        while loop.time() < deadline:
            entry = remembered.get(focus["msg_id"]) or {}
            pending_preview = entry.get("preview_pending") and _URL.search(focus["text"]) and not (entry.get("card") or entry.get("preview_image"))
            if not pending_preview and not lacks_content(with_card(focus, entry)) and not waiting_image(entry):
                return
            await asyncio.sleep(1.0 if image else 0.5)
            await self._fetch_image_descriptions(session_id, [focus])

    async def _fetch_image_descriptions(self, session_id: str, messages: Sequence[Mapping[str, Any]]) -> None:
        """MaiBot 識圖寫好的描述（image.get_description）記在收訊紀錄上；已經分到話題串的那則也補上。"""
        remembered = self._inbound.get(session_id, {})
        store = getattr(self, "_topics", {}).get(session_id)
        for message in messages:
            entry = remembered.get(message["msg_id"])
            if not entry or not waiting_image(entry):
                continue
            try:
                result = await self.ctx.call_capability("image.get_description", image_hash=entry["image_hash"])
            except Exception as exc:  # noqa: BLE001 - 拿不到描述就照「[圖片]」分
                self.ctx.logger.debug(f"讀圖片描述失敗：{exc!r}")
                return
            description = " ".join(str((result or {}).get("description") or "").split()) if isinstance(result, Mapping) else ""
            if description:
                entry["image_desc"] = description
                if store:
                    describe_member(store, message["msg_id"], description)

    def _remember_card(self, message_id: str, card: str, image: str = "") -> None:
        """預覽延遲到達時補上卡片文字或圖片，供未分配的話題使用。"""
        key = display_message_id(message_id)
        for remembered in self._inbound.values():
            if key in remembered:
                if card:
                    remembered[key]["card"] = card
                if image:
                    remembered[key]["preview_image"] = image

    async def _recall_query(self, chat: str) -> str:
        """請小模型把這段聊天寫成檢索關鍵字；失敗或它看不出在聊什麼時回傳空字串（改用原文查）。"""
        try:
            reply = await asyncio.wait_for(
                self.ctx.call_capability("llm.generate", prompt=QUERY_PROMPT.format(chat=chat), task_name=QUERY_TASK),
                QUERY_TIMEOUT_SECONDS,
            )
        except Exception as exc:  # 寫不出關鍵字就用原文查
            reply = {"error": repr(exc)}
        text = str(reply.get("response") or "").strip() if isinstance(reply, Mapping) and reply.get("success") else ""
        if not text:
            error = reply.get("error") if isinstance(reply, Mapping) else reply
            self.ctx.logger.warning(f"聯想：寫檢索關鍵字失敗，改用原文查：{error}")
        keywords = text.splitlines()[0].strip().strip("「」") if text else ""
        return "" if keywords == NOTHING else keywords

    def _finish_recall(
        self, session_id: str, recall: Dict[str, Any], answers: Optional[Mapping[str, Any]]
    ) -> tuple[str, str, Dict[str, str], Dict[str, Any]]:
        """照決策模型的答案挑記憶，記下給 planner 的那段。

        Returns:
            頁面「聯想」欄的摘要、判斷詳情裡的經過、頁面欄位（話題串、看得懂嗎、想起的記憶）、「話題串」頁用的 view。
        """
        understand = (answers or {}).get(UNDERSTAND) or {}
        pick = (answers or {}).get(PICK) or {}
        choice = str(pick.get("choice") or "")
        index = int(choice[1:]) if re.fullmatch(r"m\d+", choice) else 0
        picked = recall["hits"][index - 1] if 0 < index <= len(recall["hits"]) else ""
        lost = understand.get("choice") == "no"
        # 請求失敗沒拿到判斷時不當作看不懂，免得每次失敗都叫她別把話說死
        understood = probability(understand, "yes") if understand else 1.0
        note = recall_note(recall, picked, probability(pick, choice), understood)
        said = [message for message in recall["said"] if not message["self"]]
        targets = {
            message["msg_id"]: {"text": message["text"]}
            for message in said
        }
        self._recalls[session_id] = {"focus": recall["focus"]["msg_id"], "note": note,
                                     "terms": recall["terms"], "targets": targets}
        verdict = str(understand.get("choice") or "")
        parts = [
            f"{'看不懂' if lost else '看得懂'} {probability(understand, verdict):.2f}" if verdict in ("yes", "no") else "沒拿到判斷",
            f"想到 m{index}（{probability(pick, choice):.2f}）"
            if picked
            else "群記憶沒等到（逾時或失敗）"
            if recall["missed"]
            else f"記憶 {len(recall['hits'])} 筆都沒選",
            f"詞 {len(recall['terms'])}",
        ]
        columns = {
            "topic": recall.get("topic_label", ""),
            "understood": f"{'不懂' if lost else '懂'} {probability(understand, verdict):.2f}" if verdict in ("yes", "no") else "—",
            "memory": "檢索逾時" if recall["missed"] else f"{len(recall['hits'])} 筆・詞 {len(recall['terms'])}",
        }
        return "・".join(parts), recall_detail(recall, note), columns, recall_view(recall, picked, probability(pick, choice), understand, note)

    def _add_recall_note(self, kwargs: Dict[str, Any], session_id: str) -> None:
        """焦點還在 planner 上下文裡，就把這次的理解接在系統提示詞後面。"""
        recall = self._recalls.get(session_id) or {}
        if recall.get("note") and any(m["msg_id"] == recall["focus"] for m in planner_messages(kwargs.get("items"))):
            self._append_system_text(kwargs, recall["note"])

    @HookHandler(
        "maisaka.replyer.before_request",
        name="discord_recall_reply",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def add_recall_to_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """她回的是焦點那串（或沒指定回哪則）時，replyer 也看到同一段理解（它看不到 planner 的系統提示詞）。"""
        session_id = str(kwargs.get("session_id") or "")
        recall = self._recalls.get(session_id) or {}
        target = display_message_id(str(kwargs.get("reply_message_id") or ""))
        store = getattr(self, "_topics", {}).get(session_id) or {"of": {}, "topics": {}}
        if target and target != recall.get("focus") and target not in topic_members(store, recall.get("focus", "")):
            return {"action": "continue"}
        if not target:
            return {"action": "continue"}
        if target not in recall.get("targets", {}):
            return {"action": "continue"}
        details = recall["targets"][target]
        terms = [term for term in recall.get("terms", []) if term["content"] in details["text"]]
        lines = []
        if terms:
            lines += ["詞彙：", *(f"- {term['content']}：{term['meaning']}" for term in terms)]
        note = NOTE_HEAD + "\n" + "\n".join(lines) if lines else ""
        if not note:
            return {"action": "continue"}
        kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{note}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}
