"""Discord 入站消息编解码器。

将 Discord 消息/事件转换为 Host 侧 message_dict 结构。
"""

import asyncio
import base64
import math
import re
import time
import aiohttp
import traceback
from typing import Any, Dict, List, Optional

import discord
from maim_message import BaseMessageInfo, FormatInfo, GroupInfo, MessageBase, Seg, UserInfo

from ..emoji_grid import build_numbered_grid
from ..native_emoji import NativeEmojiRegistry
from .emoji_mapping import format_reaction_for_ai, get_emoji_meaning

VIDEO_MAX_FRAMES = 9  # 3x3，再多每格就太小，识图看不清
VIDEO_FRAME_TILE = 320
VIDEO_FFMPEG_TIMEOUT_SECONDS = 8.0
# 只准 FFmpeg 走 HTTPS：伪装成影片的 HLS 播放列表可以叫它读本机文件、画进截图里
_FFMPEG_REMOTE_INPUT = ("-protocol_whitelist", "https,tls,tcp")

# MaiBot 用 [名字] 切说话人、用「名字说：」拼回复上下文，ASCII 方括号会把名字切进正文
_NAME_ESCAPES = str.maketrans({"[": "［", "]": "］", "「": "『", "」": "』"})


def quote_name(name: Optional[str]) -> Optional[str]:
    """把昵称包进「」，让模型分得清名字到哪里结束（昵称带空格或像一句话时尤其需要）。

    Args:
        name: Discord 昵称，可为空。

    Returns:
        「昵称」；空昵称返回 None。
    """
    name = (name or "").strip()
    return f"「{name.translate(_NAME_ESCAPES)}」" if name else None


class DiscordMessageHandler:
    """Discord 入站消息处理器。

    将 Discord 消息转换为 MaiBot Host 侧的 message_dict 结构。
    不直接与 gateway 交互，仅负责转换。
    """

    def __init__(
        self,
        logger: Any,
        platform_name: str,
        chat_config: Any,
        native_emoji: Optional[NativeEmojiRegistry] = None,
    ) -> None:
        """创建处理器并保存平台名与聊天相关配置。

        Args:
            logger: 日志记录器。
            platform_name: 写入 `MessageBase` 等平台字段的平台标识。
            chat_config: 聊天/子区记忆继承等行为的配置对象。
            native_emoji: 可选的原生表情映射，记录下载的表情/贴纸图片对应的 Discord id。
        """
        self._logger = logger
        self._platform_name = platform_name
        self._chat_config = chat_config
        self._native_emoji = native_emoji
        self._account_id = ""

    def set_account_id(self, account_id: str) -> None:
        """记录 bot 自身的账号 ID，作为新版路由归属写入每条入站消息。"""
        self._account_id = str(account_id or "").strip()

    def update_config(self, platform_name: str, chat_config: Any) -> None:
        """热更新平台名与聊天配置（不改变已缓存的其他状态）。

        Args:
            platform_name: 新的平台标识。
            chat_config: 新的聊天配置对象。
        """
        self._platform_name = platform_name
        self._chat_config = chat_config

    async def handle_discord_message(self, message: discord.Message) -> Optional[Dict[str, Any]]:
        """将 Discord 消息转换为 Host 侧 `message_dict`。

        Args:
            message: Discord 入站 `Message` 对象。

        Returns:
            转换成功返回字典；异常或无可序列化内容时返回 None。
        """
        try:
            maim_message = await self._convert_discord_to_maim(message)
            if maim_message is None:
                return None
            return self.build_host_message_dict(maim_message)
        except Exception as exc:
            self._logger.error(f"处理 Discord 消息时发生错误: {exc}")
            self._logger.debug(traceback.format_exc())
            return None

    async def handle_reaction_event(
        self,
        event_type: str,
        payload: discord.RawReactionActionEvent,
        client: discord.Client,
    ) -> Optional[Dict[str, Any]]:
        """将 Reaction 原始事件转换为 Host 侧 `message_dict`。

        Args:
            event_type: 事件类型（如 reaction_add / reaction_remove）。
            payload: Discord `RawReactionActionEvent` 载荷。
            client: 当前 Bot 客户端，用于拉取用户与频道信息。

        Returns:
            转换成功返回字典；异常或无法构建时返回 None。
        """
        try:
            maim_message = await self._convert_reaction_to_maim(event_type, payload, client)
            if maim_message is None:
                return None
            return self.build_host_message_dict(maim_message)
        except Exception as exc:
            self._logger.error(f"转换 {event_type} 事件时发生错误: {exc}")
            self._logger.debug(traceback.format_exc())
            return None

    def build_host_message_dict(self, maim_message: MessageBase) -> Dict[str, Any]:
        """将 `MessageBase` 转换为 Host 运行时要求的 `message_dict` 结构。"""
        message_info = maim_message.message_info
        if not isinstance(message_info, BaseMessageInfo):
            raise ValueError("MessageBase 缺少有效的 message_info")

        user_info = getattr(message_info, "user_info", None)
        user_id = str(getattr(user_info, "user_id", "") or "").strip()
        user_nickname = str(getattr(user_info, "user_nickname", "") or user_id).strip() or user_id
        user_cardname = getattr(user_info, "user_cardname", None)
        if not user_id:
            raise ValueError("MessageBase 缺少有效的 user_info.user_id")

        raw_message = self._serialize_message_segment(getattr(maim_message, "message_segment", None))
        if not raw_message:
            raw_text = self._normalize_text(getattr(maim_message, "raw_message", None))
            if raw_text:
                raw_message = [{"type": "text", "data": raw_text}]

        traits = self._analyze_segment_tree(getattr(maim_message, "message_segment", None))
        processed_plain_text = self._build_processed_plain_text(raw_message)
        if not processed_plain_text:
            processed_plain_text = self._normalize_text(getattr(maim_message, "raw_message", None))

        message_id = str(getattr(message_info, "message_id", "") or "").strip()
        if not message_id:
            message_id = f"discord-{int(time.time() * 1000)}"

        timestamp_value = getattr(message_info, "time", time.time())
        try:
            timestamp = str(float(timestamp_value))
        except (TypeError, ValueError):
            timestamp = str(float(time.time()))

        existing_additional_config = getattr(message_info, "additional_config", None)
        additional_config: Dict[str, Any] = (
            dict(existing_additional_config)
            if isinstance(existing_additional_config, dict)
            else {}
        )

        message_info_dict: Dict[str, Any] = {
            "user_info": {
                "user_id": user_id,
                "user_nickname": user_nickname,
                "user_cardname": str(user_cardname) if user_cardname else None,
            },
            "additional_config": additional_config,
        }

        group_info = getattr(message_info, "group_info", None)
        if isinstance(group_info, GroupInfo):
            group_id = str(getattr(group_info, "group_id", "") or "").strip()
            group_name = str(getattr(group_info, "group_name", "") or group_id).strip() or group_id
            if group_id:
                message_info_dict["group_info"] = {
                    "group_id": group_id,
                    "group_name": group_name,
                }
                additional_config["platform_io_target_group_id"] = group_id
        else:
            additional_config["platform_io_target_user_id"] = user_id

        message_dict: Dict[str, Any] = {
            "message_id": message_id,
            "timestamp": timestamp,
            "platform": str(getattr(message_info, "platform", "") or self._platform_name),
            # 新版路由归属：顶层 account_id 是正式字段，scope 可选。
            # 旧版把归属塞在 additional_config，MaiBot 会警告并在下个版本移除。
            "account_id": self._account_id or None,
            "message_info": message_info_dict,
            "raw_message": raw_message,
            "is_mentioned": traits["is_mentioned"],
            "is_at": traits["is_at"],
            "is_emoji": traits["is_emoji"],
            "is_picture": traits["is_picture"],
            "is_command": bool(processed_plain_text) and processed_plain_text.lstrip().startswith("/"),
            "is_notify": bool(additional_config.get("discord_notice_type")),
            "session_id": "",
            "processed_plain_text": processed_plain_text,
            "display_message": processed_plain_text,
        }

        if traits["reply_to"]:
            message_dict["reply_to"] = traits["reply_to"]

        return message_dict

    def get_thread_routing_info(self, message: discord.Message) -> Optional[Dict[str, Any]]:
        """从 Discord 消息中提取子区（线程）路由元数据，供网关更新线程上下文。

        Args:
            message: 已判定可能位于子区内的 `Message`（通常由调用方与频道类型配合使用）。

        Returns:
            在公会内且频道为子区时返回包含 `parent_channel_id`、`thread_id`、`is_thread`、`is_inherit` 的字典；
            非公会或非子区消息返回 None。
        """
        if not message.guild:
            return None
        is_thread = hasattr(message.channel, "parent") and message.channel.parent is not None
        if not is_thread:
            return None

        inherit = getattr(self._chat_config, "inherit_channel_memory", True)
        return {
            "is_thread": True,
            "parent_channel_id": str(message.channel.parent.id),
            "thread_id": str(message.channel.id),
            "is_inherit": inherit,
        }


    def _serialize_message_segment(self, segment: Any) -> List[Dict[str, Any]]:
        """将适配器内部 `Seg` 树拍平成 Host 能识别的标准消息片段列表。"""
        serialized: List[Dict[str, Any]] = []

        def walk(seg: Any) -> None:
            if not isinstance(seg, Seg) or not getattr(seg, "type", None):
                return

            if seg.type == "seglist" and isinstance(seg.data, list):
                for sub in seg.data:
                    walk(sub)
                return

            if seg.type == "text":
                text = self._normalize_text(seg.data)
                if text:
                    serialized.append({"type": "text", "data": text})
                return

            if seg.type in {"image", "emoji", "voice"}:
                binary_component = self._build_binary_component(seg.type, seg.data)
                if binary_component is not None:
                    serialized.append(binary_component)
                else:
                    placeholder_map = {
                        "image": "[图片]",
                        "emoji": "[表情]",
                        "voice": "[语音]",
                    }
                    serialized.append({"type": "text", "data": placeholder_map[seg.type]})
                return

            if seg.type == "reply":
                if isinstance(seg.data, dict) and seg.data.get("target_message_id"):
                    serialized.append({"type": "reply", "data": dict(seg.data)})
                return

            if seg.type == "video":
                serialized.append({"type": "text", "data": "[视频]"})
                return

            if seg.type == "file":
                serialized.append({"type": "text", "data": "[文件]"})
                return

        walk(segment)
        return serialized

    def _build_binary_component(self, component_type: str, data: Any) -> Optional[Dict[str, Any]]:
        """为图片/表情/语音片段构造 Host 标准二进制组件。"""
        if not isinstance(data, str) or not data:
            return None
        return {
            "type": component_type,
            "data": "",
            "binary_data_base64": data,
        }

    def _build_processed_plain_text(self, raw_message: List[Dict[str, Any]]) -> str:
        """根据标准 raw_message 片段构建可展示的纯文本内容。"""
        parts: List[str] = []
        for component in raw_message:
            if not isinstance(component, dict):
                continue
            component_type = str(component.get("type") or "").strip()
            if component_type == "text":
                parts.append(self._normalize_text(component.get("data")))
                continue
            if component_type == "image":
                parts.append("[图片]")
                continue
            if component_type == "emoji":
                parts.append("[表情]")
                continue
            if component_type == "voice":
                parts.append("[语音]")
                continue
        return "".join(part for part in parts if part).strip()

    def _analyze_segment_tree(self, segment: Any) -> Dict[str, Any]:
        """从原始 `Seg` 树中提取触发标记与回复目标等元信息。"""
        traits: Dict[str, Any] = {
            "is_mentioned": False,
            "is_at": False,
            "is_emoji": False,
            "is_picture": False,
            "reply_to": None,
        }

        def walk(seg: Any) -> None:
            if not isinstance(seg, Seg) or not getattr(seg, "type", None):
                return

            if seg.type == "seglist" and isinstance(seg.data, list):
                for sub in seg.data:
                    walk(sub)
                return

            if seg.type == "mention" and isinstance(seg.data, dict):
                if seg.data.get("users") or seg.data.get("everyone") or seg.data.get("here"):
                    traits["is_mentioned"] = True
                    traits["is_at"] = True
                return

            if seg.type == "image":
                traits["is_picture"] = True
                return

            if seg.type == "emoji":
                traits["is_emoji"] = True
                return

            if seg.type == "reply" and traits["reply_to"] is None and isinstance(seg.data, dict):
                traits["reply_to"] = seg.data.get("target_message_id") or None

        walk(segment)
        return traits

    @staticmethod
    def _normalize_text(value: Any) -> str:
        """将任意值规整为字符串；空值返回空串。"""
        if value is None:
            return ""
        normalized = str(value)
        return normalized if normalized else ""


    _EMOJI_FETCH_LIMIT = 5 * 1024 * 1024


    @staticmethod
    def _is_complete_image(data: bytes) -> bool:
        """确认下载到的是完整图片，拒绝截断或非图片内容。"""
        if data.startswith(b"\x89PNG\r\n\x1a\n"):
            return data.endswith(b"IEND\xaeB`\x82")
        if data.startswith(b"\xff\xd8"):
            return data.endswith(b"\xff\xd9")
        if data.startswith((b"GIF87a", b"GIF89a")):
            return data.endswith(b"\x00;")
        if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
            return True
        return False

    async def _fetch_emoji_bytes(self, url: str, size: Optional[int] = 48) -> Optional[str]:
        """下載自訂表情、貼圖或嵌入預覽圖，回傳 base64；超過大小上限或失敗時回傳 None。

        ``size`` 為 None 時不加尺寸參數（非表情 CDN 的圖片）。
        """
        try:
            timeout = aiohttp.ClientTimeout(total=10)
            # Discord CDN 的表情與貼圖支援 ?size= 指定邊長（16 到 4096 的 2 的冪）。
            # 收到的表情維持 48px 不要改：MaiBot 表情庫用圖片雜湊去重，改尺寸會把每個表情再收一次；
            # 低於 64px 時 MaiBot 識別前會自己整數倍放大，只是會印一行提示。
            request_url = url if size is None else url + (("&" if "?" in url else "?") + f"size={size}")
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.get(request_url) as resp:
                    if resp.status != 200:
                        return None
                    # read(n) 只读一次，不一定读满；用 readexactly 读完整个 body，
                    # 否则存下来的图片是截断的，视觉模型会报 image file is truncated。
                    data = await resp.content.readexactly(resp.content_length) if resp.content_length else await resp.read()
            if not data or len(data) > self._EMOJI_FETCH_LIMIT:
                return None
            if not self._is_complete_image(data):
                return None
            return base64.b64encode(data).decode("utf-8")
        except (aiohttp.ClientError, TimeoutError, OSError) as exc:
            self._logger.debug(f"下载表情或贴纸失败: {exc}")
            return None

    def _build_chat_context(
        self, message: discord.Message
    ) -> tuple[Optional[GroupInfo], Dict[str, Any], Optional[Dict[str, Any]]]:
        """按消息所在频道构造 `GroupInfo`、路由用的 additional_config，以及继承子区记忆时的子区路由信息。

        Args:
            message: Discord `Message`。

        Returns:
            `(GroupInfo 或 None（私聊）, additional_config, thread_context 数据或 None)`。
        """
        group_info = None
        additional_config: Dict[str, Any] = {"platform_io_target_channel_id": str(message.channel.id)}
        thread_context: Optional[Dict[str, Any]] = None
        if not message.guild:
            return group_info, additional_config, thread_context

        is_thread = hasattr(message.channel, "parent") and message.channel.parent is not None
        if is_thread:
            thread_name = message.channel.name
            thread_id = str(message.channel.id)
            parent_channel_id = str(message.channel.parent.id)
            parent_channel_name = (
                message.channel.parent.name
                if hasattr(message.channel.parent, "name")
                else f"频道{message.channel.parent.id}"
            )
            inherit = getattr(self._chat_config, "inherit_channel_memory", True)
            additional_config.update(
                {
                    "discord_is_thread": True,
                    "discord_inherit_channel_memory": inherit,
                    "thread_id": thread_id,
                    "original_thread_id": thread_id,
                    "parent_channel_id": parent_channel_id,
                    "discord_thread_name": thread_name or "",
                }
            )

            if inherit:
                group_name = f"{parent_channel_name} @ {message.guild.name}"
                group_info = GroupInfo(
                    platform=self._platform_name,
                    group_id=parent_channel_id,
                    group_name=f"[当前子区: {thread_name}] {group_name}",
                )
                thread_context = {
                    "original_thread_id": thread_id,
                    "thread_name": thread_name,
                    "parent_channel_id": parent_channel_id,
                    "inherit_memory": True,
                }
            else:
                group_info = GroupInfo(
                    platform=self._platform_name,
                    group_id=thread_id,
                    group_name=f"{thread_name} [{parent_channel_name}] @ {message.guild.name}",
                )
            return group_info, additional_config, thread_context

        channel_name = message.channel.name if hasattr(message.channel, "name") else f"频道{message.channel.id}"
        group_info = GroupInfo(
            platform=self._platform_name,
            group_id=str(message.channel.id),
            group_name=f"{channel_name} @ {message.guild.name}",
        )
        if isinstance(message.channel, discord.VoiceChannel):
            additional_config["discord_channel_type"] = "voice"
        elif isinstance(message.channel, discord.StageChannel):
            additional_config["discord_channel_type"] = "stage"
        elif isinstance(message.channel, discord.TextChannel):
            additional_config["discord_channel_type"] = "text"
        return group_info, additional_config, thread_context

    async def _convert_discord_to_maim(self, message: discord.Message) -> Optional[MessageBase]:
        """将单条 Discord 消息装配为 `MessageBase`（用户、群组、分段、引用与线程上下文）。

        Args:
            message: Discord `Message`。

        Returns:
            成功时返回 `MessageBase`；无有效分段或出错时返回 None。
        """
        try:
            user_info = self._build_user_info(message.author)
            group_info, additional_config, thread_context = self._build_chat_context(message)
            # 只有 @ 到 bot 自己才算（回复她的消息且开着提醒也算）；is_at 是有 @ 任何人都会标
            if any(str(user.id) == self._account_id for user in message.mentions):
                additional_config["discord_mentions_bot"] = True

            message_segments: List[Seg] = []
            mentions_info = self._process_mentions(message)
            if mentions_info:
                message_segments.append(Seg(type="mention", data=mentions_info))
            message_segments.extend(await self._build_content_segments(message, message))

            reference = message.reference
            if reference and reference.type == discord.MessageReferenceType.forward:
                # 转发的消息本身没有正文，内容都在快照里
                for snapshot in message.message_snapshots:
                    snapshot_segments = await self._build_content_segments(snapshot)
                    if snapshot_segments:
                        message_segments.append(Seg(type="text", data="[转发的消息]"))
                        message_segments.extend(snapshot_segments)
            elif reference and reference.message_id:
                # 引用内容放进 reply 片段，由 MaiBot 渲染成「[回复了X的消息: ...]」；
                # 不再往正文插前缀，否则存库后的正文会被下一次引用层层叠加
                reply_data = await self._get_reply_context(
                    message.channel, reference.message_id, reference.cached_message
                )
                message_segments.insert(0, Seg(type="reply", data=reply_data))

            if not message_segments:
                return None
            content_formats = list(dict.fromkeys(seg.type for seg in message_segments))
            if thread_context:
                message_segments.append(Seg(type="thread_context", data=thread_context))

            format_info = FormatInfo(
                content_format=content_formats,
                accept_format=["text", "image", "emoji", "reply", "voice", "command", "file", "video"],
            )

            message_info = BaseMessageInfo(
                platform=self._platform_name,
                message_id=str(message.id),
                time=message.created_at.timestamp(),
                user_info=user_info,
                group_info=group_info,
                format_info=format_info,
                additional_config=additional_config,
            )

            if len(message_segments) == 1:
                message_segment = message_segments[0]
            else:
                message_segment = Seg(type="seglist", data=message_segments)

            return MessageBase(
                message_info=message_info,
                message_segment=message_segment,
                raw_message=message.content or "",
            )

        except Exception as exc:
            self._logger.error(f"转换 Discord 消息时发生错误: {exc}")
            return None

    async def _build_content_segments(self, source: Any, message: Optional[discord.Message] = None) -> List[Seg]:
        """把正文、附件、贴纸、嵌入内容和投票转成分段。

        Args:
            source: `Message`，或转发消息里的 `MessageSnapshot`。
            message: 用来把 ``<@id>`` 换成名字的原消息；快照只有原始 ID，不传。

        Returns:
            按出现顺序排好的 `Seg` 列表，可能为空。
        """
        segments: List[Seg] = []
        if source.content:
            segments.extend(await self._process_text_with_emojis(source.content, message))

        for attachment in source.attachments:
            segments.extend(await self._attachment_segments(attachment))

        for sticker in source.stickers:
            # 图片在 MaiBot 里只显示为「[表情包: 描述]」，附上名称让模型分得清是贴纸
            segments.append(Seg(type="text", data=f"[贴纸: {sticker.name}]"))
            sticker_url = getattr(sticker, "url", None)
            sticker_b64 = await self._fetch_emoji_bytes(str(sticker_url)) if sticker_url else None
            if sticker_b64:
                if self._native_emoji is not None:
                    self._native_emoji.remember(sticker_b64, "sticker", sticker.id)
                segments.append(Seg(type="emoji", data=sticker_b64))

        for embed in source.embeds:
            segments.extend(await self._embed_segments(embed))

        poll = getattr(source, "poll", None)
        if poll is not None:
            answers = " / ".join(answer.text or "" for answer in poll.answers)
            mode = "，可多选" if poll.multiple else ""
            segments.append(Seg(type="text", data=f"[投票: {poll.question}｜选项: {answers}{mode}]"))
        return segments

    async def _attachment_segments(self, attachment: discord.Attachment) -> List[Seg]:
        """图片和语音消息读成二进制分段（语音由 MaiBot 转写），影片截几格画面拼成一张图，其余附件只留类型和文件名。"""
        content_type = attachment.content_type or ""
        if content_type.startswith("video/"):
            return await self._video_segments(attachment)
        if content_type.startswith("image/"):
            kind = "image"
        elif attachment.is_voice_message():
            kind = "voice"
        else:
            label = "音频" if content_type.startswith("audio/") else "文件"
            return [Seg(type="text", data=f"[{label}: {attachment.filename}]")]

        try:
            data = await attachment.read()
        except (discord.HTTPException, discord.NotFound, OSError) as exc:
            self._logger.error(f"读取附件 {attachment.filename} 失败: {exc}")
            return []
        if kind == "voice":
            data = await self._voice_message_to_wav(data)
        return [Seg(type=kind, data=base64.b64encode(data).decode("utf-8"))]

    async def _video_segments(self, attachment: discord.Attachment) -> List[Seg]:
        """每隔固定秒数截一格画面，拼成一张带时间标签的图交给识图；做不到时只留文件名。

        FFmpeg 直接读 Discord CDN 的网址，靠 HTTP Range 只取开头的索引和每个时间点附近的片段，不整个下载。
        """
        label = Seg(type="text", data=f"[视频: {attachment.filename}]")
        interval = float(getattr(self._chat_config, "video_frame_interval_sec", 2.0) or 0)
        if interval <= 0:
            return [label]
        probe = await self._run_ffmpeg(
            "ffprobe", "-v", "error", *_FFMPEG_REMOTE_INPUT, "-show_entries", "format=duration",
            "-of", "default=nw=1:nk=1", attachment.url,
        )
        try:
            duration = float(probe.decode().strip())
        except ValueError:
            return [label]
        count = max(1, math.ceil(duration / interval))  # 不截贴着结尾的那格，那里常常读不到画面
        if count > VIDEO_MAX_FRAMES:
            count, interval = VIDEO_MAX_FRAMES, duration / VIDEO_MAX_FRAMES  # 太长就平均分布
        times = [index * interval for index in range(count)]
        frames = await asyncio.gather(*(self._video_frame(attachment.url, at) for at in times))
        shots = [(at, frame) for at, frame in zip(times, frames, strict=True) if frame]
        if not shots:
            return [label]
        sheet = await asyncio.to_thread(
            build_numbered_grid,
            [frame for _, frame in shots],
            [f"{int(at) // 60}:{int(at) % 60:02d}" for at, _ in shots],
            VIDEO_FRAME_TILE,
        )
        return [
            Seg(
                type="text",
                data=f"[视频: {attachment.filename}，约 {duration:.0f} 秒；下图是每 {round(interval, 1):g} 秒一格的画面，左上角是时间点]",
            ),
            Seg(type="image", data=base64.b64encode(sheet).decode("utf-8")),
        ]

    async def _video_frame(self, url: str, at: float) -> bytes:
        """取影片某个时间点的一格画面（JPEG）；-ss 放在 -i 前面，FFmpeg 会直接跳过去读，不从头读起。"""
        return await self._run_ffmpeg(
            "ffmpeg", "-v", "error", *_FFMPEG_REMOTE_INPUT, "-ss", f"{at:.2f}", "-i", url,
            "-frames:v", "1", "-vf", f"scale={VIDEO_FRAME_TILE}:{VIDEO_FRAME_TILE}:force_original_aspect_ratio=decrease",
            "-f", "image2pipe", "-c:v", "mjpeg", "pipe:1",
        )

    async def _run_ffmpeg(self, *args: str) -> bytes:
        """跑 FFmpeg/FFprobe 取标准输出；没装、失败或超时都返回空字节。"""
        try:
            process = await asyncio.create_subprocess_exec(
                *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
            )
        except (FileNotFoundError, NotImplementedError) as exc:
            self._logger.warning(f"无法启动 {args[0]}，影片只留文件名: {exc}")
            return b""
        try:
            stdout, stderr = await asyncio.wait_for(process.communicate(), VIDEO_FFMPEG_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            process.kill()
            await process.wait()
            self._logger.warning(f"{args[0]} 处理影片超时")
            return b""
        if process.returncode != 0:
            self._logger.debug(f"{args[0]} 处理影片失败: {stderr.decode(errors='ignore')[-200:]}")
            return b""
        return stdout

    async def _voice_message_to_wav(self, data: bytes) -> bytes:
        """Discord 语音消息是 ogg/opus，MaiBot 转写时一律当 audio.wav 上传，先转成 16kHz 单声道 wav。

        没有 FFmpeg 或转换失败时原样交出，由转写服务自己判断能不能认。
        """
        try:
            process = await asyncio.create_subprocess_exec(
                "ffmpeg", "-i", "pipe:0", "-f", "wav", "-ar", "16000", "-ac", "1", "pipe:1",
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            wav, stderr = await process.communicate(data)
        except (FileNotFoundError, NotImplementedError) as exc:
            self._logger.warning(f"无法启动 FFmpeg 转换语音消息，原样交给转写: {exc}")
            return data
        if process.returncode != 0 or not wav:
            self._logger.warning(f"FFmpeg 转换语音消息失败，原样交给转写: {stderr.decode(errors='ignore')[-200:]}")
            return data
        return wav

    _EMBED_TEXT_LIMIT = 300

    async def _embed_segments(self, embed: discord.Embed) -> List[Seg]:
        """动图（Tenor/Giphy）和图片链接只有预览图，当成表情/图片给模型看；
        其余取标题、描述和字段转成文字，卡片带图（X、Pixiv、新闻缩图等）时也附上图片。"""
        if embed.type in ("gifv", "image"):
            preview = embed.thumbnail.proxy_url or embed.thumbnail.url or embed.image.proxy_url or embed.image.url
            image_b64 = await self._fetch_emoji_bytes(str(preview), size=None) if preview else None
            return [Seg(type="emoji" if embed.type == "gifv" else "image", data=image_b64)] if image_b64 else []

        segments: List[Seg] = []
        text_segment = self._embed_text_segment(embed)
        if text_segment is not None:
            segments.append(text_segment)
        picture = embed.image.proxy_url or embed.image.url or embed.thumbnail.proxy_url or embed.thumbnail.url
        picture_b64 = await self._fetch_emoji_bytes(str(picture), size=None) if picture else None
        if picture_b64:
            segments.append(Seg(type="image", data=picture_b64))
        return segments

    def _embed_text_segment(self, embed: discord.Embed) -> Optional[Seg]:
        """把卡片的标题、描述和字段压成一段文字；没有文字时返回 None。"""
        lines = [
            embed.provider.name,
            embed.author.name,
            embed.title,
            embed.description,
            *(f"{field.name}: {field.value}" for field in embed.fields),
            embed.footer.text,
        ]
        text = "\n".join(str(line) for line in lines if line)
        if not text:
            return None
        if len(text) > self._EMBED_TEXT_LIMIT:
            text = text[: self._EMBED_TEXT_LIMIT] + "..."
        label = "卡片" if embed.type == "rich" else "链接预览"
        return Seg(type="text", data=f"[{label}: {text}]")

    @staticmethod
    def _summarize(message: Any, limit: int = 100) -> str:
        """给引用和编辑、撤回通知用的一行摘要；没有正文时说明是转发、贴纸、投票还是卡片。"""
        text = message.content or ""
        if not text:
            snapshots = getattr(message, "message_snapshots", None) or []
            embeds = getattr(message, "embeds", None) or []
            stickers = getattr(message, "stickers", None) or []
            poll = getattr(message, "poll", None)
            if snapshots:
                text = f"[转发的消息] {snapshots[0].content}".strip()
            elif stickers:
                text = f"[贴纸: {stickers[0].name}]"
            elif poll is not None:
                text = f"[投票: {poll.question}]"
            elif embeds and (embeds[0].title or embeds[0].description):
                text = f"[卡片: {embeds[0].title or embeds[0].description}]"
            else:
                text = "[无文本内容]"
        if len(text) > limit:
            text = text[:limit] + "..."
        if message.attachments:
            text += f"[包含{len(message.attachments)}个附件]"
        return text

    async def handle_message_change(
        self, message: discord.Message, before: Optional[discord.Message] = None
    ) -> Optional[Dict[str, Any]]:
        """把消息编辑（传 ``before``）或撤回转成通知消息。

        通知标成 ``is_notify``：planner 看得到内容变了，replyer 不会把它当成聊天记录。

        Args:
            message: 编辑后的消息，或被撤回的消息。
            before: 编辑前的消息；为 None 时表示撤回。

        Returns:
            Host 侧 `message_dict`；出错时返回 None。
        """
        try:
            group_info, additional_config, _ = self._build_chat_context(message)
            name = quote_name(message.author.display_name)
            if before is None:
                notice = "message_delete"
                text = f"[事件-消息撤回] {name}撤回了消息：{self._summarize(message)}"
            else:
                notice = "message_edit"
                text = f"[事件-消息编辑] {name}把消息「{self._summarize(before)}」改成了：{self._summarize(message, 300)}"
            additional_config["discord_notice_type"] = notice
            message_info = BaseMessageInfo(
                platform=self._platform_name,
                message_id=f"{notice}_{message.id}_{int(time.time() * 1000)}",
                time=time.time(),
                user_info=self._build_user_info(message.author),
                group_info=group_info,
                format_info=FormatInfo(content_format=["text"], accept_format=["text"]),
                additional_config=additional_config,
            )
            return self.build_host_message_dict(
                MessageBase(message_info=message_info, message_segment=Seg(type="text", data=text), raw_message=text)
            )
        except Exception as exc:
            self._logger.error(f"转换消息编辑/撤回事件时发生错误: {exc}")
            self._logger.debug(traceback.format_exc())
            return None

    def _build_user_info(self, author: discord.User | discord.Member) -> UserInfo:
        """从作者对象构造 Host `UserInfo`（展示名与服务器内昵称）。

        Args:
            author: 消息作者，可为 `User` 或 `Member`。

        Returns:
            包含平台、用户 ID、昵称与服务器卡片的 `UserInfo`。
        """
        return UserInfo(
            platform=self._platform_name,
            user_id=str(author.id),
            user_nickname=quote_name(author.display_name),
            user_cardname=quote_name(getattr(author, "nick", None)),
        )

    def _process_mentions(self, message: discord.Message) -> Optional[Dict[str, Any]]:
        """汇总消息中的 @用户、@角色、@频道及 @everyone/@here 信息为结构化字典。

        Args:
            message: 含 mentions 的 Discord `Message`。

        Returns:
            存在任一提及信息时返回结构化字典，否则返回 None。
        """
        mentions_data: Dict[str, Any] = {}

        if message.mentions:
            users = []
            for user in message.mentions:
                users.append({
                    "user_id": str(user.id),
                    "username": user.name,
                    "display_name": user.display_name,
                    "global_name": getattr(user, "global_name", None),
                    "server_nickname": getattr(user, "nick", None),
                    "is_bot": user.bot,
                    "discriminator": getattr(user, "discriminator", None),
                })
            mentions_data["users"] = users

        if message.role_mentions:
            roles = []
            for role in message.role_mentions:
                roles.append({
                    "role_id": str(role.id),
                    "role_name": role.name,
                    "color": str(role.color),
                    "mentionable": role.mentionable,
                })
            mentions_data["roles"] = roles

        if hasattr(message, "channel_mentions") and message.channel_mentions:
            channels = []
            for channel in message.channel_mentions:
                channels.append({
                    "channel_id": str(channel.id),
                    "channel_name": channel.name,
                    "channel_type": str(channel.type),
                })
            mentions_data["channels"] = channels

        if "@everyone" in message.content or "@here" in message.content:
            mentions_data["everyone"] = "@everyone" in message.content
            mentions_data["here"] = "@here" in message.content

        return mentions_data if mentions_data else None

    async def _process_text_with_emojis(
        self, text: str, message: Optional[discord.Message] = None
    ) -> List[Seg]:
        """将原始文本中的 Discord 提及占位符替换为可读形式，再拆分为文本/表情分段。

        Args:
            text: 消息正文字符串。
            message: 若提供，则用于解析用户/角色/频道提及以替换 `<@...>` 等。

        Returns:
            文本与表情拆分后的 `Seg` 列表。
        """
        processed_text = text
        if message and message.mentions:
            for user in message.mentions:
                for pattern in (f"<@!{user.id}>", f"<@{user.id}>"):
                    if pattern in processed_text:
                        server_nick = getattr(user, "nick", None)
                        global_name = getattr(user, "global_name", None)
                        display = server_nick or global_name or user.display_name
                        processed_text = processed_text.replace(pattern, f"@{quote_name(display)}")

        if message and message.role_mentions:
            for role in message.role_mentions:
                role_pattern = f"<@&{role.id}>"
                if role_pattern in processed_text:
                    processed_text = processed_text.replace(role_pattern, f"@{role.name}")

        if message and hasattr(message, "channel_mentions"):
            for channel in message.channel_mentions:
                channel_pattern = f"<#{channel.id}>"
                if channel_pattern in processed_text:
                    processed_text = processed_text.replace(channel_pattern, f"#{channel.name}")

        return await self._process_emoji_text(processed_text)

    async def _process_emoji_text(self, text: str) -> List[Seg]:
        """按 Discord 自定义表情语法与 Unicode 表情规则将正文拆成多个 `Seg`（以文本为主）。

        Args:
            text: 已处理提及占位符后的正文。

        Returns:
            按自定义表情切分后的 `Seg` 列表；无表情时为单段文本。
        """
        unicode_emoji_pattern = re.compile(
            "["
            "\U0001F600-\U0001F64F"
            "\U0001F300-\U0001F5FF"
            "\U0001F680-\U0001F6FF"
            "\U0001F1E0-\U0001F1FF"
            "\U00002702-\U000027B0"
            "\U000024C2-\U0001F251"
            "]+",
            flags=re.UNICODE,
        )
        discord_custom_emoji_pattern = re.compile(r"<a?:(\w+):(\d+)>")

        has_unicode = bool(unicode_emoji_pattern.search(text))
        has_custom = bool(discord_custom_emoji_pattern.search(text))

        if not has_unicode and not has_custom:
            return [Seg(type="text", data=text)]

        segments: List[Seg] = []
        current_pos = 0

        for match in discord_custom_emoji_pattern.finditer(text):
            if match.start() > current_pos:
                before = text[current_pos : match.start()]
                if before.strip():
                    segments.append(Seg(type="text", data=before))

            emoji_id = match.group(2)
            is_animated = text[match.start() : match.end()].startswith("<a:")
            # 图片在 MaiBot 里只显示为「[表情包: 描述]」；保留原始 <:名称:id>，模型可原样写进回复
            # （Discord 直接渲染）或用来回应
            segments.append(Seg(type="text", data=match.group(0)))
            # 动态表情的 .gif 端点常返回 415，Discord CDN 对同一 id 的 .png 始终给静态帧，
            # 所以先试标记对应的后缀，失败再退回另一种。
            primary = "gif" if is_animated else "png"
            emoji_b64 = None
            for ext in (primary, "png" if primary == "gif" else "gif"):
                emoji_b64 = await self._fetch_emoji_bytes(
                    f"https://cdn.discordapp.com/emojis/{emoji_id}.{ext}"
                )
                if emoji_b64:
                    break
            if emoji_b64:
                if self._native_emoji is not None:
                    self._native_emoji.remember(emoji_b64, "emoji", emoji_id)
                segments.append(Seg(type="emoji", data=emoji_b64))
            current_pos = match.end()

        if current_pos < len(text):
            remaining = text[current_pos:]
            if remaining.strip():
                segments.append(Seg(type="text", data=remaining))

        if has_unicode and not has_custom and not segments:
            segments.append(Seg(type="text", data=text))

        return segments if segments else [Seg(type="text", data=text)]

    async def _get_reply_context(
        self, channel: Any, message_id: int, cached: Optional[discord.Message] = None
    ) -> Dict[str, Any]:
        """拉取被引用（或被回应）的消息，构造 Host reply 片段数据（作者与截断正文、附件提示）。

        Args:
            channel: 目标消息所在频道，需支持 `fetch_message`。
            message_id: 目标消息 ID。
            cached: 已缓存的目标消息，有则不再请求。

        Returns:
            至少含 `target_message_id` 的字典；拉取失败时只有 ID，由 MaiBot 自行按 ID 查库。
        """
        reply_data: Dict[str, Any] = {"target_message_id": str(message_id)}
        try:
            referenced_message = cached or await channel.fetch_message(message_id)
        except Exception as exc:  # 拉不到引用不能连累整条消息
            self._logger.warning(f"拉取被回复消息失败: {exc}")
            return reply_data

        author = referenced_message.author
        reply_data.update(
            target_message_content=self._summarize(referenced_message),
            target_message_sender_id=str(author.id),
            target_message_sender_nickname=quote_name(author.display_name),
            target_message_sender_cardname=quote_name(getattr(author, "nick", None)),
        )
        return reply_data

    async def _convert_reaction_to_maim(
        self,
        event_type: str,
        payload: discord.RawReactionActionEvent,
        client: discord.Client,
    ) -> Optional[MessageBase]:
        """将 Reaction 事件装配为含文本描述与 `reaction_event` 分段的 `MessageBase`。

        Args:
            event_type: reaction_add 或 reaction_remove 等。
            payload: 原始 Reaction 事件载荷。
            client: Bot 客户端，用于解析用户与频道/线程信息。

        Returns:
            成功返回 `MessageBase`；无法解析用户或异常时返回 None。
        """
        try:
            user, member = await self._resolve_reaction_user(payload, client)
            if not user:
                self._logger.error(f"无法获取用户 {payload.user_id} 的信息")
                return None

            user_display_name = getattr(user, "display_name", None) or user.name
            server_nickname = getattr(member, "nick", None) if member else None

            user_info = UserInfo(
                platform=self._platform_name,
                user_id=str(user.id),
                user_nickname=quote_name(user_display_name),
                user_cardname=quote_name(server_nickname),
            )

            group_info = None
            is_thread = False
            thread_name = None

            if payload.guild_id:
                group_info, is_thread, thread_name = await self._build_reaction_group_info(
                    payload, client
                )

            emoji = payload.emoji
            # str() 给出可直接用于 discord_react 的写法：Unicode 字符或 <:名称:id> / <a:名称:id>
            emoji_str = str(emoji)
            emoji_name = None if emoji.is_unicode_emoji() else emoji.name

            emoji_meaning, emoji_display = get_emoji_meaning(emoji_str, emoji_name or "")
            action_text = "添加了" if event_type == "reaction_add" else "移除了"
            description = format_reaction_for_ai(emoji_str, emoji_name or "", quote_name(user_display_name), action_text)

            # 被回应的消息挂成 reply 片段，模型能看到「[引用消息]msg_id」，可直接拿去 discord_react
            reacted_message = discord.utils.get(client.cached_messages, id=payload.message_id)
            reply_data = await self._get_reply_context(
                client.get_partial_messageable(payload.channel_id), payload.message_id, reacted_message
            )
            message_segments: List[Seg] = [Seg(type="reply", data=reply_data), Seg(type="text", data=description)]

            reaction_metadata = {
                "event_type": event_type,
                "action": "add" if event_type == "reaction_add" else "remove",
                "user_id": str(payload.user_id),
                "user_name": user_display_name,
                "message_id": str(payload.message_id),
                "channel_id": str(payload.channel_id),
                "guild_id": str(payload.guild_id) if payload.guild_id else None,
                "emoji": emoji_str,
                "emoji_name": emoji_name,
                "emoji_display": emoji_display,
                "emoji_meaning": emoji_meaning,
                "is_thread": is_thread,
                "thread_name": thread_name if is_thread else None,
            }
            message_segments.append(Seg(type="reaction_event", data=reaction_metadata))

            format_info = FormatInfo(
                content_format=["reply", "text", "reaction_event"],
                accept_format=["text", "image", "emoji", "reply", "voice", "command", "file", "video", "reaction"],
            )

            timestamp = int(time.time() * 1000)
            unique_id = f"reaction_{payload.message_id}_{payload.user_id}_{event_type}_{timestamp}"

            message_info = BaseMessageInfo(
                platform=self._platform_name,
                message_id=unique_id,
                time=time.time(),
                user_info=user_info,
                group_info=group_info,
                format_info=format_info,
            )

            if len(message_segments) == 1:
                message_segment = message_segments[0]
            else:
                message_segment = Seg(type="seglist", data=message_segments)

            return MessageBase(
                message_info=message_info,
                message_segment=message_segment,
                raw_message=description,
            )

        except Exception as exc:
            self._logger.error(f"转换 reaction 事件时发生错误: {exc}")
            self._logger.debug(traceback.format_exc())
            return None

    async def _resolve_reaction_user(
        self, payload: discord.RawReactionActionEvent, client: discord.Client
    ) -> tuple[Optional[discord.User], Optional[discord.Member]]:
        """从载荷与缓存中解析操作 Reaction 的用户及公会内 `Member`（若存在）。

        Args:
            payload: 原始 Reaction 事件。
            client: 用于 `get_guild` / `fetch_member` / `fetch_user` 的客户端。

        Returns:
            `(User 或 None, Member 或 None)`；公会场景下优先填充 Member。
        """
        user: Optional[discord.User] = None
        member: Optional[discord.Member] = None

        if payload.member:
            member = payload.member
            user = payload.member
        else:
            guild = client.get_guild(payload.guild_id) if payload.guild_id else None
            if guild:
                member = guild.get_member(payload.user_id)
                if not member:
                    try:
                        member = await guild.fetch_member(payload.user_id)
                    except (discord.NotFound, discord.HTTPException):
                        pass
                if member:
                    user = member

            if not user:
                user = client.get_user(payload.user_id)
                if not user:
                    try:
                        user = await client.fetch_user(payload.user_id)
                    except (discord.NotFound, discord.HTTPException):
                        pass

        return user, member

    async def _build_reaction_group_info(
        self, payload: discord.RawReactionActionEvent, client: discord.Client
    ) -> tuple[Optional[GroupInfo], bool, Optional[str]]:
        """根据频道 ID 解析 `GroupInfo`，并判断是否在子区及子区名称。

        Args:
            payload: 含 `channel_id` / `guild_id` 的 Reaction 载荷。
            client: 用于 `get_channel` / `fetch_channel` / `get_guild` 的客户端。

        Returns:
            `(GroupInfo, 是否为子区, 子区名称或 None)`；频道不可见时仍返回基于 ID 构造的 `GroupInfo`。
        """
        is_thread = False
        thread_name = None

        channel = client.get_channel(payload.channel_id)
        if not channel:
            try:
                channel = await client.fetch_channel(payload.channel_id)
            except (discord.NotFound, discord.Forbidden, discord.HTTPException):
                channel = None

        guild = client.get_guild(payload.guild_id) if payload.guild_id else None
        guild_name = guild.name if guild else None

        if channel and isinstance(channel, discord.Thread):
            is_thread = True
            thread_name = channel.name
            parent_channel = getattr(channel, "parent", None)
            inherit = getattr(self._chat_config, "inherit_channel_memory", True)

            if parent_channel and inherit:
                channel_name = parent_channel.name
                group_id = str(parent_channel.id)
                group_name = f"[当前子区: {thread_name}] {channel_name}"
                if guild_name:
                    group_name += f" @ {guild_name}"
            else:
                group_id = str(channel.id)
                group_name = thread_name or ""
                if parent_channel:
                    group_name += f" [{parent_channel.name}]"
                if guild_name:
                    group_name += f" @ {guild_name}"
        elif channel:
            channel_name = channel.name if hasattr(channel, "name") else f"频道{channel.id}"
            group_id = str(channel.id)
            group_name = channel_name
            if guild_name:
                group_name += f" @ {guild_name}"
        else:
            group_id = str(payload.channel_id)
            group_name = f"频道{payload.channel_id}"
            if guild_name:
                group_name += f" @ {guild_name}"

        group_info = GroupInfo(
            platform=self._platform_name,
            group_id=group_id,
            group_name=group_name,
        )
        return group_info, is_thread, thread_name
