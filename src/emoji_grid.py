"""把多張圖拼成帶標籤的網格圖（影片截圖標時間點），比格子小的圖會放大填滿。"""

import io
import math
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# 視覺模型多半要求短邊至少 64px，格子取寬鬆一點
_TILE = 160
_GAP = 8


def grid_shape(count: int) -> Tuple[int, int]:
    """返回接近正方形的 (行数, 列数)。"""
    columns = max(1, math.ceil(math.sqrt(count)))
    return max(1, math.ceil(count / columns)), columns


def _fit(image: Image.Image, side: int) -> Image.Image:
    """等比縮放到最長邊剛好是 side：太大縮小、太小放大（表情常只有幾十像素）。"""
    scale = side / max(image.width, image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def build_numbered_grid(images: List[bytes], labels: Optional[List[str]] = None, tile: int = _TILE) -> bytes:
    """每张图缩放进白底方格，左上角标上标签（默认从 1 开始的序号），输出 PNG 字节。

    Args:
        images: 图片字节，顺序即格子顺序。
        labels: 每格的标签；不传时用序号。
        tile: 每格边长（像素）；比格子小的圖會放大填滿。

    Returns:
        拼好的 PNG 图片字节。
    """
    labels = labels or [str(index + 1) for index in range(len(images))]
    rows, columns = grid_shape(len(images))
    step = tile + _GAP
    canvas = Image.new("RGB", (columns * step - _GAP, rows * step - _GAP), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=24)
    for index, data in enumerate(images):
        x, y = (index % columns) * step, (index // columns) * step
        try:
            with Image.open(io.BytesIO(data)) as raw:
                image = _fit(raw.convert("RGBA"), tile)
            canvas.paste(image, (x + (tile - image.width) // 2, y + (tile - image.height) // 2), image)
        except OSError:  # 读不了的图留白，标签照标
            pass
        label_width = max(38, 14 + draw.textlength(labels[index], font=font))
        draw.rounded_rectangle((x + 2, y + 2, x + 2 + label_width, y + 32), radius=6, fill="black")
        draw.text((x + 8, y + 4), labels[index], fill="white", font=font)
    output = io.BytesIO()
    canvas.save(output, format="PNG")
    return output.getvalue()
