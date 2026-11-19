"""聯想：回話之前，先想起這段在聊什麼、參與的人是誰，再交給 planner 自己判斷。

人聽到「A：羚羊拿MP5打盾牌」時，文字（羚羊、MP5）和說話的人（A 平常打 R6）會一起喚起記憶，兩邊對得上就懂了；
想錯時靠接下來的話修正，沒把握就用問的。每輪話題分析（emote._analyze）分完話題、選好焦點之後做：

1. 話題：焦點所在的話題串（topics.py）；還沒分到串時只有焦點那一則。
2. 喚起：把這串請小模型寫成檢索關鍵字（整段原文向量比對分數過不了門檻），查群記憶，
   每個參與者再用「名字＋關鍵字」限定這個人查。人物畫像沒開放給插件，用限定人物的記憶檢索代替。
3. 整合：群記憶和參與者的記憶去重後一起當候選（最多 CANDIDATES 筆），問決策模型：看得懂這段在聊什麼嗎、
   哪一筆最有關（可選都無關）。記憶只當候選、讓上下文挑（Kintsch 的 construction-integration），不直接取最像的那筆；
   沒選中就一筆都不給。
4. 看不懂時請「Linux 沙盒電腦」插件的 lookup API 上網查（十幾秒）。
5. 整理成短短一段「聯想」：選中的那筆（清掉表情 ID 和網址）、上網查到的、沒把握時要她用問的或保守地接。
   接在 planner 系統提示詞後面，同一回合每次請求都帶，換了焦點才換；replyer 也看得到。
"""

import asyncio
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from maibot_sdk import HookHandler

from .emote import display_message_id, planner_messages
from .reply_links import NONE, plain
from .topics import focus_message, lacks_content, numbered, topic_members, with_card

LOOKUP_API = "luca.linux-computer.lookup"
# 文字聯想取幾筆、每個人取幾筆、最多看幾個人；去重後最多給決策模型挑幾筆
MEMORY_HITS = 3
PERSON_HITS = 2
CANDIDATES = 6
# 給 planner 和 replyer 的那筆記憶最多幾個字
NOTE_MEMORY_CHARS = 160
# 每個人各查一次記憶；正式機檢索慢，等不及放棄的查詢在主程式那邊還會跑完，查太多會越積越慢
MAX_PEOPLE = 3
# 要回的那則只有網址、Discord 卡片還沒補到時最多等幾秒
FOCUS_WAIT_SECONDS = 5
# 查詢字數上限（記憶檢索和整理話題用）
QUERY_CHARS = 300
# 正式機的記憶檢索常要好幾秒，4 秒時幾乎每次都逾時；等不到就這輪少一段聯想
SEARCH_TIMEOUT_SECONDS = 15
MISSED = "沒等到（逾時或失敗）"
# 上網查最多等多久；lookup 本身最多 150 秒，等不到就這輪不用
LOOKUP_TIMEOUT_SECONDS = 60
# 選中的記憶（或看得懂）的機率低於這個值，就要她用問的或保守地接
CONFIDENT = 0.5
UNDERSTAND = "recall_understand"
PICK = "recall_pick"
NOTE_HEAD = "【聯想】這段話題讓你想起的（可能不對；和眼前的聊天衝突時以聊天為準，別逐字說出來）："
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
UNSURE = "你對這段在聊什麼沒有把握：要回的話可以用問的（例如「你們在說……嗎？」），或接得保守一點，別把猜測當成事實。"

_HIT = re.compile(r"(?:^|\n)\s*\d+\.\s*")
# 記憶原文：「[2026-08-28 05:30] 名字: 內容 [2026-08-28 05:40] 名字: 內容 ...」
_STAMP = re.compile(r"\[(\d{4}-\d\d-\d\d) \d\d:\d\d\]\s*")
_EMOTE = re.compile(r"<a?:\w+:\d+>")
_URL = re.compile(r"https?://\S+")


def tidy(memory: str, limit: int = NOTE_MEMORY_CHARS) -> str:
    """記憶原文整理成一行：日期只留第一個、每句用「／」隔開，拿掉表情 ID，網址換成「（連結）」。"""
    text = _URL.sub("（連結）", _EMOTE.sub("", memory))
    pieces = _STAMP.split(text)
    date = pieces[1] if len(pieces) > 1 else ""
    # 記憶檢索把太長的截斷成「...」
    said = [" ".join(piece.split()).replace(": ", "：", 1).rstrip(" .") for piece in pieces[2::2] or pieces[:1]]
    line = "／".join(piece for piece in said if piece)
    line = f"{date} {line}" if date else line
    return line if len(line) <= limit else line[: limit - 1] + "…"


def parse_hits(result: Any) -> List[str]:
    """knowledge.search 的結果 -> 每筆記憶；沒找到或失敗時是空清單。

    SDK 成功時把結果拆成純文字「你知道这些知识: 1. … 2. …」，沒找到是「你不太了解有关…的知识」，失敗回原樣的 dict。
    """
    if not isinstance(result, str) or result.startswith("你不太了解"):
        return []
    body = result.split(":", 1)[1] if result.startswith("你知道") else result
    return [hit.strip() for hit in _HIT.split(body) if hit.strip()]


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


def participants(
    window: Sequence[Mapping[str, Any]],
    focus: Mapping[str, Any],
    context: Sequence[str],
    remembered: Mapping[str, Mapping[str, Any]],
    bot_id: str,
) -> List[Dict[str, str]]:
    """這段話題的參與者：要回那則的發話人、那則 @ 的人、前文的發話人（新的先）；不含她自己，最多 MAX_PEOPLE 個。"""
    by_id = {message["msg_id"]: message for message in window}
    people: Dict[str, Dict[str, str]] = {}

    def add(msg_id: str) -> None:
        message, entry = by_id.get(msg_id), remembered.get(msg_id) or {}
        if message and not message["self"] and entry.get("user_id"):
            people.setdefault(entry["user_id"], {"user_id": entry["user_id"], "name": plain(message["user"]), "platform": entry["platform"]})

    add(focus["msg_id"])
    entry = remembered.get(focus["msg_id"]) or {}
    for user_id, name in (entry.get("mentioned") or {}).items():
        people.setdefault(user_id, {"user_id": user_id, "name": plain(name), "platform": entry["platform"]})
    for msg_id in reversed(context):
        add(msg_id)
    return [person for user_id, person in people.items() if user_id != bot_id][:MAX_PEOPLE]


def probability(answer: Mapping[str, Any], option: str) -> float:
    """決策模型給某個選項的機率；沒給機率時只信它選的那個（用 confidence）。"""
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, Mapping):
        value = probabilities.get(option)
        return float(value) if isinstance(value, (int, float)) else 0.0
    return float(answer.get("confidence") or 1.0) if answer.get("choice") == option else 0.0


def candidates(hits: Sequence[str], people: Sequence[Mapping[str, Any]]) -> List[Dict[str, str]]:
    """決策模型挑的候選：群記憶在前，再來每個人的；同一筆只留一次，最多 CANDIDATES 筆。"""
    picked: Dict[str, Dict[str, str]] = {}
    for text, who in [*((hit, "") for hit in hits), *((text, person["name"]) for person in people for text in person["impression"])]:
        picked.setdefault(text, {"text": text, "who": who})
    return list(picked.values())[:CANDIDATES]


def recall_state(recall: Mapping[str, Any], number: str) -> str:
    """接在決策模型 state 後面：要回的是哪則、這串，和想起的記憶（m 編號）。"""
    lines = [f"\n\n【聯想】#{number} 是你要回的那則，它和這幾則在聊同一件事：\n{recall['thread_text']}"]
    if recall["candidates"]:
        lines.append(
            "你想起的記憶（可能不對）："
            + "".join(
                f"\nm{index}. " + (f"（和「{item['who']}」有關）" if item["who"] else "") + tidy(item["text"], 300)
                for index, item in enumerate(recall["candidates"], 1)
            )
        )
    return "\n".join(lines)


def recall_questions(recall: Mapping[str, Any], number: str) -> Dict[str, Dict[str, Any]]:
    """看不看得懂、哪一筆記憶最有關；沒想起記憶時只問第一題（選擇題至少兩個選項）。"""
    questions = {
        UNDERSTAND: {
            "type": "choice",
            "instructions": f"#{number} 和上面跟它在聊同一件事的那幾則，你看得懂在聊什麼嗎？",
            "criteria": {
                "yes": "看得懂：靠常識或想起的記憶就知道在聊什麼",
                "no": "看不懂：有不認識的遊戲、作品、人物、新聞、梗或術語，要先查",
            },
        }
    }
    if recall["candidates"]:
        criteria = {f"m{index}": tidy(item["text"], 300) for index, item in enumerate(recall["candidates"], 1)}
        criteria[NONE] = "都跟這段在聊的無關"
        questions[PICK] = {"type": "choice", "instructions": f"#{number} 那段在聊的，跟哪一筆記憶最有關？", "criteria": criteria}
    return questions


def recall_note(picked: Optional[Mapping[str, str]], confidence: float, lookup: Mapping[str, Any]) -> str:
    """給 planner（和 replyer）的那段：選中的那筆記憶、上網查到的、沒把握時的提醒；都沒有時是空字串。"""
    lines = []
    if picked:
        about = f"（和「{picked['who']}」有關）" if picked["who"] else ""
        lines.append(f"- 群裡以前聊過{about}（把握 {confidence:.2f}）：{tidy(picked['text'])}")
    if lookup.get("summary"):
        sources = "、".join(lookup.get("sources") or []) or "搜尋結果"
        lines.append(f"- 上網查到：{lookup['summary']}（來源：{sources}）")
    elif confidence < CONFIDENT:
        lines.append(f"- {UNSURE}")
    return NOTE_HEAD + "\n" + "\n".join(lines) if lines else ""


def recall_detail(recall: Mapping[str, Any], lookup: Mapping[str, Any], note: str) -> str:
    """判斷詳情裡的聯想經過：這串有哪幾則、用什麼關鍵字查、想起哪些候選、每個人查得如何、上網結果、最後給 planner 的那段。"""
    found = "".join(
        f"\nm{index}. " + (f"（「{item['who']}」）" if item["who"] else "") + tidy(item["text"], 300)
        for index, item in enumerate(recall["candidates"], 1)
    )
    lines = [
        "往回找話題：",
        *recall["walk"],
        f"關鍵字：{recall['query']}",
        f"想起的記憶：{found or (MISSED if recall['missed'] else '沒查到')}",
    ]
    for person in recall["people"]:
        if person["impression"]:
            state = f"{len(person['impression'])} 筆" + (f"（有一次{MISSED}）" if person["missed"] else "")
        else:
            state = MISSED if person["missed"] else "沒查到" if person.get("person_id") else "找不到這個人的人物 ID"
        lines.append(f"「{person['name']}」：{state}")
    if lookup:
        lines.append(f"上網「{lookup.get('query', '')}」：" + str(lookup.get("summary") or f"失敗：{lookup.get('error', '')}"))
    lines.append("給 planner 的：" + (f"\n{note}" if note else "沒有（什麼都沒想起來，也不缺把握）"))
    return "\n".join(lines)


class RecallMixin:
    """需要主插件提供 ctx、_append_system_text、_client_manager，和 EmoteMixin 的 _topics、_focus、_inbound。"""

    def _init_recall_state(self) -> None:
        # 每個聊天最近一次聯想：{"focus": msg_id, "note": 給 planner / replyer 的那段, "lookup": 上網查的結果}
        self._recalls: Dict[str, Dict[str, Any]] = {}

    async def _search_memory(
        self, session_id: str, query: str, person_id: str = "", limit: int = MEMORY_HITS
    ) -> Optional[List[str]]:
        """查這個群看得到的記憶（和 planner 內建 query_memory 一樣限定這個聊天）；給 person_id 時只查這個人的。

        Returns:
            每筆記憶；逾時或失敗時是 None，和「查了但沒有」的空清單分開，頁面才不會把沒等到結果寫成 0 筆。
        """
        try:
            result = await asyncio.wait_for(
                self.ctx.call_capability(
                    "knowledge.search",
                    query=query[:QUERY_CHARS],
                    limit=limit,
                    chat_id=session_id,
                    person_id=person_id,
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

    async def _person_memories(
        self, session_id: str, person: Mapping[str, str], query: str, limit: int
    ) -> Optional[List[str]]:
        if not person.get("person_id"):
            return []
        return await self._search_memory(session_id, query, person["person_id"], limit)

    async def _person_id(self, person: Mapping[str, str]) -> str:
        try:
            result = await self.ctx.call_capability("person.get_id", platform=person["platform"], user_id=person["user_id"])
        except Exception:  # 換不到人物 ID 就不查這個人
            return ""
        return result if isinstance(result, str) else ""

    async def _run_recall(
        self,
        decision: Any,
        session_id: str,
        window: Sequence[Mapping[str, Any]],
        remembered: Mapping[str, Mapping[str, Any]],
        numbers: Mapping[int, str],
        state: str,
        focus: Mapping[str, Any],
    ) -> tuple[str, str, Dict[str, str]]:
        """分完話題後跑完整個聯想；回傳頁面「聯想」欄的摘要、判斷詳情的經過和頁面欄位。"""
        recall = await self._recall(session_id, window, remembered, numbers, focus)
        number = numbers[id(recall["focus"])]
        answers = await self._ask_decision(decision, state + recall_state(recall, number), recall_questions(recall, number))
        return await self._finish_recall(decision, session_id, recall, answers)

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
        """焦點所在的這串和參與者，同時查文字聯想和每個人的整體印象。"""
        said, walk, topic_label = self._thread(session_id, window, numbers, focus)
        context = [message["msg_id"] for message in said if message["msg_id"] != focus["msg_id"]]
        # 叫她的 @ 換成「@你」：她的 Discord 暱稱會被小模型和上網查當成要查的詞
        bot = self._bot_name()
        chat = "\n".join(
            ("（你）" if message["self"] else f"「{plain(message['user'])}」")
            + "："
            + message["text"].replace(f"@「{bot}」", "@你").replace(f"@{bot}", "@你")
            for message in said
        )
        user = getattr(getattr(self._client_manager, "client", None), "user", None)
        people = participants(window, focus, context, remembered, str(getattr(user, "id", "") or ""))
        query, person_ids = await asyncio.gather(
            self._recall_query(chat), asyncio.gather(*(self._person_id(person) for person in people))
        )
        query = query or " ".join(message["text"] for message in said)
        for person, person_id in zip(people, person_ids, strict=True):
            person["person_id"] = person_id
        hits, *impressions = await asyncio.gather(
            self._search_memory(session_id, query),
            *(self._person_memories(session_id, person, f"{person['name']} {query}", PERSON_HITS) for person in people),
        )
        for person, impression in zip(people, impressions, strict=True):
            person["impression"], person["missed"] = impression or [], impression is None
        return {
            "focus": focus,
            "chat": chat,
            "thread_text": "\n".join(numbered(message, numbers) for message in said),
            "walk": walk,
            "topic_label": topic_label,
            "query": query,
            "people": people,
            "hits": hits or [],
            "candidates": candidates(hits or [], people),
            "missed": hits is None,
        }

    async def _wait_for_focus_card(self, session_id: str, window: Sequence[Mapping[str, Any]]) -> None:
        """要回的那則只有網址時，等 Discord 補上連結卡片（_remember_card），最多 FOCUS_WAIT_SECONDS 秒。"""
        focus = focus_message(window, self._inbound.get(session_id, {}))
        deadline = asyncio.get_running_loop().time() + FOCUS_WAIT_SECONDS
        while focus is not None and asyncio.get_running_loop().time() < deadline:
            entry = self._inbound.get(session_id, {}).get(focus["msg_id"]) or {}
            if not lacks_content(with_card(focus, entry)):
                return
            await asyncio.sleep(0.5)

    def _remember_card(self, message_id: str, card: str) -> None:
        """Discord 晚一點才展開連結預覽（訊息編輯事件，正文沒變）：把卡片文字補記在那則訊息上，分話題和聯想用得到。"""
        key = display_message_id(message_id)
        for remembered in self._inbound.values():
            if key in remembered:
                remembered[key]["card"] = card

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

    async def _finish_recall(
        self, decision: Any, session_id: str, recall: Dict[str, Any], answers: Optional[Mapping[str, Any]]
    ) -> tuple[str, str, Dict[str, str]]:
        """照決策模型的答案挑記憶、看不懂就上網查，記下給 planner 的那段。

        Returns:
            頁面「聯想」欄的摘要、判斷詳情裡的經過、頁面欄位（話題串、看得懂嗎、想起的記憶）。
        """
        understand = (answers or {}).get(UNDERSTAND) or {}
        pick = (answers or {}).get(PICK) or {}
        choice = str(pick.get("choice") or "")
        index = int(choice[1:]) if re.fullmatch(r"m\d+", choice) else 0
        picked = recall["candidates"][index - 1] if 0 < index <= len(recall["candidates"]) else None
        # 請求失敗沒拿到判斷時不當作沒把握，免得每次失敗都叫她別把話說死
        confidence = probability(pick, choice) if picked else probability(understand, "yes") if understand else 1.0
        lost = understand.get("choice") == "no"
        previous = self._recalls.get(session_id) or {}
        lookup: Dict[str, Any] = {}
        if previous.get("focus") == recall["focus"]["msg_id"] and previous.get("lookup", {}).get("summary"):
            lookup = previous["lookup"]  # 同一則查過就不再查
        elif lost and decision.lookup_enabled:
            lookup = await self._lookup(session_id, recall["chat"], picked["text"] if picked else "")
        note = recall_note(picked, confidence, lookup)
        self._recalls[session_id] = {"focus": recall["focus"]["msg_id"], "note": note, "lookup": lookup}
        verdict = str(understand.get("choice") or "")
        known = sum(1 for person in recall["people"] if person["impression"])
        missed = sum(1 for person in recall["people"] if person["missed"])
        parts = [
            f"{'看不懂' if lost else '看得懂'} {probability(understand, verdict):.2f}" if verdict in ("yes", "no") else "沒拿到判斷",
            f"想到 m{index}（{confidence:.2f}）"
            if picked
            else "群記憶沒等到（逾時或失敗）"
            if recall["missed"]
            else f"記憶 {len(recall['candidates'])} 筆都沒選",
            f"人 {known}/{len(recall['people'])}" + (f"（{missed} 人沒等到）" if missed else ""),
        ]
        if lookup:
            parts.append(f"上網「{lookup.get('query', '')}」" + ("" if lookup.get("summary") else f"失敗：{lookup.get('error', '')}"))
        columns = {
            "topic": recall.get("topic_label", ""),
            "understood": f"{'不懂' if lost else '懂'} {probability(understand, verdict):.2f}" if verdict in ("yes", "no") else "—",
            "memory": "檢索逾時" if recall["missed"] else f"{len(recall['candidates'])} 筆・人 {known}/{len(recall['people'])}",
        }
        return "・".join(parts), recall_detail(recall, lookup, note), columns

    async def _lookup(self, session_id: str, chat: str, hint: str) -> Dict[str, Any]:
        """請 linux 沙盒電腦插件上網查；沒裝、沒權限或逾時都回傳帶 error 的 dict。"""
        try:
            result = await self.ctx.call_capability(
                "api.call",
                timeout_ms=LOOKUP_TIMEOUT_SECONDS * 1000,
                api_name=LOOKUP_API,
                version="",
                args={"chat": chat, "hint": hint, "stream_id": session_id},
            )
        except Exception as exc:  # 查不到就照原本的聯想回
            result = {"success": False, "error": repr(exc)}
        if not isinstance(result, Mapping):
            result = {"success": False, "error": f"回傳格式不對：{result!r}"}
        if not result.get("summary"):
            self.ctx.logger.warning(f"聯想 [{session_id}]：上網查失敗：{result.get('error')}")
        return dict(result)

    def _add_recall_note(self, kwargs: Dict[str, Any], session_id: str) -> None:
        """要回的那則還在 planner 上下文裡，就把這次的聯想接在系統提示詞後面。"""
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
        """她回的是焦點那串（或沒指定回哪則）時，replyer 也看到同一段聯想（它看不到 planner 的系統提示詞）。"""
        session_id = str(kwargs.get("session_id") or "")
        recall = self._recalls.get(session_id) or {}
        target = display_message_id(str(kwargs.get("reply_message_id") or ""))
        store = getattr(self, "_topics", {}).get(session_id) or {"of": {}, "topics": {}}
        if not recall.get("note") or (target and target not in topic_members(store, recall["focus"]) and target != recall["focus"]):
            return {"action": "continue"}
        kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{recall['note']}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}
