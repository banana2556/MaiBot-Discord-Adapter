"""记录 Discord 自定义表情与贴纸的图片哈希，供出站时还原为原生表情。

入站下载的表情/贴纸图片会原样存进 MaiBot 表情库，MaiBot 发送时回传同一份字节，
因此用图片的 sha256 即可把出站表情映射回原来的 Discord id。
"""

import base64
import hashlib
import json
import logging
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .life.common import save_state

_logger = logging.getLogger("discord_adapter.native_emoji")


class NativeEmojiRegistry:
    """图片 sha256 到 (``emoji``/``sticker``, Discord id) 的映射，可选持久化为 JSON 文件。"""

    def __init__(self, path: Optional[Path] = None, logger: Any = None) -> None:
        """加载已有映射。

        Args:
            path: 持久化文件路径；为 None 时仅保存在内存中。
            logger: 可选日志记录器；未传入时使用模块默认 logger。
        """
        self._path = path
        self._logger = logger or _logger
        self._entries: Dict[str, Tuple[str, int]] = {}
        if path is not None and path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                self._entries = {key: (str(kind), int(item_id)) for key, (kind, item_id) in raw.items()}
            except (OSError, ValueError, TypeError, AttributeError) as exc:
                self._logger.warning(f"读取原生表情映射失败，将重新记录：{exc}")

    def remember(self, image_b64: str, kind: str, item_id: int) -> None:
        """记录一张表情/贴纸图片对应的 Discord id，有变化时写回文件。

        Args:
            image_b64: 入站时下载并交给 MaiBot 的图片 base64。
            kind: ``emoji`` 或 ``sticker``。
            item_id: Discord 表情或贴纸 id。
        """
        key = self._key(image_b64)
        entry = (kind, int(item_id))
        if key is None or self._entries.get(key) == entry:
            return
        self._entries[key] = entry
        self._save()

    def lookup(self, image_b64: str) -> Optional[Tuple[str, int]]:
        """按图片查找原来的 Discord 表情或贴纸。

        Args:
            image_b64: 出站表情的图片 base64。

        Returns:
            ``(kind, id)``；未记录过或数据无效时返回 None。
        """
        key = self._key(image_b64)
        return self._entries.get(key) if key else None

    @staticmethod
    def _key(image_b64: str) -> Optional[str]:
        try:
            return hashlib.sha256(base64.b64decode(image_b64)).hexdigest()
        except (ValueError, TypeError):
            return None

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            save_state(self._path, self._entries)
        except OSError as exc:
            self._logger.warning(f"保存原生表情映射失败：{exc}")
