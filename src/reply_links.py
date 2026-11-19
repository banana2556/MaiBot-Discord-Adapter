"""話題分析和聯想共用的小東西。一則訊息在接哪幾則由話題串（topics.py）決定，不再另外問。"""

# 選擇題「都不是」的選項
NONE = "none"
# 整則只有自訂表情（拿掉 <:名:ID> 和它的圖之後沒東西）時給模型看的文字（emote.strip_emotes）
EMOTE_ONLY = "（表情）"


def plain(name: str) -> str:
    """發話人名字去掉外框的「」，planner 上下文和 @ 裡的寫法才對得上。"""
    return name.strip().strip("「」")
