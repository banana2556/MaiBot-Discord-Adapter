"""表情：planner 啟動時判斷這輪要用文字、文字＋附圖、只按表情還是只發圖參與群聊，並挑表情送出。

- 收到新的 Discord 訊息後第一次進 planner 時，拿 planner 這輪看到的同一份上下文（hook 的 items），
  取最後 TARGET_CANDIDATES 則別人的訊息當可以表態的對象，一次問決策模型：情境、最值得表態的是哪一則、什麼情緒。
  群聊裡真正好笑的那則常常已經被別人接了幾句，所以表態對象不一定是最新一則。
- 不動 planner 的工具：拿掉工具時 planner 會把回覆或表情直接寫在自己的正文裡，打亂 reply / replyer 的流程。
  決策結果只用一句系統提示告訴 planner 已經做了什麼、這輪該怎麼收尾。
- 只按表情／只發圖：替她做，並提醒 planner 這輪不用再說話（MaiBot 不讓插件跳過 planner）；
  多久表態一次由 planner 的啟動頻率決定。
- 文字＋附圖：等她的文字送出、停 APPEND_DELAY_SECONDS 秒沒再送，再附一張圖（回覆可能被拆成好幾則），
  並提醒 planner 不用自己發表情。
- 挑哪張用 MaiBot 表情庫描述的向量相似度（EmoteLibrary），不經過 LLM：排除這個聊天最近用過的，
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

from ..constants import DEFAULT_PLATFORM_NAME
from .decision import ask as ask_decision
from .life.common import short_time

# 決策模型沒開時，chat.emote_chance 命中就把這段接在 planner 系統提示詞後面
EMOTE_NUDGE = (
    "【表情】這一輪如果有值得表態的訊息（好笑、好消息、附和、驚訝、無語），"
    "可以用 discord_emote 在那則訊息按個 reaction；可以和 reply 同一輪一起呼叫，也可以只按表情不說話。沒有合適的就略過。"
)
# 決策結果用系統提示告訴 planner；不動它的工具
QUICK_NOTE = (
    "【表情】你已經用{what}回應了「{user}」那則訊息，也不要再發表情或貼圖。"
    "如果沒有人在問你或叫你，這輪就不用再說話，直接呼叫 stop() 結束。"
)
QUICK_ADDRESSED_NOTE = (
    "【表情】你已經用{what}回應了「{user}」那則訊息，不要再發表情或貼圖。有人在跟你說話，需要的話照常回覆。"
)
APPEND_NOTE = "【表情】這輪你回覆送出後，會自動附上一張符合情緒的表情或貼圖，你不用自己發表情或貼圖。"
SCENES = {
    "text": "用文字參與：在聊話題、有人在問問題或在跟你說話、要給意見或資訊",
    "text_emote": "用文字參與，說完再附一張表情或貼圖讓語氣更生動",
    "quick_reaction": "不用說話，在某則訊息上按個表情就好：附和、按讚、跟著大家笑、小小的驚訝",
    "quick_sticker": "不用說話，回一張表情或貼圖就好：現在的話題很好笑或情緒很強烈，用圖比打字自然",
}
SCENE_LABELS = {
    "text": "只要文字",
    "text_emote": "文字＋附圖",
    "quick_reaction": "只按表情",
    "quick_sticker": "只發圖",
    "failed": "請求失敗（planner 自己決定）",
}
EMOTIONS = {
    "好笑": "覺得好笑、笑翻",
    "開心": "開心、興奮",
    "讚同": "認同、按讚、說得對",
    "恭喜": "恭喜、替對方高興",
    "驚訝": "驚訝、不敢相信",
    "無語": "無言、傻眼、尷尬",
    "疑惑": "疑問、聽不懂",
    "心疼": "安慰、抱抱、心疼",
    "害羞": "害羞、不好意思",
    "生氣": "不滿、生氣、抗議",
    "難過": "難過、哭哭",
    "收到": "知道了、收到、OK",
}
SCENE_QUESTION = {
    "type": "choice",
    "instructions": "看最近的群聊，你這輪要怎麼參與最自然？快速表態可以針對前面任何一則 # 訊息，不一定是最新的。",
    "criteria": SCENES,
}
TARGET_INSTRUCTIONS = "如果要按表情或發圖表態，最值得回應的是哪一則 # 訊息？真正好笑或有梗的那則，常常已經被別人接了幾句。"
EMOTION_QUESTION = {
    "type": "choice",
    "instructions": "表態或回應時，你最想表達的情緒是哪一個？",
    "criteria": EMOTIONS,
}
SEND_KIND_LABELS = {"reaction": "按表情", "sticker": "只發圖", "append": "回覆後附圖", "tool": "planner 工具"}
# 頁面「測試連線」送的範例聊天
DECISION_TEST_TARGETS = {"1": "「小明」：我終於抽到限定角色了！！！", "2": "「阿華」：笑死 歐皇", "3": "「小美」：羨慕"}
DECISION_TEST_STATE = "你是「麥麥」，在 Discord 群組聊天。上次之後沒有人 @ 你。\n\n最近的群聊（越下面越新，# 是可以表態的訊息）：\n" + "\n".join(
    f"#{number} {text}" for number, text in DECISION_TEST_TARGETS.items()
)
# 給決策模型看的群聊：planner 上下文的最後幾則；其中最後幾則別人的訊息可以當表態對象
CONTEXT_MESSAGES = 15
TARGET_CANDIDATES = 8
# 發圖不能指定回覆哪一則，表態對象不是最後這幾則別人的訊息時改成按在那則上，免得群組看不懂
STICKER_RECENT = 2
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
        """這則是不是在叫她；和 MaiBot 判斷「被提及」一樣：@ 她（含回覆時的提醒）、私訊，
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
            # 只有真的 Discord 訊息（數字 ID）按得了表情；別人的 reaction 事件（reaction_…）、
            # 語音轉文字（voice-…）也會進 planner 上下文，但不是訊息
            "targetable": message_id.isdigit(),
            "at": time.monotonic(),
        }
        remembered = self._inbound.setdefault(session_id, OrderedDict())
        # 新版 MaiBot 顯示別名，舊版顯示原始 ID，兩個都記
        for key in {display_message_id(message_id), message_id}:
            remembered[key] = entry
            remembered.move_to_end(key)
        while len(remembered) > INBOUND_REMEMBERED * 2:
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
        # discord_emote 的貼圖也從 MaiBot 表情庫挑，兩個都留就是兩套「要不要發表情包」的決定；
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
        window = list(messages)[-CONTEXT_MESSAGES:]
        # 能表態的：別人說的、換得回真的 Discord 訊息；取最後幾則
        targets = [
            message
            for message in window
            if not message["self"] and message["msg_id"] in remembered and remembered[message["msg_id"]]["targetable"]
        ][-TARGET_CANDIDATES:]
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
        questions: Dict[str, Any] = {"scene": SCENE_QUESTION, "emotion": EMOTION_QUESTION}
        # 只有一則可選時不問：只有一個選項的選擇題會被決策模型當成格式錯誤（HTTP 422），連情境也拿不到。
        # 插件只記得啟動後收到的訊息，剛重啟時常常只有一則
        if len(targets) > 1:
            questions["target"] = {
                "type": "choice",
                "instructions": TARGET_INSTRUCTIONS,
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
            "recent": index >= len(targets) - STICKER_RECENT,
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
        emotion = emotion if emotion in EMOTIONS else "開心"
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
        if requested == "quick_sticker" and not target["recent"]:
            # 發圖不能指定回覆哪一則，對象是比較早的訊息時改成按在那則上
            requested = "quick_reaction"
        if requested.startswith("quick_"):
            guild = self._guild_of(self._latest_inbound.get(session_id, {}).get("group_id"))
            usable = guild is not None and (
                any(emoji.is_usable() for emoji in guild.emojis)
                if requested == "quick_reaction"
                else bool(guild.stickers or self._emote_library.entries)
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
                found_image = await self._pick_image(guild, query, emotion, session_id)
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
        raw = message.get("raw_message")
        segments = raw if isinstance(raw, list) else [raw] if isinstance(raw, Mapping) else []
        if isinstance(raw, Mapping) and raw.get("type") == "seglist":
            segments = raw.get("data") if isinstance(raw.get("data"), list) else []
        text = "".join(
            str(segment.get("data") or "")
            for segment in segments
            if isinstance(segment, Mapping) and segment.get("type") == "text"
        )
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
            found = await self._pick_image(
                guild, f"{pending['emotion']}：{pending['reply'][-200:]}", pending["emotion"], session_id
            )
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
        """伺服器能用的自訂表情：key -> (表情, 比對用文字)；收進表情庫的用描述，沒收過的只有名稱。"""
        return {
            f"emoji:{emoji.id}": (emoji, self._emote_library.text_for(f"emoji:{emoji.id}") or emoji.name)
            for emoji in (guild.emojis if guild is not None else [])
            if emoji.is_usable()
        }

    def _sticker_items(self, guild: Any) -> Dict[str, Tuple[Any, str]]:
        """伺服器能發的貼圖：key -> (貼圖, 比對用文字)；收進表情庫的用描述，沒收過的用名稱和說明。"""
        return {
            f"sticker:{sticker.id}": (
                sticker,
                self._emote_library.text_for(f"sticker:{sticker.id}") or f"{sticker.name}：{sticker.description or ''}",
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

    async def _pick_image(
        self, guild: Any, query: str, emotion: str, session_id: str
    ) -> Optional[Tuple[str, str, float]]:
        """從 MaiBot 表情庫和這個伺服器的貼圖挑一張，回傳 ``(名稱, 圖片 base64, 相似度)``。

        表情庫裡的 Discord 表情與貼圖送出時會自動換回原生的；沒收過的伺服器貼圖用名稱和說明比，選中時才下載。
        """
        await self._emote_library.refresh()
        library = {entry["key"]: entry for entry in self._emote_library.entries}
        stickers = self._sticker_items(guild)
        items = [(key, entry["text"]) for key, entry in library.items()]
        items += [(key, text) for key, (_, text) in stickers.items() if key not in library]
        if not items:
            return None
        picked = await self._rank_fresh(session_id, query, items)
        if picked is None:
            # embedding 失敗：改用 MaiBot 自己照情緒標籤挑的方式
            self.ctx.logger.warning("表情向量排序失敗，改用 MaiBot 表情庫照情緒挑")
            found = await self.ctx.emoji.get_by_description(emotion)
            found = found.get("emoji") if isinstance(found, dict) and "success" in found else found
            return (emotion, str(found["base64"]), 0.0) if isinstance(found, dict) and found.get("base64") else None
        key, score = picked
        if key in library:
            entry = library[key]
            sticker = stickers.get(key, (None, ""))[0]
            return (sticker.name if sticker is not None else entry["text"][:20]), entry["b64"], score
        sticker = stickers[key][0]
        image_b64 = await self._message_handler._fetch_emoji_bytes(str(sticker.url), size=256)
        if not image_b64:
            return None
        # 發送鏈路只收圖片：先登記這張圖，出站時 DiscordContentBuilder 會換回原生貼圖
        self._native_emoji.remember(image_b64, "sticker", sticker.id)
        return sticker.name, image_b64, score

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
            "用表情回應：按在訊息上（reaction／react）、單發一個伺服器表情，或發一張貼圖或表情包（sticker，"
            "從伺服器貼圖和 MaiBot 表情庫挑；要發表情包也用這個）。"
            "可以和 reply 在同一輪一起呼叫（邊回話邊按表情），也可以只按表情不說話。"
            "適合好笑、好消息、附和、按讚、表示收到、驚訝、無語、心疼的時候，像真人聊天一樣偶爾用。"
            "只傳目標訊息、想表達的情緒與形式，工具會照情緒從表情庫挑，不使用普通 Unicode emoji。"
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
                description="送出形式：reaction 按在該訊息上（最輕），emoji 單獨發一個表情，sticker 發一張貼圖或表情包。預設 reaction。",
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
            found_image = await self._pick_image(target.guild, query, str(feeling or "開心"), stream_id)
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
            notice = "決策模型未啟用，到插件設定「聊天與過濾 → 決策模型」開啟；沒開時表情由 planner 自己用 discord_emote 決定。"
        elif not config.api_key:
            notice = "還沒填 API Key，現在表情由 planner 自己決定。"
        else:
            notice = ""
        last_test = state.get("last_test")
        decisions = state.get("decisions") or []
        sends = state.get("sends") or []
        names = await self._webui_chat_names() if decisions or sends else {}

        def judged(counts: Mapping[str, int]) -> int:
            return counts.get("calls", 0) - counts.get("failed", 0)

        return {
            "notice": notice,
            "provider": config.base_url,
            "model": config.model,
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
        answers = await ask_decision(
            config,
            DECISION_TEST_STATE,
            {
                "scene": SCENE_QUESTION,
                "target": {"type": "choice", "instructions": TARGET_INSTRUCTIONS, "criteria": DECISION_TEST_TARGETS},
                "emotion": EMOTION_QUESTION,
            },
            SimpleNamespace(warning=errors.append),
        )
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

    @API("webui_decision_reset", description="清除決策模型的判斷統計", version="1")
    async def webui_decision_reset(self) -> Dict[str, Any]:
        self._decision_stats.reset()
        return {"cleared": True}
