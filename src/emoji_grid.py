"""把候选表情拼成带序号的网格图，照 MaiBot send_emoji 的做法让视觉模型看图挑选。"""

import io
import math
from typing import List, Tuple

from PIL import Image, ImageDraw, ImageFont

_TILE = 128
_GAP = 8


def grid_shape(count: int) -> Tuple[int, int]:
    """返回接近正方形的 (行数, 列数)。"""
    columns = max(1, math.ceil(math.sqrt(count)))
    return max(1, math.ceil(count / columns)), columns


def build_numbered_grid(images: List[bytes]) -> bytes:
    """每张图缩放进白底方格，左上角标从 1 开始的序号，输出 PNG 字节。

    Args:
        images: 候选表情的图片字节，顺序即序号顺序。

    Returns:
        拼好的 PNG 图片字节。
    """
    rows, columns = grid_shape(len(images))
    step = _TILE + _GAP
    canvas = Image.new("RGB", (columns * step - _GAP, rows * step - _GAP), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=24)
    for index, data in enumerate(images):
        x, y = (index % columns) * step, (index // columns) * step
        try:
            with Image.open(io.BytesIO(data)) as raw:
                image = raw.convert("RGBA")
            image.thumbnail((_TILE, _TILE))
            canvas.paste(image, (x + (_TILE - image.width) // 2, y + (_TILE - image.height) // 2), image)
        except OSError:  # 读不了的图留白，序号照标
            pass
        draw.rounded_rectangle((x + 2, y + 2, x + 40, y + 32), radius=6, fill="black")
        draw.text((x + 8, y + 4), str(index + 1), fill="white", font=font)
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()
