import asyncio
import base64
from datetime import datetime, timedelta
from functools import partial
import json
from pathlib import Path
import random
import time
import tomllib
from typing import Any, Awaitable, Callable, ClassVar, Dict, List, Mapping, Optional, Sequence, Union, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import discord
from maim_message import BaseMessageInfo, MessageBase, Seg

from maibot_sdk import API, HookHandler, MaiBotPlugin, MessageGateway, PluginConfigBase, Tool
from pydantic import ValidationError
from maibot_sdk.types import ToolParameterInfo, ToolParamType

from maim_message import FormatInfo, GroupInfo, UserInfo

from .config import PAGE_MANAGED_FIELDS, WEBUI_CONFIG_TABS, DiscordPluginSettings, is_section_visible
from .constants import DEFAULT_PLATFORM_NAME, DISCORD_GATEWAY_NAME
from .filters import DiscordChatFilter
from .runtime_state import DiscordRuntimeStateManager
from .src.emoji_grid import build_numbered_grid, grid_shape
from .src.life.common import list_group_streams, load_state, readable_chat, save_state
from .src.life.cron import describe, parse_cron
from .src.life.daily_life import DailyLife
from .src.life.reminders import ReminderMixin
from .src.life.topics import TopicScout
from .src.native_emoji import NativeEmojiRegistry
from .src.recv_handler.discord_client import DiscordClientManager
from .src.recv_handler.message_handler import DiscordMessageHandler, quote_name
from .src.send_handler.message_send_handler import DiscordContentBuilder
from .src.send_handler.thread_send_handler import ThreadRoutingManager
from .src.voice.tts.gptsovits_tts import GPTSoVITSTTSProvider
from .src.voice.voice_manager import VoiceManager

DAILY_POLL_SECONDS = 10
# 调整生活时程时最多带这么多字的聊天记录，取最后面的
LIFE_CHAT_CHAR_LIMIT = 8000
# chat.emote_chance 命中时加进 planner 系统提示词的提醒
EMOTE_NUDGE = (
    "【表情】這一輪如果有值得表態的訊息（好笑、好消息、附和、驚訝、無語），"
    "可以用 discord_emote 在那則訊息按個 reaction；可以和 reply 同一輪一起呼叫，也可以只按表情不說話。沒有合適的就略過。"
)
# 睡觉时段这一轮规划只留这些工具，并接上这段提醒
SLEEP_ALLOWED_TOOLS = {"stop"}
SLEEP_NOTE = "【睡眠中】你現在正在睡覺，看不到也不會回這些訊息。這一輪不要回覆、不要發任何東西，直接呼叫 stop()。"
# 提醒到点或从页面叫醒后，这么多分钟内当作醒着
WAKE_MINUTES = 30
ONLINE_STATUS_LABELS = {"online": "🟢 在線", "idle": "🌙 閒置", "dnd": "⛔ 請勿打擾"}
TOPIC_SOURCE_LABELS = {"news": "新聞", "video": "影片", "mixed": "新聞或影片"}
VOICE_MODE_LABELS = {"fixed": "固定頻道常駐", "auto": "有人進來才加入"}
# 「DC生活 → 語音」页面能改的语音字段与显示名称
VOICE_FIELD_LABELS = {
    "voice_mode": "頻道模式",
    "fixed_channel_id": "固定頻道",
    "auto_channel_list": "自動加入的頻道",
    "idle_timeout_sec": "空閒退出（秒）",
    "enable_vad": "音量偵測說話（VAD）",
    "vad_threshold_db": "VAD 門檻（dB）",
    "vad_deactivation_delay_ms": "VAD 停止延遲（毫秒）",
    "send_text_in_voice": "語音頻道同步發文字",
}
ON_OFF = {True: "開", False: "關"}


def _tool_names(kwargs: Dict[str, Any]) -> set[str]:
    """planner Hook 载荷里本轮候选工具的名称（OpenAI function 结构）。"""
    tools = kwargs.get("tool_definitions")
    return {
        str((tool.get("function") or {}).get("name") or "")
        for tool in (tools if isinstance(tools, list) else [])
        if isinstance(tool, dict)
    }


def _short_time(iso: Any) -> str:
    """状态文件里的 ISO 时间 -> WebUI 显示的「MM-DD HH:MM」；没有或格式不对时显示「—」。"""
    try:
        return datetime.fromisoformat(str(iso)).strftime("%m-%d %H:%M")
    except ValueError:
        return "—"


class DiscordAdapterPlugin(ReminderMixin, MaiBotPlugin):
    """Discord 消息网关插件。

    负责注册双工消息网关、管理 discord.py 客户端生命周期，并将 Host 出站消息路由到
    Discord 频道或语音 TTS；同时处理 Reaction 命令与超长消息拆分发送。
    另含日常功能：定时提醒（``ReminderMixin``）、日常生活时程与主动话题，共用一个后台轮询。
    """

    config_model: ClassVar[type[PluginConfigBase] | None] = DiscordPluginSettings
    _GSV_SCHEMA_AUTO_CHOICE: ClassVar[str] = "[auto]"
    _GSV_SCHEMA_CACHE_TTL_SECONDS: ClassVar[float] = 30.0
    _GSV_SCHEMA_FAILURE_CACHE_TTL_SECONDS: ClassVar[float] = 10.0
    _GSV_SCHEMA_FETCH_TIMEOUT_SECONDS: ClassVar[float] = 0.5

    def __init__(self) -> None:
        """初始化插件实例，各子组件在连接建立前保持为未绑定状态。"""
        super().__init__()
        self._client_manager: Optional[DiscordClientManager] = None
        self._runtime_state: Optional[DiscordRuntimeStateManager] = None
        self._thread_routing: Optional[ThreadRoutingManager] = None
        self._message_handler: Optional[DiscordMessageHandler] = None
        self._content_builder: Optional[DiscordContentBuilder] = None
        self._native_emoji: Optional[NativeEmojiRegistry] = None
        self._chat_filter: Optional[DiscordChatFilter] = None
        self._voice_manager: Optional[VoiceManager] = None
        self._client_task: Optional[asyncio.Task[None]] = None
        # MaiBot session -> Discord 频道；typing 目标回复后会被清掉，提示词注入要一直认得会话
        self._discord_session_channels: Dict[str, int] = {}
        self._gsv_template_catalog_cache: Dict[
            str,
            tuple[float, Optional[Dict[str, Dict[str, List[str]]]], str],
        ] = {}
        self._reminders: List[Dict[str, Any]] = []
        self._daily_life: Optional[DailyLife] = None
        self._topic_scout: Optional[TopicScout] = None
        self._daily_task: Optional[asyncio.Task[None]] = None
        self._daily_jobs: Dict[str, asyncio.Task[None]] = {}
        # 「日常」页面设置的字段 {配置节: {字段: 值}}，见 PAGE_MANAGED_FIELDS
        self._page_settings: Dict[str, Dict[str, Any]] = {}
        self._page_settings_path: Optional[Path] = None
        # 睡觉时被调成 0 回复频率的会话，醒来时还原
        self._silenced_sessions: set[str] = set()
        self._awake_until: Optional[datetime] = None

    async def on_load(self) -> None:
        """插件加载时启动日常功能的后台轮询，并根据当前配置启动或保持 Discord 连接。"""
        await self._start_daily_features()
        await self._restart_if_needed()

    async def on_unload(self) -> None:
        """插件卸载时停止日常轮询、客户端任务并释放相关资源。"""
        for task in (self._daily_task, *self._daily_jobs.values()):
            if task is not None:
                task.cancel()
        # 不还原的话，被静默的聊天会一直不回复到 MaiBot 重启
        await self._restore_silenced()
        await self._stop_connection()

    # ===== 日常功能：定时提醒、日常生活、主动话题 =====

    async def _start_daily_features(self) -> None:
        """读回提醒和日常状态，按配置开关提醒工具，开始统一的后台轮询；与 Discord 是否连线无关。"""
        data_dir = self.ctx.paths.data_dir
        self._load_page_settings(data_dir / "daily_settings.json")
        self._load_reminders()
        await self._apply_reminder_switches()
        self._daily_life = DailyLife(
            self.ctx, data_dir / "life_schedule.json", self.set_presence, self._recent_discord_chats
        )
        self._topic_scout = TopicScout(self.ctx, data_dir / "topics.json", self._resolve_discord_stream)
        self._daily_task = asyncio.create_task(self._run_daily_loop())

    async def _run_daily_loop(self) -> None:
        # ponytail: 轮询到期事项，最多晚 DAILY_POLL_SECONDS 秒；提醒在本轮里直接触发，
        # 要调 LLM 的生活时程和主动话题放到独立任务，慢的时候也不会拖住提醒
        while True:
            await asyncio.sleep(DAILY_POLL_SECONDS)
            settings = self._load_settings()
            now = datetime.now()
            if settings.reminder.enabled:
                await self.fire_due_reminders(now)
            if settings.life.enabled and self._daily_life is not None:
                self._spawn_daily_job("life", partial(self._daily_life.tick, now, settings.life))
            sleeping = self._sleeping(now)
            if not sleeping and self._silenced_sessions:
                await self._restore_silenced()
            # ponytail: 睡觉时跳过整轮，错过的话题醒来后马上补发；要严格按时就改成睡觉时只推进 next_due
            if settings.topics.enabled and self._topic_scout is not None and not sleeping:
                self._spawn_daily_job("topics", partial(self._topic_scout.tick, now, settings.topics))

    def _spawn_daily_job(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        """上一次同名任务还没跑完时跳过这一轮。"""
        running = self._daily_jobs.get(name)
        if running is not None and not running.done():
            return

        async def run() -> None:
            try:
                await factory()
            except Exception:
                self.ctx.logger.exception(f"日常功能 {name} 执行失败")

        self._daily_jobs[name] = asyncio.create_task(run())

    async def _recent_discord_chats(self, since: datetime, until: datetime) -> str:
        """各 Discord 群聊在这段时间内的可读聊天记录，供日常生活时程调整参考。"""
        platform = self._load_settings().platform.platform_name
        parts: List[str] = []
        for stream in await list_group_streams(self.ctx, platform):
            text = await readable_chat(self.ctx, stream["stream_id"], since, until, 40)
            if text:
                parts.append(f"【{stream.get('group_name') or stream['stream_id']}】\n{text}")
        return "\n\n".join(parts)[-LIFE_CHAT_CHAR_LIMIT:]

    async def _resolve_discord_stream(self, channel_id: str) -> Optional[str]:
        """Discord 频道 ID -> MaiBot 聊天流 ID；频道在 MaiBot 里还没有会话时返回 None。"""
        platform = self._load_settings().platform.platform_name
        stream = await self.ctx.chat.get_stream_by_group_id(channel_id, platform=platform)
        if isinstance(stream, dict) and stream.get("stream_id"):
            return str(stream["stream_id"])
        return None

    @API("webui_life", description="讀取日常生活的目前狀態與今天的時程", version="1")
    async def webui_life(self) -> Dict[str, Any]:
        """WebUI 日常生活页：当前状态、最近一次调整和 24 个时段；没启用或还没生成时用 notice 说明。"""
        config = self._load_settings().life
        now = datetime.now()
        state = self._daily_life.state if self._daily_life is not None else {}
        hours = state.get("hours") if isinstance(state.get("hours"), list) and len(state["hours"]) == 24 else []
        entry = hours[now.hour] if hours else None
        if not config.enabled:
            notice = "日常生活未啟用，到插件設定的「日常」分頁開啟。"
        elif entry is None:
            notice = f"還沒有時程，啟用後過了每日生成時間（{config.generate_time}）就會自動生成。"
        elif state.get("date") != now.date().isoformat():
            notice = f"今天的時程還沒生成，暫時沿用 {state.get('date')} 的安排。"
        else:
            notice = ""
        sleeping = self._sleeping(now)
        if not (entry and entry.get("asleep")):
            sleep_state = "醒著"
        elif sleeping:
            sleep_state = "💤 睡覺中，不回覆"
        elif not config.sleep_silence:
            sleep_state = "💤 睡覺中（設定為照常回覆）"
        else:
            sleep_state = f"被叫醒了，{self._awake_until:%H:%M} 前照常回覆" if self._awake_until else "醒著"
        return {
            "now": describe(now),
            "notice": notice,
            "sleeping": sleeping,
            "sleep_state": sleep_state,
            "life_setting": config.life_setting or "（未填寫，按人設推想）",
            "current": (f"{entry['activity']}（{entry['mood']}）" if entry["mood"] else entry["activity"]) if entry else "—",
            "presence": f"{entry['status_text']}｜{ONLINE_STATUS_LABELS.get(entry['online_status'], entry['online_status'])}"
            if entry
            else "—",
            "date": state.get("date") or "—",
            "adjusted_at": _short_time(state.get("adjusted_at")),
            "note": state.get("note") or "—",
            "hours": [
                {
                    "time": f"{'▶ ' if hour == now.hour else ''}{hour:02d}:00",
                    "activity": item["activity"],
                    "mood": item["mood"] or "—",
                    "status_text": item["status_text"],
                    "online_status": ONLINE_STATUS_LABELS.get(item["online_status"], item["online_status"]),
                    "asleep": "💤" if item.get("asleep") else "",
                }
                for hour, item in enumerate(hours)
            ],
        }

    def _ensure_daily_job_idle(self, name: str) -> None:
        """页面按钮要跑后台任务前确认同名任务没在跑，否则直接告诉使用者。"""
        running = self._daily_jobs.get(name)
        if running is not None and not running.done():
            raise ValueError("上一次還在跑，稍後再試")

    @API("webui_life_save_setting", description="儲存日常生活的生活設定", version="1")
    async def webui_life_save_setting(self, life_setting: str = "") -> Dict[str, Any]:
        """整段取代生活设定；下次生成时程时生效。"""
        self._save_page_setting("life", "life_setting", life_setting.strip())
        return {"saved": True}

    @API("webui_life_regenerate", description="立即重新生成今天的日常生活時程", version="1")
    async def webui_life_regenerate(self) -> Dict[str, Any]:
        """在后台重新生成，页面稍后重新整理就能看到新时程。"""
        config = self._load_settings().life
        if not config.enabled or self._daily_life is None:
            raise ValueError("日常生活未啟用")
        self._ensure_daily_job_idle("life")
        self._spawn_daily_job("life", partial(self._daily_life.generate, datetime.now(), config))
        return {"started": True}

    @API("webui_life_wake", description="把睡覺中的麥麥叫醒一陣子", version="1")
    async def webui_life_wake(self) -> Dict[str, Any]:
        if not self._sleeping(datetime.now()):
            raise ValueError("她現在沒在睡覺")
        await self._wake_up()
        return {"awake_until": f"{self._awake_until:%H:%M}"}

    @API("webui_topics_add_channel", description="加入主動話題的頻道", version="1")
    async def webui_topics_add_channel(self, new_channel_id: str = "") -> Dict[str, Any]:
        channel_id = new_channel_id.strip()
        if not channel_id.isdigit():
            raise ValueError("頻道 ID 要是一串數字（在 Discord 對頻道按右鍵 → 複製頻道 ID）")
        channels = self._load_settings().topics.channel_list
        if channel_id in channels:
            raise ValueError("這個頻道已經在列表裡")
        self._save_page_setting("topics", "channel_list", [*channels, channel_id])
        return {"saved": True}

    @API("webui_topics_remove_channel", description="移除主動話題的頻道", version="1")
    async def webui_topics_remove_channel(self, channel_id: str = "") -> Dict[str, Any]:
        channels = self._load_settings().topics.channel_list
        self._save_page_setting("topics", "channel_list", [item for item in channels if item != channel_id])
        return {"saved": True}

    @API("webui_topics_save_cron", description="儲存主動話題的排程", version="1")
    async def webui_topics_save_cron(self, cron: str = "") -> Dict[str, Any]:
        cron = " ".join(cron.split())
        parse_cron(cron)  # 不合法时抛 ValueError，页面直接显示原因
        self._save_page_setting("topics", "cron", cron)
        return {"saved": True}

    @API("webui_topics_run_now", description="立刻為每個頻道開一次主動話題", version="1")
    async def webui_topics_run_now(self) -> Dict[str, Any]:
        """不看排程马上跑一次，不影响下次排程时间。"""
        config = self._load_settings().topics
        if self._topic_scout is None or not config.channel_list:
            raise ValueError("還沒設定頻道")
        if not (config.news_enabled or config.video_enabled):
            raise ValueError("新聞和影片都關了，到插件設定的「日常」分頁開啟")
        self._ensure_daily_job_idle("topics")
        self._spawn_daily_job("topics", partial(self._topic_scout.run_all, datetime.now(), config))
        return {"started": True}

    # ===== WebUI：语音 =====

    def _describe_channel(self, channel_id: Any) -> str:
        """Discord 频道 ID -> 「频道名 @ 服务器名」；没连上 Discord 或找不到时带上 ID 说明。"""
        client = self._client_manager.client if self._client_manager is not None else None
        channel = client.get_channel(int(channel_id)) if client is not None and str(channel_id).isdigit() else None
        if channel is None:
            return f"{channel_id}（找不到頻道或還沒連上 Discord）"
        guild = getattr(channel, "guild", None)
        return f"{channel.name} @ {guild.name}" if guild is not None else str(channel.name)

    @API("webui_voice", description="讀取語音功能的連線狀態與頻道設定", version="1")
    async def webui_voice(self) -> Dict[str, Any]:
        """WebUI 语音页：现在在哪个频道、页面管的各项设置目前的值、自动加入的频道。"""
        settings = self._load_settings()
        voice = settings.voice
        channel_id = self._voice_manager.get_connected_channel_id() if self._voice_manager is not None else None
        if not voice.enabled:
            notice = "語音功能未啟用，到插件設定的「語音」分頁開啟。"
        elif not settings.connection.intent_voice_states:
            notice = "插件設定裡的「語音狀態權限」沒開，加入語音頻道、自動進出和語音辨識都不會動。"
        else:
            notice = ""
        current = {
            "voice_mode": VOICE_MODE_LABELS[voice.voice_mode],
            "fixed_channel_id": self._describe_channel(voice.fixed_channel_id) if voice.fixed_channel_id else "—",
            "idle_timeout_sec": voice.idle_timeout_sec,
            "enable_vad": ON_OFF[voice.enable_vad],
            "vad_threshold_db": voice.vad_threshold_db,
            "vad_deactivation_delay_ms": voice.vad_deactivation_delay_ms,
            "send_text_in_voice": ON_OFF[voice.send_text_in_voice],
        }
        return {
            "notice": notice,
            "connected": channel_id is not None,
            "current_channel": self._describe_channel(channel_id) if channel_id is not None else "未連線",
            "mode": VOICE_MODE_LABELS[voice.voice_mode],
            "tts": voice.tts_provider,
            "stt": voice.stt_provider,
            "settings": [{"item": VOICE_FIELD_LABELS[field], "value": str(value)} for field, value in current.items()],
            "auto_channels": [
                {"channel_id": item, "channel": self._describe_channel(item)} for item in voice.auto_channel_list
            ],
        }

    async def _save_voice_settings(self, updates: Dict[str, Any]) -> None:
        """先把所有改动一起交给配置模型校验和转换，全部通过才存，然后重连让语音管理器套用。"""
        voice = self._load_settings().voice
        try:
            checked = type(voice).model_validate({**voice.model_dump(), **updates})
        except ValidationError as exc:
            fields = "、".join(VOICE_FIELD_LABELS.get(str(error["loc"][0]), str(error["loc"][0])) for error in exc.errors())
            raise ValueError(f"格式不對：{fields}") from exc
        for field in updates:
            self._save_page_setting("voice", field, getattr(checked, field))
        await self._restart_if_needed()

    @API("webui_voice_save", description="儲存語音的頻道模式、空閒退出與 VAD 設定", version="1")
    async def webui_voice_save(
        self,
        voice_mode: Optional[str] = None,
        fixed_channel_id: Optional[str] = None,
        idle_timeout_sec: Optional[str] = None,
        enable_vad: Optional[str] = None,
        vad_threshold_db: Optional[str] = None,
        vad_deactivation_delay_ms: Optional[str] = None,
        send_text_in_voice: Optional[str] = None,
    ) -> Dict[str, Any]:
        """只改有填的项目；数字与开关由配置模型转换。"""
        submitted = {
            "voice_mode": voice_mode,
            "fixed_channel_id": fixed_channel_id,
            "idle_timeout_sec": idle_timeout_sec,
            "enable_vad": enable_vad,
            "vad_threshold_db": vad_threshold_db,
            "vad_deactivation_delay_ms": vad_deactivation_delay_ms,
            "send_text_in_voice": send_text_in_voice,
        }
        updates = {field: value.strip() for field, value in submitted.items() if value is not None}
        if not updates:
            raise ValueError("沒有填任何要改的項目")
        if updates.get("fixed_channel_id") and not updates["fixed_channel_id"].isdigit():
            raise ValueError("固定頻道要填語音頻道 ID（一串數字），清空則不指定")
        await self._save_voice_settings(updates)
        return {"saved": sorted(updates)}

    @API("webui_voice_add_channel", description="加入自動模式的語音頻道", version="1")
    async def webui_voice_add_channel(self, new_channel_id: str = "") -> Dict[str, Any]:
        channel_id = new_channel_id.strip()
        if not channel_id.isdigit():
            raise ValueError("頻道 ID 要是一串數字（在 Discord 對語音頻道按右鍵 → 複製頻道 ID）")
        channels = self._load_settings().voice.auto_channel_list
        if channel_id in channels:
            raise ValueError("這個頻道已經在列表裡")
        await self._save_voice_settings({"auto_channel_list": [*channels, channel_id]})
        return {"saved": True}

    @API("webui_voice_remove_channel", description="移除自動模式的語音頻道", version="1")
    async def webui_voice_remove_channel(self, channel_id: str = "") -> Dict[str, Any]:
        channels = self._load_settings().voice.auto_channel_list
        await self._save_voice_settings({"auto_channel_list": [item for item in channels if item != channel_id]})
        return {"saved": True}

    @API("webui_voice_leave", description="讓麥麥離開目前的語音頻道", version="1")
    async def webui_voice_leave(self) -> Dict[str, Any]:
        """固定模式下离开后要等下次重连才会回去。"""
        if self._voice_manager is None or self._voice_manager.get_connected_channel_id() is None:
            raise ValueError("目前不在語音頻道")
        await self._voice_manager.disconnect()
        return {"left": True}

    @API("webui_topics", description="讀取主動話題的排程、頻道與分享紀錄", version="1")
    async def webui_topics(self) -> Dict[str, Any]:
        """WebUI 主动话题页：下次时间、各频道是否认得、分享过的内容（新的在前）。"""
        config = self._load_settings().topics
        state = self._topic_scout.state if self._topic_scout is not None else {}
        names = await self._webui_chat_names()
        channels = []
        for channel_id in config.channel_list:
            stream_id = await self._resolve_discord_stream(channel_id)
            chat = names.get(stream_id, stream_id) if stream_id else "（還沒有人說話，MaiBot 不認得，會跳過）"
            channels.append({"channel_id": channel_id, "chat": chat})
        history = [
            (str(item.get("time") or ""), stream_id, item)
            for stream_id, items in (state.get("history") or {}).items()
            for item in items
        ]
        history.sort(key=lambda row: row[0], reverse=True)
        sources = [label for enabled, label in ((config.news_enabled, "新聞"), (config.video_enabled, "影片")) if enabled]
        if not config.enabled:
            notice = "主動話題未啟用，到插件設定的「日常」分頁開啟。"
        elif not config.channel_list:
            notice = "還沒設定頻道，主動話題不會發。"
        elif not sources:
            notice = "新聞和影片都關了，主動話題不會發。"
        else:
            notice = ""
        next_due = state.get("next_due") if config.enabled and state.get("cron") == config.cron else None
        return {
            "now": describe(datetime.now()),
            "notice": notice,
            "next_due": describe(datetime.fromisoformat(next_due)) if next_due else "—",
            "cron": config.cron,
            "sources": "、".join(sources) or "—",
            "shared_count": len(history),
            "channels": channels,
            "history": [
                {
                    "time": _short_time(when),
                    "chat": names.get(stream_id, stream_id),
                    "kind": TOPIC_SOURCE_LABELS.get(item.get("source"), item.get("source") or "—"),
                    "topic": item.get("topic") or "—",
                    "title": item.get("title") or "—",
                    "url": item.get("url") or "—",
                }
                for when, stream_id, item in history
            ],
        }

    @staticmethod
    def _append_system_text(kwargs: Dict[str, Any], text: str) -> Dict[str, Any]:
        """把一段文字接在 Hook 载荷的系统提示词后面；没有系统消息时原样放行。"""
        items = kwargs.get("items")
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem" and isinstance(item.get("parts"), list):
                item["parts"].append({"type": "text", "text": text})
                return {"action": "continue", "modified_kwargs": kwargs}
        return {"action": "continue"}

    def _inject_life_state(self, kwargs: Dict[str, Any]) -> Dict[str, Any]:
        """把当前生活状态接在系统提示词后面；没启用或没时程时原样放行。"""
        life_config = self._load_settings().life
        if not (life_config.enabled and life_config.inject_prompt) or self._daily_life is None:
            return {"action": "continue"}
        text = self._daily_life.prompt_text(datetime.now())
        return self._append_system_text(kwargs, text) if text else {"action": "continue"}

    @HookHandler(
        "maisaka.planner.before_request",
        name="discord_emote_nudge",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def nudge_emote_in_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """按 chat.emote_chance 偶尔提醒可以按表情；不提醒时 planner 几乎只会选 reply。

        只在本轮工具列表里有 discord_emote（也就是 Discord 聊天）时才提醒。
        """
        if "discord_emote" not in _tool_names(kwargs) or random.random() >= self._load_settings().chat.emote_chance:
            return {"action": "continue"}
        return self._append_system_text(kwargs, EMOTE_NUDGE)

    # ===== 日常生活：睡觉时不回复 =====

    def _sleeping(self, now: datetime) -> bool:
        """时程上在睡觉、开了「睡觉时不回复」，且没有被提醒或页面叫醒。"""
        life = self._load_settings().life
        if not (life.enabled and life.sleep_silence) or self._daily_life is None:
            return False
        if self._awake_until is not None and now < self._awake_until:
            return False
        return self._daily_life.asleep(now)

    async def _restore_silenced(self) -> None:
        """醒来、关掉睡觉静默或插件卸载时，把调成 0 的回复频率还原。"""
        for session_id in list(self._silenced_sessions):
            result = await self.ctx.call_capability("frequency.set_adjust", chat_id=session_id, value=1.0)
            if isinstance(result, dict) and not result.get("success", True):
                self.ctx.logger.warning(f"还原聊天 {session_id} 的回复频率失败：{result.get('error')}")
            self._silenced_sessions.discard(session_id)

    async def _wake_up(self) -> None:
        """提醒到点或页面叫醒：接下来 WAKE_MINUTES 分钟当作醒着，被静默的聊天恢复回复。"""
        now = datetime.now()
        if not self._sleeping(now):
            return
        self._awake_until = now + timedelta(minutes=WAKE_MINUTES)
        await self._restore_silenced()
        self.ctx.logger.info(f"麦麦被叫醒，{self._awake_until:%H:%M} 前照常回复")

    @HookHandler(
        "maisaka.planner.before_request",
        name="daily_life_sleep_silence",
        mode="blocking",
        order="early",
        timeout_ms=1000,
    )
    async def silence_planner_while_asleep(self, **kwargs: Any) -> Dict[str, Any]:
        """睡觉时段的 Discord 聊天：回复频率调成 0，之后 MaiBot 只静默接收、不再进规划；这一轮只留 stop。

        私聊不吃回复频率，每轮都会走到这里被拿掉工具。
        """
        if not any(name.startswith("discord_") for name in _tool_names(kwargs)) or not self._sleeping(datetime.now()):
            return {"action": "continue"}
        session_id = str(kwargs.get("session_id") or "")
        if session_id:
            # 旧版 SDK 没有 ctx.frequency，直接调能力
            await self.ctx.call_capability("frequency.set_adjust", chat_id=session_id, value=0)
            self._silenced_sessions.add(session_id)
        kwargs["tool_definitions"] = [
            tool for tool in kwargs["tool_definitions"] if _tool_names({"tool_definitions": [tool]}) & SLEEP_ALLOWED_TOOLS
        ]
        self._append_system_text(kwargs, SLEEP_NOTE)
        return {"action": "continue", "modified_kwargs": kwargs}

    @HookHandler(
        "maisaka.planner.before_request",
        name="daily_life_planner_state",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def inject_life_state_into_planner(self, **kwargs: Any) -> Dict[str, Any]:
        """规划时知道自己正在做什么，决定要不要回、怎么回。"""
        return self._inject_life_state(kwargs)

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="daily_life_replyer_state",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def inject_life_state_into_replyer(self, **kwargs: Any) -> Dict[str, Any]:
        """回复时语气和内容能带到当前在做的事。"""
        return self._inject_life_state(kwargs)

    async def on_config_update(self, scope: str, config_data: Dict[str, Any], version: str) -> None:
        """响应 Host 下发的配置变更，在作用于本插件时应用配置并视需要重连。

        Args:
            scope: 配置作用域；仅 ``"self"`` 时会应用 ``config_data``。
            config_data: 新的插件配置字典。
            version: 配置版本标识，可用于日志。
        """
        if scope != "self":
            return
        self.set_plugin_config(config_data)
        if version:
            self.ctx.logger.debug(f"Discord 适配器收到配置更新通知: {version}")
        await self._apply_reminder_switches()
        await self._restart_if_needed()


    def normalize_plugin_config(self, config_data: Mapping[str, Any] | None) -> tuple[dict[str, Any], bool]:
        normalized_config, changed = super().normalize_plugin_config(config_data)
        voice_config = normalized_config.get("voice")
        gptsovits_config = normalized_config.get("gptsovits_tts")
        if not isinstance(voice_config, dict) or not isinstance(gptsovits_config, dict):
            return normalized_config, changed

        cleared_auto_choice = False
        for field_name in ("model", "voice"):
            raw_value = gptsovits_config.get(field_name)
            if isinstance(raw_value, str) and raw_value.strip() == self._GSV_SCHEMA_AUTO_CHOICE:
                gptsovits_config[field_name] = ""
                cleared_auto_choice = True

        raw_template_model = str(gptsovits_config.get("model") or "").strip()
        normalized_template_model = GPTSoVITSTTSProvider._normalize_template_model_name(
            raw_template_model
        )
        cleared_openai_model_id = False
        if raw_template_model and not normalized_template_model and raw_template_model.startswith("GSVI-"):
            gptsovits_config["model"] = ""
            cleared_openai_model_id = True

        normalized_catalog_values = self._normalize_gptsovits_catalog_values(
            voice_config=voice_config,
            gptsovits_config=gptsovits_config,
            allow_fetch=False,
        )
        return (
            normalized_config,
            changed or cleared_auto_choice or cleared_openai_model_id or normalized_catalog_values,
        )

    def get_webui_config_schema(
        self,
        *,
        plugin_id: str = "",
        plugin_name: str = "",
        plugin_version: str = "",
        plugin_description: str = "",
        plugin_author: str = "",
    ) -> Dict[str, Any]:
        schema = super().get_webui_config_schema(
            plugin_id=plugin_id,
            plugin_name=plugin_name,
            plugin_version=plugin_version,
            plugin_description=plugin_description,
            plugin_author=plugin_author,
        )
        for section, field in PAGE_MANAGED_FIELDS:
            fields = (schema.get("sections") or {}).get(section, {}).get("fields")
            if isinstance(fields, dict):
                fields.pop(field, None)
        try:
            self._prune_inactive_config_sections(schema)
        except Exception as exc:
            self._get_logger().warning(f"Failed to prune inactive provider sections from config schema: {exc}")
        try:
            self._inject_gptsovits_template_choices(schema)
        except Exception as exc:
            self._get_logger().warning(
                f"Failed to enrich GPT-SoVITS config schema with local template choices: {exc}"
            )
        self._apply_config_tabs(schema)
        return schema

    def _prune_inactive_config_sections(self, schema: Dict[str, Any]) -> None:
        """按已保存配置移除未选用的 TTS / STT 服务商配置节。

        WebUI 不处理字段级 ``depends_on``，未选用的服务商会整节空占位显示；
        这里只改 Schema，表单保存时仍会提交完整配置，被隐藏节的已有值不会丢失。
        """
        sections = schema.get("sections")
        if not isinstance(sections, dict):
            return

        config_snapshot = self._load_schema_config_snapshot()
        for section_name, field_info in DiscordPluginSettings.model_fields.items():
            if section_name in sections and not is_section_visible(field_info.annotation, config_snapshot):
                sections.pop(section_name)

    @staticmethod
    def _apply_config_tabs(schema: Dict[str, Any]) -> None:
        """把配置节按 ``WEBUI_CONFIG_TABS`` 分到基础 / 聊天 / 语音三个标签页。"""
        sections = schema.get("sections")
        if not isinstance(sections, dict) or not sections:
            return

        tabs: List[Dict[str, Any]] = []
        for order, (tab_id, title, tab_sections) in enumerate(WEBUI_CONFIG_TABS):
            present = [name for name in tab_sections if name in sections]
            if present:
                tabs.append({"id": tab_id, "title": title, "sections": present, "order": order})
        schema["layout"] = {"type": "tabs", "tabs": tabs}

    def _inject_gptsovits_template_choices(self, schema: Dict[str, Any]) -> None:
        sections = schema.get("sections")
        if not isinstance(sections, dict):
            return

        gptsovits_section = sections.get("gptsovits_tts")
        if not isinstance(gptsovits_section, dict):
            return

        fields = gptsovits_section.get("fields")
        if not isinstance(fields, dict):
            return

        model_field = fields.get("model")
        voice_field = fields.get("voice")
        if not isinstance(model_field, dict) or not isinstance(voice_field, dict):
            return

        config_snapshot = self._load_schema_config_snapshot()
        voice_config = config_snapshot.get("voice")
        gptsovits_config = config_snapshot.get("gptsovits_tts")
        if not isinstance(voice_config, dict) or not isinstance(gptsovits_config, dict):
            return

        if str(voice_config.get("tts_provider") or "").strip() != "gptsovits":
            return

        api_base = str(gptsovits_config.get("api_base") or "http://127.0.0.1:8000").strip().rstrip("/")
        version = str(gptsovits_config.get("version") or "v4").strip() or "v4"
        self._inject_gptsovits_infer_single_choices(
            model_field=model_field,
            voice_field=voice_field,
            gptsovits_config=gptsovits_config,
            api_base=api_base,
            version=version,
        )

    def _inject_gptsovits_infer_single_choices(
        self,
        *,
        model_field: Dict[str, Any],
        voice_field: Dict[str, Any],
        gptsovits_config: Dict[str, Any],
        api_base: str,
        version: str,
    ) -> None:
        catalog, error_message = self._fetch_gptsovits_template_catalog(api_base, version)
        if not catalog:
            fallback_message = "当前未能从本地 GSV 拉取模板列表，将继续手动填写。"
            model_field["hint"] = self._append_schema_hint(model_field.get("hint"), fallback_message)
            voice_field["hint"] = self._append_schema_hint(voice_field.get("hint"), fallback_message)
            if error_message:
                self._get_logger().warning(
                    "Failed to fetch GPT-SoVITS template catalog for schema rendering "
                    f"[api={api_base}, version={version}, error={error_message}]"
                )
            return

        configured_model = GPTSoVITSTTSProvider._normalize_template_model_name(
            gptsovits_config.get("model")
        )
        model_choices = [self._GSV_SCHEMA_AUTO_CHOICE, *catalog.keys()]
        if configured_model and configured_model not in model_choices:
            model_choices.append(configured_model)

        model_field["ui_type"] = "select"
        model_field["choices"] = model_choices
        model_field["hint"] = self._append_schema_hint(
            model_field.get("hint"),
            f"已从本地 GSV 拉取 {len(catalog)} 个模板模型；选择“{self._GSV_SCHEMA_AUTO_CHOICE}”时会按运行时规则自动挑选。",
        )
        if configured_model and configured_model not in catalog:
            model_field["hint"] = self._append_schema_hint(
                model_field.get("hint"),
                "当前已保存的模板模型不在本地返回列表中，请重新选择或改回自动。",
            )

        configured_voice = str(gptsovits_config.get("voice") or "").strip()
        emotion_choices, language_summary = self._collect_gptsovits_emotion_choices(
            catalog=catalog,
            configured_model=configured_model,
            preferred_language=gptsovits_config.get("text_lang"),
        )
        if configured_voice and configured_voice not in emotion_choices:
            emotion_choices.append(configured_voice)

        if emotion_choices:
            voice_field["ui_type"] = "select"
            voice_field["choices"] = [self._GSV_SCHEMA_AUTO_CHOICE, *emotion_choices]
            voice_field["hint"] = self._append_schema_hint(
                voice_field.get("hint"),
                (
                    f"已从本地 GSV 拉取可选情感/音色 {len(emotion_choices)} 项"
                    f"{f'（{language_summary}）' if language_summary else ''}；"
                    f"选择“{self._GSV_SCHEMA_AUTO_CHOICE}”时会在运行时自动挑选。"
                ),
            )
        else:
            voice_field["hint"] = self._append_schema_hint(
                voice_field.get("hint"),
                "当前没有拉取到可选情感/音色；可以继续手动填写，或先保存模板模型后刷新配置页。",
            )

    def _load_schema_config_snapshot(self) -> Dict[str, Any]:
        config_path = Path(__file__).resolve().parent / "config.toml"
        raw_config: Mapping[str, Any] | None = None
        if config_path.exists():
            try:
                with config_path.open("rb") as file_obj:
                    loaded = tomllib.load(file_obj)
                if isinstance(loaded, dict):
                    raw_config = loaded
            except Exception as exc:
                self._get_logger().debug(
                    f"Failed to read plugin config snapshot for schema enrichment: {exc}"
                )

        if raw_config is None:
            current_config = self.get_plugin_config_data()
            raw_config = current_config if current_config else self.get_default_config()

        normalized_config, _ = self.normalize_plugin_config(raw_config)
        return normalized_config

    def _normalize_gptsovits_catalog_values(
        self,
        *,
        voice_config: Dict[str, Any],
        gptsovits_config: Dict[str, Any],
        allow_fetch: bool,
    ) -> bool:
        if str(voice_config.get("tts_provider") or "").strip() != "gptsovits":
            return False

        api_base = str(gptsovits_config.get("api_base") or "http://127.0.0.1:8000").strip().rstrip("/")
        version = str(gptsovits_config.get("version") or "v4").strip() or "v4"
        changed = False

        template_catalog, _ = self._fetch_gptsovits_template_catalog(
            api_base,
            version,
            allow_fetch=allow_fetch,
        )
        if not template_catalog:
            return changed

        current_model_value = str(gptsovits_config.get("model") or "").strip()
        normalized_model_value = GPTSoVITSTTSProvider._normalize_template_model_name(
            current_model_value
        )
        matched_model_value = self._match_gptsovits_template_model(
            template_catalog,
            normalized_model_value,
        )
        if matched_model_value and matched_model_value != current_model_value:
            gptsovits_config["model"] = matched_model_value
            changed = True
            self._get_logger().debug(
                "Normalized GPT-SoVITS template model from local catalog "
                f"[from={current_model_value}, to={matched_model_value}]"
            )

        current_voice_value = str(gptsovits_config.get("voice") or "").strip()
        if current_voice_value:
            emotion_choices, _ = self._collect_gptsovits_emotion_choices(
                catalog=template_catalog,
                configured_model=matched_model_value or normalized_model_value,
                preferred_language=gptsovits_config.get("text_lang"),
            )
            matched_voice_value = self._match_gptsovits_choice(
                emotion_choices,
                current_voice_value,
            )
            if matched_voice_value and matched_voice_value != current_voice_value:
                gptsovits_config["voice"] = matched_voice_value
                changed = True
                self._get_logger().debug(
                    "Normalized GPT-SoVITS template emotion from local catalog "
                    f"[from={current_voice_value}, to={matched_voice_value}]"
                )

        return changed

    def _fetch_gptsovits_template_catalog(
        self,
        api_base: str,
        version: str,
        *,
        allow_fetch: bool = True,
    ) -> tuple[Optional[Dict[str, Dict[str, List[str]]]], str]:
        normalized_api_base = str(api_base or "").strip().rstrip("/")
        normalized_version = str(version or "v4").strip() or "v4"
        if not normalized_api_base:
            return None, "empty api_base"

        cache_key = f"{normalized_api_base}|{normalized_version}"
        cached = self._gsv_template_catalog_cache.get(cache_key)
        now = time.monotonic()
        if cached:
            cached_at, cached_catalog, cached_error = cached
            ttl = (
                self._GSV_SCHEMA_CACHE_TTL_SECONDS
                if cached_catalog
                else self._GSV_SCHEMA_FAILURE_CACHE_TTL_SECONDS
            )
            if now - cached_at <= ttl:
                return cached_catalog, cached_error

        if not allow_fetch:
            return None, "catalog unavailable without network fetch"

        request_url = f"{normalized_api_base}/models/{quote(normalized_version, safe='')}"
        try:
            request = Request(request_url, headers={"Accept": "application/json"})
            with urlopen(request, timeout=self._GSV_SCHEMA_FETCH_TIMEOUT_SECONDS) as response:
                payload = json.loads(response.read().decode("utf-8", errors="ignore"))
        except HTTPError as exc:
            error = f"HTTP {exc.code}"
            self._gsv_template_catalog_cache[cache_key] = (now, None, error)
            return None, error
        except URLError as exc:
            error = str(exc.reason or exc)
            self._gsv_template_catalog_cache[cache_key] = (now, None, error)
            return None, error
        except Exception as exc:
            error = str(exc)
            self._gsv_template_catalog_cache[cache_key] = (now, None, error)
            return None, error

        models = payload.get("models")
        if not isinstance(models, dict):
            error = "response missing models"
            self._gsv_template_catalog_cache[cache_key] = (now, None, error)
            return None, error

        catalog: Dict[str, Dict[str, List[str]]] = {}
        for raw_model_name, raw_languages in models.items():
            model_name = str(raw_model_name or "").strip()
            if not model_name or not isinstance(raw_languages, dict):
                continue

            normalized_languages: Dict[str, List[str]] = {}
            for raw_language, raw_emotions in raw_languages.items():
                language = str(raw_language or "").strip()
                if not language or not isinstance(raw_emotions, list):
                    continue
                normalized_emotions = [
                    str(item).strip() for item in raw_emotions if str(item).strip()
                ]
                if normalized_emotions:
                    normalized_languages[language] = normalized_emotions

            if normalized_languages:
                catalog[model_name] = normalized_languages

        if not catalog:
            error = "empty template catalog"
            self._gsv_template_catalog_cache[cache_key] = (now, None, error)
            return None, error

        self._gsv_template_catalog_cache[cache_key] = (now, catalog, "")
        self._get_logger().debug(
            "Loaded GPT-SoVITS template catalog for schema rendering "
            f"[api={normalized_api_base}, version={normalized_version}, models={len(catalog)}]"
        )
        return catalog, ""

    def _collect_gptsovits_emotion_choices(
        self,
        *,
        catalog: Dict[str, Dict[str, List[str]]],
        configured_model: str,
        preferred_language: Any,
    ) -> tuple[List[str], str]:
        matched_model_name = self._match_gptsovits_template_model(catalog, configured_model)
        preferred_language_name = GPTSoVITSTTSProvider._map_language_alias(preferred_language) or ""

        if matched_model_name:
            language_sets = [catalog.get(matched_model_name, {})]
            scope_label = f"模型 {matched_model_name}"
        else:
            language_sets = list(catalog.values())
            scope_label = "全部模板模型"

        preferred_emotions: List[str] = []
        fallback_emotions: List[str] = []
        seen: set[str] = set()

        for language_map in language_sets:
            if not isinstance(language_map, dict):
                continue
            for language_name, emotions in language_map.items():
                if not isinstance(emotions, list):
                    continue
                normalized_language = GPTSoVITSTTSProvider._map_language_alias(language_name) or str(
                    language_name
                ).strip()
                target_bucket = (
                    preferred_emotions
                    if preferred_language_name and normalized_language == preferred_language_name
                    else fallback_emotions
                )
                for raw_emotion in emotions:
                    emotion = str(raw_emotion or "").strip()
                    if not emotion or emotion in seen:
                        continue
                    seen.add(emotion)
                    target_bucket.append(emotion)

        if preferred_emotions:
            return preferred_emotions, f"{scope_label}，语言 {preferred_language_name}"
        if fallback_emotions:
            return fallback_emotions, scope_label
        return [], scope_label

    @staticmethod
    def _match_gptsovits_template_model(
        catalog: Dict[str, Dict[str, List[str]]],
        configured_model: str,
    ) -> str:
        model_name = str(configured_model or "").strip()
        if not model_name:
            return ""
        if model_name in catalog:
            return model_name
        fuzzy_matches = [
            candidate
            for candidate in catalog
            if model_name in candidate or candidate.endswith(model_name)
        ]
        return fuzzy_matches[0] if len(fuzzy_matches) == 1 else ""

    @staticmethod
    def _match_gptsovits_choice(choices: List[str], configured_value: str) -> str:
        normalized_value = str(configured_value or "").strip()
        if not normalized_value:
            return ""

        for candidate in choices:
            if normalized_value == candidate:
                return candidate

        lowercase_value = normalized_value.lower()
        for candidate in choices:
            if candidate.lower() == lowercase_value:
                return candidate

        fuzzy_matches = [
            candidate
            for candidate in choices
            if normalized_value in candidate
            or candidate.endswith(normalized_value)
            or candidate.lower().endswith(lowercase_value)
        ]
        if len(fuzzy_matches) == 1:
            return fuzzy_matches[0]

        if lowercase_value in {"default", "默认"}:
            for candidate in choices:
                if candidate in {"默认", "default", "Default"}:
                    return candidate

        return ""

    @staticmethod
    def _append_schema_hint(existing_hint: Any, extra_hint: str) -> str:
        base_hint = str(existing_hint or "").strip()
        extra = str(extra_hint or "").strip()
        if not base_hint:
            return extra
        if not extra or extra in base_hint:
            return base_hint
        return f"{base_hint} {extra}"

    @HookHandler(
        "chat.receive.after_process",
        name="discord_typing_target_recorder",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def remember_discord_typing_target(
        self,
        message: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """记录 Discord 入站消息对应的 typing 目标，并启动当前会话 typing。"""
        del kwargs
        if self._client_manager is None or not isinstance(message, dict):
            return {"action": "continue"}

        settings = self._load_settings()
        if str(message.get("platform") or "").strip() != settings.platform.platform_name:
            return {"action": "continue"}

        # 只记录 typing 目标，不在消息进入时就启动。Discord 的 typing 一次显示约 10 秒，
        # 如果麦麦决定不回复就没有东西去停它。真正开始 reply 时才由 before_request 启动。
        session_id = str(message.get("session_id") or "").strip()
        channel_id = self._extract_discord_typing_channel_id(message)
        if session_id and isinstance(channel_id, int):
            self._client_manager.remember_typing_target(session_id, channel_id)
            self._discord_session_channels[session_id] = channel_id
        return {"action": "continue"}

    _REPLYER_EMOJI_HINT: ClassVar[str] = (
        "\n补充：这里是 Discord。上面说的不要输出表情包，不包括聊天记录里出现过的服务器表情（<:名称:id>），"
        "觉得合适时可以原样用在句中，不必每句都用。"
    )

    @HookHandler(
        "maisaka.replyer.before_model_request",
        name="discord_emoji_replyer_hint",
        mode="blocking",
        order="late",
        timeout_ms=1000,
    )
    async def inject_discord_emoji_replyer_hint(self, **kwargs: Any) -> Dict[str, Any]:
        """replyer 默认被要求不输出表情包；Discord 服务器会话里补一句，允许照抄聊天记录里的服务器表情。"""
        items = kwargs.get("items")
        if str(kwargs.get("session_id") or "").strip() not in self._discord_session_channels or not isinstance(
            items, list
        ):
            return {"action": "continue"}
        for item in items:
            if isinstance(item, dict) and item.get("item_type") == "SystemMessageItem" and isinstance(item.get("parts"), list):
                item["parts"].append({"type": "text", "text": self._REPLYER_EMOJI_HINT})
                return {"action": "continue", "modified_kwargs": kwargs}
        return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.before_request",
        name="discord_typing_on_reply",
        mode="observe",
        order="early",
        timeout_ms=1000,
    )
    async def start_discord_typing_on_reply(
        self,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """只有真正开始生成回复时才启动 Discord typing。"""
        del kwargs
        if self._client_manager is not None and str(session_id or "").strip():
            await self._client_manager.start_typing_for_session(session_id)
        return {"action": "continue"}

    @HookHandler(
        "send_service.before_send",
        name="discord_typing_before_send",
        mode="blocking",
        order="early",
        timeout_ms=1000,
    )
    async def stop_discord_typing_before_send(
        self,
        message: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """发送前先停止 Discord typing，避免后台刷新延续到回复之后。"""
        del kwargs
        self._stop_discord_typing_for_hook_message(message)
        return {"action": "continue"}

    @HookHandler(
        "maisaka.replyer.after_response",
        name="discord_typing_after_replyer_response",
        mode="observe",
        order="late",
        timeout_ms=1000,
    )
    async def stop_discord_typing_after_replyer_response(
        self,
        session_id: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """replyer 产出可见回复文本后结束 Discord typing。"""
        del kwargs
        if self._client_manager is not None:
            self._client_manager.stop_typing_for_session(session_id)
        return {"action": "continue"}

    @HookHandler(
        "send_service.after_send",
        name="discord_typing_after_send",
        mode="observe",
        order="late",
        timeout_ms=1000,
    )
    async def stop_discord_typing_after_send(
        self,
        message: Dict[str, Any] | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """发送流程结束后按出站消息再收束一次 Discord typing。"""
        del kwargs
        self._stop_discord_typing_for_hook_message(message)
        return {"action": "continue"}

    def _stop_discord_typing_for_hook_message(self, message: Mapping[str, Any] | None) -> None:
        if self._client_manager is None or not isinstance(message, Mapping):
            return

        session_id = str(message.get("session_id") or "").strip()
        if session_id:
            self._client_manager.stop_typing_for_session(session_id)

        channel_id = self._extract_discord_typing_channel_id(message)
        if isinstance(channel_id, int):
            self._client_manager.stop_typing_indicator(channel_id)

    @classmethod
    def _extract_discord_typing_channel_id(cls, message: Mapping[str, Any]) -> Optional[int]:
        message_info = message.get("message_info")
        if not isinstance(message_info, Mapping):
            return None

        raw_thread_id = cls._extract_thread_context_channel_id(message.get("raw_message"))
        if raw_thread_id is not None:
            return raw_thread_id

        additional_config = message_info.get("additional_config")
        if isinstance(additional_config, Mapping):
            channel_id = cls._normalize_int_id(additional_config.get("platform_io_target_channel_id"))
            if channel_id is not None:
                return channel_id
            channel_id = cls._normalize_int_id(additional_config.get("platform_io_target_group_id"))
            if channel_id is not None:
                return channel_id

        group_info = message_info.get("group_info")
        if isinstance(group_info, Mapping):
            return cls._normalize_int_id(group_info.get("group_id"))
        return None

    @classmethod
    def _extract_thread_context_channel_id(cls, raw_message: Any) -> Optional[int]:
        for segment in cls._iter_message_segments(raw_message):
            if not isinstance(segment, Mapping):
                continue
            if segment.get("type") != "thread_context":
                continue
            data = segment.get("data")
            if isinstance(data, Mapping):
                channel_id = cls._normalize_int_id(data.get("original_thread_id"))
                if channel_id is not None:
                    return channel_id
        return None

    @classmethod
    def _iter_message_segments(cls, value: Any) -> Sequence[Mapping[str, Any]]:
        if isinstance(value, list):
            return [item for item in value if isinstance(item, Mapping)]
        if isinstance(value, Mapping):
            if value.get("type") == "seglist" and isinstance(value.get("data"), list):
                return [item for item in value["data"] if isinstance(item, Mapping)]
            return [value]
        return []

    @staticmethod
    def _normalize_int_id(value: Any) -> Optional[int]:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None

    @MessageGateway(
        name=DISCORD_GATEWAY_NAME,
        route_type="duplex",
        platform="discord",
        protocol="discord",
        description="Discord Bot 双工消息网关",
    )
    async def handle_discord_gateway(
        self,
        message: Dict[str, Any],
        route: Optional[Dict[str, Any]] = None,
        metadata: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """处理 Host 经消息网关转发的出站数据，解析为 MaiBot 消息后发往 Discord。

        Args:
            message: 序列化后的消息体字典。
            route: 路由信息（当前未使用）。
            metadata: 附加元数据（当前未使用）。
            **kwargs: 兼容网关调用的额外参数（当前未使用）。

        Returns:
            dict: 至少包含 ``success``；失败时包含 ``error`` 说明原因。
        """
        del metadata, kwargs

        if self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "Discord 客户端未就绪"}

        try:
            maim_message = self._deserialize_outbound_message(message)
        except (ValueError, TypeError) as exc:
            return {"success": False, "error": f"消息解析失败: {exc}"}

        message_info = maim_message.message_info
        if not isinstance(message_info, BaseMessageInfo):
            return {"success": False, "error": "消息缺少有效的 message_info"}

        segment = maim_message.message_segment
        if not isinstance(segment, Seg) or not getattr(segment, "type", None):
            return {"success": False, "error": "消息缺少有效的消息段"}

        segment_type = getattr(segment, "type", None)

        if segment_type == "command":
            return await self._handle_outbound_command(maim_message)

        if segment_type == "notify":
            self.ctx.logger.debug(f"收到 notify 消息，已忽略：{segment.data}")
            return {"success": True}

        return await self._handle_outbound_message(maim_message)

    def _deserialize_outbound_message(self, message: Dict[str, Any]) -> MessageBase:
        """兼容解析 Host MessageDict 与旧版 maim_message 字典。"""
        if isinstance(message.get("message_segment"), dict):
            return MessageBase.from_dict(message)

        raw_message = message.get("raw_message")
        if isinstance(raw_message, list):
            return self._build_message_from_host_dict(message)

        raise ValueError("消息字典既不是 maim_message 格式，也不是 Host MessageDict 格式")

    def _build_message_from_host_dict(self, message: Dict[str, Any]) -> MessageBase:
        """将 Host 运行时的 `MessageDict` 转换为插件内部使用的 `MessageBase`。"""
        message_info_dict = message.get("message_info", {})
        if not isinstance(message_info_dict, dict):
            raise ValueError("消息缺少有效的 message_info")

        user_info_dict = message_info_dict.get("user_info", {})
        if not isinstance(user_info_dict, dict):
            raise ValueError("消息缺少有效的 message_info.user_info")

        platform = str(message.get("platform") or "discord").strip() or "discord"
        user_id = str(user_info_dict.get("user_id") or "").strip()
        user_nickname = str(user_info_dict.get("user_nickname") or user_id).strip() or user_id
        if not user_id:
            raise ValueError("消息缺少有效的 user_id")

        user_info = UserInfo(
            platform=platform,
            user_id=user_id,
            user_nickname=user_nickname,
            user_cardname=self._normalize_optional_string(user_info_dict.get("user_cardname")),
        )

        group_info = None
        group_info_dict = message_info_dict.get("group_info")
        if isinstance(group_info_dict, dict):
            group_id = str(group_info_dict.get("group_id") or "").strip()
            if group_id:
                group_info = GroupInfo(
                    platform=platform,
                    group_id=group_id,
                    group_name=str(group_info_dict.get("group_name") or group_id).strip() or group_id,
                )

        raw_message = message.get("raw_message", [])
        if not isinstance(raw_message, list):
            raise ValueError("消息缺少有效的 raw_message")

        segments = self._convert_host_raw_message_to_segments(raw_message)
        fallback_text = str(message.get("processed_plain_text") or message.get("display_message") or "").strip()
        if not segments and fallback_text:
            segments = [Seg(type="text", data=fallback_text)]
        if not segments:
            raise ValueError("消息缺少可发送的内容")

        format_info = FormatInfo(
            content_format=self._infer_content_formats(segments),
            accept_format=["text", "image", "emoji", "reply", "voice", "command", "file", "video"],
        )

        timestamp_raw = message.get("timestamp")
        try:
            timestamp = float(timestamp_raw) if timestamp_raw is not None else time.time()
        except (TypeError, ValueError):
            timestamp = time.time()

        additional_config = message_info_dict.get("additional_config")
        message_info = BaseMessageInfo(
            platform=platform,
            message_id=str(message.get("message_id") or f"discord-out-{int(time.time() * 1000)}"),
            time=timestamp,
            user_info=user_info,
            group_info=group_info,
            format_info=format_info,
            additional_config=additional_config if isinstance(additional_config, dict) else None,
        )

        message_segment = segments[0] if len(segments) == 1 else Seg(type="seglist", data=segments)
        return MessageBase(
            message_info=message_info,
            message_segment=message_segment,
            raw_message=fallback_text,
        )

    def _convert_host_raw_message_to_segments(self, raw_message: List[Dict[str, Any]]) -> List[Seg]:
        """将 Host 标准片段列表转换为 Discord 发送链可消费的 `Seg` 列表。"""
        segments: List[Seg] = []

        for item in raw_message:
            if not isinstance(item, dict):
                continue

            item_type = str(item.get("type") or "").strip()
            item_data = item.get("data")

            if item_type == "text":
                text = str(item_data or "")
                if text:
                    segments.append(Seg(type="text", data=text))
                continue

            if item_type in {"image", "emoji", "voice"}:
                binary_data = item.get("binary_data_base64")
                if isinstance(binary_data, str) and binary_data:
                    segments.append(Seg(type=item_type, data=binary_data))
                    continue

                fallback_text = str(item_data or "").strip()
                placeholder_map = {
                    "image": "[图片]",
                    "emoji": "[表情]",
                    "voice": "[语音]",
                }
                segments.append(Seg(type="text", data=fallback_text or placeholder_map[item_type]))
                continue

            if item_type == "at":
                mention_data = item_data if isinstance(item_data, dict) else {}
                target_user_id = str(mention_data.get("target_user_id") or "").strip()
                target_user_nickname = self._normalize_optional_string(
                    mention_data.get("target_user_nickname")
                )
                target_user_cardname = self._normalize_optional_string(
                    mention_data.get("target_user_cardname")
                )
                if target_user_id:
                    segments.append(
                        Seg(
                            type="mention",
                            data={
                                "users": [
                                    {
                                        "user_id": target_user_id,
                                        "display_name": target_user_nickname or target_user_cardname or target_user_id,
                                        "server_nickname": target_user_cardname,
                                        "username": target_user_nickname or target_user_id,
                                    }
                                ]
                            },
                        )
                    )
                continue

            if item_type == "reply":
                target_message_id = ""
                if isinstance(item_data, dict):
                    target_message_id = str(item_data.get("target_message_id") or "").strip()
                elif item_data is not None:
                    target_message_id = str(item_data).strip()
                if target_message_id:
                    segments.append(Seg(type="reply", data={"message_id": target_message_id}))
                continue

            if item_type == "dict" and isinstance(item_data, dict):
                custom_type = str(item_data.get("type") or "").strip()
                if custom_type and custom_type not in {"seglist", "dict"}:
                    segments.append(Seg(type=custom_type, data=item_data.get("data")))
                continue

            if item_type == "forward":
                segments.append(Seg(type="text", data="[转发消息]"))

        return segments

    @staticmethod
    def _infer_content_formats(segments: List[Seg]) -> List[str]:
        """根据片段列表推导 `FormatInfo.content_format`。"""
        formats: List[str] = []
        for seg in segments:
            seg_type = str(getattr(seg, "type", "") or "").strip()
            if seg_type and seg_type not in formats:
                formats.append(seg_type)
        return formats or ["text"]

    @staticmethod
    def _normalize_optional_string(value: Any) -> Optional[str]:
        """将任意值规整为可选字符串。"""
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    def _stop_typing_for_outbound_channels(self, *channel_ids: Optional[int]) -> None:
        if self._client_manager is None:
            return
        seen: set[int] = set()
        for channel_id in channel_ids:
            if isinstance(channel_id, int) and channel_id not in seen:
                self._client_manager.stop_typing_indicator(channel_id)
                seen.add(channel_id)


    async def _handle_outbound_message(self, message: MessageBase) -> Dict[str, Any]:
        """将普通文本/媒体出站消息解析目标频道并发送（含语音频道 TTS 分支）。

        Args:
            message: 已解析的 ``MessageBase`` 实例。

        Returns:
            dict: ``success`` 表示是否发送成功；失败时 ``error`` 为原因说明。
        """
        if self._thread_routing is None or self._content_builder is None:
            return {"success": False, "error": "出站组件未初始化"}

        additional_config = getattr(message.message_info, "additional_config", None)
        voice_output_requested = bool(
            isinstance(additional_config, dict)
            and additional_config.get("discord_voice_output")
        )
        source_channel_id: Optional[int] = None
        if isinstance(additional_config, dict):
            raw_source_channel_id = additional_config.get("platform_io_target_channel_id")
            try:
                if raw_source_channel_id not in (None, ""):
                    source_channel_id = int(str(raw_source_channel_id).strip())
            except (TypeError, ValueError):
                source_channel_id = None

        target_channel = await self._thread_routing.resolve_target_channel(message)
        if target_channel is None:
            self._stop_typing_for_outbound_channels(source_channel_id)
            return {"success": False, "error": "无法解析目标频道"}

        target_channel_id = getattr(target_channel, "id", None)
        target_channel_name = getattr(target_channel, "name", "unknown")
        target_channel_type = type(target_channel).__name__
        self._stop_typing_for_outbound_channels(source_channel_id, target_channel_id)
        self.ctx.logger.debug(
            "Discord outbound target resolved "
            f"[channel_id={target_channel_id}, channel={target_channel_name}, "
            f"type={target_channel_type}, voice_output_requested={voice_output_requested}, "
            f"voice_manager={'yes' if self._voice_manager else 'no'}]"
        )

        if isinstance(target_channel, (discord.VoiceChannel, discord.StageChannel)):
            if self._voice_manager is not None:
                self.ctx.logger.info(
                    "Discord outbound message targets a voice channel and will be routed through TTS "
                    f"[channel_id={target_channel.id}, channel={target_channel.name}, type={target_channel_type}]"
                )
                voice_result = await self._handle_voice_outbound(
                    message,
                    channel_id=target_channel.id,
                    text_channel=target_channel,
                )
                self._stop_typing_for_outbound_channels(source_channel_id, target_channel_id)
                if voice_result.get("success"):
                    return voice_result

                if voice_result.get("text_sent"):
                    self.ctx.logger.error(
                        "Discord voice TTS delivery failed after text echo was already sent "
                        f"[channel_id={target_channel.id}, error={voice_result.get('error', '')}]"
                    )
                    return {
                        "success": True,
                        "warning": voice_result.get("error") or "TTS failed after text fallback",
                        "tts_success": False,
                    }

                self.ctx.logger.error(
                    "Discord voice TTS delivery failed; falling back to text chat delivery in the voice channel "
                    f"[channel_id={target_channel.id}, error={voice_result.get('error', '')}]"
                )
            else:
                self.ctx.logger.error(
                    "Discord outbound target is a voice channel, but voice manager is unavailable; "
                    f"falling back to text chat delivery [channel_id={target_channel.id}]"
                )

        client = self._client_manager.client if self._client_manager is not None else None
        content, files, stickers = self._content_builder.build(message.message_segment, client, target_channel)
        reference = await self._thread_routing.get_reply_reference(message, target_channel)

        if not content and not files and not stickers:
            self._stop_typing_for_outbound_channels(source_channel_id, target_channel_id)
            return {"success": False, "error": "消息内容为空且无附件"}

        try:
            sent_message = await self._send_with_length_check(target_channel, content, files, reference, stickers)
            result: Dict[str, Any] = {"success": True}
            if sent_message is not None:
                result["external_message_id"] = str(sent_message.id)
                result["message_id"] = str(sent_message.id)
            return result
        except Exception as exc:
            return {"success": False, "error": str(exc)}
        finally:
            self._stop_typing_for_outbound_channels(source_channel_id, target_channel_id)

    async def _handle_voice_outbound(
        self,
        message: MessageBase,
        *,
        channel_id: Optional[int] = None,
        text_channel: Optional[discord.abc.Messageable] = None,
    ) -> Dict[str, Any]:
        """TTS 播报分支：将出站文本合成语音并在语音频道播放。"""
        if self._voice_manager is None or self._content_builder is None:
            self.ctx.logger.error("Discord voice outbound aborted: voice components are not initialized")
            return {"success": False, "error": "语音组件未初始化", "text_sent": False}

        content, _, _ = self._content_builder.build(message.message_segment)
        if not content or not content.strip():
            self.ctx.logger.error("Discord voice outbound aborted: message content is empty after rendering")
            return {"success": False, "error": "语音出站消息无文本内容", "text_sent": False}

        settings = self._load_settings()
        provider = getattr(self._voice_manager, "tts_provider", None)
        provider_name = type(provider).__name__ if provider is not None else "None"
        target_channel_id = channel_id or self._voice_manager.get_connected_channel_id()
        self.ctx.logger.info(
            "Discord voice outbound starting "
            f"[channel_id={target_channel_id}, chars={len(content)}, provider={provider_name}, "
            f"text_echo={'on' if settings.voice.send_text_in_voice else 'off'}]"
        )
        text_sent = False
        if settings.voice.send_text_in_voice:
            target_text_channel = text_channel
            if target_text_channel is None:
                resolved_channel_id = channel_id or self._voice_manager.get_connected_channel_id()
                if resolved_channel_id and self._client_manager and self._client_manager.client:
                    target_text_channel = self._client_manager.client.get_channel(
                        resolved_channel_id
                    )
            if target_text_channel and hasattr(target_text_channel, "send"):
                self.ctx.logger.debug(
                    "Discord voice outbound will also send text to the voice-channel chat "
                    f"[channel_id={getattr(target_text_channel, 'id', None)}]"
                )
                try:
                    await target_text_channel.send(content=content)
                    text_sent = True
                except Exception as exc:
                    self.ctx.logger.warning(f"发送语音频道文字失败: {exc}")
            else:
                self.ctx.logger.warning(
                    "Discord voice outbound requested text echo, but no writable voice text channel was resolved"
                )

        self.ctx.logger.debug(
            "Discord voice outbound invoking voice manager speak "
            f"[channel_id={target_channel_id}, chars={len(content)}]"
        )
        success = await self._voice_manager.speak(content, channel_id=channel_id)
        if success:
            self.ctx.logger.info(
                "Discord voice outbound completed successfully "
                f"[channel_id={target_channel_id}, provider={provider_name}, text_echo_sent={text_sent}]"
            )
        else:
            provider_last_error = ""
            if provider is not None:
                getter = getattr(provider, "get_last_error", None)
                if callable(getter):
                    try:
                        provider_last_error = str(getter() or "").strip()
                    except Exception:
                        provider_last_error = ""
                elif hasattr(provider, "_last_error"):
                    provider_last_error = str(getattr(provider, "_last_error") or "").strip()
            error_message = (
                f"TTS 播报失败: {provider_last_error}"
                if provider_last_error
                else "TTS 播报失败"
            )
            self.ctx.logger.error(
                "Discord voice outbound failed "
                f"[channel_id={target_channel_id}, provider={provider_name}, text_echo_sent={text_sent}, "
                f"provider_error={provider_last_error or '<empty>'}]"
            )
        return {
            "success": success,
            "error": "" if success else error_message,
            "text_sent": text_sent,
        }

    async def _handle_outbound_command(self, message: MessageBase) -> Dict[str, Any]:
        """处理 ``command`` 类型消息段，按 ``data.type`` 分发到具体子处理逻辑。

        Args:
            message: 含 command 类型消息段的 ``MessageBase``。

        Returns:
            dict: 子处理器返回的结果字典；未知类型时 ``success`` 为 False。
        """
        segment = message.message_segment
        command_data = segment.data if hasattr(segment, "data") else {}
        command_type = command_data.get("type", "") if isinstance(command_data, dict) else ""

        if command_type == "reaction":
            return await self._handle_reaction_command(command_data)

        self.ctx.logger.warning(f"收到未知 command 类型: {command_type}")
        return {"success": False, "error": f"未知命令类型: {command_type}"}

    async def _handle_reaction_command(self, command_data: Dict[str, Any]) -> Dict[str, Any]:
        """在指定频道消息上添加或移除表情反应。

        Args:
            command_data: 需包含 ``message_id``、``channel_id``、``emoji``；可选 ``action``（``add``/``remove``）。

        Returns:
            dict: 操作是否成功及失败时的 ``error`` 信息。
        """
        if self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "Discord 客户端未就绪"}

        action = command_data.get("action", "add")
        target_message_id = command_data.get("message_id")
        channel_id = command_data.get("channel_id")
        emoji_str = command_data.get("emoji")

        if not target_message_id or not channel_id or not emoji_str:
            return {"success": False, "error": "缺少必要参数 (message_id, channel_id, emoji)"}

        client = self._client_manager.client
        try:
            self._client_manager.stop_typing_indicator(int(channel_id))
        except (TypeError, ValueError):
            pass
        try:
            channel = client.get_channel(int(channel_id))
            if not channel:
                channel = await client.fetch_channel(int(channel_id))

            target_msg = await channel.fetch_message(int(target_message_id))

            if action == "add":
                await target_msg.add_reaction(emoji_str)
            elif action == "remove":
                await target_msg.remove_reaction(emoji_str, client.user)
            else:
                return {"success": False, "error": f"未知 reaction 操作: {action}"}

            return {"success": True}

        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            return {"success": False, "error": str(exc)}
        except (ValueError, TypeError) as exc:
            return {"success": False, "error": f"参数格式错误: {exc}"}

    _EMOTE_FORMS: ClassVar[Dict[str, str]] = {"reaction": "表情回应", "emoji": "表情", "sticker": "贴图"}

    # ponytail: platforms 写死默认平台名，platform_name 改成别的名字时工具不会出现
    # 按表情、单发表情、发贴图是同一种「不用文字回应」的情境，合成一个工具，只用 form 区分送出形式；
    # 首轮就可能用来代替回复，保持 visible；描述兼作检索索引，写进用户会说的动词和同义词
    @Tool(
        "discord_emote",
        description=(
            "用 Discord 伺服器的原生表情回應：按在訊息上（reaction／react）、單發一個伺服器表情，或發一張伺服器貼圖（sticker）。"
            "可以和 reply 在同一輪一起呼叫（邊回話邊按表情），也可以只按表情不說話。"
            "適合好笑、好消息、附和、按讚、表示收到、驚訝、無語、心疼的時候，像真人聊天一樣偶爾用。"
            "只傳目標訊息、想表達的情緒與形式，工具會看聊天上下文與候選圖自行挑選，不使用普通 Unicode emoji。"
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
        """Maisaka 工具：挑一个服务器表情或贴图，按 form 加成 reaction 或单独发出。

        Args:
            msg_id: 目标消息 ID（Host 已把模型可见的别名换回原始 ID）。
            feeling: 想表達的情緒提示；選圖模型會依上下文挑選。
            form: ``reaction`` 加在目标消息上，``emoji`` 单发服务器表情，``sticker`` 发服务器贴图。
            group_id: Host 注入的会话频道 ID。
            user_id: Host 注入的私聊对象用户 ID。
            stream_id: Host 注入的聊天流 ID，用于取挑图时的聊天上下文，也是单发时的目标。

        Returns:
            dict: ``success`` 与给模型看的 ``content`` 或 ``error``。
        """
        del kwargs
        if self._thread_routing is None or self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "Discord 客户端未就绪"}
        form = str(form or "reaction").strip()
        if form not in self._EMOTE_FORMS:
            return {"success": False, "error": f"form 只能是 {'、'.join(self._EMOTE_FORMS)}"}
        stream_id = str(stream_id or "").strip()
        if form != "reaction" and not stream_id:
            return {"success": False, "error": "缺少聊天流，无法单独发送"}

        target = await self._thread_routing.find_message(str(msg_id or "").strip(), group_id, user_id)
        if target is None:
            return {"success": False, "error": f"找不到消息 {msg_id}"}

        label = self._EMOTE_FORMS[form]
        picked = await self._pick_native_item(target, str(feeling or "").strip(), stream_id, stickers=form == "sticker")
        if picked is None:
            return {"success": False, "error": f"本伺服器沒有可用的{label}，或選圖失敗；沒有送出"}
        item, image_b64 = picked

        if form == "reaction":
            try:
                await target.add_reaction(item)
            except discord.HTTPException as exc:
                return {"success": False, "error": f"添加表情回应 {item} 失败：{exc}"}
            return {"success": True, "content": f"已用 {item} 回应消息 {msg_id}"}

        # 走 Host 发送链路，消息才会进聊天记录
        if form == "emoji":
            sent = await self.ctx.send.text(str(item), stream_id, sync_to_maisaka_history=True)
        else:
            if self._native_emoji is None:
                return {"success": False, "error": "原生贴图映射未就绪"}
            # 发送链路只收表情图片：先登记这张图的哈希，出站时 DiscordContentBuilder 会还原成原生贴图
            self._native_emoji.remember(image_b64, "sticker", item.id)
            sent = await self.ctx.send.emoji(image_b64, stream_id, sync_to_maisaka_history=True)
        if not sent:
            return {"success": False, "error": f"发送{label}失败"}
        return {"success": True, "content": f"已发送{label} {item if form == 'emoji' else item.name}"}

    _EMOJI_CONTEXT_LIMIT: ClassVar[int] = 12  # 与 MaiBot 表情子代理带的上下文条数一致
    _EMOJI_SELECTION_PROMPT: ClassVar[str] = (
        "你需要根据上下文和当前语气，选择一个合适的{kind}来回应指定的消息。\n"
        "其中包含一张 {rows}x{columns} 的{kind}拼图，一共 {count} 个位置。\n"
        "每张小图左上角都有一个较大的序号，范围是 1 到 {count}。\n"
        "你需要从这 {count} 张图里选出最合适的一张。\n"
        "你必须返回一个 JSON 对象（json object），不要输出任何 JSON 之外的内容。\n"
        '返回格式为：{{"emoji_index":1,"reason":"简短理由"}}'
    )

    async def _pick_native_item(
        self, target: discord.Message, feeling: str, stream_id: str, *, stickers: bool = False
    ) -> Optional[tuple[Union[discord.Emoji, discord.GuildSticker], str]]:
        """照 MaiBot send_emoji：随机抽候选表情或贴图拼成带序号的图，让视觉模型看着聊天上下文挑一张。

        Args:
            target: 要回应的消息。
            feeling: planner 给的情绪提示，可为空。
            stream_id: 聊天流 ID，用于取最近的聊天记录。
            stickers: True 时从服务器贴图里挑，否则从服务器自定义表情里挑。

        Returns:
            选中的表情或贴图及其图片 base64；没有可用候选、图片全部下载失败或模型不可用时返回 None。
        """
        guild = target.guild
        if guild is None or self._message_handler is None:
            return None
        if stickers:
            # Lottie 贴图是动画 JSON，不是图片，视觉模型看不了
            usable: List[Any] = [
                item for item in guild.stickers if item.available and item.format != discord.StickerFormatType.lottie
            ]
        else:
            usable = [item for item in guild.emojis if item.is_usable()]
        if not usable:
            return None
        # 插件没有跨模块读取 emoji.emoji_send_num 的能力令牌；
        # Discord reaction 只需少量候选，固定抽 25 张即可避免整条路径被拒绝。
        candidates = random.sample(usable, min(len(usable), 25))
        # 动态表情取第一帧 png，视觉模型多半不收动图
        images = await asyncio.gather(
            *(
                self._message_handler._fetch_emoji_bytes(
                    str(item.url) if stickers else f"{discord.Asset.BASE}/emojis/{item.id}.png", size=128
                )
                for item in candidates
            )
        )
        loaded = [(item, image) for item, image in zip(candidates, images) if image]
        if not loaded:
            return None
        grid = await asyncio.to_thread(build_numbered_grid, [base64.b64decode(image) for _, image in loaded])
        rows, columns = grid_shape(len(loaded))

        history = ""
        if stream_id:
            try:
                recent = await self.ctx.message.get_recent(chat_id=stream_id, limit=self._EMOJI_CONTEXT_LIMIT)
                history = await self.ctx.message.build_readable(recent) if isinstance(recent, list) and recent else ""
            except Exception as exc:  # 拿不到上下文就只看被回应的消息
                self.ctx.logger.debug(f"读取表情选择上下文失败：{exc}")
        task_text = (
            f"[聊天记录]\n{history if isinstance(history, str) and history else '（无）'}\n\n"
            f"[要回应的消息]{(target.content or '[无文字]')[:200]}\n"
            + (f"[想表达]{feeling}\n" if feeling else "")
            + f"[选择任务]\n候选总数: {len(loaded)}\n拼图布局: {rows}x{columns}\n请只输出 JSON。"
        )
        system_prompt = self._EMOJI_SELECTION_PROMPT.format(
            kind="贴图" if stickers else "表情", rows=rows, columns=columns, count=len(loaded)
        )
        prompt = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": task_text},
                    {"type": "image", "image_format": "png", "image_base64": base64.b64encode(grid).decode("ascii")},
                ],
            },
        ]
        # ponytail: MaiBot 在 emoji 任务没配模型时会先看 planner 是否支持视觉；插件读不到模型配置，直接退到 vlm
        result: Dict[str, Any] = {}
        for task_name in ("emoji", "vlm"):
            try:
                result = await self.ctx.llm.generate(prompt, task_name=task_name)
            except Exception as exc:
                result = {"success": False, "error": str(exc)}
            if result.get("success"):
                break
        else:
            self.ctx.logger.warning(f"挑选表情失败：{result.get('error')}")
            return None

        response = str(result.get("response") or "")
        try:
            index = int(json.loads(response[response.find("{") : response.rfind("}") + 1])["emoji_index"])
        except (ValueError, KeyError, TypeError):
            self.ctx.logger.warning("選圖模型未回傳有效 emoji_index；不送出")
            return None
        if not 1 <= index <= len(loaded):
            self.ctx.logger.warning(f"選圖模型回傳超出候選範圍的序號 {index}；不送出")
            return None
        return loaded[index - 1]

    # 以下工具首轮用不到，设为 deferred：名称统一 discord_ 前缀，tool_search 搜 discord 时会一起命中；
    # MaiBot 的检索按子串匹配，描述里简繁两种写法的常用说法都放进去
    @Tool(
        "discord_unreact",
        description=(
            "收回、取消、移除自己在一則 Discord 訊息上加的表情回應（reaction），例如按錯或不想再表態時。"
            "（收回表情、取消表情、撤回表情、取消回应、移除 reaction）"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id", param_type=ToolParamType.STRING, description="要收回表情的訊息 msg_id。", required=True
            ),
        ],
        platforms=DEFAULT_PLATFORM_NAME,
        visibility="deferred",
    )
    async def handle_discord_unreact(
        self, msg_id: str = "", group_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """收回 bot 自己在目标消息上的全部 reaction。"""
        del kwargs
        if self._thread_routing is None or self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "Discord 客户端未就绪"}
        target = await self._thread_routing.find_message(str(msg_id or "").strip(), group_id, user_id)
        if target is None:
            return {"success": False, "error": f"找不到消息 {msg_id}"}
        mine = [reaction.emoji for reaction in target.reactions if reaction.me]
        if not mine:
            return {"success": False, "error": f"消息 {msg_id} 上没有自己加的表情回应"}
        try:
            for emoji in mine:
                await target.remove_reaction(emoji, self._client_manager.client.user)
        except discord.HTTPException as exc:
            return {"success": False, "error": f"收回表情回应失败：{exc}"}
        return {"success": True, "content": f"已收回消息 {msg_id} 上的 {' '.join(str(emoji) for emoji in mine)}"}

    async def _find_own_message(
        self, msg_id: str, group_id: str, user_id: str
    ) -> tuple[Optional[discord.Message], Optional[str]]:
        """找出 bot 自己发的消息；找不到或不是自己的时返回给模型看的错误。"""
        if self._thread_routing is None or self._client_manager is None or self._client_manager.client is None:
            return None, "Discord 客户端未就绪"
        target = await self._thread_routing.find_message(str(msg_id or "").strip(), group_id, user_id)
        if target is None:
            return None, f"找不到消息 {msg_id}"
        if target.author.id != self._client_manager.client.user.id:
            return None, f"消息 {msg_id} 不是自己发的，只能改或删自己的发言"
        return target, None

    @Tool(
        "discord_message_edit",
        description=(
            "修改、編輯自己在 Discord 已經發出的一則訊息內容，例如打錯字、說錯話想更正、補充內容；只能改自己的發言。"
            "（改訊息、改消息、编辑消息、修改发言、改错字、更正、订正、edit）"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id", param_type=ToolParamType.STRING, description="要修改的自己那則訊息的 msg_id。", required=True
            ),
            ToolParameterInfo(
                name="content",
                param_type=ToolParamType.STRING,
                description="改好後的完整訊息內容，會整則取代原本的文字。",
                required=True,
            ),
        ],
        platforms=DEFAULT_PLATFORM_NAME,
        visibility="deferred",
    )
    async def handle_discord_message_edit(
        self, msg_id: str = "", content: str = "", group_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """把 bot 自己的一则消息整则改成新内容。"""
        del kwargs
        content = str(content or "").strip()
        if not content:
            return {"success": False, "error": "缺少修改后的内容；想整则删掉请用 discord_message_delete"}
        if len(content) > 2000:
            return {"success": False, "error": "Discord 单则消息最多 2000 字"}
        target, error = await self._find_own_message(msg_id, group_id, user_id)
        if target is None:
            return {"success": False, "error": error}
        try:
            await target.edit(content=content)
        except discord.HTTPException as exc:
            return {"success": False, "error": f"修改消息失败：{exc}"}
        return {"success": True, "content": f"已把消息 {msg_id} 改成：{content}"}

    @Tool(
        "discord_message_delete",
        description=(
            "刪除、收回、撤回自己在 Discord 已經發出的一則訊息，例如說錯話、發錯頻道或不該說的內容；只能刪自己的發言。"
            "（删除消息、刪訊息、撤回消息、收回发言、撤回、收回、delete）"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id", param_type=ToolParamType.STRING, description="要刪除的自己那則訊息的 msg_id。", required=True
            ),
        ],
        platforms=DEFAULT_PLATFORM_NAME,
        visibility="deferred",
    )
    async def handle_discord_message_delete(
        self, msg_id: str = "", group_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """删除 bot 自己的一则消息。"""
        del kwargs
        target, error = await self._find_own_message(msg_id, group_id, user_id)
        if target is None:
            return {"success": False, "error": error}
        summary = (target.content or "")[:50]
        try:
            await target.delete()
        except discord.HTTPException as exc:
            return {"success": False, "error": f"删除消息失败：{exc}"}
        return {"success": True, "content": f"已删除消息 {msg_id}（{summary}）"}

    @Tool(
        "discord_voice_join",
        description=(
            "加入、進入 Discord 語音頻道，到發話者目前所在的語音頻道用語音聊天；只能進設定允許的頻道。"
            "（加入语音、进语音、来语音、上麦、进通话、语音聊天、voice）"
        ),
        parameters=[
            ToolParameterInfo(
                name="msg_id",
                param_type=ToolParamType.STRING,
                description="提出要求的那則訊息 msg_id，用來找出對方所在的語音頻道。",
                required=True,
            ),
        ],
        platforms=DEFAULT_PLATFORM_NAME,
        visibility="deferred",
    )
    async def handle_discord_voice_join(
        self, msg_id: str = "", group_id: str = "", user_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """连接发话者所在的语音频道；频道必须在固定频道或自动候选列表里。"""
        del kwargs
        if self._voice_manager is None:
            return {"success": False, "error": "语音功能未启用"}
        if self._thread_routing is None:
            return {"success": False, "error": "Discord 客户端未就绪"}
        target = await self._thread_routing.find_message(str(msg_id or "").strip(), group_id, user_id)
        if target is None:
            return {"success": False, "error": f"找不到消息 {msg_id}"}
        # 私聊里的作者是 User，没有语音状态
        voice_state = getattr(target.author, "voice", None)
        channel = voice_state.channel if voice_state is not None else None
        if channel is None:
            return {"success": False, "error": "对方目前不在语音频道"}
        if not self._voice_manager.can_join(channel.id):
            return {"success": False, "error": f"语音频道 {channel.name} 不在设置允许加入的频道里"}
        if not await self._voice_manager.connect(channel.id):
            return {"success": False, "error": f"加入语音频道 {channel.name} 失败（权限或连接问题，详见日志）"}
        return {"success": True, "content": f"已加入语音频道 {channel.name}"}

    @Tool(
        "discord_voice_leave",
        description=(
            "離開、退出目前所在的 Discord 語音頻道，停止用語音說話；設定為固定頻道常駐時不會離開。"
            "（离开语音、退出语音、下麦、挂断、退出通话、voice）"
        ),
        parameters=[],
        platforms=DEFAULT_PLATFORM_NAME,
        visibility="deferred",
    )
    async def handle_discord_voice_leave(self, group_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """断开语音连接；只接受同一服务器频道里的要求，固定模式不离开。"""
        del kwargs
        if self._voice_manager is None or self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "语音功能未启用"}
        voice_channel_id = self._voice_manager.get_connected_channel_id()
        if voice_channel_id is None:
            return {"success": False, "error": "目前不在语音频道"}
        if self._voice_manager.voice_mode == "fixed":
            return {"success": False, "error": "语音设置为固定频道常驻，不能离开"}
        client = self._client_manager.client
        session_channel = client.get_channel(int(group_id)) if str(group_id or "").isdigit() else None
        voice_channel = client.get_channel(voice_channel_id)
        # 只有一个语音连接，别的服务器或私聊里的人不能把它叫走
        if session_channel is None or voice_channel is None or session_channel.guild.id != voice_channel.guild.id:
            return {"success": False, "error": "只能在同一个服务器的频道里要求离开语音"}
        await self._voice_manager.disconnect()
        return {"success": True, "content": f"已离开语音频道 {voice_channel.name}"}

    @API("adapter.discord.channel.send_message", description="发送 Discord 频道消息", version="1", public=True)
    async def send_channel_message(self, channel_id: str, text: str) -> Dict[str, Any]:
        """给 room 这类跨会话转发提供稳定的频道直发入口。"""
        if self._client_manager is None or self._client_manager.client is None:
            return {"success": False, "error": "Discord 客户端未就绪"}

        normalized_channel_id = str(channel_id or "").strip()
        content = str(text or "")
        if not normalized_channel_id:
            return {"success": False, "error": "缺少必要参数 channel_id"}
        if not content.strip():
            return {"success": False, "error": "消息内容为空"}

        try:
            numeric_channel_id = int(normalized_channel_id)
        except (TypeError, ValueError) as exc:
            return {"success": False, "error": f"参数格式错误: {exc}"}

        client = self._client_manager.client
        try:
            channel = client.get_channel(numeric_channel_id)
            if not channel:
                channel = await client.fetch_channel(numeric_channel_id)
            if not hasattr(channel, "send"):
                return {"success": False, "error": "目标频道不可发送消息"}

            # 这里复用正常出站的长度处理，避免 room 直发绕过 Discord 2000 字限制。
            sent_message = await self._send_with_length_check(channel, content, (), None)
            result: Dict[str, Any] = {"success": True}
            if sent_message is not None:
                message_id = str(getattr(sent_message, "id", "") or "")
                if message_id:
                    result["external_message_id"] = message_id
                    result["message_id"] = message_id
            return result
        except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
            return {"success": False, "error": str(exc)}

    @API("adapter.discord.presence.set", description="设置 Discord 机器人的状态文字与在线状态", version="1", public=True)
    async def set_presence(
        self, text: str = "", activity_type: str = "custom", status: str = "online"
    ) -> Dict[str, Any]:
        """给其他插件或之后的功能改机器人状态；改到配置重载前，重载后回到配置里的内容。

        Args:
            text: 状态文字，空字符串清掉活动，最多 128 字。
            activity_type: ``custom`` / ``playing`` / ``listening`` / ``watching`` / ``competing``。
            status: ``online`` / ``idle`` / ``dnd`` / ``invisible``。
        """
        if self._client_manager is None:
            return {"success": False, "error": "Discord 客户端未就绪"}
        try:
            await self._client_manager.set_presence(text, activity_type, status)
        except ValueError as exc:
            return {"success": False, "error": str(exc)}
        except discord.HTTPException as exc:
            return {"success": False, "error": f"设置状态失败：{exc}"}
        return {"success": True}

    MAX_MESSAGE_LENGTH: int = 2000

    async def _send_with_length_check(
        self,
        channel: discord.abc.Messageable,
        content: Optional[str],
        files: Sequence[discord.File],
        reference: Optional[discord.Message],
        stickers: Sequence[discord.GuildSticker] = (),
    ) -> Optional[discord.Message]:
        """在 Discord 消息长度与附件数量限制下发送内容；超长文本会拆分发送。

        Args:
            channel: 可发送消息的 Discord 频道或线程等对象。
            content: 文本内容，可为空（仅发附件时）。
            files: 待上传的附件序列，超过上限时截断为前 10 个。
            reference: 可选的回复引用消息。
            stickers: 随第一条消息发送的贴纸，超过上限时截断为前 3 个。
        """
        MAX_FILES = 10
        MAX_STICKERS = 3
        first_sent_message: Optional[discord.Message] = None
        if files or stickers:
            file_list = list(files)
            if len(file_list) > MAX_FILES:
                self.ctx.logger.warning(
                    f"消息包含 {len(file_list)} 个文件，超过限制，仅发送前 {MAX_FILES} 个"
                )
                file_list = file_list[:MAX_FILES]
            sticker_list = list(stickers)
            if len(sticker_list) > MAX_STICKERS:
                self.ctx.logger.warning(
                    f"消息包含 {len(sticker_list)} 个贴纸，超过限制，仅发送前 {MAX_STICKERS} 个"
                )
                sticker_list = sticker_list[:MAX_STICKERS]

            send_content: Optional[str] = None
            if content and len(content) <= self.MAX_MESSAGE_LENGTH:
                send_content = content
                content = None

            first_sent_message = await channel.send(
                content=send_content, files=file_list, stickers=sticker_list, reference=reference
            )

        if content:
            if len(content) <= self.MAX_MESSAGE_LENGTH:
                sent_message = await channel.send(
                    content=content, reference=reference if first_sent_message is None else None
                )
                if first_sent_message is None:
                    first_sent_message = sent_message
            else:
                sent_message = await self._send_long_message(
                    channel, content, reference if first_sent_message is None else None
                )
                if first_sent_message is None:
                    first_sent_message = sent_message

        return first_sent_message

    async def _send_long_message(
        self,
        channel: discord.abc.Messageable,
        content: str,
        reference: Optional[discord.Message],
    ) -> Optional[discord.Message]:
        """将超长纯文本拆成多条消息依次发送，首条可携带回复引用。

        Args:
            channel: 目标可发送对象。
            content: 完整长文本。
            reference: 仅第一条片段使用的回复引用。
        """
        max_len = self.MAX_MESSAGE_LENGTH - 10
        if "```" in content:
            parts = self._split_preserve_codeblocks(content, max_len)
        else:
            parts = self._split_by_lines(content, max_len)

        first_sent_message: Optional[discord.Message] = None
        for index, part in enumerate(parts):
            if part.strip():
                try:
                    sent_message = await channel.send(
                        content=part, reference=reference if index == 0 else None
                    )
                    if first_sent_message is None:
                        first_sent_message = sent_message
                except (discord.HTTPException, discord.Forbidden) as exc:
                    self.ctx.logger.error(f"发送消息片段失败：{exc}")

        return first_sent_message

    @staticmethod
    def _split_preserve_codeblocks(content: str, max_len: int) -> List[str]:
        """在不超过 ``max_len`` 的前提下按行累积，并尽量保持 Markdown 代码块边界完整。

        Args:
            content: 原始文本。
            max_len: 单段最大字符数。

        Returns:
            list[str]: 拆分后的文本段列表。
        """
        parts: List[str] = []
        current = ""
        in_codeblock = False
        codeblock_start = ""

        for line in content.split("\n"):
            if line.strip().startswith("```"):
                if not in_codeblock:
                    in_codeblock = True
                    codeblock_start = line.strip()
                else:
                    in_codeblock = False

            test = f"{current}\n{line}" if current else line

            if len(test) > max_len:
                if in_codeblock:
                    parts.append(current + "\n```")
                    current = codeblock_start + "\n" + line
                else:
                    if current:
                        parts.append(current)
                    current = line
            else:
                current = test

        if current:
            parts.append(current)
        return parts

    @staticmethod
    def _split_by_lines(content: str, max_len: int) -> List[str]:
        """按行合并为段，单行超长时在 ``max_len`` 处硬切分。

        Args:
            content: 原始文本。
            max_len: 单段最大字符数。

        Returns:
            list[str]: 拆分后的文本段列表。
        """
        parts: List[str] = []
        current = ""

        for line in content.split("\n"):
            if len(line) > max_len:
                if current:
                    parts.append(current)
                    current = ""
                while len(line) > max_len:
                    parts.append(line[:max_len])
                    line = line[max_len:]
                current = line
            else:
                test = f"{current}\n{line}" if current else line
                if len(test) > max_len:
                    parts.append(current)
                    current = line
                else:
                    current = test

        if current:
            parts.append(current)
        return parts


    def _load_settings(self) -> DiscordPluginSettings:
        """返回当前插件配置，并用「日常」页面设置的值覆盖对应字段。

        Returns:
            DiscordPluginSettings: 当前 ``self.config`` 的配置模型实例。
        """
        settings = cast(DiscordPluginSettings, self.config)
        for section, fields in getattr(self, "_page_settings", {}).items():
            target = getattr(settings, section)
            for field, value in fields.items():
                if getattr(target, field) != value:
                    setattr(target, field, value)
        return settings

    def _load_page_settings(self, path: Path) -> None:
        """读回页面设置；缺的字段第一次从原配置搬过来，之后以页面为准。"""
        self._page_settings_path = path
        self._page_settings = {
            section: fields for section, fields in load_state(path).items() if isinstance(fields, dict)
        }
        settings = self._load_settings()
        missing = [
            (section, field)
            for section, field in PAGE_MANAGED_FIELDS
            if field not in self._page_settings.get(section, {})
        ]
        for section, field in missing:
            self._page_settings.setdefault(section, {})[field] = getattr(getattr(settings, section), field)
        if missing:
            save_state(path, self._page_settings)

    def _save_page_setting(self, section: str, field: str, value: Any) -> None:
        """页面改设置：先赋给配置模型做校验和规范化，再存进 daily_settings.json。"""
        target = getattr(self._load_settings(), section)
        setattr(target, field, value)
        self._page_settings.setdefault(section, {})[field] = getattr(target, field)
        if self._page_settings_path is not None:
            save_state(self._page_settings_path, self._page_settings)

    async def _restart_if_needed(self) -> None:
        """先停止现有连接，再在配置与运行环境允许时重建组件并启动 Discord 客户端任务。"""
        settings = self._load_settings()

        await self._stop_connection()

        if not settings.should_connect():
            self.ctx.logger.info("Discord 适配器保持空闲状态（插件未启用）")
            return

        if not settings.validate_runtime_config(self.ctx.logger):
            return

        token = settings.connection.token

        self._ensure_components(settings, token)

        client_manager = self._client_manager
        if client_manager is None:
            return
        self._client_task = asyncio.create_task(client_manager.start())
        client_manager.start_monitor()

        self.ctx.logger.info("Discord 适配器启动任务已创建")

    def _ensure_components(self, settings: DiscordPluginSettings, token: str) -> None:
        """初始化或更新聊天过滤、运行时状态、消息处理、路由、客户端及语音等子组件。

        Args:
            settings: 完整插件配置。
            token: Discord Bot Token，传入 ``DiscordClientManager``。
        """
        platform_name = settings.platform.platform_name

        if self._chat_filter is None:
            self._chat_filter = DiscordChatFilter(self.ctx.logger)
        self._chat_filter.configure(settings.chat)

        if self._runtime_state is None:
            self._runtime_state = DiscordRuntimeStateManager(
                gateway_capability=self.ctx.gateway,
                logger=self.ctx.logger,
                gateway_name=DISCORD_GATEWAY_NAME,
            )

        if self._native_emoji is None:
            # 旧版 SDK 没有 ctx.paths，此时映射只保存在内存中
            paths = getattr(self.ctx, "paths", None)
            self._native_emoji = NativeEmojiRegistry(
                path=paths.data_dir / "native_emoji.json" if paths is not None else None,
                logger=self.ctx.logger,
            )

        if self._message_handler is None:
            self._message_handler = DiscordMessageHandler(
                logger=self.ctx.logger,
                platform_name=platform_name,
                chat_config=settings.chat,
                native_emoji=self._native_emoji,
            )
        else:
            self._message_handler.update_config(platform_name, settings.chat)

        if self._content_builder is None:
            self._content_builder = DiscordContentBuilder(logger=self.ctx.logger, native_emoji=self._native_emoji)

        if self._thread_routing is None:
            self._thread_routing = ThreadRoutingManager(
                logger=self.ctx.logger,
                chat_config=settings.chat,
            )
        else:
            self._thread_routing.update_config(settings.chat)

        self._client_manager = DiscordClientManager(
            logger=self.ctx.logger,
            token=token,
            intents_config=settings.get_intents_dict(),
            gateway_name=DISCORD_GATEWAY_NAME,
            gateway_capability=self.ctx.gateway,
            message_handler=self._message_handler,
            thread_routing_manager=self._thread_routing,
            chat_filter=self._chat_filter,
            filter_config=settings.filters,
            connection_check_interval=settings.connection.connection_check_interval,
            retry_delay=settings.connection.retry_delay,
            typing_indicator_enabled=settings.chat.show_typing_indicator,
            typing_indicator_delay_ms=settings.chat.typing_indicator_delay_ms,
            typing_indicator_timeout_sec=settings.chat.typing_indicator_timeout_sec,
        )
        if self._daily_life is not None:
            # 新客户端登录时没有状态文字，日常生活下一轮要重新推送当前时段
            self._daily_life.invalidate_presence()

        self._voice_manager = self._build_voice_manager(settings)

        runtime_state = self._runtime_state
        voice_manager = self._voice_manager

        async def _on_connected() -> None:
            if self._client_manager and self._client_manager.client and self._client_manager.client.user:
                bot_user = self._client_manager.client.user
                await runtime_state.report_connected(
                    account_id=str(bot_user.id),
                    bot_name=str(bot_user),
                )
                if self._message_handler is not None:
                    self._message_handler.set_account_id(str(bot_user.id))
                if voice_manager:
                    voice_manager.bot = self._client_manager.client
                    await voice_manager.start()

        async def _on_disconnected() -> None:
            await runtime_state.report_disconnected()
            if voice_manager:
                await voice_manager.stop()

        self._client_manager.set_lifecycle_callbacks(
            on_connected=_on_connected,
            on_disconnected=_on_disconnected,
        )

        if self._voice_manager:
            self._client_manager.voice_manager = self._voice_manager

    def _build_voice_manager(self, settings: DiscordPluginSettings) -> Optional[VoiceManager]:
        """根据配置创建 VoiceManager 实例（含 TTS/STT Provider）。"""
        voice_cfg = settings.voice
        if not voice_cfg.enabled:
            return None

        tts_provider = None
        stt_provider = None
        logger = self.ctx.logger

        if voice_cfg.tts_provider == "siliconflow":
            from .src.voice.tts.siliconflow_tts import SiliconFlowTTSProvider
            tts_provider = SiliconFlowTTSProvider(settings.siliconflow_tts, logger)
        elif voice_cfg.tts_provider == "gptsovits":
            from .src.voice.tts.gptsovits_tts import GPTSoVITSTTSProvider
            tts_provider = GPTSoVITSTTSProvider(settings.gptsovits_tts, logger)
        elif voice_cfg.tts_provider == "minimax":
            from .src.voice.tts.minimax_tts import MiniMaxTTSProvider
            tts_provider = MiniMaxTTSProvider(settings.minimax_tts, logger)
        elif voice_cfg.tts_provider == "fishaudio":
            from .src.voice.tts.fishaudio_tts import FishAudioTTSProvider
            tts_provider = FishAudioTTSProvider(settings.fishaudio_tts, logger)

        if voice_cfg.stt_provider == "siliconflow_sensevoice":
            from .src.voice.stt.siliconflow_stt import SiliconFlowSTTProvider
            stt_provider = SiliconFlowSTTProvider(settings.siliconflow_stt, logger)
        elif voice_cfg.stt_provider == "aliyun":
            from .src.voice.stt.aliyun_stt import AliyunSTTProvider
            stt_provider = AliyunSTTProvider(settings.aliyun_stt, logger)
        elif voice_cfg.stt_provider == "tencent":
            from .src.voice.stt.tencent_stt import TencentSTTProvider
            stt_provider = TencentSTTProvider(settings.tencent_stt, logger)
        elif voice_cfg.stt_provider == "gemini":
            from .src.voice.stt.gemini_stt import GeminiSTTProvider
            stt_provider = GeminiSTTProvider(settings.gemini_stt, logger)

        platform_name = settings.platform.platform_name
        gateway_name = DISCORD_GATEWAY_NAME
        gateway_cap = self.ctx.gateway

        async def _on_stt_result(member: Any, text: str) -> None:
            timestamp = time.time()
            user_info = UserInfo(
                platform=platform_name,
                user_id=str(member.id),
                user_nickname=quote_name(member.display_name),
                user_cardname=quote_name(getattr(member, "nick", None)),
            )
            voice_state = getattr(member, "voice", None)
            channel = getattr(voice_state, "channel", None)
            group_info = None
            if channel and getattr(channel, "guild", None):
                group_info = GroupInfo(
                    platform=platform_name,
                    group_id=str(channel.id),
                    group_name=f"{channel.name} @ {channel.guild.name}",
                )
            format_info = FormatInfo(
                content_format=["text"],
                accept_format=["text", "image", "emoji", "reply", "voice", "command", "file", "video"],
            )
            message_info = BaseMessageInfo(
                platform=platform_name,
                message_id=f"voice-{member.id}-{int(timestamp * 1000)}",
                time=timestamp,
                user_info=user_info,
                group_info=group_info,
                format_info=format_info,
                additional_config={"discord_voice_output": True} if group_info else None,
            )
            msg = MessageBase(
                message_info=message_info,
                message_segment=Seg(type="text", data=text),
                raw_message=text,
            )
            try:
                handler = self._message_handler
                if handler is None:
                    logger.error("Discord 入站消息处理器未初始化，无法上报 STT 结果")
                    return
                await gateway_cap.route_message(
                    gateway_name,
                    handler.build_host_message_dict(msg),
                    external_message_id=message_info.message_id,
                    dedupe_key=message_info.message_id,
                )
            except Exception as exc:
                logger.error(f"发送 STT 结果到 MaiCore 失败: {exc}")

        vm = VoiceManager(
            bot=None,  # type: ignore[arg-type]  # will be set in _on_connected
            logger=logger,
            voice_mode=voice_cfg.voice_mode,
            fixed_channel_id=voice_cfg.fixed_channel_id,
            auto_channel_list=voice_cfg.auto_channel_list,
            idle_timeout_sec=voice_cfg.idle_timeout_sec,
            tts_provider=tts_provider,
            stt_provider=stt_provider,
            on_stt_result=_on_stt_result,
            enable_vad=voice_cfg.enable_vad,
            vad_threshold_db=voice_cfg.vad_threshold_db,
            vad_deactivation_delay_ms=voice_cfg.vad_deactivation_delay_ms,
        )
        return vm

    async def _stop_connection(self) -> None:
        """关闭语音模块、取消后台客户端任务、停止客户端管理器并上报断开状态。"""
        if self._voice_manager is not None:
            await self._voice_manager.close()
            self._voice_manager = None

        if self._client_task and not self._client_task.done():
            self._client_task.cancel()
            try:
                await self._client_task
            except asyncio.CancelledError:
                pass
            self._client_task = None

        if self._client_manager is not None:
            await self._client_manager.stop()
            self._client_manager = None

        if self._runtime_state is not None:
            await self._runtime_state.report_disconnected()


def create_plugin() -> DiscordAdapterPlugin:
    """供 MaiBot SDK 入口调用的插件工厂，返回新的适配器实例。

    Returns:
        DiscordAdapterPlugin: 未加载状态下的 ``DiscordAdapterPlugin`` 实例。
    """
    return DiscordAdapterPlugin()
