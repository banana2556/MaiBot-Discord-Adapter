"""把多張圖拼成帶標籤的網格圖（影片截圖標時間點），比格子小的圖會放大填滿；動圖、影片的格子最上面加一行說明。"""

import io
import math
from typing import List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

# 視覺模型多半要求短邊至少 64px，格子取寬鬆一點
_TILE = 160
_GAP = 8
_CAPTION = 32  # 說明列高度；預設字型沒有中文字，說明用英文


def grid_shape(count: int) -> Tuple[int, int]:
    """返回接近正方形的 (行数, 列数)。"""
    columns = max(1, math.ceil(math.sqrt(count)))
    return max(1, math.ceil(count / columns)), columns


def _fit(image: Image.Image, side: int) -> Image.Image:
    """等比縮放到最長邊剛好是 side：太大縮小、太小放大（表情常只有幾十像素）。"""
    scale = side / max(image.width, image.height)
    size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


def still_image(data: bytes, frames: int = 4, tile: int = 256) -> bytes:
    """動圖（GIF、動態 PNG／WebP）平均取幾格拼成一張靜態 PNG；靜態圖原樣回傳，只有一格的 GIF 轉成 PNG。

    MaiBot 的圖片記憶只收靜態的 PNG／JPEG／WebP／BMP，送動圖會一直寫入失敗、留在佇列重試。
    """
    with Image.open(io.BytesIO(data)) as image:
        count = int(getattr(image, "n_frames", 1) or 1)
        if count <= 1 and image.format != "GIF":
            return data
        picks = sorted({round(index * (count - 1) / max(frames - 1, 1)) for index in range(min(frames, count))})
        shots = []
        for index in picks:
            image.seek(index)
            buffer = io.BytesIO()
            image.convert("RGBA").save(buffer, format="PNG")
            shots.append(buffer.getvalue())
    return shots[0] if len(shots) == 1 else animation_sheet(shots, tile)


def animation_sheet(frames: List[bytes], tile: int = 256) -> bytes:
    """動圖的幾格畫面拼成一張，最上面寫明是動圖依時間排的格子。

    MaiBot 識圖的提示詞只說「描述這張圖」，不寫明的話會當成四格漫畫，把序號和格線當成圖的內容。
    """
    return build_numbered_grid(frames, tile=tile, caption=f"Animated GIF: frames 1-{len(frames)} in time order")


def build_numbered_grid(
    images: List[bytes], labels: Optional[List[str]] = None, tile: int = _TILE, caption: str = ""
) -> bytes:
    """每张图缩放进白底方格，左上角标上标签（默认从 1 开始的序号），输出 PNG 字节。

    Args:
        images: 图片字节，顺序即格子顺序。
        labels: 每格的标签；不传时用序号。
        tile: 每格边长（像素）；比格子小的圖會放大填滿。
        caption: 最上面黑底白字的一行說明；空字串時不加。

    Returns:
        拼好的 PNG 图片字节。
    """
    labels = labels or [str(index + 1) for index in range(len(images))]
    rows, columns = grid_shape(len(images))
    step = tile + _GAP
    top = _CAPTION + _GAP if caption else 0
    canvas = Image.new("RGB", (columns * step - _GAP, top + rows * step - _GAP), "white")
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default(size=24)
    if caption:
        draw.rectangle((0, 0, canvas.width, _CAPTION), fill="black")
        draw.text((8, 6), caption, fill="white", font=ImageFont.load_default(size=20))
    for index, data in enumerate(images):
        x, y = (index % columns) * step, top + (index // columns) * step
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
