"""話題串：每則新訊息進來時分配到目前開著的話題串，或開一串新的（線上對話分線）。

研究上叫 conversation disentanglement；這裡照 Liu et al. 2020（transition-based）和 Takada & Mori 2026
（LLM 的 dialogue-level assignment）的做法：不拿訊息兩兩比，而是問「這則屬於哪一串」。
用 2026-11-18 正式機 12:50～14:52 的 77 則測過：選項給每串的開頭那則、最近兩則和距離幾分鐘，
比往回一則一則問少很多請求，有時間間隔的換話題也分得出來。

- 每輪表情決策那次請求順便問：這輪還沒分配的訊息，各出一題選擇題（選項：開著的話題串、這輪前面剛問的那幾則、新話題）。
- 用回覆功能回的直接跟被回覆那則同一串；她自己說的跟她回的那則（沒有就是她說之前最近叫她的那則）同一串；只有表情的不分。
- 自己上傳的圖片直接附給決策模型看（連結預覽的圖不附，用預覽文字；表情和貼圖不附）。
- 只有網址、卡片還沒到，或只有圖片的訊息：卡片晚到時插件會補記（emote._remember_card）；圖片直接附給決策模型看，
  不等主程式寫圖片描述。卡片沒到的先等（PENDING_SECONDS 內不問），不擋住這輪。
- 話題串留 TOPIC_TTL_SECONDS；插件重啟就清空，聯想照舊往回找（recall._walk_thread）。
"""

import re
import time
from collections import OrderedDict
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from .reply_links import EMOTE_ONLY, NONE

TOPIC = "topic_"
# 選項最多給幾串（最近有人說話的優先）、一輪最多分配幾則（其餘下一輪）
LIVE_TOPICS = 5
ASSIGN_LIMIT = 6
TOPIC_TTL_SECONDS = 12 * 3600
MAX_TOPICS = 60
# 內容還看不到（卡片沒到）的訊息最多等多久才照現有內容分配
PENDING_SECONDS = 20
GAP_HINT = "隔了很久（10 分鐘以上）才出現的通常是新話題，除非內容明顯接得上那一串。"
NEW_TOPIC = "開了新話題，不屬於上面任何一串"
_URL = re.compile(r"https?://\S+")
_LABEL = re.compile(r"\[[^\]]*\]")
_CARD = ("[卡片", "[链接预览", "[連結預覽")


def lacks_content(text: str) -> bool:
    """只有網址（卡片還沒到）：拿掉網址和標籤後沒剩字，而且沒有卡片文字。"""
    if any(label in text for label in _CARD) or not _URL.search(text):
        return False
    return not _LABEL.sub("", _URL.sub("", text)).strip()


def with_card(message: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    """訊息內容；卡片晚到時（插件補記在 entry["card"]）接在後面。"""
    text = str(message["text"])
    card = str(entry.get("card") or "")
    return f"{text} {card}" if card and card not in text else text


def clock(stamp: Optional[float]) -> str:
    return datetime.fromtimestamp(stamp).strftime("%H:%M") if stamp else "--:--"


def topic_line(message: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    who = "（你）" if message["self"] else f"「{message['user']}」"
    return f"[{clock(message.get('time'))}] {who}：{with_card(message, entry)}"


def new_store() -> Dict[str, Any]:
    """一個聊天的話題串：topics = {串 ID: {"members", "lines", "last"}}，of = {msg_id: 串 ID}。"""
    return {"topics": OrderedDict(), "of": {}, "next": 1, "seen": {}}


def add_member(store: Dict[str, Any], topic_id: Optional[int], message: Mapping[str, Any], line: str) -> int:
    """把這則加進那一串（None 就開新的一串），回傳串 ID。"""
    if topic_id is None or topic_id not in store["topics"]:
        topic_id = store["next"]
        store["next"] += 1
        store["topics"][topic_id] = {"members": [], "lines": [], "last": 0.0}
    topic = store["topics"][topic_id]
    topic["members"].append(message["msg_id"])
    topic["lines"].append(line)
    # replyer 的聊天紀錄沒有 msg_id，只對得上「時間｜發話人」（她自己是空字串）
    if message.get("time"):
        who = "" if message.get("self") else message["user"]
        topic.setdefault("heads", set()).add(f"{datetime.fromtimestamp(message['time']):%H:%M:%S}|{who}")
    topic["last"] = max(topic["last"], float(message.get("time") or time.time()))
    store["topics"].move_to_end(topic_id)
    store["of"][message["msg_id"]] = topic_id
    return topic_id


def prune(store: Dict[str, Any], now: float) -> None:
    """過期和太多的話題串丟掉。"""
    for topic_id in [tid for tid, topic in store["topics"].items() if now - topic["last"] > TOPIC_TTL_SECONDS]:
        del store["topics"][topic_id]
    while len(store["topics"]) > MAX_TOPICS:
        store["topics"].popitem(last=False)
    alive = set(store["topics"])
    store["of"] = {msg_id: tid for msg_id, tid in store["of"].items() if tid in alive}
    store["seen"] = {msg_id: at for msg_id, at in store["seen"].items() if now - at < TOPIC_TTL_SECONDS}


def describe_topic(topic: Mapping[str, Any], at: Optional[float]) -> str:
    """選項文字：開頭那則、最近兩則和距離幾分鐘。"""
    recent = topic["lines"][-2:]
    head = "" if len(topic["lines"]) <= 2 else f"開頭：{topic['lines'][0]}｜"
    gap = max(0, round(((at or time.time()) - topic["last"]) / 60))
    return f"{head}最近：" + " ／ ".join(recent) + f"（最後一則是 {gap} 分鐘前）"


def own_topic(window: Sequence[Mapping[str, Any]], index: int, store: Mapping[str, Any], remembered: Mapping[str, Any]) -> Optional[int]:
    """她說的那則屬於哪一串：她回覆的那則，沒有就是她說之前最近叫她的那則，再沒有就是最近一則別人說的。"""
    message = window[index]
    if message["quote"] in store["of"]:
        return store["of"][message["quote"]]
    earlier = [m for m in window[:index] if not m["self"] and m["msg_id"] in store["of"]]
    calling = [m for m in earlier if (remembered.get(m["msg_id"]) or {}).get("addressed")]
    pick = (calling or earlier or [None])[-1]
    return store["of"].get(pick["msg_id"]) if pick else None


def topic_questions(
    store: Dict[str, Any],
    window: Sequence[Mapping[str, Any]],
    numbers: Mapping[int, str],
    remembered: Mapping[str, Any],
    now: float,
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]], List[str]]:
    """先用規則分配得了的（回覆、她自己說的），其餘這輪還沒分配的各出一題。

    Returns:
        (要加進請求的 questions, 題目 ID -> {"msg_id", "line", "options": {選項: ("topic", 串 ID) 或 ("msg", msg_id)}},
        要附的圖片 data URL)。
    """
    prune(store, now)
    questions: Dict[str, Dict[str, Any]] = {}
    keys: Dict[str, Dict[str, Any]] = {}
    images: List[str] = []
    asked: List[Mapping[str, Any]] = []
    for index, message in enumerate(window):
        msg_id = message["msg_id"]
        if msg_id in store["of"] or message["text"] == EMOTE_ONLY:
            continue
        entry = remembered.get(msg_id) or {}
        line = topic_line(message, entry)
        if message["self"]:
            add_member(store, own_topic(window, index, store, remembered), message, line)
            continue
        if message["quote"] in store["of"]:
            add_member(store, store["of"][message["quote"]], message, line)
            continue
        first = store["seen"].setdefault(msg_id, now)
        if lacks_content(with_card(message, entry)) and now - first < PENDING_SECONDS:
            continue  # 卡片可能還沒到，下一輪再分
        if len(asked) >= ASSIGN_LIMIT:
            break
        options: Dict[str, Tuple[str, Any]] = {}
        criteria: Dict[str, str] = {}
        for topic_id, topic in list(store["topics"].items())[-LIVE_TOPICS:]:
            options[f"t{topic_id}"] = ("topic", topic_id)
            criteria[f"t{topic_id}"] = describe_topic(topic, message.get("time"))
        for earlier in asked:  # 這輪前面剛問的，可能跟它開了同一串新話題
            key = f"m{numbers[id(earlier)]}"
            options[key] = ("msg", earlier["msg_id"])
            criteria[key] = f"跟這輪的 #{numbers[id(earlier)]} 同一串：{topic_line(earlier, remembered.get(earlier['msg_id']) or {})}"
        if not options:  # 還沒有任何話題串：直接開一串（選擇題至少要兩個選項，只有「新話題」會被回 422）
            add_member(store, None, message, line)
            continue
        criteria[NONE] = NEW_TOPIC
        picture = ""
        # 附圖只給自己上傳的圖；連結預覽的圖不附（預覽文字已經夠，也省得上傳）。表情和貼圖收訊時就不記
        if entry.get("image") and not any(label in line for label in _CARD):
            images.append(f"data:image/png;base64,{entry['image']}")
            picture = f"（這則的圖片是附圖第 {len(images)} 張）"
        number = numbers[id(message)]
        question_id = f"{TOPIC}{number}"
        questions[question_id] = {
            "type": "choice",
            "instructions": f"#{number} {line}{picture}\n#{number} 這則在接哪一串話題？{GAP_HINT}",
            "criteria": criteria,
        }
        keys[question_id] = {"msg_id": msg_id, "line": line, "message": message, "options": options}
        asked.append(message)
    return questions, keys, images


def apply_topics(store: Dict[str, Any], keys: Mapping[str, Mapping[str, Any]], answers: Optional[Mapping[str, Any]]) -> str:
    """照決策模型的答案分配；沒拿到答案的留著下一輪再問。回傳頁面摘要。"""
    parts = []
    for question_id, key in keys.items():
        answer = (answers or {}).get(question_id)
        if not isinstance(answer, Mapping):
            continue
        kind, target = key["options"].get(str(answer.get("choice") or ""), ("new", None))
        topic_id = target if kind == "topic" else store["of"].get(target) if kind == "msg" else None
        topic_id = add_member(store, topic_id, key["message"], key["line"])
        parts.append(f"#{question_id[len(TOPIC):]}→串{topic_id}")
    return "；".join(parts)


def topic_members(store: Mapping[str, Any], msg_id: str) -> List[str]:
    """這則所在那串的 msg_id；還沒分配時是空清單。"""
    topic_id = store["of"].get(msg_id)
    return list(store["topics"][topic_id]["members"]) if topic_id in store["topics"] else []


def topic_heads(store: Mapping[str, Any], msg_id: str) -> set:
    """這則所在那串每則的「時間｜發話人」；還沒分配時是空集合。"""
    topic_id = store["of"].get(msg_id)
    return set(store["topics"][topic_id].get("heads", ())) if topic_id in store["topics"] else set()


# replyer 的聊天紀錄（MaiBot 1.4.6 起整段塞在一則 user 訊息裡）：每行 ``[時:分:秒] 「名字」说：…``，
# 她自己是 ``名字(你)说：`` 或 ``你说：``；圖片 part 跟在那行後面；聊天紀錄後面接「现在…引起了你的注意」
_LINE = re.compile(r"\n(?=\[\d\d:\d\d:\d\d\] )")
_HEAD = re.compile(r"\[(\d\d:\d\d:\d\d)\] (?:「(.*?)」(?:\[回复了| ?说：))?")
_TAIL = re.compile(r"\n\n现在[^\n]*引起了你的注意")


def keep_thread_lines(parts: List[Dict[str, Any]], heads: set) -> Optional[Tuple[List[Dict[str, Any]], int]]:
    """只留聊天紀錄裡屬於這串的行（連同跟在後面的圖片），回傳 (新的 parts, 略過幾行)。

    找不到聊天紀錄的結尾、一行都對不上時回傳 None，不動。
    """
    # 每段是一串 part：第一段是模板開頭，之後每段是一行聊天和跟在後面的圖片
    segments: List[List[Dict[str, Any]]] = [[]]
    for part in parts:
        if part.get("type") != "text":
            segments[-1].append(part)
            continue
        pieces = _LINE.split(str(part.get("text") or ""))
        if pieces[0]:
            segments[-1].append({**part, "text": pieces[0]})
        segments.extend([{**part, "text": "\n" + piece}] for piece in pieces[1:])
    last = segments[-1]
    found = next(
        ((index, match) for index in reversed(range(len(last))) if last[index].get("type") == "text"
         for match in reversed(list(_TAIL.finditer(last[index]["text"])))),
        None,
    )
    if len(segments) < 2 or found is None:
        return None
    index, match = found
    text = last[index]["text"]
    tail = [{**last[index], "text": text[match.start():]}, *last[index + 1:]]
    segments[-1] = [*last[:index], *([{**last[index], "text": text[:match.start()]}] if match.start() else [])]
    kept, dropped = [], 0
    for segment in segments[1:]:
        head = _HEAD.match(segment[0]["text"].lstrip("\n")) if segment and segment[0].get("type") == "text" else None
        if head is None or f"{head.group(1)}|{head.group(2) or ''}" in heads:
            kept.append(segment)
        else:
            dropped += 1
    if not dropped or len(kept) == 0:
        return None
    merged: List[Dict[str, Any]] = []
    for part in [*segments[0], *(part for segment in kept for part in segment), *tail]:
        if merged and part.get("type") == "text" and merged[-1].get("type") == "text":
            merged[-1] = {**merged[-1], "text": merged[-1]["text"] + part["text"]}
        else:
            merged.append(part)
    return merged, dropped
