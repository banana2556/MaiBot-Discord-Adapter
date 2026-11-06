"""表情：planner 啟動時判斷這輪要用文字、文字＋附圖、只按表情還是只發圖參與群聊，並挑表情送出。

- 收到新的 Discord 訊息後第一次進 planner 時，拿 planner 這輪看到的同一份上下文（hook 的 items），
  取最後幾則別人的訊息當可以表態的對象，一次問決策模型：情境、最值得表態的是哪一則、什麼情緒。
  送幾則、提示詞、情境說明和情緒選項都在「DC生活 → 決策模型」頁面調整（DecisionConfig）。
  群聊裡真正好笑的那則常常已經被別人接了幾句，所以表態對象不一定是最新一則。
- 不動 planner 的工具：拿掉工具時 planner 會把回覆或表情直接寫在自己的正文裡，打亂 reply / replyer 的流程。
  決策結果只用一句系統提示告訴 planner 已經做了什麼、這輪該怎麼收尾。
- 只按表情／只發圖：替她做，並提醒 planner 這輪不用再說話（MaiBot 不讓插件跳過 planner）；
  多久表態一次由 planner 的啟動頻率決定。
- 文字＋附圖：等她的文字送出、停 APPEND_DELAY_SECONDS 秒沒再送，再附一張圖（回覆可能被拆成好幾則）。
- 不限制連續發圖：替她發過之後，planner 想再發也可以。
- 只挑這個伺服器能直接發的貼圖和自訂表情（MaiBot 表情庫裡的一般圖片送出去會變成圖片附件，不挑），
  用「名字：描述」的向量相似度挑（EmoteLibrary），不經過 LLM：排除這個聊天最近用過的，
  再從前 PICK_POOL 名照相似度加權抽一張，免得每次都發同一張。
- 決策模型沒開時偶爾照 chat.emote_chance 提醒 planner 可以用 discord_emote；那個工具挑圖一樣用向量，不經過 LLM。
"""

import asyncio
import base64
import hashlib
import html
import math
import random
import re
import time
from collections import OrderedDict, deque
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import discord
from maibot_sdk import API, HookHandler, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from ..config import PAGE_FIELDS
from ..constants import DEFAULT_PLATFORM_NAME
from .decision import ask as ask_decision
from .life.common import sent_text, short_time

# 決策模型沒開時，chat.emote_chance 命中就把這段接在 planner 系統提示詞後面
EMOTE_NUDGE = (
    "【表情】這一輪如果有值得表態的訊息（好笑、好消息、附和、驚訝、無語），"
    "可以用 discord_emote 在那則訊息按個 reaction；可以和 reply 同一輪一起呼叫，也可以只按表情不說話。沒有合適的就略過。"
)
# 決策結果用系統提示告訴 planner；不動它的工具
QUICK_NOTE = (
    "【表情】你已經用{what}回應了「{user}」那則訊息。"
    "如果沒有人在問你或叫你，這輪就不用再說話，直接呼叫 stop() 結束。"
)
QUICK_ADDRESSED_NOTE = (
    "【表情】你已經用{what}回應了「{user}」那則訊息。有人在跟你說話，需要的話照常回覆。"
)
APPEND_NOTE = "【表情】這輪你回覆送出後，會自動附上一張符合情緒的表情或貼圖。"
# 決策模型能選的情境（說明在設定裡，可在頁面改）
SCENES = ("text", "text_emote", "quick_reaction", "quick_sticker")
SCENE_LABELS = {
    "text": "只要文字",
    "text_emote": "文字＋附圖",
    "quick_reaction": "只按表情",
    "quick_sticker": "只發圖",
    "failed": "請求失敗（planner 自己決定）",
}
SEND_KIND_LABELS = {"reaction": "按表情", "sticker": "只發圖", "append": "回覆後附圖", "tool": "planner 工具"}
# 頁面「測試連線」送的範例聊天
DECISION_TEST_TARGETS = {"1": "「小明」：我終於抽到限定角色了！！！", "2": "「阿華」：笑死 歐皇", "3": "「小美」：羨慕"}
DECISION_TEST_STATE = "你是「麥麥」，在 Discord 群組聊天。上次之後沒有人 @ 你。\n\n最近的群聊（越下面越新，# 是可以表態的訊息）：\n" + "\n".join(
    f"#{number} {text}" for number, text in DECISION_TEST_TARGETS.items()
)
# 頁面只列最近幾筆判斷與送出，全部的紀錄還是照 decision.py 的上限保存
PAGE_RECENT = 10
# 頁面「恢復預設」會重設的欄位：提示詞、情境說明、情緒選項、送出的則數
PROMPT_FIELDS = (
    "context_messages",
    "target_candidates",
    "scene_instructions",
    "scene_text",
    "scene_text_emote",
    "scene_quick_reaction",
    "scene_quick_sticker",
    "target_instructions",
    "emotion_instructions",
    "emotions",
)
# 決策模型（vLLM-SR）的選擇題至少要兩個選項，少於兩個整個請求回 422
MIN_EMOTIONS = 2
# MaiBot 的暱稱與別名（bot.nickname / bot.alias_names）多久重讀一次，用來判斷訊息是不是在叫她
BOT_NAMES_REFRESH_SECONDS = 600
# 每個聊天記住最近幾則收到的訊息（planner 上下文裡只有 msg_id 別名，靠這裡換回 Discord 訊息 ID）
INBOUND_REMEMBERED = 60
# 回覆後附圖：她的文字最後一則送出後等幾秒；決定附圖後多久內沒回覆就作廢
APPEND_DELAY_SECONDS = 1.5
APPEND_TTL = timedelta(minutes=2)
# 挑表情：向量排序取前 PICK_POOL 名，越接近越容易抽中（溫度越小越偏向最接近的）；
# 同一個聊天最近 RECENT_PICKS 次用過的先排除，剩不到 MIN_FRESH 個才放回來
PICK_POOL = 8
PICK_TEMPERATURE = 0.05
RECENT_PICKS = 10
MIN_FRESH = 3

_MESSAGE_HEAD = re.compile(r'<message ((?:[\w-]+="[^"]*"\s*)*)>\n?')
_ATTRIBUTE = re.compile(r'([\w-]+)="([^"]*)"')


def named(name: str, description: Optional[str]) -> str:
    """向量比對用的文字：名字放前面，才找得到「叫名字」的貼圖或表情。"""
    return f"{name}：{description}" if description else name


def decision_questions(decision: Any) -> Dict[str, Dict[str, Any]]:
    """情境題和情緒題，提示詞和選項照設定；對象題的選項是每輪的訊息，另外組。"""
    return {
        "scene": {"type": "choice", "instructions": decision.scene_instructions, "criteria": decision.scene_criteria()},
        "emotion": {"type": "choice", "instructions": decision.emotion_instructions, "criteria": decision.emotion_options()},
    }


def display_message_id(message_id: str) -> str:
    """MaiBot planner 上下文裡顯示的 msg_id：超過 12 字元的 ID 換成 ``m`` + 6 位 base32 雜湊。

    照抄 MaiBot ``maisaka/context/message_id_alias.py``（2026-10-02 起）；改了的話這裡也要跟著改，有測試釘住。
    """
    if len(message_id) <= 12:
        return message_id
    return "m" + base64.b32encode(hashlib.sha1(message_id.encode("utf-8")).digest()).decode("ascii").lower()[:6]


def planner_messages(items: Any) -> List[Dict[str, Any]]:
    """從 planner hook 的 items 取出聊天訊息：每則一個 item，第一段文字開頭是 ``<message msg_id=… user=…>``。"""
    messages: List[Dict[str, Any]] = []
    for item in items if isinstance(items, list) else []:
        parts = item.get("parts") if isinstance(item, dict) else None
        parts = parts if isinstance(parts, list) else []
        texts = [part.get("text") or "" for part in parts if isinstance(part, dict) and part.get("type") == "text"]
        head = _MESSAGE_HEAD.match(texts[0]) if texts else None
        if head is None:
            continue
        attributes = {key: html.unescape(value) for key, value in _ATTRIBUTE.findall(head.group(1))}
        images = sum(1 for part in parts if isinstance(part, dict) and part.get("type") == "image")
        content = (texts[0][head.end() :] + "".join(texts[1:])).strip() + " [圖片]" * images
        messages.append(
            {
                "msg_id": attributes.get("msg_id", ""),
                "user": attributes.get("group_card") or attributes.get("user", ""),
                "self": attributes.get("is_self_message") == "true",
                "text": " ".join(content.split())[:120],
            }
        )
    return messages


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
    """表情決策與送出；需要主插件提供 ctx、_load_settings、_append_system_text、_thread_routing、
    _client_manager、_message_handler、_native_emoji、_decision_stats、_emote_library。"""

    def _init_emote_state(self) -> None:
        # 有新的 Discord 訊息、還沒問過決策模型的聊天；一回合 planner 會請求好幾次，只有第一次問
        self._emote_unjudged: set[str] = set()
        # 每個聊天最近收到的訊息：msg_id（別名與原始 ID 都記）-> 原始 ID、有沒有在跟她說話、能不能按表情
        self._inbound: Dict[str, "OrderedDict[str, Dict[str, Any]]"] = {}
        # 每個聊天最新一則的頻道與發話人，送出時找頻道和訊息用
        self._latest_inbound: Dict[str, Dict[str, Any]] = {}
        # 上次問決策模型的時間（time.monotonic）；之後收到的才算「新訊息」
        self._judged_at: Dict[str, float] = {}
        self._pending_append: Dict[str, Dict[str, Any]] = {}
        self._append_tasks: Dict[str, asyncio.Task[None]] = {}
        # 只按表情／只發圖的背景任務；要留著引用，沒人引用的 Task 可能被回收
        self._emote_tasks: set[asyncio.Task[None]] = set()
        # 每個聊天最近挑過的表情 key，下次先排除
        self._recent_picks: Dict[str, "deque[str]"] = {}
        # MaiBot 設定的暱稱與別名，和它的「被提及必回覆」用同一份
        self._bot_names: List[str] = []
        self._bot_names_at = 0.0

    # ===== 收訊與 planner =====

    def _is_calling_her(self, message: Mapping[str, Any]) -> bool:
        """這則是不是在叫她；和 MaiBot 判斷「被提及」一樣：@ 她、回覆她的訊息、私訊，
        或提到 bot.nickname / bot.alias_names 裡的名字。睡覺被吵醒和表情決策共用這一個判斷。"""
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
        message_id = str(message.get("message_id") or "")
        self._emote_unjudged.add(session_id)
        self._latest_inbound[session_id] = {
            "group_id": str(group.get("group_id") or ""),
            "user_id": str(user.get("user_id") or ""),
        }
        if not message_id:
            return
        entry = {
            "id": message_id,
            "addressed": self._is_calling_her(message),
            # 只有真的 Discord 訊息（數字 ID）按得了表情；語音轉文字（voice-…）也會進 planner 上下文，但不是訊息
            "targetable": message_id.isdigit(),
            "at": time.monotonic(),
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
        name="discord_emote_decision",
        mode="blocking",
        order="late",
        timeout_ms=5000,
    )
    async def decide_emote_in_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """Discord 聊天裡拿掉 MaiBot 原生的 send_emoji；新訊息後第一次進 planner 時問決策模型，結果用系統提示告訴 planner。"""
        # 沒有這個工具：不是 Discord 聊天，或睡覺時段只剩 stop
        if "discord_emote" not in tool_names(kwargs):
            return {"action": "continue"}
        # discord_emote 也能發貼圖，兩個都留就是兩套「要不要發表情包」的決定；
        # 只拿掉 send_emoji，planner 手上還有 discord_emote，不會因為沒工具而把表情寫進正文
        kwargs["tool_definitions"] = [
            tool for tool in kwargs["tool_definitions"] if tool_names({"tool_definitions": [tool]}) != {"send_emoji"}
        ]
        result = {"action": "continue", "modified_kwargs": kwargs}
        settings = self._load_settings()
        decision = settings.decision
        session_id = str(kwargs.get("session_id") or "")
        if not (decision.enabled and decision.api_key.strip() and session_id):
            if random.random() < settings.chat.emote_chance:
                self._append_system_text(kwargs, EMOTE_NUDGE)
            return result
        # 同一回合後面的請求、沒有新訊息的回合（主動話題、提醒、逾時）不問
        if session_id not in self._emote_unjudged:
            return result
        self._emote_unjudged.discard(session_id)
        outcome = await self._decide_scene(decision, session_id, planner_messages(kwargs.get("items")))
        if outcome is None or outcome["scene"] == "text":
            return result
        if outcome["scene"] == "text_emote":
            self._append_system_text(kwargs, APPEND_NOTE)
            return result
        what = "表情" if outcome["scene"] == "quick_reaction" else "一張圖"
        note = QUICK_ADDRESSED_NOTE if outcome["addressed"] else QUICK_NOTE
        self._append_system_text(kwargs, note.format(what=what, user=outcome["target"]["user"]))
        return result

    async def _decide_scene(
        self, decision: Any, session_id: str, messages: Sequence[Mapping[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """問決策模型情境、表態對象與情緒，套上安全條件；只按表情／只發圖在背景送出，文字＋附圖記下等她回完。

        Returns:
            ``{"scene", "target", "addressed"}``；planner 上下文裡沒有可以表態的訊息或請求失敗時回傳 None。
        """
        now = datetime.now()
        remembered = self._inbound.get(session_id, {})
        window = list(messages)[-decision.context_messages :]
        # 能表態的：別人說的、換得回真的 Discord 訊息；取最後幾則
        targets = [
            message
            for message in window
            if not message["self"] and message["msg_id"] in remembered and remembered[message["msg_id"]]["targetable"]
        ][-decision.target_candidates :]
        if not targets:
            self.ctx.logger.info(f"表情決策 [{session_id}]：planner 上下文裡沒有可以表態的訊息，這輪 planner 自己決定")
            return None
        numbers = {id(message): str(index) for index, message in enumerate(targets, 1)}
        reactions = self._reaction_summaries([remembered[message["msg_id"]]["id"] for message in targets])
        lines = []
        for message in window:
            if message["self"]:
                lines.append(f"（你）：{message['text']}")
                continue
            number = numbers.get(id(message))
            extra = reactions.get(remembered[message["msg_id"]]["id"], "") if number else ""
            lines.append(f"{'#' + number + ' ' if number else ''}「{message['user']}」：{message['text']}{extra}")
        judged_at = self._judged_at.get(session_id, 0.0)
        self._judged_at[session_id] = time.monotonic()
        addressed = any(entry["addressed"] and entry["at"] > judged_at for entry in remembered.values())
        state = (
            f"你是「{self._bot_name()}」，在 Discord 群組聊天。"
            + ("上次之後有人 @ 你、回覆你或私訊你。" if addressed else "上次之後沒有人 @ 你。")
            + "\n\n最近的群聊（越下面越新，# 是可以表態的訊息）：\n"
            + "\n".join(lines)
        )
        questions = decision_questions(decision)
        # 只有一則可選時不問：vLLM-SR 的選擇題至少要兩個選項，一個選項會回 422（question_malformed），
        # 整個請求失敗、連情境也拿不到。插件只記得啟動後收到的訊息，剛重啟時常常只有一則
        if len(targets) > 1:
            questions["target"] = {
                "type": "choice",
                "instructions": decision.target_instructions,
                "criteria": {numbers[id(message)]: f"「{message['user']}」：{message['text']}" for message in targets},
            }
        started = time.monotonic()
        answers = await ask_decision(decision, state, questions, self.ctx.logger)
        latency_ms = round((time.monotonic() - started) * 1000)
        scene_answer = (answers or {}).get("scene") or {}
        requested = str(scene_answer.get("choice") or "")
        target_choice = str(((answers or {}).get("target") or {}).get("choice") or "")
        valid = target_choice.isdigit() and 1 <= int(target_choice) <= len(targets)
        index = int(target_choice) - 1 if valid else len(targets) - 1
        target = {
            **targets[index],
            "id": remembered[targets[index]["msg_id"]]["id"],
        }
        summary = f"「{target['user']}」：{target['text']}"
        if requested not in SCENES:
            self.ctx.logger.warning(
                f"表情決策 [{session_id}]：沒拿到有效的情境（可表態 {len(targets)} 則），這輪 planner 自己決定"
            )
            self._decision_stats.record_decision(now, session_id, summary, "failed", latency_ms)
            return None
        probabilities = scene_answer.get("probabilities") if isinstance(scene_answer.get("probabilities"), dict) else {}
        probability = float(probabilities.get(requested) or 0)
        emotion = str(((answers or {}).get("emotion") or {}).get("choice") or "")
        emotions = decision.emotion_options()
        emotion = emotion if emotion in emotions else next(iter(emotions), "開心")
        scene = self._guard_scene(decision, requested, probability, target, session_id)
        self.ctx.logger.info(
            f"表情決策 [{session_id}]：{SCENE_LABELS[scene]}，對象 #{index + 1}/{len(targets)} {summary[:40]}"
            f"（模型選「{SCENE_LABELS[requested]}」{probability:.2f}，情緒「{emotion}」，"
            f"門檻 {decision.emote_threshold:.2f}，{latency_ms} ms）"
        )
        self._decision_stats.record_decision(
            now, session_id, summary, scene, latency_ms, requested=requested, emotion=emotion, probability=probability
        )
        if scene in ("quick_reaction", "quick_sticker"):
            task = asyncio.create_task(self._quick_send(session_id, scene, emotion, target))
            self._emote_tasks.add(task)
            task.add_done_callback(self._emote_tasks.discard)
        elif scene == "text_emote":
            self._pending_append[session_id] = {"emotion": emotion, "expires": now + APPEND_TTL, "reply": ""}
        return {"scene": scene, "target": target, "addressed": addressed}

    def _reaction_summaries(self, message_ids: Sequence[str]) -> Dict[str, str]:
        """從 discord.py 的訊息快取讀別人已經按了什麼（不打 API），給決策模型當「大家都在笑哪則」的線索。"""
        client = getattr(self._client_manager, "client", None)
        wanted = set(message_ids)
        summaries: Dict[str, str] = {}
        for cached in getattr(client, "cached_messages", None) or []:
            if str(cached.id) not in wanted or not cached.reactions:
                continue
            shown = "、".join(f"{reaction.emoji}×{reaction.count}" for reaction in cached.reactions[:4])
            mine = "，你也按了" if any(reaction.me for reaction in cached.reactions) else ""
            summaries[str(cached.id)] = f"（已有人按 {shown}{mine}）"
        return summaries

    def _guard_scene(
        self,
        decision: Any,
        requested: str,
        probability: float,
        target: Mapping[str, Any],
        session_id: str,
    ) -> str:
        """模型選的情境要過門檻；只按表情／只發圖還要有地方按、有東西可發，否則降成文字＋附圖。

        有人在叫她時（例如「幫這則按個讚」）也照做，只是那輪 planner 照常可以說話，不會被限制成只能 stop。
        """
        if requested == "text" or probability < decision.emote_threshold:
            return "text"
        if requested.startswith("quick_"):
            guild = self._guild_of(self._latest_inbound.get(session_id, {}).get("group_id"))
            usable = guild is not None and (
                any(emoji.is_usable() for emoji in guild.emojis)
                if requested == "quick_reaction"
                else bool(self._sticker_items(guild) or self._emoji_items(guild))
            )
            if decision.quick_enabled and usable:
                return requested
            requested = "text_emote"
        return "text_emote" if decision.append_enabled else "text"

    # ===== 送出 =====

    async def _quick_send(self, session_id: str, scene: str, emotion: str, target: Mapping[str, Any]) -> None:
        """只按表情：按在表態對象上；只發圖：在聊天裡發一張。背景執行，planner 不用等。"""
        started = time.monotonic()
        query = f"{emotion}：{target.get('text') or ''}"
        kind = "reaction" if scene == "quick_reaction" else "sticker"
        latest = self._latest_inbound.get(session_id, {})
        picked, score, ok = "", 0.0, False
        try:
            guild = self._guild_of(latest.get("group_id"))
            if kind == "reaction":
                message = await self._thread_routing.find_message(
                    str(target.get("id") or ""), str(latest.get("group_id") or ""), str(latest.get("user_id") or "")
                )
                found = await self._pick_custom_emoji(guild, query, session_id) if message is not None else None
                if found:
                    emoji, score = found
                    await message.add_reaction(emoji)
                    picked, ok = str(emoji), True
            else:
                found_image = await self._pick_image(guild, query, session_id)
                if found_image:
                    picked, image_b64, score = found_image
                    ok = bool(await self.ctx.send.emoji(image_b64, session_id, sync_to_maisaka_history=True))
        except Exception as exc:  # 背景任務：沒接住只會變成沒人看的 Task 例外
            self.ctx.logger.warning(f"{SEND_KIND_LABELS[kind]}失敗：{exc!r}")
        self._record_send(session_id, kind, started, picked, score, ok)

    def _schedule_append(self, message: Mapping[str, Any]) -> None:
        """她的文字送出後（send_service.after_send）重設附圖計時；圖本身送出時不算。"""
        session_id = str(message.get("session_id") or "")
        pending = self._pending_append.get(session_id)
        if not pending:
            return
        if datetime.now() > pending["expires"]:
            self._pending_append.pop(session_id, None)
            return
        text = sent_text(message)
        if not text:
            return
        pending["reply"] += text
        running = self._append_tasks.get(session_id)
        if running is not None and not running.done():
            running.cancel()
        self._append_tasks[session_id] = asyncio.create_task(self._append_after_reply(session_id))

    async def _append_after_reply(self, session_id: str) -> None:
        await asyncio.sleep(APPEND_DELAY_SECONDS)
        pending = self._pending_append.pop(session_id, None)
        if not pending:
            return
        started = time.monotonic()
        latest = self._latest_inbound.get(session_id, {})
        guild = self._guild_of(latest.get("group_id"))
        picked, score, ok = "", 0.0, False
        try:
            found = await self._pick_image(guild, f"{pending['emotion']}：{pending['reply'][-200:]}", session_id)
            if found:
                picked, image_b64, score = found
                ok = bool(await self.ctx.send.emoji(image_b64, session_id, sync_to_maisaka_history=True))
        except Exception as exc:  # 背景任務：沒接住只會變成沒人看的 Task 例外
            self.ctx.logger.warning(f"回覆後附圖失敗：{exc!r}")
        self._record_send(session_id, "append", started, picked, score, ok)

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

    def _bot_name(self) -> str:
        user = getattr(getattr(self._client_manager, "client", None), "user", None)
        return str(getattr(user, "display_name", "") or "麥麥")

    # ===== planner 工具（決策模型沒開或失敗時才給 planner）=====

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
            sent = await self.ctx.send.emoji(image_b64, stream_id, sync_to_maisaka_history=True)
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
                sent = await self.ctx.send.text(picked, stream_id, sync_to_maisaka_history=True)
        self._record_send(stream_id or "", "tool", started, picked, score, bool(sent))
        if not sent:
            return {"success": False, "error": f"發送{label}失敗"}
        return {"success": True, "content": f"已用{label} {picked} 回應訊息 {msg_id}"}

    # ===== 「DC生活 → 決策模型」頁面 =====

    @API("webui_decision", description="讀取決策模型的設定與判斷統計", version="1")
    async def webui_decision(self) -> Dict[str, Any]:
        """決策模型頁：今天的判斷與送出、近 14 天走勢、最近的判斷與送出；沒啟用或沒填 Key 時用 notice 說明。"""
        config = self._load_settings().decision
        state = self._decision_stats.state
        days = state.get("days") or {}
        today = days.get(datetime.now().date().isoformat()) or {}
        if not config.enabled:
            notice = "決策模型未啟用，在下方「設定」開啟；沒開時表情由 planner 自己用 discord_emote 決定。"
        elif not config.api_key:
            notice = "還沒填 API Key（在插件設定「聊天與過濾 → 決策模型」），現在表情由 planner 自己決定。"
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
            "settings": self._setting_rows(
                "decision", [field for field in PAGE_FIELDS["decision"] if field not in PROMPT_FIELDS[2:]]
            ),
            "prompts": self._setting_rows("decision", PROMPT_FIELDS[2:-1]),
            "emotions": [{"name": name, "description": text} for name, text in config.emotion_options().items()],
            "threshold": f"{config.emote_threshold:.2f}",
            "last_test": (
                f"{short_time(last_test['time'])} {'✅' if last_test['ok'] else '❌'} {last_test['detail']}"
                if last_test
                else "還沒測試"
            ),
            "today_calls": today.get("calls", 0),
            "today_latency": f"{_avg(today.get('latency_ms', 0), judged(today))} ms" if judged(today) else "—",
            "today_failed": today.get("failed", 0),
            "today_scenes": "・".join(
                f"{SCENE_LABELS[scene]} {today.get(scene, 0)}"
                for scene in ("text", "text_emote", "quick_reaction", "quick_sticker")
            ),
            "today_sends": f"送出 {today.get('sends', 0)}・沒送成 {today.get('send_failed', 0)}",
            "today_send_latency": (
                f"{_avg(today.get('send_latency_ms', 0), today.get('sends', 0))} ms" if today.get("sends") else "—"
            ),
            "daily": [
                {
                    "date": day[5:],
                    "calls": counts.get("calls", 0),
                    "emotes": counts.get("text_emote", 0)
                    + counts.get("quick_reaction", 0)
                    + counts.get("quick_sticker", 0),
                    "sends": counts.get("sends", 0),
                    "latency": _avg(counts.get("latency_ms", 0), judged(counts)),
                }
                for day, counts in sorted(days.items())
            ],
            "decisions": [
                {
                    "time": short_time(item["time"]),
                    "chat": names.get(item["chat"], item["chat"]),
                    "scene": SCENE_LABELS.get(item["scene"], item["scene"]),
                    "requested": SCENE_LABELS.get(item["requested"], "—") if item["requested"] else "—",
                    "emotion": item["emotion"] or "—",
                    "probability": f"{item['probability']:.2f}" if item["scene"] != "failed" else "—",
                    "latency": f"{item['latency_ms']} ms",
                    "message": item["message"],
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

    @API("webui_decision_test", description="用一段範例聊天測試決策模型連不連得上", version="1")
    async def webui_decision_test(self) -> Dict[str, Any]:
        """沒啟用也能測，方便先確認 Key 和網址；結果記在頁面的「上次測試」。"""
        config = self._load_settings().decision
        if not config.api_key.strip():
            raise ValueError("還沒填 API Key")
        errors: List[str] = []
        started = time.monotonic()
        questions = decision_questions(config)
        questions["target"] = {
            "type": "choice",
            "instructions": config.target_instructions,
            "criteria": DECISION_TEST_TARGETS,
        }
        answers = await ask_decision(config, DECISION_TEST_STATE, questions, SimpleNamespace(warning=errors.append))
        latency_ms = round((time.monotonic() - started) * 1000)
        scene = str(((answers or {}).get("scene") or {}).get("choice") or "")
        if scene in SCENES:
            emotion = str(((answers or {}).get("emotion") or {}).get("choice") or "—")
            target = str(((answers or {}).get("target") or {}).get("choice") or "—")
            detail = f"{latency_ms} ms，範例判斷成「{SCENE_LABELS[scene]}」、對象 #{target}、情緒「{emotion}」"
            self._decision_stats.set_last_test(datetime.now(), True, detail)
            return {"ok": True, "detail": detail}
        detail = errors[-1] if errors else "回應裡沒有判斷結果，檢查模型名稱"
        self._decision_stats.set_last_test(datetime.now(), False, detail)
        raise ValueError(f"決策模型測試失敗：{detail}")

    @API("webui_decision_save", description="儲存決策模型的設定與提示詞", version="1")
    async def webui_decision_save(self, **fields: Optional[str]) -> Dict[str, Any]:
        """只改有填的項目；留空不改，免得把提示詞或情境說明清空（空的選項說明會讓決策模型回 422）。"""
        fields.pop("emotions", None)  # 情緒選項用下面的新增／刪除
        return {"saved": self._save_page_fields("decision", fields)}

    @API("webui_decision_save_emotion", description="新增或修改一個情緒選項", version="1")
    async def webui_decision_save_emotion(
        self, emotion_name: str = "", emotion_description: str = ""
    ) -> Dict[str, Any]:
        """標籤已經有就改說明，沒有就加在最後；說明沒填就用標籤本身。"""
        name = emotion_name.strip()
        if not name or "：" in name:
            raise ValueError("要填情緒標籤（不能含全形冒號「：」），例如：傻眼")
        options = self._load_settings().decision.emotion_options()
        options[name] = emotion_description.strip() or options.get(name) or name
        self._save_page_setting("decision", "emotions", [f"{key}：{text}" for key, text in options.items()])
        return {"saved": name}

    @API("webui_decision_remove_emotion", description="刪除一個情緒選項", version="1")
    async def webui_decision_remove_emotion(self, emotion_name: str = "") -> Dict[str, Any]:
        options = self._load_settings().decision.emotion_options()
        if emotion_name not in options:
            raise ValueError("找不到這個情緒，請重新整理頁面")
        if len(options) <= MIN_EMOTIONS:
            raise ValueError(f"至少要留 {MIN_EMOTIONS} 個情緒：選項少於兩個時決策模型會拒絕整個請求")
        del options[emotion_name]
        self._save_page_setting("decision", "emotions", [f"{key}：{text}" for key, text in options.items()])
        return {"removed": emotion_name}

    @API("webui_decision_reset_prompts", description="提示詞、情境說明、情緒選項與送出的則數恢復預設", version="1")
    async def webui_decision_reset_prompts(self) -> Dict[str, Any]:
        defaults = type(self._load_settings().decision)()
        for field in PROMPT_FIELDS:
            self._save_page_setting("decision", field, getattr(defaults, field))
        return {"reset": list(PROMPT_FIELDS)}

    @API("webui_decision_reset", description="清除決策模型的判斷統計", version="1")
    async def webui_decision_reset(self) -> Dict[str, Any]:
        self._decision_stats.reset()
        return {"cleared": True}
