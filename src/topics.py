"""話題串：每則新訊息進來時分配到目前開著的話題串，或開一串新的（線上對話分線）。

研究上叫 conversation disentanglement；這裡照 Liu et al. 2020（transition-based）和 Takada & Mori 2026
（LLM 的 dialogue-level assignment）的做法：不拿訊息兩兩比，而是問「這則屬於哪一串」。
用 2026-11-18 正式機 12:50～14:52 的 77 則測過：選項給每串的開頭那則、最近兩則和距離幾分鐘，
比往回一則一則問少很多請求，有時間間隔的換話題也分得出來。

- 每輪新訊息後第一次進 planner 時問：這輪還沒分配的訊息，各出一題選擇題（選項：開著的話題串、這輪前面剛問的那幾則、新話題）。
- 分完後焦點（叫她的優先，否則最新一則別人說的）所在那串就是 planner、表達方式選擇和 replyer 唯一看得到的聊天（focus_ids）。
- 用回覆功能回的直接跟被回覆那則同一串；她自己說的跟她回的那則（沒有就是她說之前最近叫她的那則）同一串；只有表情的不分。
- 自己上傳的圖片直接附給決策模型看（連結預覽的圖不附，用預覽文字；表情和貼圖不附），
  也等 MaiBot 的識圖寫好描述（image.get_description）再分：「[圖片]」換成「[圖片：描述]」，話題串裡看得出圖片在講什麼。
  描述還沒好的跟卡片沒到的一樣先等（PENDING_SECONDS 內不問）；分完才拿到描述的，話題串裡那則會補上（describe_member）。
- 只有網址、卡片還沒到，或只有圖片的訊息：卡片晚到時插件會補記（emote._remember_card）；圖片直接附給決策模型看，
  不等主程式寫圖片描述。卡片沒到的先等（PENDING_SECONDS 內不問），不擋住這輪。
- 話題串留 TOPIC_TTL_SECONDS；存在資料目錄的 threads.json（dump_stores / load_stores），插件重啟後讀回。
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
# 一段對話最多延續兩小時；舊版沒有 created 的巨串只保留歷史，不再當候選。
SEGMENT_SECONDS = 2 * 3600
MAX_TOPICS = 60
# 每串留給「話題串」頁顯示的最後幾則
TOPIC_ITEMS = 30
# 內容還看不到（卡片沒到）的訊息最多等多久才照現有內容分配
PENDING_SECONDS = 20
GAP_HINT = "隔了很久（10 分鐘以上）才出現的通常是新話題，除非內容明顯接得上那一串。"
NEW_TOPIC = "開了新話題，不屬於上面任何一串"
# 叫她的訊息常常隔很久才接著她上次說的（「C醬做點什麼吧」接一天前她說的話）
CALLING_HINT = "這則在叫你（@ 你、回覆你或喊你名字），常常是在接你最近說過話的那一串，就算隔了很久。"
# 網址到空白或括號為止：卡片緊接在網址後面（「…&s=12[卡片: …]」），不能把卡片吃進網址
_URL = re.compile(r"https?://[^\s\[\]（）()]+")


def hide_urls(text: str) -> str:
    """網址換成「（連結）」：給人看的詳情和給模型的檢索字串都用不到一長串網址。"""
    return _URL.sub("（連結）", text)
_LABEL = re.compile(r"\[[^\]]*\]")
_CARD = ("[卡片", "[链接预览", "[連結預覽")


def lacks_content(text: str) -> bool:
    """只有網址（卡片還沒到）：拿掉網址和標籤後沒剩字，而且沒有卡片文字。"""
    if any(label in text for label in _CARD) or not _URL.search(text):
        return False
    return not _LABEL.sub("", _URL.sub("", text)).strip()


IMAGE_MARKS = ("[圖片]", "[图片，识别中.....]")
IMAGE_DESC_CHARS = 120


def waiting_image(entry: Mapping[str, Any]) -> bool:
    """有自己上傳的圖片，MaiBot 還沒寫好描述。"""
    return bool(entry.get("image_hash")) and not entry.get("image_desc")


def describe_image(text: str, description: str) -> str:
    """第一個「[圖片]」換成「[圖片：描述]」。"""
    for mark in IMAGE_MARKS:
        if mark in text:
            return text.replace(mark, f"[圖片：{description[:IMAGE_DESC_CHARS]}]", 1)
    return text


def with_card(message: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    """訊息內容；卡片晚到時（插件補記在 entry["card"]）接在後面，圖片有描述了（entry["image_desc"]）換進去。"""
    text = str(message["text"])
    if entry.get("image_desc"):
        text = describe_image(text, str(entry["image_desc"]))
    card = str(entry.get("card") or "")
    return f"{text} {card}" if card and card not in text else text


def describe_member(store: Mapping[str, Any], msg_id: str, description: str) -> None:
    """分完話題才拿到圖片描述：話題串裡那則（選項文字和泡泡）補上。"""
    topic = store["topics"].get(store["of"].get(msg_id))
    if not topic or msg_id not in topic["members"]:
        return
    index = topic["members"].index(msg_id)
    topic["lines"][index] = describe_image(topic["lines"][index], description)
    for item in topic.get("items", []):
        if item.get("msg_id") == msg_id:
            item["text"] = describe_image(item["text"], description)


def clock(stamp: Optional[float]) -> str:
    return datetime.fromtimestamp(stamp).strftime("%H:%M") if stamp else "--:--"


def topic_line(message: Mapping[str, Any], entry: Mapping[str, Any]) -> str:
    who = "（你）" if message["self"] else f"「{message['user']}」"
    return f"[{clock(message.get('time'))}] {who}：{with_card(message, entry)}"


def numbered(message: Mapping[str, Any], numbers: Mapping[int, str]) -> str:
    """決策模型 state 裡那一行的寫法：別人的有 # 編號，她的標「（你）」。"""
    if message["self"]:
        return f"（你）：{message['text']}"
    return f"#{numbers[id(message)]} 「{message['user']}」：{message['text']}"


def unanswered(messages: Sequence[Mapping[str, Any]], seen_until: float = 0.0) -> List[Mapping[str, Any]]:
    """她上次說話之後的訊息。MaiBot 的上下文裡她說的話常常只在 reply 的工具結果裡、不是聊天訊息，
    所以也拿她上次說話時看過的訊息截到哪（seen_until，那輪 planner 開始的時間）切：在那之前收到的她已經看過了。"""
    last_self = max((index for index, message in enumerate(messages) if message["self"]), default=-1)
    return [message for message in messages[last_self + 1 :] if (message.get("time") or seen_until + 1) > seen_until]


def focus_message(
    window: Sequence[Mapping[str, Any]], remembered: Mapping[str, Mapping[str, Any]], seen_until: float = 0.0
) -> Optional[Mapping[str, Any]]:
    """這輪的焦點：她上次說話之後叫她的最新一則，沒有就是最新一則別人的訊息；只有表情的不算（看不出在聊什麼）。"""
    calling = [
        message
        for message in unanswered(window, seen_until)
        if not message["self"] and (remembered.get(message["msg_id"]) or {}).get("addressed")
    ]
    others = [message for message in window if not message["self"] and message["text"] != EMOTE_ONLY]
    return (calling or others or [None])[-1]


def new_store() -> Dict[str, Any]:
    """一個聊天的話題串：topics = {串 ID: {"members", "lines", "last"}}，of = {msg_id: 串 ID}。"""
    return {"topics": OrderedDict(), "of": {}, "next": 1, "seen": {}}


def add_member(store: Dict[str, Any], topic_id: Optional[int], message: Mapping[str, Any], line: str) -> int:
    """把這則加進那一串（None 就開新的一串），回傳串 ID。"""
    if topic_id is None or topic_id not in store["topics"]:
        topic_id = store["next"]
        store["next"] += 1
        created = float(message.get("time") or time.time())
        store["topics"][topic_id] = {"members": [], "lines": [], "last": 0.0, "created": created}
    topic = store["topics"][topic_id]
    topic["members"].append(message["msg_id"])
    topic["lines"].append(line)
    # 「話題串」頁的對話泡泡：誰、說了什麼、幾點；只留最後 TOPIC_ITEMS 則
    items = topic.setdefault("items", [])
    items.append({
        "msg_id": message["msg_id"],
        "who": "你" if message.get("self") else str(message.get("user") or ""),
        "self": bool(message.get("self")),
        # 在叫她（@、回覆她、喊名字）：agenda 用來找「叫她還沒回」
        "addressed": bool(message.get("addressed")),
        "text": hide_urls(line.split("：", 1)[-1])[:300],
        "time": clock(message.get("time")),
        "at": float(message.get("time") or time.time()),
    })
    del items[:-TOPIC_ITEMS]
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


def active_topic(topic: Mapping[str, Any], at: float) -> bool:
    """對話段有建立時間、仍在活動期且沒有超過段長；舊格式僅供歷史查閱。"""
    created = float(topic.get("created") or 0.0)
    last = float(topic.get("last") or 0.0)
    return bool(created and -1 <= at - created < SEGMENT_SECONDS and -1 <= at - last < TOPIC_TTL_SECONDS)


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
    # 一輪最多問 ASSIGN_LIMIT 則，問最新的（焦點通常是最新的）；剛重啟時更早的就不分了
    unasked = [
        index
        for index, message in enumerate(window)
        if not message["self"] and message["text"] != EMOTE_ONLY and message["msg_id"] not in store["of"]
    ]
    skipped = set(unasked[:-ASSIGN_LIMIT])
    for index, message in enumerate(window):
        if index in skipped:
            continue
        msg_id = message["msg_id"]
        if msg_id in store["of"] or message["text"] == EMOTE_ONLY:
            continue
        entry = remembered.get(msg_id) or {}
        line = topic_line(message, entry)
        if message["self"]:
            own = own_topic(window, index, store, remembered)
            if own is None:
                add_member(store, None, message, line)
            elif active_topic(store["topics"][own], now):
                add_member(store, own, message, line)
            continue
        first = store["seen"].setdefault(msg_id, now)
        if (lacks_content(with_card(message, entry)) or waiting_image(entry)) and now - first < PENDING_SECONDS:
            continue  # 卡片可能還沒到、圖片還沒識別完，下一輪再分
        options: Dict[str, Tuple[str, Any]] = {}
        criteria: Dict[str, str] = {}
        active = [tid for tid, topic in store["topics"].items() if active_topic(topic, now)]
        quoted_topic = store["of"].get(message["quote"])
        candidates = active[-LIVE_TOPICS:]
        if quoted_topic in active and quoted_topic not in candidates:
            candidates.insert(0, quoted_topic)
        for topic_id in candidates:
            topic = store["topics"][topic_id]
            options[f"t{topic_id}"] = ("topic", topic_id)
            criteria[f"t{topic_id}"] = describe_topic(topic, message.get("time"))
        # 回覆是一條明確的關係線索，但內容轉題時仍可選新串。
        if quoted_topic in store["topics"] and f"t{quoted_topic}" in criteria:
            criteria[f"t{quoted_topic}"] += "（這則回覆了本串中的訊息；仍要依內容判斷是否轉題）"
        for earlier in asked:  # 這輪前面剛問的，可能跟它開了同一串新話題
            key = f"m{numbers[id(earlier)]}"
            options[key] = ("msg", earlier["msg_id"])
            criteria[key] = f"跟這輪的 #{numbers[id(earlier)]} 同一串：{topic_line(earlier, remembered.get(earlier['msg_id']) or {})}"
        if not options:  # 沒有活躍話題：第一則直接開串；同批後續訊息可選它
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
        calling = CALLING_HINT if entry.get("addressed") else ""
        questions[question_id] = {
            "type": "choice",
            "instructions": f"#{number} {line}{picture}\n#{number} 這則在接哪一串話題？{GAP_HINT}{calling}",
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
        choice = str(answer.get("choice") or "")
        if choice not in key["options"] and choice != NONE:
            continue  # 模型回了無效選項，保持未分配，下一輪重試
        kind, target = key["options"].get(choice, ("new", None))
        if kind == "msg" and target not in store["of"]:
            continue  # 同批的前一則仍未判定，不能憑空開新串
        topic_id = target if kind == "topic" else store["of"].get(target) if kind == "msg" else None
        topic_id = add_member(store, topic_id, key["message"], key["line"])
        parts.append(f"#{question_id[len(TOPIC):]}→串{topic_id}")
    return "；".join(parts)


def topic_members(store: Mapping[str, Any], msg_id: str) -> List[str]:
    """這則所在那串的 msg_id；還沒分配時是空清單。"""
    topic_id = store["of"].get(msg_id)
    return list(store["topics"][topic_id]["members"]) if topic_id in store["topics"] else []


def earlier_items(topic: Mapping[str, Any], present: Sequence[str], before: Optional[float], limit: int = 12) -> List[Dict[str, Any]]:
    """這串裡已經不在上下文的前文：MaiBot 壓縮上下文時把較早的訊息換成摘要，摘要常常漏掉這串開頭在問什麼。

    Args:
        present: 上下文裡還有的聊天 msg_id。
        before: 上下文裡最早那則聊天的時間；只補比它早的（她說的話不是聊天訊息，比它晚的都還在工具結果裡）。
    """
    shown = set(present)
    return [
        item
        for item in topic.get("items", [])
        if item.get("msg_id") not in shown and (before is None or item.get("at", 0) < before)
    ][-limit:]


def focus_ids(store: Mapping[str, Any], msg_id: str, arrived: Sequence[str]) -> List[str]:
    """planner、表達方式選擇和 replyer 的 msg_id 快照：只包含已確認同段的成員；
    焦點未判定時僅保留焦點本身，避免將其他話題硬併進來。
    arrived 是這輪已收到的 msg_id，用於確認待判定的焦點在上下文中。
    """
    members = topic_members(store, msg_id)
    # 已有確定的歸類時，不把尚未判定的其他串混進焦點。
    if not members:
        return [msg_id] if msg_id in arrived else []
    return members


def dump_stores(stores: Mapping[str, Mapping[str, Any]], seen_until: Mapping[str, float]) -> Dict[str, Any]:
    """所有聊天的話題串和她上次說話時看過的訊息截到哪，存成 JSON（串 ID 變成字串）。"""
    return {
        "chats": {
            session_id: {**store, "topics": {str(tid): topic for tid, topic in store["topics"].items()}}
            for session_id, store in stores.items()
        },
        "seen_until": dict(seen_until),
    }


def load_stores(data: Mapping[str, Any]) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, float]]:
    """dump_stores 的反向；格式不對的聊天跳過（當作沒存過）。"""
    stores: Dict[str, Dict[str, Any]] = {}
    for session_id, raw in (data.get("chats") or {}).items():
        try:
            store = {**new_store(), **raw}
            store["topics"] = OrderedDict((int(tid), topic) for tid, topic in raw["topics"].items())
            store["of"] = {msg_id: int(tid) for msg_id, tid in raw["of"].items()}
        except (KeyError, TypeError, ValueError, AttributeError):
            continue
        stores[session_id] = store
    return stores, {key: float(value) for key, value in (data.get("seen_until") or {}).items()}
