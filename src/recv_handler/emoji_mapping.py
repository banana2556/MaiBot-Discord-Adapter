"""Emoji映射模块

将Discord的emoji转换为AI可理解的中文含义
"""

# Unicode Emoji 到中文含义的映射
UNICODE_EMOJI_MAPPING = {
    # 笑脸类
    "😀": ("开心大笑", "grinning"),
    "😁": ("露齿笑", "grin"),
    "😂": ("笑哭了", "joy"),
    "🤣": ("笑翻了", "rofl"),
    "😃": ("开心", "smiley"),
    "😄": ("微笑", "smile"),
    "😅": ("尴尬笑", "sweat_smile"),
    "😆": ("眯眼笑", "laughing"),
    "😉": ("眨眼", "wink"),
    "😊": ("微笑脸红", "blush"),
    "😇": ("天使笑", "innocent"),

    # 爱心类
    "❤️": ("爱心", "heart"),
    "🧡": ("橙色心", "orange_heart"),
    "💛": ("黄色心", "yellow_heart"),
    "💚": ("绿色心", "green_heart"),
    "💙": ("蓝色心", "blue_heart"),
    "💜": ("紫色心", "purple_heart"),
    "🖤": ("黑色心", "black_heart"),
    "🤍": ("白色心", "white_heart"),
    "🤎": ("棕色心", "brown_heart"),
    "💔": ("心碎", "broken_heart"),
    "❤️‍🔥": ("燃烧的心", "heart_on_fire"),
    "💕": ("两颗心", "two_hearts"),
    "💖": ("闪亮心", "sparkling_heart"),
    "💗": ("成长的心", "heartpulse"),
    "💓": ("心跳", "heartbeat"),
    "💞": ("旋转的心", "revolving_hearts"),
    "💝": ("心形礼物", "gift_heart"),

    # 手势类
    "👍": ("赞", "thumbsup"),
    "👎": ("踩", "thumbsdown"),
    "👏": ("鼓掌", "clap"),
    "🙏": ("祈祷/感谢", "pray"),
    "🤝": ("握手", "handshake"),
    "👋": ("挥手", "wave"),
    "✌️": ("胜利", "v"),
    "🤞": ("祈愿", "crossed_fingers"),
    "🤟": ("我爱你手势", "love_you_gesture"),
    "🤘": ("摇滚手势", "metal"),
    "👌": ("OK手势", "ok_hand"),
    "👈": ("左指", "point_left"),
    "👉": ("右指", "point_right"),
    "👆": ("上指", "point_up_2"),
    "👇": ("下指", "point_down"),
    "✋": ("举手", "raised_hand"),
    "🤚": ("抬手背", "raised_back_of_hand"),
    "🖐️": ("张开手掌", "hand_splayed"),
    "💪": ("肌肉/加油", "muscle"),

    # 表情符号
    "😢": ("哭泣", "cry"),
    "😭": ("大哭", "sob"),
    "😤": ("生气", "triumph"),
    "😠": ("愤怒", "angry"),
    "😡": ("发怒", "rage"),
    "🤬": ("爆粗口", "face_with_symbols_over_mouth"),
    "😱": ("尖叫", "scream"),
    "😨": ("恐惧", "fearful"),
    "😰": ("焦虑", "cold_sweat"),
    "😥": ("失望", "disappointed_relieved"),
    "😓": ("冷汗", "sweat"),
    "🤔": ("思考", "thinking"),
    "🤨": ("挑眉", "raised_eyebrow"),
    "😐": ("面无表情", "neutral_face"),
    "😑": ("无语", "expressionless"),
    "🙄": ("翻白眼", "eye_roll"),
    "😏": ("得意", "smirk"),
    "😒": ("不爽", "unamused"),
    "😞": ("失望", "disappointed"),
    "😔": ("沉思", "pensive"),
    "😖": ("困惑", "confounded"),
    "😫": ("疲惫", "tired_face"),
    "😩": ("厌倦", "weary"),
    "🥺": ("恳求", "pleading_face"),

    # 符号类
    "✅": ("对勾/完成", "white_check_mark"),
    "❌": ("叉号/错误", "x"),
    "⭐": ("星星", "star"),
    "🌟": ("闪亮星", "star2"),
    "✨": ("闪光", "sparkles"),
    "💫": ("晕眩", "dizzy"),
    "🔥": ("火焰/热门", "fire"),
    "💯": ("满分", "100"),
    "⚡": ("闪电", "zap"),
    "💥": ("爆炸", "boom"),
    "🎉": ("庆祝", "tada"),
    "🎊": ("五彩纸屑", "confetti_ball"),
    "🎈": ("气球", "balloon"),
    "🎁": ("礼物", "gift"),
    "🏆": ("奖杯", "trophy"),
    "🥇": ("金牌", "first_place"),
    "🥈": ("银牌", "second_place"),
    "🥉": ("铜牌", "third_place"),

    # 动物类
    "🐶": ("狗", "dog"),
    "🐱": ("猫", "cat"),
    "🐭": ("鼠", "mouse"),
    "🐹": ("仓鼠", "hamster"),
    "🐰": ("兔子", "rabbit"),
    "🦊": ("狐狸", "fox"),
    "🐻": ("熊", "bear"),
    "🐼": ("熊猫", "panda_face"),
    "🐨": ("考拉", "koala"),
    "🐯": ("老虎", "tiger"),
    "🦁": ("狮子", "lion_face"),
    "🐮": ("牛", "cow"),
    "🐷": ("猪", "pig"),
    "🐸": ("青蛙", "frog"),

    # 食物类
    "🍕": ("披萨", "pizza"),
    "🍔": ("汉堡", "hamburger"),
    "🍟": ("薯条", "fries"),
    "🌭": ("热狗", "hotdog"),
    "🍿": ("爆米花", "popcorn"),
    "🍩": ("甜甜圈", "doughnut"),
    "🍪": ("饼干", "cookie"),
    "🎂": ("蛋糕", "birthday"),
    "🍰": ("蛋糕片", "cake"),
    "🧁": ("纸杯蛋糕", "cupcake"),
    "🍦": ("冰淇淋", "icecream"),
    "🍧": ("刨冰", "shaved_ice"),
    "🍨": ("冰淇淋", "ice_cream"),
    "🍫": ("巧克力", "chocolate_bar"),
    "🍬": ("糖果", "candy"),
    "🍭": ("棒棒糖", "lollipop"),

    # 活动类
    "⚽": ("足球", "soccer"),
    "🏀": ("篮球", "basketball"),
    "🎮": ("游戏", "video_game"),
    "🎯": ("靶心", "dart"),
    "🎲": ("骰子", "game_die"),
    "🎸": ("吉他", "guitar"),
    "🎹": ("钢琴", "musical_keyboard"),
    "🎤": ("麦克风", "microphone"),
    "🎧": ("耳机", "headphones"),
    "📱": ("手机", "iphone"),
    "💻": ("电脑", "computer"),
    "⌨️": ("键盘", "keyboard"),
    "🖱️": ("鼠标", "mouse_three_button"),

    # 其他常用
    "💤": ("睡觉", "zzz"),
    "💭": ("思考泡泡", "thought_balloon"),
    "💬": ("对话泡泡", "speech_balloon"),
    "👀": ("眼睛", "eyes"),
    "🧠": ("大脑", "brain"),
    "🫡": ("敬礼", "saluting_face"),
    "🤡": ("小丑", "clown"),
    "👻": ("鬼", "ghost"),
    "💀": ("骷髅", "skull"),
    "☠️": ("骷髅头", "skull_crossbones"),
}

# 自定义Emoji映射（服务器特定emoji）
CUSTOM_EMOJI_MAPPING = {
    # 这里可以添加服务器自定义emoji的映射
    # 格式: "emoji_name": ("中文含义", "emoji_name")
    # 例如: "pepe": ("佩佩蛙", "pepe")
}


def get_emoji_meaning(emoji_str: str, emoji_name: str = None) -> tuple[str, str]:
    """获取emoji的中文含义
    
    Args:
        emoji_str: emoji字符串（Unicode或自定义emoji的名称）
        emoji_name: emoji名称（Discord提供的name字段）
        
    Returns:
        tuple[str, str]: (中文含义, 英文名称)
    """
    # 优先查找Unicode emoji
    if emoji_str in UNICODE_EMOJI_MAPPING:
        return UNICODE_EMOJI_MAPPING[emoji_str]

    # 查找自定义emoji
    if emoji_name and emoji_name in CUSTOM_EMOJI_MAPPING:
        return CUSTOM_EMOJI_MAPPING[emoji_name]

    # 如果都找不到，返回原始名称
    display_name = emoji_name if emoji_name else emoji_str
    return (f"表情「{display_name}」", display_name)


def format_reaction_for_ai(emoji_str: str, emoji_name: str, user_name: str, action: str = "添加了") -> str:
    """格式化reaction信息为AI可理解的文本，保留原始表情写法以便模型用同一个表情回应

    Args:
        emoji_str: Unicode 表情字符或 <:名称:id> / <a:名称:id>
        emoji_name: 自定义表情名称；Unicode 表情为空
        user_name: 用户名
        action: 「添加了」或「移除了」

    Returns:
        str: 格式化后的文本描述
    """
    known = UNICODE_EMOJI_MAPPING.get(emoji_str) or CUSTOM_EMOJI_MAPPING.get(emoji_name)
    meaning = f"（{known[0]}）" if known else ""
    return f"用户{user_name}{action}表情回应 {emoji_str}{meaning}"
