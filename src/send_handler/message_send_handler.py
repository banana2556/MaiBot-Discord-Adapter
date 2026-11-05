"""将 MaiBot Seg 结构转换为 Discord 可接受的文本与附件。"""

import base64
import binascii
import functools
import io
import json
import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple, Union

import discord
from maim_message import Seg

from ..native_emoji import NativeEmojiRegistry

_logger = logging.getLogger("discord_adapter.content_builder")

SegmentDict = Dict[str, Any]

# split 用的分组：奇数位是网址，原样保留，免得中文路径被转坏
_URL_PATTERN = re.compile(r"(https?://\S+)")


@functools.lru_cache(maxsize=None)
def script_converter(mode: str) -> Optional[Callable[[str], str]]:
    """OpenCC 简转繁的转换函数。

    Args:
        mode: OpenCC 配置名（s2twp / s2t / s2hk），``off`` 表示不转。

    Returns:
        转换函数；不转或没安装 opencc 时返回 None（缺套件只警告一次）。
    """
    if mode == "off":
        return None
    try:
        import opencc
    except ImportError:
        _logger.warning("没有安装 opencc，发出的文字不会转繁体；请执行 pip install opencc")
        return None
    convert = opencc.OpenCC(mode).convert
    return lambda text: "".join(
        part if index % 2 else convert(part) for index, part in enumerate(_URL_PATTERN.split(text))
    )


class DiscordContentBuilder:
    """将 MaiBot 的 Seg 片段树递归解析为 Discord 可用的正文与附件列表。

    支持文本、提及、表情/图片（base64）、语音等类型；会跳过 thread_context 与嵌套
    seglist，并对无法处理的类型输出占位说明文本。来自 Discord 自定义表情/贴纸的表情
    会还原为原生表情或贴纸，其余表情作为图片附件上传。
    """

    def __init__(self, logger: Any = None, native_emoji: Optional[NativeEmojiRegistry] = None) -> None:
        """初始化内容构建器。

        Args:
            logger: 可选日志记录器；未传入时使用模块默认 logger。
            native_emoji: 可选的原生表情映射，用于把表情图片还原为 Discord 表情/贴纸。
        """

        self._logger = logger or _logger
        self._native_emoji = native_emoji
        # chat.output_script，插件每次套用配置时更新
        self.output_script = "off"

    def to_output_script(self, text: str) -> str:
        """按 output_script 把要发出去的文字转成繁体；不转时原样返回。"""
        convert = script_converter(self.output_script)
        return convert(text) if convert else text

    def build(
        self,
        message_segment: Seg,
        client: Optional[discord.Client] = None,
        channel: Any = None,
    ) -> Tuple[Optional[str], List[discord.File], List[discord.GuildSticker]]:
        """从根 Seg 递归构建 Discord 消息正文、附件与贴纸列表。

        Args:
            message_segment: MaiBot 消息根片段（可含 seglist 嵌套）。
            client: 用于查询表情/贴纸缓存的 Discord 客户端；为 None 时表情一律作为图片上传。
            channel: 目标频道，用于判断贴纸归属与外部表情权限。

        Returns:
            三元组：(正文字符串，若无有效文本则为 None)、`discord.File` 附件列表、贴纸列表。
        """
        content_parts: List[str] = []
        files: List[discord.File] = []
        stickers: List[discord.GuildSticker] = []

        def process(seg: Seg) -> None:
            if not getattr(seg, "type", None):
                return

            if seg.type == "thread_context":
                return

            if seg.type == "seglist" and isinstance(seg.data, list):
                for sub in seg.data:
                    if isinstance(sub, Seg):
                        process(sub)
                return

            if seg.type == "text":
                if seg.data is not None:
                    # 先转繁体再换 :名称:，换上去的 <:名称:id> 不会被动到
                    content_parts.append(self._render_emoji_names(self.to_output_script(str(seg.data)), channel))
                return

            if seg.type == "mention":
                mention_text = self._render_mention(seg.data)
                if mention_text:
                    content_parts.append(mention_text)
                return

            if seg.type in {"emoji", "image"}:
                if seg.type == "emoji" and isinstance(seg.data, str):
                    native = self._resolve_native_emoji(seg.data, client, channel)
                    if isinstance(native, str):
                        content_parts.append(native)
                        return
                    if native is not None:
                        stickers.append(native)
                        return
                file_item: Optional[discord.File] = None
                if isinstance(seg.data, str):
                    file_item = self._decode_image_to_attachment(seg.type, seg.data)
                if file_item:
                    files.append(file_item)
                else:
                    display = "表情" if seg.type == "emoji" else "图片"
                    content_parts.append(f"[{display}处理失败]")
                return

            if seg.type == "voice":
                voice_file = self._decode_voice(seg.data)
                if voice_file:
                    files.append(voice_file)
                else:
                    content_parts.append("[语音处理失败]")
                return

            if seg.type == "video":
                content_parts.append(f"[视频: {seg.data}]")
                return

            if seg.type == "file":
                content_parts.append(f"[文件: {seg.data}]")
                return

            if seg.type == "command":
                content_parts.append(f"[命令: {seg.data}]")
                return

            if seg.type == "notify":
                content_parts.append(f"[通知: {seg.data}]")
                return

            if seg.type == "reply":
                return

        process(message_segment)
        content_text = "\n".join(part for part in content_parts if part)
        return (content_text if content_text else None, files, stickers)

    # 前面紧贴 < 或字母数字的是已成形的 <:名称:id> / <a:名称:id>，不再处理
    _EMOJI_NAME_PATTERN = re.compile(r"(?<![<A-Za-z0-9_]):([A-Za-z0-9_]{2,32}):(?!\d)")

    @classmethod
    def _render_emoji_names(cls, text: str, channel: Any) -> str:
        """把模型写的 ``:名称:`` 换成目标服务器的原生表情；不认识的名称原样保留。"""
        guild = getattr(channel, "guild", None)
        if guild is None or ":" not in text:
            return text

        def replace(match: re.Match[str]) -> str:
            emoji = discord.utils.get(guild.emojis, name=match.group(1))
            return str(emoji) if emoji is not None and emoji.is_usable() else match.group(0)

        return cls._EMOJI_NAME_PATTERN.sub(replace, text)

    def _resolve_native_emoji(
        self, data: str, client: Optional[discord.Client], channel: Any
    ) -> Union[str, discord.GuildSticker, None]:
        """若表情图片来自 bot 当前可用的 Discord 自定义表情或贴纸，返回其原生形式。

        Args:
            data: 表情图片的 base64。
            client: 用于查询表情/贴纸缓存的 Discord 客户端。
            channel: 目标频道。

        Returns:
            自定义表情返回 ``<:name:id>`` 文本，贴纸返回 `GuildSticker`；不可用时返回 None（退回上传图片）。
        """
        if self._native_emoji is None or client is None:
            return None
        found = self._native_emoji.lookup(data)
        if found is None:
            return None
        kind, item_id = found
        guild = getattr(channel, "guild", None)

        if kind == "emoji":
            emoji = client.get_emoji(item_id)
            if emoji is None or not emoji.is_usable():
                return None
            # 跨服务器表情需要“使用外部表情”权限，否则 Discord 只显示 :name: 文字
            if (
                guild is not None
                and emoji.guild_id != guild.id
                and not channel.permissions_for(guild.me).use_external_emojis
            ):
                return None
            return str(emoji)

        # bot 只能发送目标服务器自己的贴纸
        sticker = client.get_sticker(item_id)
        if sticker is None or not sticker.available or guild is None or sticker.guild_id != guild.id:
            return None
        return sticker

    def _render_mention(self, mention_data: Any) -> str:
        """将提及载荷规范化为 Discord 可发送的 @ 文本。

        Args:
            mention_data: 用户/角色提及数据（dict 或可解析为 dict 的 JSON 字符串）。

        Returns:
            空格连接的提及片段；无法解析时返回空字符串。
        """
        normalized = self._normalize_dict_payload(mention_data)
        if normalized is None:
            return ""

        parts: List[str] = []

        for user in normalized.get("users") or []:
            user_id = user.get("user_id")
            display = user.get("display_name") or user.get("username") or "未知用户"
            parts.append(f"<@{user_id}>" if user_id else f"@{display}")

        for role in normalized.get("roles") or []:
            role_id = role.get("role_id")
            role_name = role.get("role_name") or "未知角色"
            parts.append(f"<@&{role_id}>" if role_id else f"@{role_name}")

        if normalized.get("everyone"):
            parts.append("@everyone")

        return " ".join(parts)

    def _decode_image_to_attachment(self, seg_type: str, data: str) -> Optional[discord.File]:
        """将 base64 图片或表情数据解码为带合适扩展名的 Discord 附件。

        Args:
            seg_type: 片段类型，应为 ``emoji`` 或 ``image``（影响文件名前缀）。
            data: base64 编码的字节串。

        Returns:
            成功时返回 `discord.File`；数据为空或解码失败时返回 None。
        """
        if not data:
            return None
        try:
            decoded = base64.b64decode(str(data))
        except (ValueError, TypeError, binascii.Error) as exc:
            self._logger.warning(f"解码 {seg_type} base64 数据失败：{exc}")
            return None
        suffix = self._detect_image_suffix(decoded)
        prefix = "emoji" if seg_type == "emoji" else "image"
        filename = f"{prefix}_{int(time.time())}.{suffix}"
        return discord.File(fp=io.BytesIO(decoded), filename=filename)

    def _decode_voice(self, data: Any) -> Optional[discord.File]:
        """将 base64 语音数据解码为 WAV 附件。

        Args:
            data: base64 编码的字符串；非字符串或空串不予处理。

        Returns:
            成功时返回 `discord.File`；否则返回 None。
        """
        if not isinstance(data, str) or not data:
            return None
        try:
            decoded = base64.b64decode(str(data))
        except (ValueError, TypeError, binascii.Error) as exc:
            self._logger.warning(f"解码语音失败：{exc}")
            return None
        filename = f"voice_{int(time.time())}.wav"
        return discord.File(fp=io.BytesIO(decoded), filename=filename)

    @staticmethod
    def _detect_image_suffix(image_bytes: bytes) -> str:
        """按魔数推断图片扩展名，无法识别时退回 ``bin``。

        Args:
            image_bytes: 图片文件头字节。

        Returns:
            小写扩展名；未识别时为 ``bin``。
        """
        if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
            return "png"
        if image_bytes.startswith(b"\xff\xd8\xff"):
            return "jpg"
        if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
            return "gif"
        if len(image_bytes) >= 12 and image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
            return "webp"
        if image_bytes.startswith(b"BM"):
            return "bmp"
        return "bin"

    @staticmethod
    def _normalize_dict_payload(payload: Any) -> Optional[SegmentDict]:
        """将 dict 或可解析为对象的 JSON 字符串规范化为字典。

        Args:
            payload: 字典、JSON 字符串或其它类型。

        Returns:
            字典；无法解析或顶层非对象时返回 None。
        """
        if isinstance(payload, dict):
            return payload
        if isinstance(payload, str):
            try:
                loaded = json.loads(payload)
            except json.JSONDecodeError:
                return None
            return loaded if isinstance(loaded, dict) else None
        return None
