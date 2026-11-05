"""表情向量檢索：用 MaiBot 表情庫的描述算向量，照「想表達的情緒＋相關文字」找最接近的幾個，不經過 LLM。

表情庫裡的 Discord 表情與貼圖是收訊時原樣存進去的，用圖片 sha256 就能經 ``NativeEmojiRegistry`` 對回 Discord id；
描述是 MaiBot 收錄時用視覺模型寫的。沒進過表情庫的伺服器表情只有名稱可比。
向量快取在插件資料目錄的 ``emote_vectors.json``：``{key: {"text": 描述, "vector": [...]}}``，描述變了才重算。
"""

import base64
import hashlib
import math
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from .life.common import load_state, save_state

EMBED_BATCH = 32
# 表情庫多久看一次數量有沒有變（變了才重抓整個表情庫）
REFRESH_SECONDS = 300


class EmoteLibrary:
    """MaiBot 表情庫的快取與向量排序。

    ``entries`` 每項 ``{"key", "b64", "text", "native": (kind, id) 或 None}``；
    key 在能對回 Discord 時是 ``emoji:<id>`` / ``sticker:<id>``，否則是 ``img:<sha256>``。
    """

    def __init__(self, ctx: Any, path: Optional[Path], native: Callable[[], Any]) -> None:
        """native 回傳目前的 NativeEmojiRegistry；它在 Discord 連上時才建立，所以傳取用函式。"""
        self._ctx = ctx
        self._path = path
        self._native = native
        self._vectors: Dict[str, Dict[str, Any]] = load_state(path) if path else {}
        self.entries: List[Dict[str, Any]] = []
        self._count: Optional[int] = None
        self._checked_at = 0.0

    def text_for(self, key: str) -> str:
        """表情庫裡這個 Discord 表情或貼圖的描述；沒收錄過時回傳空字串。"""
        for entry in self.entries:
            if entry["key"] == key:
                return entry["text"]
        return ""

    async def refresh(self, force: bool = False) -> None:
        """表情庫數量變了才重抓；新的描述在背景算好向量，免得第一次挑圖等很久。"""
        if not force and time.monotonic() - self._checked_at < REFRESH_SECONDS:
            return
        self._checked_at = time.monotonic()
        count = await self._ctx.call_capability("emoji.get_count")
        count = count.get("count") if isinstance(count, dict) else count
        if count == self._count and self.entries:
            return
        emojis = await self._ctx.call_capability("emoji.get_all")
        emojis = emojis.get("emojis") if isinstance(emojis, dict) else emojis
        if not isinstance(emojis, list):
            self._ctx.logger.warning(f"讀取 MaiBot 表情庫失敗：{emojis}")
            return
        entries: Dict[str, Dict[str, Any]] = {}
        for emoji in emojis:
            b64 = str((emoji or {}).get("base64") or "")
            try:
                sha = hashlib.sha256(base64.b64decode(b64)).hexdigest()
            except (ValueError, TypeError):
                continue
            registry = self._native()
            native = registry.lookup(b64) if registry is not None else None
            key = f"{native[0]}:{native[1]}" if native else f"img:{sha}"
            text = "，".join(part for part in (str(emoji.get("description") or ""), str(emoji.get("emotion") or "")) if part)
            entries[key] = {"key": key, "b64": b64, "text": text or "表情", "native": native}
        self.entries = list(entries.values())
        self._count = count
        await self.ensure_vectors([(entry["key"], entry["text"]) for entry in self.entries])

    async def ensure_vectors(self, items: Sequence[Tuple[str, str]]) -> None:
        """補算沒有向量（或描述變了）的項目；一批 EMBED_BATCH 則。"""
        # ponytail: 表情刪掉後舊向量不清，表情庫通常只有幾百張；檔案太大再在 refresh 時只留現有 key
        missing = [(key, text) for key, text in items if (self._vectors.get(key) or {}).get("text") != text]
        for start in range(0, len(missing), EMBED_BATCH):
            batch = missing[start : start + EMBED_BATCH]
            vectors = await self._embed([text for _, text in batch])
            if vectors is None:
                return
            for (key, text), vector in zip(batch, vectors, strict=True):
                self._vectors[key] = {"text": text, "vector": [round(value, 5) for value in vector]}
        if missing and self._path:
            save_state(self._path, self._vectors)

    async def rank(self, query: str, items: Sequence[Tuple[str, str]], top: int = 3) -> Optional[List[Tuple[str, float]]]:
        """照與 query 的餘弦相似度排序 items（``(key, 描述)``），回傳前 top 個 ``(key, 相似度)``。

        embedding 失敗時回傳 None，呼叫端改用別的方法挑。
        """
        await self.ensure_vectors(items)
        query_vectors = await self._embed([query])
        if not query_vectors:
            return None
        target = query_vectors[0]
        target_norm = math.sqrt(sum(value * value for value in target)) or 1.0
        scored = []
        for key, _ in items:
            vector = (self._vectors.get(key) or {}).get("vector")
            # 換過 embedding 模型時維度會不同，舊向量直接略過，下次 ensure_vectors 會重算
            if not vector or len(vector) != len(target):
                continue
            norm = math.sqrt(sum(value * value for value in vector)) or 1.0
            scored.append((key, sum(a * b for a, b in zip(target, vector, strict=True)) / (norm * target_norm)))
        scored.sort(key=lambda item: item[1], reverse=True)
        return scored[:top]

    async def _embed(self, texts: List[str]) -> Optional[List[List[float]]]:
        """用 MaiBot 的 embedding 模型算向量；失敗時記日誌回傳 None。"""
        try:
            result = await self._ctx.call_capability("llm.embed", texts=texts)
        except Exception as exc:
            self._ctx.logger.warning(f"表情向量計算失敗：{exc}")
            return None
        rows = result.get("results") if isinstance(result, dict) else None
        if not isinstance(rows, list) or len(rows) != len(texts):
            self._ctx.logger.warning(f"表情向量計算失敗：{result.get('error') if isinstance(result, dict) else result}")
            return None
        vectors = [row.get("embedding") for row in rows if isinstance(row, dict)]
        if len(vectors) != len(texts) or not all(isinstance(vector, list) and vector for vector in vectors):
            return None
        return vectors
