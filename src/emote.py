"""表情：決定這則訊息要用文字、文字＋附圖、只按表情還是只發圖回應，並挑表情送出。

- 收到新的 Discord 訊息後第一次進 planner 時，問決策模型這則屬於哪種情境、該表達什麼情緒（一次請求兩題）。
  決策模型開著且回答成功時，表情一律由這裡處理，planner 拿不到 discord_emote 和 MaiBot 的 send_emoji。
- 只按表情／只發圖：替她做，這輪 planner 只留 stop（MaiBot 不讓插件跳過 planner）。
- 文字＋附圖：等她的文字送出、停 APPEND_DELAY_SECONDS 秒沒再送，再附一張圖（回覆可能被拆成好幾則）。
- 挑哪張用 MaiBot 表情庫描述的向量相似度（EmoteLibrary），從前 TOP_CANDIDATES 名隨機挑，不經過 LLM。
- 決策模型沒開或那輪請求失敗：planner 照舊有 discord_emote 工具，偶爾照 chat.emote_chance 提醒它。
"""

import asyncio
import random
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any, Dict, List, Mapping, Optional, Tuple

import discord
from maibot_sdk import API, HookHandler, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from ..constants import DEFAULT_PLATFORM_NAME
from .decision import ask as ask_decision
from .life.common import readable_chat, short_time

# 決策模型沒開時，chat.emote_chance 命中就把這段接在 planner 系統提示詞後面
EMOTE_NUDGE = (
    "【表情】這一輪如果有值得表態的訊息（好笑、好消息、附和、驚訝、無語），"
    "可以用 discord_emote 在那則訊息按個 reaction；可以和 reply 同一輪一起呼叫，也可以只按表情不說話。沒有合適的就略過。"
)
# 決策模型負責時從 planner 拿掉的工具：這兩個都是「要不要發表情或貼圖」，留著就變成兩套決定
EMOTE_TOOLS = {"discord_emote", "send_emoji"}
QUICK_NOTE = "【表情】你已經用{what}回應了最新那則訊息，這輪不用再說話，直接呼叫 stop()。"
SCENES = {
    "text": "只需要用文字回應：在聊話題、在問問題、要給意見或資訊，或在跟你說話",
    "text_emote": "用文字回應，說完再附一張表情或貼圖讓語氣更生動",
    "quick_reaction": "不用說話，在這則訊息上按個表情就好：附和、按讚、收到、小小的好笑或驚訝",
    "quick_sticker": "不用說話，回一張表情或貼圖就好：很好笑、情緒很強烈，用圖比打字自然",
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
    "instructions": "看聊天紀錄的最後一則訊息：你要怎麼回應它最自然？",
    "criteria": SCENES,
}
EMOTION_QUESTION = {
    "type": "choice",
    "instructions": "回應最後一則訊息時，你最想表達的情緒是哪一個？",
    "criteria": EMOTIONS,
}
SEND_KIND_LABELS = {"reaction": "按表情", "sticker": "只發圖", "append": "回覆後附圖", "tool": "planner 工具"}
# 頁面「測試連線」送的範例聊天
DECISION_TEST_STATE = "你是「麥麥」，在 Discord 上跟大家聊天。最新一則沒有 @ 你。\n\n[12:00] 小明说：我終於抽到限定角色了！！！"
# 問決策模型時帶最近多久、最多幾則聊天
EMOTE_CONTEXT_HOURS = 1
EMOTE_CONTEXT_MESSAGES = 8
# 決策結果在同一回合後面的 planner 請求還有效多久（期間 planner 拿不到 discord_emote）
SCENE_TTL = timedelta(minutes=2)
# 回覆後附圖：她的文字最後一則送出後等幾秒；決定附圖後多久內沒回覆就作廢
APPEND_DELAY_SECONDS = 1.5
APPEND_TTL = timedelta(minutes=2)
# 向量排序後從前幾名隨機挑，免得每次都發同一張
TOP_CANDIDATES = 3


def tool_names(kwargs: Mapping[str, Any]) -> set[str]:
    """planner Hook 載荷裡本輪候選工具的名稱（OpenAI function 結構）。"""
    tools = kwargs.get("tool_definitions")
    return {
        str((tool.get("function") or {}).get("name") or "")
        for tool in (tools if isinstance(tools, list) else [])
        if isinstance(tool, dict)
    }


def _keep_tools(kwargs: Dict[str, Any], keep: Any) -> None:
    kwargs["tool_definitions"] = [tool for tool in kwargs["tool_definitions"] if keep(tool_names({"tool_definitions": [tool]}))]


def _avg(total: int, count: int) -> int:
    return round(total / count) if count > 0 else 0


class EmoteMixin:
    """表情決策與送出；需要主插件提供 ctx、_load_settings、_append_system_text、_thread_routing、
    _client_manager、_message_handler、_native_emoji、_decision_stats、_emote_library。"""

    def _init_emote_state(self) -> None:
        # 有新的 Discord 訊息、還沒問過決策模型的聊天；一回合 planner 會請求好幾次，只有第一次問
        self._emote_unjudged: set[str] = set()
        # 每個聊天最新一則 Discord 訊息的摘要（目標訊息 ID、有沒有在跟她說話）
        self._latest_inbound: Dict[str, Dict[str, Any]] = {}
        # 決策成功的聊天 -> 結果有效到何時；期間 planner 拿不到 discord_emote
        self._emote_scene: Dict[str, datetime] = {}
        self._pending_append: Dict[str, Dict[str, Any]] = {}
        self._append_tasks: Dict[str, asyncio.Task[None]] = {}
        # 只按表情／只發圖的背景任務；要留著引用，沒人引用的 Task 可能被回收
        self._emote_tasks: set[asyncio.Task[None]] = set()

    # ===== 收訊與 planner =====

    def _remember_inbound(self, session_id: str, message: Mapping[str, Any]) -> None:
        """chat.receive 時記下最新一則，給決策和只按表情用。"""
        info = message.get("message_info") if isinstance(message.get("message_info"), Mapping) else {}
        extra = info.get("additional_config") if isinstance(info.get("additional_config"), Mapping) else {}
        group = info.get("group_info") if isinstance(info.get("group_info"), Mapping) else {}
        user = info.get("user_info") if isinstance(info.get("user_info"), Mapping) else {}
        self._emote_unjudged.add(session_id)
        self._latest_inbound[session_id] = {
            "message_id": str(message.get("message_id") or ""),
            "group_id": str(group.get("group_id") or ""),
            "user_id": str(user.get("user_id") or ""),
            # 私訊一定是在跟她說話
            "addressed": bool(extra.get("discord_mentions_bot")) or not group,
            "voice": bool(extra.get("discord_voice_output")),
            "text": str(message.get("processed_plain_text") or "")[:200],
        }

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_emote_decision",
        mode="blocking",
        order="late",
        timeout_ms=5000,
    )
    async def decide_emote_in_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """新訊息後第一次進 planner 時問決策模型；成功時表情由插件處理，planner 拿不到 discord_emote 和 send_emoji。"""
        # 沒有這個工具：不是 Discord 聊天，或睡覺時段只剩 stop
        if "discord_emote" not in tool_names(kwargs):
            return {"action": "continue"}
        settings = self._load_settings()
        decision = settings.decision
        session_id = str(kwargs.get("session_id") or "")
        if not (decision.enabled and decision.api_key.strip() and session_id):
            if random.random() < settings.chat.emote_chance:
                return self._append_system_text(kwargs, EMOTE_NUDGE)
            return {"action": "continue"}

        scene = None
        if session_id in self._emote_unjudged:
            self._emote_unjudged.discard(session_id)
            scene = await self._decide_scene(decision, session_id)
            if scene is None:
                # 請求失敗：這回合 planner 照舊有 discord_emote，自己決定
                self._emote_scene.pop(session_id, None)
                return {"action": "continue"}
            self._emote_scene[session_id] = datetime.now() + SCENE_TTL
        elif datetime.now() > self._emote_scene.get(session_id, datetime.min):
            # 沒有新訊息的回合（主動話題、提醒、逾時）或決策失敗：planner 照舊自己決定
            return {"action": "continue"}

        _keep_tools(kwargs, lambda names: not names & EMOTE_TOOLS)
        if scene in ("quick_reaction", "quick_sticker"):
            _keep_tools(kwargs, lambda names: names == {"stop"})
            self._append_system_text(kwargs, QUICK_NOTE.format(what="表情" if scene == "quick_reaction" else "一張圖"))
        return {"action": "continue", "modified_kwargs": kwargs}

    async def _decide_scene(self, decision: Any, session_id: str) -> Optional[str]:
        """問決策模型情境與情緒，套上安全條件；只按表情／只發圖在背景送出，文字＋附圖記下等她回完。"""
        now = datetime.now()
        chat = await readable_chat(
            self.ctx, session_id, now - timedelta(hours=EMOTE_CONTEXT_HOURS), now, EMOTE_CONTEXT_MESSAGES
        )
        if not chat:
            self.ctx.logger.info(f"表情決策 [{session_id}]：最近一小時讀不到聊天紀錄，這輪 planner 自己決定")
            return None
        latest = self._latest_inbound.get(session_id, {})
        addressed = "是在跟你說話（@ 你、回覆你或私訊）" if latest.get("addressed") else "沒有 @ 你"
        state = f"你是「{self._bot_name()}」，在 Discord 上跟大家聊天。最新一則{addressed}。\n\n聊天紀錄（最後一則是最新的）：\n{chat}"
        started = time.monotonic()
        answers = await ask_decision(
            decision, state, {"scene": SCENE_QUESTION, "emotion": EMOTION_QUESTION}, self.ctx.logger
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        message = chat.splitlines()[-1][:200]
        scene_answer = (answers or {}).get("scene") or {}
        requested = str(scene_answer.get("choice") or "")
        if requested not in SCENES:
            self.ctx.logger.warning(f"表情決策 [{session_id}]：沒拿到有效的情境，這輪 planner 自己決定")
            self._decision_stats.record_decision(now, session_id, message, "failed", latency_ms)
            return None
        probabilities = scene_answer.get("probabilities") if isinstance(scene_answer.get("probabilities"), dict) else {}
        probability = float(probabilities.get(requested) or 0)
        emotion = str(((answers or {}).get("emotion") or {}).get("choice") or "")
        emotion = emotion if emotion in EMOTIONS else "開心"
        scene = self._guard_scene(decision, requested, probability, latest)
        self.ctx.logger.info(
            f"表情決策 [{session_id}]：{SCENE_LABELS[scene]}（模型選「{SCENE_LABELS[requested]}」{probability:.2f}，"
            f"情緒「{emotion}」，門檻 {decision.emote_threshold:.2f}，{latency_ms} ms）"
        )
        self._decision_stats.record_decision(
            now, session_id, message, scene, latency_ms, requested=requested, emotion=emotion, probability=probability
        )
        if scene in ("quick_reaction", "quick_sticker"):
            task = asyncio.create_task(self._quick_send(session_id, scene, emotion, latest))
            self._emote_tasks.add(task)
            task.add_done_callback(self._emote_tasks.discard)
        elif scene == "text_emote":
            self._pending_append[session_id] = {"emotion": emotion, "expires": now + APPEND_TTL, "reply": ""}
        return scene

    def _guard_scene(self, decision: Any, requested: str, probability: float, latest: Mapping[str, Any]) -> str:
        """模型選的情境要過門檻；只按表情／只發圖還要不是在跟她說話、有地方按、有東西可發，否則降成文字＋附圖。"""
        if requested == "text" or probability < decision.emote_threshold:
            return "text"
        if requested.startswith("quick_"):
            guild = self._guild_of(latest.get("group_id"))
            usable = guild is not None and (
                any(emoji.is_usable() for emoji in guild.emojis)
                if requested == "quick_reaction"
                else bool(guild.stickers or self._emote_library.entries)
            )
            if (
                decision.quick_enabled
                and usable
                and latest.get("message_id")
                and not latest.get("addressed")
                and not latest.get("voice")
            ):
                return requested
            requested = "text_emote"
        return "text_emote" if decision.append_enabled else "text"

    # ===== 送出 =====

    async def _quick_send(self, session_id: str, scene: str, emotion: str, latest: Mapping[str, Any]) -> None:
        """只按表情：按在最新那則上；只發圖：在聊天裡發一張。背景執行，planner 不用等。"""
        started = time.monotonic()
        query = f"{emotion}：{latest.get('text') or ''}"
        kind = "reaction" if scene == "quick_reaction" else "sticker"
        picked, score, ok = "", 0.0, False
        try:
            guild = self._guild_of(latest.get("group_id"))
            if kind == "reaction":
                target = await self._thread_routing.find_message(
                    str(latest.get("message_id") or ""), str(latest.get("group_id") or ""), str(latest.get("user_id") or "")
                )
                found = await self._pick_custom_emoji(guild, query) if target is not None else None
                if found:
                    emoji, score = found
                    await target.add_reaction(emoji)
                    picked, ok = str(emoji), True
            else:
                found_image = await self._pick_image(guild, query, emotion)
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
            found = await self._pick_image(guild, f"{pending['emotion']}：{pending['reply'][-200:]}", pending["emotion"])
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

    async def _pick_custom_emoji(self, guild: Any, query: str) -> Optional[Tuple[Any, float]]:
        """從伺服器能用的自訂表情挑一個：有收進表情庫的用描述比，沒收過的只能用名稱比。"""
        usable = [emoji for emoji in (guild.emojis if guild is not None else []) if emoji.is_usable()]
        if not usable:
            return None
        await self._emote_library.refresh()
        items = [(f"emoji:{emoji.id}", self._emote_library.text_for(f"emoji:{emoji.id}") or emoji.name) for emoji in usable]
        ranked = await self._emote_library.rank(query, items, top=TOP_CANDIDATES)
        if not ranked:
            self.ctx.logger.warning("表情向量排序失敗，改成隨機挑一個伺服器表情")
            return random.choice(usable), 0.0
        key, score = random.choice(ranked)
        by_key = {f"emoji:{emoji.id}": emoji for emoji in usable}
        return by_key[key], score

    async def _pick_image(self, guild: Any, query: str, emotion: str) -> Optional[Tuple[str, str, float]]:
        """從 MaiBot 表情庫和這個伺服器的貼圖挑一張，回傳 ``(名稱, 圖片 base64, 相似度)``。

        表情庫裡的 Discord 表情與貼圖送出時會自動換回原生的；沒收過的伺服器貼圖用名稱和說明比，選中時才下載。
        """
        await self._emote_library.refresh()
        library = {entry["key"]: entry for entry in self._emote_library.entries}
        stickers = {
            f"sticker:{sticker.id}": sticker
            for sticker in (guild.stickers if guild is not None else [])
            if sticker.available and sticker.format != discord.StickerFormatType.lottie
        }
        items = [(key, entry["text"]) for key, entry in library.items()]
        items += [
            (key, f"{sticker.name}：{sticker.description or ''}") for key, sticker in stickers.items() if key not in library
        ]
        if not items:
            return None
        ranked = await self._emote_library.rank(query, items, top=TOP_CANDIDATES)
        if not ranked:
            # embedding 失敗：改用 MaiBot 自己照情緒標籤挑的方式
            self.ctx.logger.warning("表情向量排序失敗，改用 MaiBot 表情庫照情緒挑")
            found = await self.ctx.emoji.get_by_description(emotion)
            found = found.get("emoji") if isinstance(found, dict) and "success" in found else found
            return (emotion, str(found["base64"]), 0.0) if isinstance(found, dict) and found.get("base64") else None
        key, score = random.choice(ranked)
        if key in library:
            entry = library[key]
            sticker = stickers.get(key)
            return (sticker.name if sticker is not None else entry["text"][:20]), entry["b64"], score
        sticker = stickers[key]
        image_b64 = await self._message_handler._fetch_emoji_bytes(str(sticker.url), size=256)
        if not image_b64:
            return None
        # 發送鏈路只收圖片：先登記這張圖，出站時 DiscordContentBuilder 會換回原生貼圖
        self._native_emoji.remember(image_b64, "sticker", sticker.id)
        return sticker.name, image_b64, score

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
            "用 Discord 伺服器的原生表情回應：按在訊息上（reaction／react）、單發一個伺服器表情，或發一張伺服器貼圖（sticker）。"
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
            found_image = await self._pick_image(target.guild, query, str(feeling or "開心"))
            if not found_image:
                return {"success": False, "error": f"沒有可用的{label}，沒有送出"}
            picked, image_b64, score = found_image
            sent = await self.ctx.send.emoji(image_b64, stream_id, sync_to_maisaka_history=True)
        else:
            found = await self._pick_custom_emoji(target.guild, query)
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
            {"scene": SCENE_QUESTION, "emotion": EMOTION_QUESTION},
            SimpleNamespace(warning=errors.append),
        )
        latency_ms = round((time.monotonic() - started) * 1000)
        scene = str(((answers or {}).get("scene") or {}).get("choice") or "")
        if scene in SCENES:
            emotion = str(((answers or {}).get("emotion") or {}).get("choice") or "—")
            detail = f"{latency_ms} ms，範例判斷成「{SCENE_LABELS[scene]}」、情緒「{emotion}」"
            self._decision_stats.set_last_test(datetime.now(), True, detail)
            return {"ok": True, "detail": detail}
        detail = errors[-1] if errors else "回應裡沒有判斷結果，檢查模型名稱"
        self._decision_stats.set_last_test(datetime.now(), False, detail)
        raise ValueError(f"決策模型測試失敗：{detail}")

    @API("webui_decision_reset", description="清除決策模型的判斷統計", version="1")
    async def webui_decision_reset(self) -> Dict[str, Any]:
        self._decision_stats.reset()
        return {"cleared": True}
