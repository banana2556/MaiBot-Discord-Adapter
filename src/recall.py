"""聯想：回一則訊息之前，先想起這段在聊什麼、參與的人是誰，再交給 planner 自己判斷。

人聽到「A：羚羊拿MP5打盾牌」時，文字（羚羊、MP5）和說話的人（A 平常打 R6）會一起喚起記憶，兩邊對得上就懂了；
想錯時靠接下來的話修正，沒把握就用問的。照這個順序，在表情決策那次請求前後做（要回時才想，不邊聽邊想）：

1. 喚起：要回的那則（叫她的優先，否則最新一則）連同前文（reply_links.context_ids 的明確連結），
   先請小模型寫成檢索關鍵字（整段原文向量比對分數過不了門檻），查群記憶（文字聯想），
   每個參與者再用「名字＋關鍵字」限定這個人查（整體印象）。
   人物畫像沒開放給插件，用限定人物的記憶檢索代替。
2. 整合：決策模型同一次請求多兩題：看得懂這段在聊什麼嗎、哪一筆記憶最有關（可選都無關）。
   記憶只當候選、讓上下文挑（Kintsch 的 construction-integration），不直接取最像的那筆。
3. 看不懂、這輪又要用文字參與時，請「Linux 沙盒電腦」插件的 lookup API 上網查（十幾秒）。
4. 用確定的話題（選中的記憶或查到的說明）再查參與者跟這個話題有關的記憶。
5. 整理成一段「聯想」接在 planner 系統提示詞後面：同一回合每次請求都帶，換了要回的那則才換；
   標明是聯想、把握多少，沒把握時要她用問的或保守地接。她回這則時 replyer 也看得到。
"""

import asyncio
import re
from typing import Any, Dict, List, Mapping, Optional, Sequence

from maibot_sdk import HookHandler

from .emote import display_message_id, planner_messages
from .reply_links import NONE, context_ids, plain

LOOKUP_API = "luca.linux-computer.lookup"
# 文字聯想取幾筆、每個人取幾筆、最多看幾個人
MEMORY_HITS = 3
PERSON_HITS = 2
MAX_PEOPLE = 4
# 查詢字數上限（記憶檢索和整理話題用）
QUERY_CHARS = 300
# 正式機的記憶檢索常要好幾秒，4 秒時幾乎每次都逾時；等不到就這輪少一段聯想
# ponytail: 群記憶和每個人同時查，檢索慢時會互相拖慢；還是常逾時就改成先查群記憶、人只查要回那則的發話人
SEARCH_TIMEOUT_SECONDS = 15
MISSED = "沒等到（逾時或失敗）"
# 上網查最多等多久；lookup 本身最多 150 秒，等不到就這輪不用
LOOKUP_TIMEOUT_SECONDS = 60
# 選中的記憶（或看得懂）的機率低於這個值，就要她用問的或保守地接
CONFIDENT = 0.5
UNDERSTAND = "recall_understand"
PICK = "recall_pick"
NOTE_HEAD = "【聯想】回 msg_id={msg_id}（{user}）之前，你想起這些。這是聯想，不一定對；和眼前的聊天衝突時以聊天為準，也別逐字說出來："
# 記憶檢索是向量比對：整段聊天原文太長太雜，分數常過不了門檻，先請小模型寫成關鍵字再查
QUERY_TASK = "utils"
QUERY_TIMEOUT_SECONDS = 10
QUERY_PROMPT = """下面是 Discord 群聊裡的一段對話：
{chat}

寫一行記憶檢索用的關鍵字（3～8 個詞，空格分開）：對話裡的遊戲、作品、人物、事件、梗、術語，和這段在聊的主題。
只輸出關鍵字這一行，不要解釋。"""
UNSURE = "你對這段在聊什麼沒有把握：要回的話可以用問的（例如「你們在說……嗎？」），或接得保守一點，別把猜測當成事實。"

_HIT = re.compile(r"(?:^|\n)\s*\d+\.\s*")


def parse_hits(result: Any) -> List[str]:
    """knowledge.search 的結果 -> 每筆記憶；沒找到或失敗時是空清單。

    SDK 成功時把結果拆成純文字「你知道这些知识: 1. … 2. …」，沒找到是「你不太了解有关…的知识」，失敗回原樣的 dict。
    """
    if not isinstance(result, str) or result.startswith("你不太了解"):
        return []
    body = result.split(":", 1)[1] if result.startswith("你知道") else result
    return [hit.strip() for hit in _HIT.split(body) if hit.strip()]


def focus_message(window: Sequence[Mapping[str, Any]], remembered: Mapping[str, Mapping[str, Any]]) -> Optional[Mapping[str, Any]]:
    """要回的那則：她上次說話之後叫她的最新一則，沒有就是最新一則別人的訊息。"""
    last_self = max((index for index, message in enumerate(window) if message["self"]), default=-1)
    calling = [
        message
        for message in window[last_self + 1 :]
        if not message["self"] and (remembered.get(message["msg_id"]) or {}).get("addressed")
    ]
    others = [message for message in window if not message["self"]]
    return (calling or others or [None])[-1]


def participants(
    window: Sequence[Mapping[str, Any]],
    focus: Mapping[str, Any],
    context: Sequence[str],
    remembered: Mapping[str, Mapping[str, Any]],
    bot_id: str,
) -> List[Dict[str, str]]:
    """這段話題的參與者：要回那則的發話人、前文的發話人（新的先）、那則 @ 的人；不含她自己，最多 MAX_PEOPLE 個。"""
    by_id = {message["msg_id"]: message for message in window}
    people: Dict[str, Dict[str, str]] = {}
    for msg_id in [focus["msg_id"], *reversed(context)]:
        message, entry = by_id.get(msg_id), remembered.get(msg_id) or {}
        if message and not message["self"] and entry.get("user_id"):
            people.setdefault(entry["user_id"], {"user_id": entry["user_id"], "name": plain(message["user"]), "platform": entry["platform"]})
    entry = remembered.get(focus["msg_id"]) or {}
    for user_id, name in (entry.get("mentioned") or {}).items():
        people.setdefault(user_id, {"user_id": user_id, "name": plain(name), "platform": entry["platform"]})
    return [person for user_id, person in people.items() if user_id != bot_id][:MAX_PEOPLE]


def probability(answer: Mapping[str, Any], option: str) -> float:
    """決策模型給某個選項的機率；沒給機率時只信它選的那個（用 confidence）。"""
    probabilities = answer.get("probabilities")
    if isinstance(probabilities, Mapping):
        value = probabilities.get(option)
        return float(value) if isinstance(value, (int, float)) else 0.0
    return float(answer.get("confidence") or 1.0) if answer.get("choice") == option else 0.0


def recall_state(recall: Mapping[str, Any], number: str) -> str:
    """接在決策模型 state 後面：要回的是哪則、想起的記憶（m 編號）和對參與者的印象。"""
    lines = [f"\n\n【聯想】#{number} 是你要回的那則。下面是你想起的，可能不對。"]
    if recall["hits"]:
        lines.append("想起的群聊記憶：" + "".join(f"\nm{index}. {hit}" for index, hit in enumerate(recall["hits"], 1)))
    for person in recall["people"]:
        if person["impression"]:
            lines.append(f"對「{person['name']}」的印象：" + "；".join(person["impression"]))
    return "\n".join(lines)


def recall_questions(recall: Mapping[str, Any], number: str) -> Dict[str, Dict[str, Any]]:
    """看不看得懂、哪一筆記憶最有關；沒想起記憶時只問第一題（選擇題至少兩個選項）。"""
    questions = {
        UNDERSTAND: {
            "type": "choice",
            "instructions": f"#{number} 和它在接的那幾則，你看得懂在聊什麼嗎？",
            "criteria": {
                "yes": "看得懂：靠常識或想起的記憶就知道在聊什麼",
                "no": "看不懂：有不認識的遊戲、作品、人物、新聞、梗或術語，要先查",
            },
        }
    }
    if recall["hits"]:
        criteria = {f"m{index}": hit for index, hit in enumerate(recall["hits"], 1)}
        criteria[NONE] = "都跟這段在聊的無關"
        questions[PICK] = {"type": "choice", "instructions": f"#{number} 那段在聊的，跟哪一筆記憶最有關？", "criteria": criteria}
    return questions


def recall_note(recall: Mapping[str, Any], picked: str, confidence: float, lookup: Mapping[str, Any]) -> str:
    """給 planner（和 replyer）的那段；什麼都沒想起、也不缺把握時是空字串。"""
    lines = []
    if lookup.get("summary"):
        sources = "、".join(lookup.get("sources") or []) or "搜尋結果"
        lines.append(f"- 上網查到：{lookup['summary']}（來源：{sources}）")
    if picked:
        lines.append(f"- 可能在聊：{picked}（把握 {confidence:.2f}）")
    elif recall["hits"] and not lookup.get("summary"):
        lines.append("- 想起幾筆可能有關的記憶，但看不出是哪一筆：" + "；".join(recall["hits"]))
    remembered = False
    for person in recall["people"]:
        memories = list(dict.fromkeys([*person.get("details", []), *person["impression"]]))[:3]
        remembered = remembered or bool(memories)
        if not memories and person.get("missed"):
            continue  # 沒等到結果不等於沒有記憶，別跟她說「沒有相關的記憶」
        lines.append(f"- 「{person['name']}」：" + ("；".join(memories) if memories else "沒有相關的記憶"))
    unsure = not lookup.get("summary") and confidence < CONFIDENT
    if unsure:
        lines.append(UNSURE)
    elif not (lookup.get("summary") or recall["hits"] or remembered):
        return ""
    focus = recall["focus"]
    return NOTE_HEAD.format(msg_id=focus["msg_id"], user=focus["user"]) + "\n" + "\n".join(lines)


def recall_detail(recall: Mapping[str, Any], lookup: Mapping[str, Any], note: str) -> str:
    """判斷詳情裡的聯想經過：用什麼關鍵字查、每一邊查到什麼、上網結果、最後給 planner 的那段。"""
    hits = "".join(f"\nm{index}. {hit}" for index, hit in enumerate(recall["hits"], 1))
    lines = [f"關鍵字：{recall['query']}", f"群記憶：{hits or (MISSED if recall['missed'] else '沒查到')}"]
    for person in recall["people"]:
        found = list(dict.fromkeys([*person["impression"], *person.get("details", [])]))
        if found:
            state = "；".join(found) + (f"（有一次{MISSED}）" if person["missed"] else "")
        else:
            state = MISSED if person["missed"] else "沒查到" if person.get("person_id") else "找不到這個人的人物 ID"
        lines.append(f"「{person['name']}」：{state}")
    if lookup:
        lines.append(f"上網「{lookup.get('query', '')}」：" + str(lookup.get("summary") or f"失敗：{lookup.get('error', '')}"))
    lines.append("給 planner 的：" + (f"\n{note}" if note else "沒有（什麼都沒想起來，也不缺把握）"))
    return "\n".join(lines)


class RecallMixin:
    """需要主插件提供 ctx、_append_system_text、_client_manager，和 EmoteMixin 的 _reply_links。"""

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

    async def _recall(
        self, session_id: str, window: Sequence[Mapping[str, Any]], remembered: Mapping[str, Mapping[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """第 1 步：找出要回的那則和參與者，同時查文字聯想和每個人的整體印象；沒有別人的訊息時回傳 None。"""
        focus = focus_message(window, remembered)
        if focus is None:
            return None
        judged = self._reply_links.get(session_id, {}).get(focus["msg_id"], [])
        context = context_ids(window, focus["msg_id"], remembered.get(focus["msg_id"]) or {}, judged)
        by_id = {message["msg_id"]: message for message in window}
        said = [by_id[msg_id] for msg_id in [*context, focus["msg_id"]]]
        chat = "\n".join(("（你）" if message["self"] else f"「{plain(message['user'])}」") + f"：{message['text']}" for message in said)
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
        return {"focus": focus, "chat": chat, "query": query, "people": people, "hits": hits or [], "missed": hits is None}

    async def _recall_query(self, chat: str) -> str:
        """請小模型把這段聊天寫成檢索關鍵字；失敗時回傳空字串（改用原文查）。"""
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
        return text.splitlines()[0].strip() if text else ""

    def _recall_ask(self, recall: Mapping[str, Any], number: str) -> tuple[str, Dict[str, Dict[str, Any]]]:
        """接在決策模型 state 後面的文字和要加的題目（表情決策那次請求一起問）。"""
        return recall_state(recall, number), recall_questions(recall, number)

    async def _finish_recall(
        self, decision: Any, session_id: str, recall: Dict[str, Any], answers: Optional[Mapping[str, Any]], scene: str
    ) -> tuple[str, str]:
        """第 2～5 步：照決策模型的答案挑記憶、看不懂就上網查、用話題查參與者，記下給 planner 的那段。

        Returns:
            頁面「聯想」欄的摘要，和判斷詳情裡的經過（用什麼查、查到什麼、最後給 planner 什麼）。
        """
        understand = (answers or {}).get(UNDERSTAND) or {}
        pick = (answers or {}).get(PICK) or {}
        choice = str(pick.get("choice") or "")
        index = int(choice[1:]) if re.fullmatch(r"m\d+", choice) else 0
        picked = recall["hits"][index - 1] if 0 < index <= len(recall["hits"]) else ""
        # 請求失敗沒拿到判斷時不當作沒把握，免得每次失敗都叫她別把話說死
        confidence = probability(pick, choice) if picked else probability(understand, "yes") if understand else 1.0
        lost = understand.get("choice") == "no"
        previous = self._recalls.get(session_id) or {}
        lookup: Dict[str, Any] = {}
        if previous.get("focus") == recall["focus"]["msg_id"] and previous.get("lookup", {}).get("summary"):
            lookup = previous["lookup"]  # 同一則查過就不再查
        elif lost and decision.lookup_enabled and scene in ("text", "text_emote"):
            lookup = await self._lookup(session_id, recall["chat"], picked)
        topic = picked or str(lookup.get("summary") or "")
        if topic:
            details = await asyncio.gather(
                *(self._person_memories(session_id, person, topic, PERSON_HITS) for person in recall["people"])
            )
            for person, found in zip(recall["people"], details, strict=True):
                person["details"] = found or []
                person["missed"] = person["missed"] or found is None
        note = recall_note(recall, picked, confidence, lookup)
        self._recalls[session_id] = {"focus": recall["focus"]["msg_id"], "note": note, "lookup": lookup}
        verdict = str(understand.get("choice") or "")
        known = sum(1 for person in recall["people"] if person["impression"] or person.get("details"))
        missed = sum(1 for person in recall["people"] if person["missed"])
        parts = [
            f"{'看不懂' if lost else '看得懂'} {probability(understand, verdict):.2f}" if verdict in ("yes", "no") else "沒拿到判斷",
            f"想到 m{index}（{confidence:.2f}）"
            if picked
            else "群記憶沒等到（逾時或失敗）"
            if recall["missed"]
            else f"記憶 {len(recall['hits'])} 筆都沒選",
            f"人 {known}/{len(recall['people'])}" + (f"（{missed} 人沒等到）" if missed else ""),
        ]
        if lookup:
            parts.append(f"上網「{lookup.get('query', '')}」" + ("" if lookup.get("summary") else f"失敗：{lookup.get('error', '')}"))
        return "・".join(parts), recall_detail(recall, lookup, note)

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
        """她回的正是聯想的那則時，replyer 也看到同一段聯想（它看不到 planner 的系統提示詞）。"""
        recall = self._recalls.get(str(kwargs.get("session_id") or "")) or {}
        target = display_message_id(str(kwargs.get("reply_message_id") or ""))
        if not recall.get("note") or recall["focus"] != target:
            return {"action": "continue"}
        kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{recall['note']}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}
