"""話題切分：群聊裡常常好幾個話題交錯，planner 只看到一長串平鋪的訊息，容易把不同話題的人和內容混在一起。

參考 topic_segmenter（規則級聯、只和少數活躍話題比）、ChatLab（LLM 話題帳本）和 ChatIndex（LLM 失敗時保留原歸屬）：

- 直接切 planner 這輪看到的訊息（hook 的 items）：她自己的發言、回覆關係（quote）和 msg_id 都跟她看到的一致，
  不用另外收訊、對 ID。每則只在第一次出現時歸類，之後沿用。
- 依序套用規則，命中就停：回覆了哪則 → @ 了誰（叫她的算 @ 她）→ 附和或表情 → 同一人短時間連發 → 嵌入相似度 → 開新話題。
  超過 thread_ttl_minutes 沒有新訊息的話題不再收新訊息（回覆除外）。
- 新訊息每輪批次算一次向量，最多等 EMBED_TIMEOUT 秒；算不出來的那則只靠規則。
- 每累積 ledger_every 則新訊息，背景請 LLM 重新歸類最近 ledger_window 則、給話題命名和寫摘要，下一輪生效；失敗時保留規則結果。
- planner：在每輪最後那條提醒前面插一段「話題線索」，不改系統提示詞，前面的請求前綴還能命中快取；兩個話題以上才插。
  replyer：告訴它要回的那則屬於哪個話題、群裡同時還在聊什麼。
- 狀態只放記憶體，重啟後從新的 planner 輪重新累積。頁面「最近的歸類」列出每則用哪條規則、相似度多少，用來調門檻。
"""

import asyncio
import math
import re
import time
from collections import OrderedDict
from datetime import datetime
from itertools import count
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from uuid import uuid4

from maibot_sdk import API, HookHandler

from ..config import PAGE_FIELDS
from .emote import display_message_id, planner_messages
from .life.common import generate_json

# 她自己的發話人鍵（不會和群友名字撞）
SELF = "\0self"
EMBED_TIMEOUT = 2.0
# 每個頻道記住最近幾則的歸類
REMEMBERED = 300
# 話題的向量：最近幾則有向量的訊息取平均，話題內容漂移時跟著走
CENTROID_MESSAGES = 8
# 線索裡每個話題最多列幾個 msg_id
IDS_PER_THREAD = 12
PAGE_RECENT = 30
# 去掉標點、空白和表情後只剩附和語
FILLER = re.compile(
    r"(?:[哈呵嘿嘻草哦噢喔嗯恩啊呀欸誒唉蛤嗷]+|[6６]+|1+|ok+|好+的?|是+的?|[對对]+|行|可以|[確确]實|真的|笑死|xs|lol|233+|w+)",
    re.IGNORECASE,
)
RULE_LABELS = {
    "quote": "回覆",
    "at": "@",
    "filler": "附和",
    "speaker": "連發",
    "embed": "語意相近",
    "new": "新話題",
    "llm": "LLM 整理",
}
PLANNER_NOTE = (
    "【話題線索】群裡同時有好幾個話題，下面是最近的訊息分成的話題（最近活躍的在前），依回覆、@、連發和語意相似度自動分組，"
    "可能有誤，以聊天紀錄為準。決定回應誰之前，先看對方在哪個話題、在跟誰說話；回覆只接那個話題，不要把不同話題的人和內容混在一起。"
)
LEDGER_PROMPT = """你在整理 Discord 群聊的話題。群裡常常同時有好幾個話題交錯進行，請把下面每則訊息歸到它真正在聊的話題。

目前的話題：
{threads}

訊息（#編號 [目前話題] 發言人: 內容；→#n 表示回覆第 n 則）：
{lines}

規則：
- 依內容判斷誰在回應誰、在聊什麼，不要只看先後順序；同一個人也可能在兩個話題之間來回。
- 沿用既有話題編號；確實是新話題才用 new1、new2 這樣的新編號；兩個話題其實在聊同一件事就合成一個。
- 附和、表情、「哈哈」之類歸到它回應的那個話題。
- title 不超過 12 字；summary 一句話，說誰在聊什麼、聊到哪。

只輸出 JSON，每則訊息都要歸類，threads 列出 assign 用到的每個話題：
{{"assign": {{"1": "t3", "2": "new1"}}, "threads": {{"t3": {{"title": "…", "summary": "…"}}, "new1": {{"title": "…", "summary": "…"}}}}}}"""

_thread_ids = count(1)


def is_filler(text: str) -> bool:
    """附和、表情、只有一個字之類沒有話題內容的訊息。"""
    core = re.sub(r"[\W_]+", "", re.sub(r"\[圖片\]", "", text))
    return len(core) <= 1 or FILLER.fullmatch(core) is not None


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return sum(x * y for x, y in zip(a, b, strict=True)) / norm if norm else 0.0


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _plain(name: str) -> str:
    """發話人名字去掉外框的「」，planner 上下文和 @ 裡的寫法才對得上。"""
    return name.strip().strip("「」")


class ChannelThreads:
    """一個聊天的歸類：``entries`` 照第一次出現的順序 {msg_id: 那則的歸類}，``threads`` {話題 ID: 標題與摘要}。"""

    def __init__(self) -> None:
        self.entries: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
        self.threads: Dict[str, Dict[str, str]] = {}
        self.pending = 0
        self.ledger_task: Optional[asyncio.Task[None]] = None
        self.ledger_at: Optional[datetime] = None

    def new_thread(self) -> str:
        thread_id = f"t{next(_thread_ids)}"
        self.threads[thread_id] = {"title": "", "summary": ""}
        return thread_id

    def active(self, now: float, ttl: float, limit: int) -> List[str]:
        """最近 ttl 秒內有訊息的話題，最近活躍的在前。"""
        found: List[str] = []
        for entry in reversed(self.entries.values()):
            if now - entry["time"] > ttl or len(found) >= limit:
                break
            if entry["thread"] not in found:
                found.append(entry["thread"])
        return found

    def last_of(self, speakers: Sequence[str], threads: Sequence[str]) -> Optional[Dict[str, Any]]:
        for entry in reversed(self.entries.values()):
            if entry["speaker"] in speakers and entry["thread"] in threads:
                return entry
        return None

    def centroid(self, thread_id: str) -> Optional[List[float]]:
        vectors = [e["vector"] for e in reversed(self.entries.values()) if e["thread"] == thread_id and e["vector"]]
        vectors = vectors[:CENTROID_MESSAGES]
        return [sum(column) / len(vectors) for column in zip(*vectors, strict=True)] if vectors else None

    def label(self, thread_id: str) -> str:
        """話題標題；LLM 還沒命名時用第一則有內容的訊息代替。"""
        if title := self.threads[thread_id]["title"]:
            return title
        first = next((e["text"] for e in self.entries.values() if e["thread"] == thread_id and not is_filler(e["text"])), "")
        return _clip(first, 12) if first else "（附和與表情）"

    def prune(self) -> None:
        while len(self.entries) > REMEMBERED:
            self.entries.popitem(last=False)
        used = {entry["thread"] for entry in self.entries.values()}
        for thread_id in [t for t in self.threads if t not in used]:
            del self.threads[thread_id]


def assign(state: ChannelThreads, entry: Mapping[str, Any], config: Any) -> Tuple[str, str, Optional[float]]:
    """為一則新訊息選話題，回傳 (話題 ID, 規則, 最高相似度)；entry 還沒加進 state。"""
    quoted = state.entries.get(entry["quote"])
    if quoted is not None:
        # 回覆是最明確的訊號，被回覆的話題冷掉了也接回去
        return quoted["thread"], "quote", None
    active = state.active(entry["time"], config.thread_ttl_minutes * 60, config.max_active_threads)
    if not active:
        return state.new_thread(), "new", None
    if entry["mentions"] and (mentioned := state.last_of(entry["mentions"], active)) is not None:
        return mentioned["thread"], "at", None
    if is_filler(entry["text"]):
        return active[0], "filler", None
    previous = state.last_of([entry["speaker"]], list(state.threads))
    if previous is not None and previous["thread"] in active and entry["time"] - previous["time"] <= config.same_speaker_seconds:
        return previous["thread"], "speaker", None
    best, score = "", None
    if entry["vector"]:
        for thread_id in active:
            centroid = state.centroid(thread_id)
            # 換過 embedding 模型時維度不同，舊向量不比
            if centroid and len(centroid) == len(entry["vector"]):
                similarity = cosine(entry["vector"], centroid)
                if score is None or similarity > score:
                    best, score = thread_id, similarity
    if score is not None and score >= config.similarity_threshold:
        return best, "embed", score
    return state.new_thread(), "new", score


def ledger_prompt(state: ChannelThreads, window: Sequence[Mapping[str, Any]]) -> str:
    index = {message["msg_id"]: number for number, message in enumerate(window, 1)}
    lines = []
    for number, message in enumerate(window, 1):
        quote = f" →#{index[message['quote']]}" if message["quote"] in index else ""
        who = "（你）" if message["self"] else _plain(message["user"])
        lines.append(f"#{number} [{state.entries[message['msg_id']]['thread']}] {who}:{quote} {message['text']}")
    used = dict.fromkeys(state.entries[message["msg_id"]]["thread"] for message in window)
    threads = "\n".join(
        f"- {t}: {state.label(t)}" + (f"（{state.threads[t]['summary']}）" if state.threads[t]["summary"] else "")
        for t in used
    )
    return LEDGER_PROMPT.format(threads=threads, lines="\n".join(lines))


def apply_ledger(state: ChannelThreads, window: Sequence[Mapping[str, Any]], result: Any) -> int:
    """照 LLM 的結果改窗口裡訊息的話題、更新標題摘要，回傳改了幾則；格式不對時拋 ValueError。

    整理期間新來的訊息不在窗口裡，保持原樣；已經淘汰的跳過。
    """
    if not isinstance(result, dict) or not isinstance(result.get("assign"), dict):
        raise ValueError(f"LLM 回覆缺少 assign：{str(result)[:200]}")
    infos = result.get("threads") if isinstance(result.get("threads"), dict) else {}
    mapping: Dict[str, str] = {}
    changed = 0
    for raw_number, label in result["assign"].items():
        try:
            message = window[int(str(raw_number).lstrip("#")) - 1]
        except (ValueError, IndexError):
            continue
        entry = state.entries.get(message["msg_id"])
        if entry is None or not isinstance(label, str):
            continue
        if label not in mapping:
            mapping[label] = label if label in state.threads else state.new_thread()
        if entry["thread"] != mapping[label]:
            entry["thread"], entry["rule"] = mapping[label], "llm"
            changed += 1
    for label, info in infos.items():
        thread = state.threads.get(mapping.get(label, label))
        if thread is not None and isinstance(info, dict):
            thread["title"] = _clip(str(info.get("title") or ""), 20)
            thread["summary"] = _clip(str(info.get("summary") or ""), 80)
    state.prune()
    return changed


def planner_note(state: ChannelThreads, messages: Sequence[Mapping[str, Any]]) -> str:
    """這輪 planner 看得到的訊息分成的話題；不到兩個話題時回傳空字串。"""
    groups: Dict[str, List[Mapping[str, Any]]] = {}
    for message in messages:
        if entry := state.entries.get(message["msg_id"]):
            # 先移除再放回，字典順序就是各話題最後一則出現的順序
            groups[entry["thread"]] = [*groups.pop(entry["thread"], []), message]
    if len(groups) < 2:
        return ""
    lines = []
    for thread_id, members in reversed(groups.items()):
        people = "、".join(dict.fromkeys("你" if m["self"] else _plain(m["user"]) for m in members))
        ids = " ".join(m["msg_id"] for m in members[-IDS_PER_THREAD:])
        line = f"- [{thread_id}] {state.label(thread_id)}｜{people}｜msg_id：{ids}"
        if summary := state.threads[thread_id]["summary"]:
            line += f"\n  {summary}"
        lines.append(line)
    return PLANNER_NOTE + "\n" + "\n".join(lines)


class TopicThreadsMixin:
    """需要主插件提供 ctx、_load_settings、_setting_rows、_save_page_fields、_save_page_setting、
    _resolve_discord_stream、_webui_chat_names，和 EmoteMixin 收訊時記下的 _inbound（mentions、addressed）與 _latest_inbound。"""

    def _init_thread_state(self) -> None:
        self._threads: Dict[str, ChannelThreads] = {}
        self._threads_embed_warned = False

    def _cancel_thread_tasks(self) -> None:
        for state in self._threads.values():
            if state.ledger_task is not None:
                state.ledger_task.cancel()

    async def _embed_texts(self, texts: List[str]) -> Optional[List[List[float]]]:
        """一次算完這輪新訊息的向量；失敗或逾時回傳 None，只在第一次失敗時記警告，免得每輪都刷。"""
        try:
            result = await asyncio.wait_for(self.ctx.call_capability("llm.embed", texts=texts), EMBED_TIMEOUT)
        except Exception as exc:  # 逾時或 RPC 失敗
            result = {"error": repr(exc)}
        rows = result.get("results") if isinstance(result, dict) else None
        vectors = [row.get("embedding") for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
        if len(vectors) == len(texts) and all(isinstance(vector, list) and vector for vector in vectors):
            self._threads_embed_warned = False
            return vectors
        if not self._threads_embed_warned:
            self._threads_embed_warned = True
            error = result.get("error") if isinstance(result, dict) else result
            self.ctx.logger.warning(f"話題切分算不出向量，先只用規則：{error}")
        return None

    async def _assign_new(self, session_id: str, messages: Sequence[Mapping[str, Any]]) -> ChannelThreads:
        """把這輪第一次出現的訊息歸進話題；有新訊息時累計，夠了就在背景請 LLM 整理。"""
        config = self._load_settings().threads
        state = self._threads.setdefault(session_id, ChannelThreads())
        remembered = self._inbound.get(session_id, {})
        fresh = [m for m in messages if m["msg_id"] and m["msg_id"] not in state.entries]
        if not fresh:
            return state
        to_embed = [m for m in fresh if not is_filler(m["text"])]
        vectors = await self._embed_texts([m["text"] for m in to_embed]) if to_embed else None
        vector_of = {id(m): v for m, v in zip(to_embed, vectors, strict=True)} if vectors else {}
        now = time.time()
        for message in fresh:
            inbound = remembered.get(message["msg_id"]) or {}
            mentions = [_plain(name) for name in inbound.get("mentions", [])]
            if inbound.get("addressed"):
                mentions.append(SELF)
            entry = {
                "speaker": SELF if message["self"] else _plain(message["user"]),
                "text": message["text"],
                "quote": message["quote"],
                "time": message["time"] or now,
                "mentions": mentions,
                "vector": vector_of.get(id(message)),
            }
            entry["thread"], entry["rule"], entry["score"] = assign(state, entry, config)
            state.entries[message["msg_id"]] = entry
        state.prune()
        state.pending += len(fresh)
        if (
            config.ledger_enabled
            and state.pending >= config.ledger_every
            and (state.ledger_task is None or state.ledger_task.done())
        ):
            state.pending = 0
            window = [
                {key: value for key, value in m.items() if key != "head"} for m in messages if m["msg_id"] in state.entries
            ][-config.ledger_window :]
            state.ledger_task = asyncio.create_task(self._run_ledger(session_id, state, window))
        return state

    async def _run_ledger(self, session_id: str, state: ChannelThreads, window: List[Dict[str, Any]]) -> None:
        """LLM 整理；失敗時保留規則結果，下次累積夠了再試。"""
        try:
            result = await generate_json(self.ctx, ledger_prompt(state, window), self._load_settings().threads.model_task)
            if result is None:  # generate_json 已記過原因
                return
            changed = apply_ledger(state, window, result)
            state.ledger_at = datetime.now()
            self.ctx.logger.info(f"話題整理 [{session_id}]：{len(window)} 則，改了 {changed} 則的話題")
        except Exception as exc:
            self.ctx.logger.warning(f"話題整理 [{session_id}] 失敗，保留規則切分結果：{exc}")

    def _in_listed_channel(self, session_id: str) -> bool:
        """只切頻道列表裡的頻道（Discord 頻道的 group_id 就是頻道 ID）；私訊沒有 group_id，不會切。"""
        channel_id = self._latest_inbound.get(session_id, {}).get("group_id")
        return bool(channel_id) and channel_id in self._load_settings().threads.channel_list

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_topic_threads",
        mode="blocking",
        # 在回覆標籤（quote_context，late）加進正文之前讀，算向量和給 LLM 看的是原本的內容
        order="normal",
        timeout_ms=3000,
    )
    async def add_topic_threads_to_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """這輪新出現的訊息歸進話題，兩個話題以上時在最後那條提醒前面插一段話題線索。"""
        session_id = str(kwargs.get("session_id") or "")
        items = kwargs.get("items")
        if not (self._load_settings().threads.enabled and self._in_listed_channel(session_id) and isinstance(items, list)):
            return {"action": "continue"}
        messages = planner_messages(items)
        state = await self._assign_new(session_id, messages)
        note = planner_note(state, messages)
        if not note:
            return {"action": "continue"}
        last = items[-1] if items else {}
        last_text = "".join(str(part.get("text") or "") for part in last.get("parts") or [] if isinstance(part, dict))
        stamp = (last.get("meta") or {}).get("timestamp") or datetime.now().isoformat()
        item = {
            "item_type": "UserMessageItem",
            "meta": {"item_id": f"discord-topic-threads-{uuid4().hex}", "logical_turn_id": None, "timestamp": stamp},
            "parts": [{"type": "text", "text": note}],
        }
        # MaiBot 每輪最後有一條一次性提醒（不是聊天訊息）：插在它前面，讓提醒還是最後一條
        reminder_last = last.get("item_type") == "UserMessageItem" and not last_text.startswith("<message ")
        items.insert(len(items) - 1 if reminder_last else len(items), item)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="discord_topic_threads_reply",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def add_topic_thread_to_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """replyer 只拿到要回的那則：告訴它那則屬於哪個話題、群裡同時還在聊什麼。"""
        config = self._load_settings().threads
        state = self._threads.get(str(kwargs.get("session_id") or ""))
        entry = state.entries.get(display_message_id(str(kwargs.get("reply_message_id") or ""))) if state else None
        if state is None or entry is None or not config.enabled:
            return {"action": "continue"}
        thread_id = entry["thread"]
        members = [e for e in state.entries.values() if e["thread"] == thread_id]
        people = "、".join(dict.fromkeys("你" if e["speaker"] == SELF else e["speaker"] for e in members[-IDS_PER_THREAD:]))
        note = f"你要回覆的這則屬於話題「{state.label(thread_id)}」（在聊的人：{people}）。"
        if summary := state.threads[thread_id]["summary"]:
            note += summary
        others = [t for t in state.active(time.time(), config.thread_ttl_minutes * 60, config.max_active_threads) if t != thread_id]
        if others:
            note += f"群裡同時還在聊{'、'.join(f'「{state.label(t)}」' for t in others)}，回覆只接這個話題，不要把其他話題的人和內容混進來。"
        kwargs["extra_prompt"] = f"{kwargs.get('extra_prompt') or ''}\n{note}".strip()
        return {"action": "continue", "modified_kwargs": kwargs}

    # ===== DC生活頁面 =====

    @API("webui_threads", description="讀取話題切分的設定、目前的話題與最近的歸類", version="1")
    async def webui_threads(self) -> Dict[str, Any]:
        """話題切分頁：每個頻道目前的話題、最近幾則用哪條規則歸類（相似度用來調門檻）；沒啟用時用 notice 說明。"""
        config = self._load_settings().threads
        names = await self._webui_chat_names()
        ttl = config.thread_ttl_minutes * 60
        now = time.time()
        threads, recent = [], []
        for session_id, state in self._threads.items():
            chat = names.get(session_id, session_id)
            for thread_id in state.active(now, ttl, len(state.threads)):
                members = [e for e in state.entries.values() if e["thread"] == thread_id]
                threads.append(
                    {
                        "chat": chat,
                        "thread": thread_id,
                        "title": state.label(thread_id),
                        "people": "、".join(dict.fromkeys("她" if e["speaker"] == SELF else e["speaker"] for e in members)),
                        "count": len(members),
                        "last": datetime.fromtimestamp(members[-1]["time"]).strftime("%H:%M:%S"),
                        "summary": state.threads[thread_id]["summary"] or "—",
                    }
                )
            for msg_id, entry in state.entries.items():
                recent.append(
                    {
                        "at": entry["time"],
                        "time": datetime.fromtimestamp(entry["time"]).strftime("%m-%d %H:%M:%S"),
                        "chat": chat,
                        "msg_id": msg_id,
                        "speaker": "她" if entry["speaker"] == SELF else entry["speaker"],
                        "text": entry["text"] or "—",
                        "thread": f"[{entry['thread']}] {state.label(entry['thread'])}",
                        "rule": RULE_LABELS[entry["rule"]],
                        "score": f"{entry['score']:.3f}" if entry["score"] is not None else "—",
                    }
                )
        recent.sort(key=lambda row: row["at"], reverse=True)
        for row in recent:
            del row["at"]
        channels = []
        for channel_id in config.channel_list:
            stream_id = await self._resolve_discord_stream(channel_id)
            state = self._threads.get(stream_id or "")
            if not stream_id:
                status = "還沒有人說話，MaiBot 不認得這個頻道"
            elif state is None:
                status = "等待新訊息：有人說話、麥麥開始思考時就會切"
            else:
                status = f"切分中：{len(state.active(now, ttl, len(state.threads)))} 個活躍話題，記了 {len(state.entries)} 則"
            channels.append({"channel_id": channel_id, "chat": names.get(stream_id or "", "—"), "status": status})
        ledgers = [state.ledger_at for state in self._threads.values() if state.ledger_at]
        notice = ""
        if not config.enabled:
            notice = "話題切分沒有啟用：在下面設定裡打開。"
        elif not config.channel_list:
            notice = "還沒設定頻道，話題切分不會動：在下面加入 Discord 頻道 ID。"
        elif self._threads_embed_warned:
            notice = "算不出向量（MaiBot 沒設 embedding 模型或呼叫失敗），目前只用規則和 LLM 整理；詳情看日誌。"
        return {
            "notice": notice,
            "settings": self._setting_rows("threads", [field for field in PAGE_FIELDS["threads"] if field != "channel_list"]),
            "channel_count": len(config.channel_list),
            "channels": channels,
            "active": len(threads),
            "ledger_at": max(ledgers).strftime("%H:%M:%S") if ledgers else "—",
            "threshold": f"{config.similarity_threshold:.2f}",
            "threads": threads,
            "recent": recent[:PAGE_RECENT],
        }

    @API("webui_threads_save", description="儲存話題切分的設定", version="1")
    async def webui_threads_save(self, **fields: Optional[str]) -> Dict[str, Any]:
        fields.pop("channel_list", None)  # 頻道用下面的加入／移除
        return {"saved": self._save_page_fields("threads", fields)}

    @API("webui_threads_add_channel", description="加入要切分話題的頻道", version="1")
    async def webui_threads_add_channel(self, new_channel_id: str = "") -> Dict[str, Any]:
        channel_id = new_channel_id.strip()
        if not channel_id.isdigit():
            raise ValueError("頻道 ID 要是一串數字（在 Discord 對頻道按右鍵 → 複製頻道 ID）")
        channels = self._load_settings().threads.channel_list
        if channel_id in channels:
            raise ValueError("這個頻道已經在列表裡")
        self._save_page_setting("threads", "channel_list", [*channels, channel_id])
        return {"saved": True}

    @API("webui_threads_remove_channel", description="移除要切分話題的頻道", version="1")
    async def webui_threads_remove_channel(self, channel_id: str = "") -> Dict[str, Any]:
        """移除後這個頻道的話題也清掉，planner 和 replyer 不再收到它的話題提示。"""
        channels = self._load_settings().threads.channel_list
        self._save_page_setting("threads", "channel_list", [item for item in channels if item != channel_id])
        if stream_id := await self._resolve_discord_stream(channel_id):
            state = self._threads.pop(stream_id, None)
            if state is not None and state.ledger_task is not None:
                state.ledger_task.cancel()
        return {"saved": True}
