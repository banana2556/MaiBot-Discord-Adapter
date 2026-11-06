"""Discord 客户端管理器。

管理 Discord Bot 客户端连接、事件处理、连接监控和 Reaction 事件绑定。
"""

import asyncio
import re
import traceback
from collections import defaultdict
from time import time as get_time
from typing import Any, Dict, Optional

import discord

from ..send_handler.thread_send_handler import ThreadRoutingManager
from .message_handler import DiscordMessageHandler

TYPING_INDICATOR_REFRESH_SECONDS = 5.0
LINK_PREVIEW_WAIT_SECONDS = 3.0  # 带链接的消息最多等这么久，让 Discord 补上链接预览
LINK_PREVIEW_POLL_SECONDS = 0.5
# 用 <> 包住的链接 Discord 不会展开预览，不用等
_PREVIEWABLE_LINK = re.compile(r"(?<!<)https?://")

PRESENCE_TEXT_LIMIT = 128  # Discord 自定义状态与活动名称的长度上限
_ACTIVITY_TYPES = {
    "playing": discord.ActivityType.playing,
    "listening": discord.ActivityType.listening,
    "watching": discord.ActivityType.watching,
    "competing": discord.ActivityType.competing,
}


def build_presence(
    text: str = "", activity_type: str = "custom", status: str = "online"
) -> tuple[Optional[discord.BaseActivity], discord.Status]:
    """把状态文字、显示方式和在线状态转成 discord.py 的活动与状态对象。

    Args:
        text: 状态文字；为空时不显示活动，超过上限会截断。
        activity_type: ``custom`` 或 ``playing`` / ``listening`` / ``watching`` / ``competing``。
        status: ``online`` / ``idle`` / ``dnd`` / ``invisible``。

    Returns:
        `(活动或 None, 在线状态)`。

    Raises:
        ValueError: 显示方式或在线状态不认识。
    """
    if activity_type != "custom" and activity_type not in _ACTIVITY_TYPES:
        raise ValueError(f"显示方式只能是 custom、{'、'.join(_ACTIVITY_TYPES)}")
    if status not in {"online", "idle", "dnd", "invisible"}:
        raise ValueError("在线状态只能是 online、idle、dnd、invisible")
    text = str(text or "").strip()[:PRESENCE_TEXT_LIMIT]
    activity: Optional[discord.BaseActivity] = None
    if text:
        activity = (
            discord.CustomActivity(name=text)
            if activity_type == "custom"
            else discord.Activity(type=_ACTIVITY_TYPES[activity_type], name=text)
        )
    return activity, discord.Status(status)


class DiscordClientManager:
    """Discord 客户端管理器。

    构造时接收所有外部依赖，不使用任何全局状态。
    内置连接健康监控和 Reaction 事件动态绑定。
    """

    def __init__(
        self,
        *,
        logger: Any,
        token: str,
        intents_config: Dict[str, bool],
        gateway_name: str,
        gateway_capability: Any,
        message_handler: DiscordMessageHandler,
        thread_routing_manager: ThreadRoutingManager,
        chat_filter: Any,
        filter_config: Any,
        connection_check_interval: int = 30,
        retry_delay: int = 5,
        typing_indicator_enabled: bool = False,
        typing_indicator_delay_ms: int = 1200,
        typing_indicator_timeout_sec: int = 5,
        presence: Optional[tuple[Optional[discord.BaseActivity], discord.Status]] = None,
    ) -> None:
        """初始化管理器状态并创建 Discord 客户端与事件绑定。

        Args:
            logger: 日志记录器。
            token: Discord Bot 登录令牌。
            intents_config: 各 Gateway Intent 的开关配置（如 guild_messages、dm_messages、
                reactions 等）。
            gateway_name: 当前网关在路由层使用的名称。
            gateway_capability: 网关能力对象，用于将消息路由到 Host。
            message_handler: 入站消息处理器，负责 Discord 消息到 Host 结构的转换。
            thread_routing_manager: 子区/线程路由管理器，需与客户端实例绑定。
            chat_filter: 频道与用户维度的入站过滤逻辑。
            filter_config: 过滤相关配置（如是否忽略自身或机器人消息）。
            connection_check_interval: 连接健康监控的检查间隔（秒）。
            retry_delay: 启动或重连失败后的重试等待时间（秒）。
            presence: 登录时带上的 `(活动, 在线状态)`，由 `build_presence` 生成；None 为在线、无活动。
        """
        self._logger = logger
        self._token = token
        self._intents_config = intents_config
        self._gateway_name = gateway_name
        self._gateway_capability = gateway_capability
        self._message_handler = message_handler
        self._thread_routing_manager = thread_routing_manager
        self._chat_filter = chat_filter
        self._filter_config = filter_config
        self._connection_check_interval = connection_check_interval
        self._retry_delay = retry_delay
        self._typing_indicator_enabled = typing_indicator_enabled
        self._typing_indicator_delay_seconds = max(float(typing_indicator_delay_ms or 0) / 1000.0, 0.0)
        self._typing_indicator_timeout_seconds = max(float(typing_indicator_timeout_sec or 0), 0.0)
        self._presence = presence or build_presence()

        self.client: Optional[discord.Client] = None
        self.is_connected: bool = False
        self.is_shutting_down: bool = False
        self.is_reconnecting: bool = False

        self._monitor_task: Optional[asyncio.Task[None]] = None
        self._reconnect_task: Optional[asyncio.Task[None]] = None

        self._last_health_check: float = 0
        self._health_check_interval: float = 60
        self._health_check_failures: int = 0
        self._typing_indicator_tasks: Dict[int, asyncio.Task[None]] = {}
        self._typing_indicator_stop_events: Dict[int, asyncio.Event] = {}
        self._typing_targets_by_session_id: Dict[str, int] = {}
        # 等链接预览、截影片画面会让消息晚几秒；同频道排队送出，后面的 @ 不会抢在前面先触发思考
        self._channel_locks: Dict[int, asyncio.Lock] = defaultdict(asyncio.Lock)

        self._on_connected_callback: Optional[Any] = None
        self._on_disconnected_callback: Optional[Any] = None

        self.voice_manager: Optional[Any] = None

        self._setup_client()

    def set_lifecycle_callbacks(
        self,
        on_connected: Any = None,
        on_disconnected: Any = None,
    ) -> None:
        """注册连接就绪与断开时的生命周期回调。

        Args:
            on_connected: 连接就绪或恢复时调用的异步回调，可为 None。
            on_disconnected: 连接断开时调用的异步回调，可为 None。
        """
        self._on_connected_callback = on_connected
        self._on_disconnected_callback = on_disconnected

    def _setup_client(self) -> None:
        """根据配置创建 `discord.Client`、绑定路由并注册各类事件处理器。"""
        intents = discord.Intents.default()
        intents.guild_messages = self._intents_config.get("guild_messages", True)
        intents.guilds = self._intents_config.get("guilds", True)
        intents.dm_messages = self._intents_config.get("dm_messages", True)
        intents.message_content = self._intents_config.get("message_content", True)
        # 別人按的表情不送進 MaiBot（不是對話內容，送進去會讓 planner 對著「誰按了什麼」回應）；
        # 仍要開這個 intent，discord.py 才會更新快取訊息的 reactions，決策模型拿它當「已經有人按了什麼」的線索
        intents.reactions = self._intents_config.get("reactions", True)
        intents.voice_states = self._intents_config.get("voice_states", False)

        # 状态随登录一起送出；重连时重建客户端，也会带上最近一次 set_presence 的内容
        activity, status = self._presence
        client = discord.Client(intents=intents, activity=activity, status=status)
        self.client = client
        self._thread_routing_manager.bind_client(client)

        @client.event
        async def on_ready() -> None:
            await self._on_ready()

        @client.event
        async def on_message(message: discord.Message) -> None:
            await self._on_message(message)

        # ponytail: 只收缓存里的消息（默认最近 1000 条）；更早的消息被改或撤回不会通知，需要时改用 on_raw_* 事件
        @client.event
        async def on_message_edit(before: discord.Message, after: discord.Message) -> None:
            # 链接预览展开、置顶也会触发编辑事件，正文没变就不通知
            if before.content != after.content:
                await self._on_message_change(after, before)

        @client.event
        async def on_message_delete(message: discord.Message) -> None:
            await self._on_message_change(message)

        @client.event
        async def on_error(event: str, *args: Any, **kwargs: Any) -> None:
            self._logger.error(f"Discord 事件 {event} 发生错误")

        @client.event
        async def on_disconnect() -> None:
            self.is_connected = False
            self._stop_all_typing_indicators()
            self._logger.warning("Discord 客户端连接断开")
            if self._on_disconnected_callback:
                try:
                    await self._on_disconnected_callback()
                except Exception:
                    pass

        @client.event
        async def on_resume() -> None:
            self.is_connected = True
            self._logger.info("Discord 客户端连接已恢复")
            if self._on_connected_callback:
                try:
                    await self._on_connected_callback()
                except Exception:
                    pass

        @client.event
        async def on_voice_state_update(
            member: discord.Member,
            before: discord.VoiceState,
            after: discord.VoiceState,
        ) -> None:
            if self.voice_manager:
                try:
                    await self.voice_manager.on_voice_state_update(member, before, after)
                except Exception as exc:
                    self._logger.error(f"语音状态更新处理失败: {exc}")

    async def _on_ready(self) -> None:
        """Gateway 就绪时更新连接状态、记录日志、注册 Reaction 并触发连接回调。"""
        self.is_connected = True
        client = self.client
        if client is None:
            return
        self._logger.info(f"Discord 客户端已连接: {client.user}")
        self._logger.info(f"Bot 已加入 {len(client.guilds)} 个服务器")

        if self._on_connected_callback:
            try:
                await self._on_connected_callback()
            except Exception as exc:
                self._logger.warning(f"连接就绪回调执行失败: {exc}")

    async def set_presence(self, text: str = "", activity_type: str = "custom", status: str = "online") -> None:
        """改机器人的状态文字与在线状态；连线中立即生效，之后重连也沿用。

        Args:
            text: 状态文字；为空时清掉活动。
            activity_type: ``custom`` 或 ``playing`` / ``listening`` / ``watching`` / ``competing``。
            status: ``online`` / ``idle`` / ``dnd`` / ``invisible``。

        Raises:
            ValueError: 显示方式或在线状态不认识。
        """
        # ponytail: Discord 每个连接大约 20 秒内只收 5 次状态更新，超过会被限流；高频更新的调用方自己节流
        self._presence = build_presence(text, activity_type, status)
        if self.client is not None and self.is_connected:
            activity, presence_status = self._presence
            await self.client.change_presence(activity=activity, status=presence_status)

    def _is_ignored_author(self, author: Any) -> bool:
        """按过滤配置忽略 bot 自己和其他 bot 的消息。"""
        bot_user = getattr(self.client, "user", None)
        if bot_user and author.id == bot_user.id and getattr(self._filter_config, "ignore_self_message", True):
            return True
        return bool(author.bot) and getattr(self._filter_config, "ignore_bot_message", True)

    def _is_chat_allowed(self, message: discord.Message) -> bool:
        """按频道/用户规则判断消息是否放行；子区按配置继承父频道的规则。"""
        is_thread = hasattr(message.channel, "parent") and message.channel.parent is not None
        check_channel_id = str(message.channel.id)
        if is_thread:
            inherit_perms = getattr(
                self._chat_filter._config, "inherit_channel_permissions", True
            ) if self._chat_filter._config else True
            if inherit_perms and message.channel.parent:
                check_channel_id = str(message.channel.parent.id)

        return self._chat_filter.is_allowed(
            user_id=str(message.author.id),
            guild_id=str(message.guild.id) if message.guild else None,
            channel_id=check_channel_id,
            thread_id=str(message.channel.id) if is_thread else None,
            is_thread=is_thread,
        )

    async def _on_message_change(self, message: discord.Message, before: Optional[discord.Message] = None) -> None:
        """消息被编辑（带 ``before``）或撤回时，按同样的过滤规则转成通知送给 Host。"""
        try:
            if self._is_ignored_author(message.author) or not self._is_chat_allowed(message):
                return
            message_dict = await self._message_handler.handle_message_change(message, before)
            if message_dict is None:
                return
            await self._gateway_capability.route_message(
                self._gateway_name,
                message_dict,
                external_message_id=message_dict["message_id"],
                dedupe_key=message_dict["message_id"],
            )
        except Exception as exc:
            self._logger.error(f"处理消息编辑/撤回事件时发生错误: {exc}")
            self._logger.debug(traceback.format_exc())

    async def _on_message(self, message: discord.Message) -> None:
        """处理入站频道/DM 消息：过滤后转换为 Host 结构并路由，必要时更新子区上下文。

        Args:
            message: Discord 推送的 `Message` 对象。
        """
        try:
            if self._is_ignored_author(message.author):
                return

            guild_id = str(message.guild.id) if message.guild else None
            channel_id = str(message.channel.id)
            channel_type = type(message.channel).__name__
            is_voice_chat_message = isinstance(
                message.channel, (discord.VoiceChannel, discord.StageChannel)
            )

            is_thread = hasattr(message.channel, "parent") and message.channel.parent is not None

            if is_voice_chat_message:
                self._logger.info(
                    "Received Discord voice-channel chat message "
                    f"[message_id={message.id}, guild_id={guild_id}, channel_id={channel_id}, "
                    f"channel={getattr(message.channel, 'name', 'unknown')}, author_id={message.author.id}, "
                    f"channel_type={channel_type}, chars={len(message.content or '')}]"
                )
            else:
                self._logger.debug(
                    "Received Discord message "
                    f"[message_id={message.id}, guild_id={guild_id}, channel_id={channel_id}, "
                    f"channel_type={channel_type}, author_id={message.author.id}, "
                    f"chars={len(message.content or '')}]"
                )

            if not self._is_chat_allowed(message):
                if is_voice_chat_message:
                    self._logger.warning(
                        "Ignored Discord voice-channel chat message because it was blocked by chat filter "
                        f"[message_id={message.id}, channel_id={channel_id}]"
                    )
                return

            # 进锁前不能有 await：discord.py 按到达顺序建任务，asyncio.Lock 按排队顺序放行
            async with self._channel_locks[message.channel.id]:
                if (
                    not message.embeds
                    and not message.flags.suppress_embeds
                    and _PREVIEWABLE_LINK.search(message.content or "")
                ):
                    await self._wait_for_link_preview(message)
                    if not message.embeds:
                        # 預覽太晚出現（或網站不給預覽）時只剩網址，圖片就進不了識圖和圖片記憶
                        self._logger.debug(
                            f"連結預覽 {LINK_PREVIEW_WAIT_SECONDS:g} 秒內沒出現，只送文字 [message_id={message.id}]"
                        )

                message_dict = await self._message_handler.handle_discord_message(message)
                if message_dict is None:
                    return

                await self._gateway_capability.route_message(
                    self._gateway_name,
                    message_dict,
                    external_message_id=str(message.id),
                    dedupe_key=str(message.id),
                )

            routing_info = self._message_handler.get_thread_routing_info(message)
            if routing_info:
                if routing_info["is_inherit"]:
                    self._thread_routing_manager.update_thread_context(
                        routing_info["parent_channel_id"],
                        routing_info["thread_id"],
                    )
                elif not routing_info["is_thread"]:
                    pass
            elif message.guild and not is_thread:
                inherit_mem = getattr(
                    self._message_handler._chat_config, "inherit_channel_memory", True
                )
                if inherit_mem:
                    self._thread_routing_manager.clear_thread_context(str(message.channel.id))

        except Exception as exc:
            self._logger.error(f"处理 Discord 消息时发生错误: {exc}")
            self._logger.debug(traceback.format_exc())

    @staticmethod
    async def _wait_for_link_preview(message: discord.Message) -> None:
        """Discord 送出消息后才抓网页、补上链接预览，这时只发一次编辑事件。

        discord.py 收到编辑会就地更新缓存里同一个 ``Message``，所以这里等它的 ``embeds`` 出现再转换，
        图片链接才会当成图片交给麦麦，而不是只剩一串网址。
        """
        # ponytail: 不展开预览的网站会白等满 3 秒，同频道后面的消息也跟着排队
        for _ in range(int(LINK_PREVIEW_WAIT_SECONDS / LINK_PREVIEW_POLL_SECONDS)):
            await asyncio.sleep(LINK_PREVIEW_POLL_SECONDS)
            if message.embeds:
                return

    def remember_typing_target(self, session_id: str, channel_id: int) -> None:
        """记录 MaiBot session 对应的 Discord typing 目标。"""
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id or not isinstance(channel_id, int):
            return
        self._typing_targets_by_session_id[normalized_session_id] = channel_id

    async def start_typing_for_session(self, session_id: str) -> Optional[int]:
        """在 Maisaka 真正开始请求模型时，为对应 Discord 会话启动 typing。"""
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id or self.client is None:
            return None

        channel_id = self._typing_targets_by_session_id.get(normalized_session_id)
        if not isinstance(channel_id, int):
            return None

        channel = self.client.get_channel(channel_id)
        if channel is None:
            try:
                channel = await self.client.fetch_channel(channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException) as exc:
                self._logger.debug(
                    "Discord typing target fetch failed "
                    f"[session_id={normalized_session_id}, channel_id={channel_id}, error={exc}]"
                )
                return None
        return self.start_typing_indicator(channel)

    def stop_typing_for_session(self, session_id: str) -> None:
        normalized_session_id = str(session_id or "").strip()
        if not normalized_session_id:
            return
        channel_id = self._typing_targets_by_session_id.pop(normalized_session_id, None)
        self.stop_typing_indicator(channel_id)

    def start_typing_indicator(self, channel: discord.abc.Messageable) -> Optional[int]:
        """在指定消息目标上启动短时 typing 指示。"""
        if not self._typing_indicator_enabled:
            return None

        channel_id = getattr(channel, "id", None)
        typing_method = getattr(channel, "typing", None)
        if not isinstance(channel_id, int) or not callable(typing_method):
            return None

        existing_task = self._typing_indicator_tasks.get(channel_id)
        if existing_task is not None and not existing_task.done():
            existing_stop_event = self._typing_indicator_stop_events.get(channel_id)
            if existing_stop_event is None or not existing_stop_event.is_set():
                return channel_id

        stop_event = asyncio.Event()
        task = asyncio.create_task(
            self._typing_indicator_worker(channel, channel_id, stop_event)
        )
        self._typing_indicator_tasks[channel_id] = task
        self._typing_indicator_stop_events[channel_id] = stop_event
        self._logger.debug(f"Discord typing indicator started [channel_id={channel_id}]")
        return channel_id

    def stop_typing_indicator(self, channel_id: Optional[int]) -> None:
        """停止指定频道上的 typing 指示任务。"""
        if not isinstance(channel_id, int):
            return

        stop_event = self._typing_indicator_stop_events.pop(channel_id, None)
        if stop_event is not None:
            stop_event.set()

        task = self._typing_indicator_tasks.pop(channel_id, None)
        if task is not None and not task.done():
            task.cancel()

    def _stop_all_typing_indicators(self) -> None:
        """停止当前客户端维护的全部 typing 指示任务。"""
        for stop_event in list(self._typing_indicator_stop_events.values()):
            stop_event.set()

        for task in list(self._typing_indicator_tasks.values()):
            if not task.done():
                task.cancel()
        self._typing_indicator_tasks.clear()
        self._typing_indicator_stop_events.clear()

    async def _typing_indicator_worker(
        self,
        channel: discord.abc.Messageable,
        channel_id: int,
        stop_event: asyncio.Event,
    ) -> None:
        """保持 Discord typing 状态，直到发送完成或超时。"""
        try:
            if self._typing_indicator_delay_seconds > 0:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(),
                        timeout=self._typing_indicator_delay_seconds,
                    )
                    return
                except asyncio.TimeoutError:
                    pass

            if stop_event.is_set():
                return

            self._logger.debug(
                "Discord typing indicator activated "
                f"[channel_id={channel_id}, timeout_sec={self._typing_indicator_timeout_seconds}]"
            )
            started_at = asyncio.get_running_loop().time()
            while not stop_event.is_set():
                if self._typing_indicator_timeout_seconds > 0:
                    elapsed = asyncio.get_running_loop().time() - started_at
                    remaining_timeout = self._typing_indicator_timeout_seconds - elapsed
                    if remaining_timeout <= 0:
                        self._logger.debug(
                            "Discord typing indicator timed out before any outbound reply "
                            f"[channel_id={channel_id}]"
                        )
                        return
                else:
                    remaining_timeout = TYPING_INDICATOR_REFRESH_SECONDS

                await channel.typing()

                wait_timeout = min(TYPING_INDICATOR_REFRESH_SECONDS, remaining_timeout)
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=wait_timeout)
                except asyncio.TimeoutError:
                    continue
        except asyncio.CancelledError:
            raise
        except (discord.Forbidden, discord.HTTPException) as exc:
            self._logger.debug(
                f"Discord typing indicator could not be delivered [channel_id={channel_id}, error={exc}]"
            )
        except Exception as exc:
            self._logger.warning(
                "Discord typing indicator worker failed unexpectedly "
                f"[channel_id={channel_id}, error={exc}]"
            )
        finally:
            current_task = asyncio.current_task()
            if self._typing_indicator_tasks.get(channel_id) is current_task:
                self._typing_indicator_tasks.pop(channel_id, None)
            self._typing_indicator_stop_events.pop(channel_id, None)


    async def start(self) -> None:
        """在关闭标志未置位时循环尝试启动客户端，使用 `client.start` 阻塞直至断开或致命错误。"""
        self._logger.info(f"正在启动 Discord 客户端... (重试间隔: {self._retry_delay}s)")

        attempt = 0
        while not self.is_shutting_down:
            try:
                if attempt > 0:
                    self._logger.info(f"第 {attempt} 次重试启动 Discord 客户端...")
                    await asyncio.sleep(self._retry_delay)
                    await self._reset_client()

                if self.client is None:
                    self._setup_client()
                await self.client.start(self._token)  # type: ignore[union-attr]
                self._logger.warning("Discord 客户端连接意外断开")
                break

            except (discord.LoginFailure, discord.HTTPException) as exc:
                error_text = str(exc).lower()
                if any(kw in error_text for kw in ("login", "token", "unauthorized")):
                    self._logger.error("Token 相关错误，请检查插件配置中的 connection.token")
                    raise
                self._logger.warning(f"第 {attempt + 1} 次尝试失败: {exc}")

            except (ConnectionError, TimeoutError, OSError) as exc:
                self._logger.warning(f"第 {attempt + 1} 次尝试失败: {exc}")

            except Exception as exc:
                self._logger.error(f"第 {attempt + 1} 次尝试时发生未知错误: {exc}")

            attempt += 1

    async def stop(self) -> None:
        """请求关闭：停止连接监控并关闭 Discord 客户端。"""
        self.is_shutting_down = True
        self._stop_monitor()
        self._stop_all_typing_indicators()

        if self.client and not self.client.is_closed():
            await self.client.close()
            self._logger.info("Discord 客户端已关闭")

    async def _reset_client(self) -> None:
        """关闭旧客户端（若存在）、清空 Reaction 注册标记并重新 `_setup_client`。"""
        self._stop_all_typing_indicators()
        if self.client and not self.client.is_closed():
            try:
                await self.client.close()
            except Exception:
                pass

        self._setup_client()
        self.is_connected = False

    async def force_reconnect(self) -> None:
        """在非关闭且非重连中时强制关闭当前连接并后台启动 `_reconnect_loop`。"""
        if self.is_shutting_down or self.is_reconnecting:
            return

        self._logger.info("强制重连 Discord 客户端...")
        self.is_reconnecting = True

        try:
            self.is_connected = False

            if self._reconnect_task and not self._reconnect_task.done():
                self._reconnect_task.cancel()
                try:
                    await self._reconnect_task
                except asyncio.CancelledError:
                    pass

            if self.client and not self.client.is_closed():
                try:
                    await asyncio.wait_for(self.client.close(), timeout=3.0)
                except asyncio.TimeoutError:
                    self._logger.warning("关闭 Discord 客户端超时，强制继续")

            await asyncio.sleep(0.5)
            await self._reset_client()
            self._reconnect_task = asyncio.create_task(self._reconnect_loop())

        except Exception as exc:
            self._logger.error(f"强制重连时发生错误: {exc}")
            self.is_connected = False
            self.is_reconnecting = False

    async def _reconnect_loop(self) -> None:
        """后台重连循环：在关闭前反复尝试 `client.start`，认证失败则退出并清除重连标志。"""
        try:
            attempt = 0
            while not self.is_shutting_down:
                try:
                    if attempt > 0:
                        await asyncio.sleep(self._retry_delay)
                        await self._reset_client()
                    if self.client is None:
                        self._setup_client()
                    await self.client.start(self._token)  # type: ignore[union-attr]
                    self.is_connected = False
                    attempt += 1
                except (discord.LoginFailure, discord.HTTPException) as exc:
                    self._logger.error(f"重连认证错误，停止: {exc}")
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._logger.warning(f"第 {attempt + 1} 次重连失败: {exc}")
                    attempt += 1
        except asyncio.CancelledError:
            pass
        finally:
            self.is_reconnecting = False


    def start_monitor(self) -> None:
        """若监控任务未在运行，则创建 `_connection_monitor_loop` 异步任务。"""
        if self._monitor_task is not None and not self._monitor_task.done():
            return
        self._monitor_task = asyncio.create_task(self._connection_monitor_loop())

    def _stop_monitor(self) -> None:
        """取消连接监控任务（若仍在运行）。"""
        if self._monitor_task and not self._monitor_task.done():
            self._monitor_task.cancel()

    async def _connection_monitor_loop(self) -> None:
        """等待首次连接就绪后，按固定间隔调用连接状态检查直至关闭或取消。"""
        while not self.is_connected and not self.is_shutting_down:
            await asyncio.sleep(2)

        self._logger.info("Discord 连接监控已启动")

        while not self.is_shutting_down:
            try:
                await asyncio.sleep(self._connection_check_interval)
                if self.is_shutting_down:
                    break
                await self._check_connection_status()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                self._logger.error(f"连接监控异常: {exc}")

        self._logger.debug("连接监控已停止")

    async def _check_connection_status(self) -> None:
        """根据就绪状态与延迟判断连接是否健康，必要时恢复标志、执行主动健康检查或触发重连。"""
        client = self.client
        if client is None:
            return

        if client.is_closed():
            if self.is_connected:
                self.is_connected = False
            return

        try:
            is_ready, latency = await asyncio.wait_for(
                self._quick_check_ready(), timeout=3.0
            )

            latency_valid = (
                latency is not None
                and latency != float("inf")
                and latency == latency  # NaN check
                and latency >= 0
            )

            if is_ready and latency_valid and latency is not None and latency < 10.0:
                if not self.is_connected:
                    self._logger.info("Discord 连接已恢复")
                    self.is_connected = True
                    if self._on_connected_callback:
                        try:
                            await self._on_connected_callback()
                        except Exception:
                            pass

                current_time = get_time()
                if current_time - self._last_health_check >= self._health_check_interval:
                    health_ok = await self._active_health_check()
                    self._last_health_check = current_time

                    if not health_ok:
                        self._health_check_failures += 1
                        self._logger.warning(
                            f"主动健康检查失败 ({self._health_check_failures}/3)"
                        )
                        if self._health_check_failures >= 3:
                            self._logger.error("检测到连接坏死，触发重连")
                            self._health_check_failures = 0
                            self.is_connected = False
                            await self.force_reconnect()
                    else:
                        self._health_check_failures = 0

            elif is_ready and (not latency_valid or (latency is not None and latency >= 10.0)):
                self._logger.warning(f"Discord 延迟异常: {latency}，等待自动恢复")
                self.is_connected = False

            else:
                if self.is_connected:
                    self.is_connected = False

        except asyncio.TimeoutError:
            self._logger.warning("连接状态检查超时")
            self.is_connected = False
        except Exception as exc:
            self._logger.error(f"检查连接状态异常: {exc}")
            self.is_connected = False

    async def _quick_check_ready(self) -> tuple[bool, Optional[float]]:
        """快速读取客户端是否就绪及当前 Gateway 延迟（毫秒级心跳延迟）。

        Returns:
            (是否就绪, 延迟秒数)；客户端不可用时返回 (False, None)。
        """
        try:
            client = self.client
            if client is None:
                return False, None
            return client.is_ready(), client.latency
        except Exception:
            return False, None

    async def _active_health_check(self) -> bool:
        """通过 `fetch_user` 验证当前登录用户是否仍可被 API 正常解析。

        Returns:
            检查通过为 True，超时或 HTTP/未知错误为 False。
        """
        try:
            if self.client and self.client.user:
                user = await asyncio.wait_for(
                    self.client.fetch_user(self.client.user.id), timeout=30.0
                )
                return user is not None
            return False
        except (asyncio.TimeoutError, discord.HTTPException, discord.NotFound):
            return False
        except Exception:
            return False
