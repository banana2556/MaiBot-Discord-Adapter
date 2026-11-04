"""Discord 适配器配置模型。"""

from typing import Any, ClassVar, Dict, List, Literal, Mapping

from maibot_sdk import Field, PluginConfigBase
from pydantic import field_validator

from .constants import (
    DEFAULT_CHAT_LIST_TYPE,
    DEFAULT_CONNECTION_CHECK_INTERVAL_SEC,
    DEFAULT_PLATFORM_NAME,
    DEFAULT_RETRY_DELAY_SEC,
    SUPPORTED_CONFIG_VERSION,
)


class DiscordPluginOptions(PluginConfigBase):
    """插件级配置。"""

    __ui_label__: ClassVar[str] = "插件设置"
    __ui_order__: ClassVar[int] = 0

    enabled: bool = Field(
        default=False,
        description="是否启用 Discord 适配器。",
        json_schema_extra={
            "hint": "关闭后插件保持空闲，不会建立 Discord 连接。",
            "label": "启用适配器",
            "order": 0,
        },
    )
    config_version: str = Field(
        default=SUPPORTED_CONFIG_VERSION,
        description="当前配置结构版本。",
        json_schema_extra={
            "disabled": True,
            "hidden": True,
            "label": "配置版本",
            "order": 99,
        },
    )

    def should_connect(self) -> bool:
        """是否应根据当前插件开关尝试建立 Discord 连接。

        Returns:
            bool: ``enabled`` 为 True 时返回 True。
        """
        return self.enabled

    @field_validator("config_version", mode="before")
    @classmethod
    def _normalize_config_version(cls, value: Any) -> str:
        normalized = _normalize_string(value)
        return normalized or SUPPORTED_CONFIG_VERSION


class DiscordConnectionConfig(PluginConfigBase):
    """Discord Bot 连接配置。

    Bot Token 由插件配置提供，可在自动生成的 ``config.toml`` 或 WebUI 中手动填写。
    """

    __ui_label__: ClassVar[str] = "Discord 连接"
    __ui_order__: ClassVar[int] = 1

    token: str = Field(
        default="",
        description="Discord Bot Token。",
        json_schema_extra={
            "hint": "在 Discord Developer Portal 的 Bot 页面复制 Token 并粘贴到这里，仅保存在本地配置中，不要提交到仓库。",
            "input_type": "password",
            "label": "Discord Bot Token",
            "order": 0,
            "placeholder": "请输入 Discord Bot Token",
        },
    )
    intent_messages: bool = Field(
        default=True,
        description="服务器消息权限（Guild Messages，仅控制服务器内频道/子区消息）。",
        json_schema_extra={
            "hint": "对应 discord.py 的 guild_messages，不包含私信消息。",
            "label": "服务器消息权限",
            "order": 1,
        },
    )
    intent_guilds: bool = Field(
        default=True,
        description="服务器权限（获取服务器信息）。",
        json_schema_extra={"label": "服务器权限", "order": 2},
    )
    intent_dm_messages: bool = Field(
        default=True,
        description="私信消息权限（Direct Messages，仅控制私聊消息）。",
        json_schema_extra={
            "hint": "对应 discord.py 的 dm_messages，与服务器消息权限分开控制。",
            "label": "私信消息权限",
            "order": 3,
        },
    )
    intent_message_content: bool = Field(
        default=True,
        description="消息内容权限（必须启用，否则无法读取消息内容）。",
        json_schema_extra={"label": "消息内容权限", "order": 4},
    )
    intent_voice_states: bool = Field(
        default=False,
        description="语音状态权限（语音功能必须启用）。",
        json_schema_extra={
            "label": "语音状态权限",
            "order": 5,
            "hint": "自动进出语音频道、语音状态监听和 STT 都依赖这个 intent；只做纯文本收发时可以关闭。",
        },
    )
    retry_delay: int = Field(
        default=DEFAULT_RETRY_DELAY_SEC,
        description="断线重试间隔（秒）。",
        json_schema_extra={
            "hint": "Discord 连接断开后等待该时长再尝试重连。",
            "label": "重试间隔（秒）",
            "order": 6,
            "step": 1,
        },
    )
    connection_check_interval: int = Field(
        default=DEFAULT_CONNECTION_CHECK_INTERVAL_SEC,
        description="连接状态检查间隔（秒，建议 30 秒以上）。",
        json_schema_extra={
            "hint": "定期检查 Discord 连接是否存活。",
            "label": "连接检查间隔（秒）",
            "order": 7,
            "step": 1,
        },
    )

    @field_validator("token", mode="before")
    @classmethod
    def _normalize_token(cls, value: Any) -> str:
        return _normalize_string(value)

    @field_validator("retry_delay", "connection_check_interval", mode="before")
    @classmethod
    def _normalize_positive_int(cls, value: Any) -> int:
        if isinstance(value, int) and value > 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
            if parsed > 0:
                return parsed
        return DEFAULT_RETRY_DELAY_SEC


class DiscordChatConfig(PluginConfigBase):
    """聊天名单过滤配置。"""

    __ui_label__: ClassVar[str] = "聊天过滤"
    __ui_order__: ClassVar[int] = 2

    guild_list_type: Literal["whitelist", "blacklist"] = Field(
        default=DEFAULT_CHAT_LIST_TYPE,
        description="服务器名单模式。",
        json_schema_extra={
            "hint": "白名单模式仅允许列表内服务器，黑名单模式屏蔽列表内服务器。",
            "label": "服务器名单模式",
            "order": 0,
        },
    )
    guild_list: List[str] = Field(
        default_factory=list,
        description="服务器 ID 名单。",
        json_schema_extra={
            "hint": "右键服务器图标 -> 复制服务器 ID。",
            "label": "服务器名单",
            "order": 1,
            "placeholder": "请输入服务器 ID",
        },
    )
    channel_list_type: Literal["whitelist", "blacklist"] = Field(
        default=DEFAULT_CHAT_LIST_TYPE,
        description="频道名单模式。",
        json_schema_extra={
            "hint": "白名单模式仅允许列表内频道，黑名单模式屏蔽列表内频道。",
            "label": "频道名单模式",
            "order": 2,
        },
    )
    channel_list: List[str] = Field(
        default_factory=list,
        description="频道 ID 名单。",
        json_schema_extra={
            "hint": "右键频道名 -> 复制频道 ID。",
            "label": "频道名单",
            "order": 3,
            "placeholder": "请输入频道 ID",
        },
    )
    thread_list_type: Literal["whitelist", "blacklist"] = Field(
        default=DEFAULT_CHAT_LIST_TYPE,
        description="子区名单模式（继承频道权限时此项无效）。",
        json_schema_extra={
            "label": "子区名单模式",
            "order": 4,
        },
    )
    thread_list: List[str] = Field(
        default_factory=list,
        description="子区 ID 名单。",
        json_schema_extra={
            "label": "子区名单",
            "order": 5,
            "placeholder": "请输入子区 ID",
        },
    )
    user_list_type: Literal["whitelist", "blacklist"] = Field(
        default=DEFAULT_CHAT_LIST_TYPE,
        description="用户名单模式。",
        json_schema_extra={
            "label": "用户名单模式",
            "order": 6,
        },
    )
    user_list: List[str] = Field(
        default_factory=list,
        description="用户 ID 名单。",
        json_schema_extra={
            "label": "用户名单",
            "order": 7,
            "placeholder": "请输入用户 ID",
        },
    )
    allow_thread_interaction: bool = Field(
        default=True,
        description="是否允许在子区（Thread）中互动。",
        json_schema_extra={
            "label": "允许子区互动",
            "order": 8,
        },
    )
    inherit_channel_permissions: bool = Field(
        default=True,
        description="子区是否继承父频道权限（推荐开启）。",
        json_schema_extra={
            "hint": "开启后父频道允许则子区允许，子区名单将被忽略。",
            "label": "继承频道权限",
            "order": 9,
        },
    )
    inherit_channel_memory: bool = Field(
        default=True,
        description="子区是否继承父频道记忆。",
        json_schema_extra={
            "hint": "开启后子区与父频道共享上下文记忆；关闭则各自独立。",
            "label": "继承频道记忆",
            "order": 10,
        },
    )
    show_typing_indicator: bool = Field(
        default=True,
        description="Maisaka 真正开始向模型发起规划请求前，是否显示 Discord 的“正在输入”状态。",
        json_schema_extra={
            "hint": "开启后，适配器会先记录 Discord 会话目标，只在 MaiBot 进入 Maisaka planner 请求前启动 typing，直到发出首条回复或内部超时结束。",
            "label": "显示正在输入",
            "order": 11,
        },
    )
    typing_indicator_delay_ms: int = Field(
        default=0,
        description="显示“正在输入”前的延迟时间（毫秒）。",
        json_schema_extra={
            "hint": "避免极快回复也闪一下 typing；只有处理时间超过这个阈值时才会真正显示。",
            "label": "输入提示延迟(ms)",
            "order": 12,
            "step": 100,
            "depends_on": "chat.show_typing_indicator",
            "depends_value": True,
        },
    )
    typing_indicator_timeout_sec: int = Field(
        default=5,
        description="“正在输入”状态的最长保持时间（秒），设为 0 表示直到回复发出前都不主动超时。",
        json_schema_extra={
            "hint": "建议设得比常见 LLM 请求耗时更长；设为 0 时仅在成功发送、失败或断线时结束。",
            "label": "输入提示超时(秒)",
            "order": 13,
            "step": 1,
            "depends_on": "chat.show_typing_indicator",
            "depends_value": True,
        },
    )
    emote_chance: float = Field(
        default=0.15,
        ge=0,
        le=1,
        description="每次思考时，有这个机率提醒麦麦可以顺手用服务器表情按 reaction 或发表情；0 表示不提醒。",
        json_schema_extra={
            "hint": "只是提醒，最后发不发仍由麦麦判断。不提醒时她几乎只会在被要求时才用表情。",
            "label": "主动表情机率",
            "order": 14,
            "step": 0.05,
        },
    )

    @field_validator("guild_list_type", "channel_list_type", "thread_list_type", "user_list_type", mode="before")
    @classmethod
    def _normalize_list_types(cls, value: Any) -> Literal["whitelist", "blacklist"]:
        normalized = _normalize_string(value)
        if normalized in ("whitelist", "blacklist"):
            return normalized  # type: ignore[return-value]
        return DEFAULT_CHAT_LIST_TYPE  # type: ignore[return-value]

    @field_validator("typing_indicator_delay_ms", "typing_indicator_timeout_sec", mode="before")
    @classmethod
    def _normalize_non_negative_int(cls, value: Any) -> int:
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.strip().isdigit():
            return int(value.strip())
        return 0

    @field_validator("guild_list", "channel_list", "thread_list", "user_list", mode="before")
    @classmethod
    def _normalize_id_lists(cls, value: Any) -> List[str]:
        return _normalize_string_list(value)


class DiscordPlatformConfig(PluginConfigBase):
    """平台标识配置。"""

    __ui_label__: ClassVar[str] = "平台设置"
    __ui_order__: ClassVar[int] = 3

    platform_name: str = Field(
        default=DEFAULT_PLATFORM_NAME,
        description="平台标识符（多实例运行时需唯一）。",
        json_schema_extra={
            "hint": "用于在 MaiBot 中区分不同的 Discord 实例。",
            "label": "平台名称",
            "order": 0,
            "placeholder": "discord",
        },
    )

    @field_validator("platform_name", mode="before")
    @classmethod
    def _normalize_platform_name(cls, value: Any) -> str:
        normalized = _normalize_string(value)
        return normalized or DEFAULT_PLATFORM_NAME


class DiscordFilterConfig(PluginConfigBase):
    """消息过滤配置。"""

    __ui_label__: ClassVar[str] = "消息过滤"
    __ui_order__: ClassVar[int] = 4

    ignore_self_message: bool = Field(
        default=True,
        description="是否忽略机器人自身发送的消息。",
        json_schema_extra={
            "hint": "建议保持开启，避免机器人处理自己刚刚发出的消息。",
            "label": "忽略自身消息",
            "order": 0,
        },
    )
    ignore_bot_message: bool = Field(
        default=True,
        description="是否忽略其他机器人发送的消息。",
        json_schema_extra={
            "hint": "开启后将忽略所有 Bot 用户的消息。",
            "label": "忽略机器人消息",
            "order": 1,
        },
    )


class DiscordVoiceConfig(PluginConfigBase):
    """语音功能配置。开关语音或切换 TTS / STT 提供商后，保存并刷新页面即可只显示所选服务商的设置。"""

    __ui_label__: ClassVar[str] = "语音功能"
    __ui_order__: ClassVar[int] = 6

    enabled: bool = Field(
        default=False,
        description="是否启用语音功能。",
        json_schema_extra={
            "label": "启用语音",
            "order": 0,
            "hint": "关闭时不会创建语音管理器，也不会主动加入任何语音频道；TTS / STT 服务商设置也会一并隐藏。",
        },
    )
    voice_mode: Literal["fixed", "auto"] = Field(
        default="auto",
        description="语音频道模式：fixed=固定频道, auto=自动跟随。",
        json_schema_extra={
            "label": "频道模式",
            "hint": "fixed: 指定一个频道常驻；auto: 有人进入时自动加入，无人时退出。",
            "order": 1,
        },
    )
    fixed_channel_id: str = Field(
        default="",
        description="固定模式下的语音频道 ID。",
        json_schema_extra={
            "label": "固定频道 ID",
            "hint": "填写真正的语音频道 ID，而不是频道分类 ID。",
            "order": 2,
            "depends_on": "voice.voice_mode",
            "depends_value": "fixed",
            "placeholder": "频道 ID",
        },
    )
    auto_channel_list: List[str] = Field(
        default_factory=list,
        description="自动模式下的候选语音频道 ID 列表。",
        json_schema_extra={
            "label": "候选频道列表",
            "hint": "只会在这里列出的语音频道里自动进出；无效 ID 会在运行时被忽略。",
            "order": 3,
            "depends_on": "voice.voice_mode",
            "depends_value": "auto",
            "placeholder": "请输入频道 ID",
        },
    )
    idle_timeout_sec: int = Field(
        default=300,
        description="自动模式下无人后等待退出的秒数。",
        json_schema_extra={
            "label": "空闲退出超时（秒）",
            "order": 4,
            "depends_on": "voice.voice_mode",
            "depends_value": "auto",
            "step": 30,
        },
    )
    tts_provider: Literal["siliconflow", "gptsovits", "minimax", "fishaudio"] = Field(
        default="siliconflow",
        description="TTS 语音合成服务提供商。",
        json_schema_extra={
            "label": "TTS 提供商",
            "hint": "切换后保存并刷新页面，下方会换成对应服务商的设置。",
            "order": 5,
        },
    )
    stt_provider: Literal["siliconflow_sensevoice", "aliyun", "tencent"] = Field(
        default="siliconflow_sensevoice",
        description="STT 语音识别服务提供商。",
        json_schema_extra={
            "label": "STT 提供商",
            "hint": "切换后保存并刷新页面，下方会换成对应服务商的设置。",
            "order": 6,
        },
    )
    enable_vad: bool = Field(
        default=True,
        description="是否启用基于音量的语音活动检测（VAD）。",
        json_schema_extra={
            "label": "启用音量 VAD",
            "hint": "开启后同时支持麦克风开关检测和音量阈值检测；关闭则仅依赖麦克风开关。",
            "order": 7,
        },
    )
    vad_threshold_db: float = Field(
        default=-50.0,
        description="VAD 音量阈值（dB），高于此值视为正在说话。范围约 -60（灵敏）~ -30（严格）。",
        json_schema_extra={
            "label": "VAD 阈值（dB）",
            "hint": "-60 很灵敏易误触，-50 默认平衡，-30 需大声说话。",
            "order": 8,
            "step": 1,
        },
    )
    vad_deactivation_delay_ms: int = Field(
        default=500,
        description="VAD 关闭延迟（毫秒），低于阈值后等待此时长才判定停止说话，避免断句。",
        json_schema_extra={
            "label": "VAD 关闭延迟（ms）",
            "hint": "越大尾巴越长越自然，越小切得越快但容易断句。",
            "order": 9,
            "step": 50,
        },
    )
    send_text_in_voice: bool = Field(
        default=False,
        description="TTS 播报时是否同时发送文字到语音频道文字区域（调试用）。",
        json_schema_extra={"label": "同步发送文字（调试）", "order": 10},
    )

    @field_validator("auto_channel_list", mode="before")
    @classmethod
    def _normalize_channel_list(cls, value: Any) -> List[str]:
        return _normalize_string_list(value)


# 服务商配置节的显示条件：WebUI 不处理字段级 depends_on，
# 因此由插件在生成 Schema 时按已保存配置整节移除未选用的服务商。
# 每项为 (配置路径, 期望值)，全部满足时才显示该节。
UIVisibleWhen = tuple[tuple[str, Any], ...]


class SiliconFlowTTSConfig(PluginConfigBase):
    """SiliconFlow TTS 配置。"""

    __ui_label__: ClassVar[str] = "SiliconFlow TTS"
    __ui_order__: ClassVar[int] = 7
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (("voice.enabled", True), ("voice.tts_provider", "siliconflow"))

    api_key: str = Field(
        default="",
        description="SiliconFlow API 密钥。",
        json_schema_extra={"label": "API Key", "input_type": "password", "order": 0},
    )
    api_base: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="SiliconFlow API 地址。",
        json_schema_extra={"label": "API 地址", "order": 1},
    )
    model: str = Field(
        default="fnlp/MOSS-TTSD-v0.5",
        description="TTS 模型标识。",
        json_schema_extra={"label": "模型", "order": 2},
    )
    voice: str = Field(
        default="fnlp/MOSS-TTSD-v0.5:alex",
        description="TTS 音色标识。",
        json_schema_extra={"label": "音色", "order": 3},
    )
    sample_rate: int = Field(
        default=32000,
        description="音频采样率。SiliconFlow 官方约束：opus 只支持 48000；wav/pcm 支持 8000/16000/24000/32000/44100；mp3 支持 32000/44100。",
        json_schema_extra={
            "label": "采样率", "order": 4,
            "hint": "推荐搭配 wav 使用 32000 或 44100；不要再把 pcm/wav 配成 48000。",
        },
    )
    speed: float = Field(
        default=1.0,
        description="语速（0.1 ~ 2.0）。",
        json_schema_extra={"label": "语速", "order": 5},
    )
    response_format: str = Field(
        default="wav",
        description="音频返回格式（mp3/opus/wav/pcm）。",
        json_schema_extra={
            "label": "返回格式", "order": 6,
            "hint": "推荐默认用 wav；只有确实需要时再手动改成 pcm/mp3/opus。",
        },
    )


class GPTSoVITSConfig(PluginConfigBase):
    """GPT-SoVITS TTS 配置。"""

    __ui_label__: ClassVar[str] = "GPT-SoVITS TTS"
    __ui_order__: ClassVar[int] = 8
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (("voice.enabled", True), ("voice.tts_provider", "gptsovits"))

    api_base: str = Field(
        default="http://127.0.0.1:8000",
        description="GPT-SoVITS API 地址。",
        json_schema_extra={"label": "API 地址", "order": 0},
    )
    version: str = Field(
        default="v4",
        description="GPT-SoVITS 服务版本号。",
        json_schema_extra={
            "label": "服务版本",
            "hint": "填写当前 GSV 服务实际支持的版本，例如 v4。这里保留为可扩展文本，后续服务端新增版本时可直接改这里。",
            "order": 1,
            "placeholder": "v4",
        },
    )
    model: str = Field(
        default="",
        description="模板模型名；用于 infer_single 模板模型接口。",
        json_schema_extra={
            "label": "模板模型名",
            "hint": "配置页会优先从本地 GSV `/models/{version}` 拉取模板模型供选择；拉不到时也可以手动填写。不要填写 GSVI-v4 这类 OpenAI 兼容模型 ID。",
            "order": 2,
            "placeholder": "明日方舟-中文-Mon3tr",
        },
    )
    voice: str = Field(
        default="",
        description="模板模型情感/音色（可选）；用于 infer_single 模板模型接口。",
        json_schema_extra={
            "label": "模板情感/音色（可选）",
            "hint": "这是可选项。配置页会优先根据本地 GSV 模板模型列表拉取可选情感/音色；留空则自动选择。",
            "order": 3,
            "placeholder": "留空自动选择",
        },
    )
    text_lang: str = Field(
        default="zh",
        description="合成语言代码。",
        json_schema_extra={"label": "文本语言", "order": 4},
    )
    response_format: str = Field(
        default="wav",
        description="期望的音频格式。",
        json_schema_extra={
            "label": "音频格式",
            "hint": "推荐 wav；请按 infer_single 接口和服务端实际支持情况填写。",
            "order": 5,
        },
    )
    speed_factor: float = Field(
        default=1.0,
        description="语速因子。",
        json_schema_extra={"label": "语速", "order": 6},
    )


class MiniMaxTTSConfig(PluginConfigBase):
    """MiniMax TTS 配置。"""

    __ui_label__: ClassVar[str] = "MiniMax TTS"
    __ui_order__: ClassVar[int] = 9
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (("voice.enabled", True), ("voice.tts_provider", "minimax"))

    api_key: str = Field(
        default="",
        description="MiniMax API 密钥。",
        json_schema_extra={"label": "API Key", "input_type": "password", "order": 0},
    )
    api_base: str = Field(
        default="https://api.minimax.io",
        description="MiniMax API 地址。",
        json_schema_extra={"label": "API 地址", "order": 1},
    )
    model: str = Field(
        default="speech-2.8-hd",
        description="TTS 模型。",
        json_schema_extra={"label": "模型", "order": 2},
    )
    voice_id: str = Field(
        default="male-qn-qingse",
        description="音色 ID。",
        json_schema_extra={"label": "音色 ID", "order": 3},
    )
    speed: float = Field(
        default=1.0,
        description="语速（0.5 ~ 2.0）。",
        json_schema_extra={"label": "语速", "order": 4},
    )
    vol: float = Field(
        default=1.0,
        description="音量（0.1 ~ 2.0）。",
        json_schema_extra={"label": "音量", "order": 5},
    )
    pitch: float = Field(
        default=0.0,
        description="音调偏移（-12 ~ 12）。",
        json_schema_extra={"label": "音调", "order": 6},
    )
    audio_sample_rate: int = Field(
        default=32000,
        description="输出采样率，将映射到 MiniMax audio_setting.sample_rate。",
        json_schema_extra={
            "label": "采样率", "order": 7,
            "hint": "官方可选值为 8000/16000/22050/24000/32000/44100，默认推荐 32000。",
        },
    )
    output_format: str = Field(
        default="mp3",
        description="音频编码格式，将映射到 MiniMax audio_setting.format（pcm/mp3/flac/wav）。",
        json_schema_extra={
            "label": "音频格式", "order": 8,
            "hint": "插件内部会固定请求 hex 响应，便于直接解码；这里控制的是生成音频本身的格式。",
        },
    )


class FishAudioTTSConfig(PluginConfigBase):
    """Fish Audio TTS 配置。"""

    __ui_label__: ClassVar[str] = "Fish Audio TTS"
    __ui_order__: ClassVar[int] = 10
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (("voice.enabled", True), ("voice.tts_provider", "fishaudio"))

    api_key: str = Field(
        default="",
        description="Fish Audio API 密钥。",
        json_schema_extra={
            "label": "API Key", "input_type": "password", "order": 0,
            "hint": "在 fish.audio 控制台的 API Keys 页面创建。",
        },
    )
    api_base: str = Field(
        default="https://api.fish.audio",
        description="Fish Audio API 地址。",
        json_schema_extra={"label": "API 地址", "order": 1},
    )
    model: str = Field(
        default="s2.1-pro",
        description="TTS 模型，通过请求头 model 传给 Fish Audio。",
        json_schema_extra={
            "label": "模型", "order": 2,
            "hint": "可选 s2.1-pro / s2.1-pro-free（免费开发者额度）/ s2-pro / s1 / drama-3；填错时服务端会回退到 s2.1-pro。",
        },
    )
    reference_id: str = Field(
        default="",
        description="音色模型 ID（reference_id）。",
        json_schema_extra={
            "label": "音色模型 ID", "order": 3,
            "hint": "打开 fish.audio 上的音色页面，网址最后那串 ID 就是；留空则使用服务端默认音色。",
            "placeholder": "例如 802e3bc2b27e49c2995d23ef70e6ac89",
        },
    )
    response_format: str = Field(
        default="wav",
        description="音频格式（wav/mp3/pcm）。",
        json_schema_extra={
            "label": "音频格式", "order": 4,
            "hint": "推荐 wav；pcm 会在插件内包装成 wav 后播放。",
        },
    )
    sample_rate: int = Field(
        default=44100,
        description="采样率。wav/pcm 支持 8000/16000/24000/32000/44100；mp3 支持 32000/44100。",
        json_schema_extra={"label": "采样率", "order": 5},
    )
    speed: float = Field(
        default=1.0,
        description="语速（0.5 ~ 2.0）。",
        json_schema_extra={"label": "语速", "order": 6, "step": 0.1},
    )
    volume: float = Field(
        default=0.0,
        description="音量增益（dB），正数更大声，负数更小声。",
        json_schema_extra={"label": "音量（dB）", "order": 7, "step": 1},
    )
    temperature: float = Field(
        default=0.7,
        description="采样温度（0 ~ 1），越高越有变化。",
        json_schema_extra={"label": "Temperature", "order": 8, "step": 0.05},
    )
    top_p: float = Field(
        default=0.7,
        description="Top-p 采样（0 ~ 1）。",
        json_schema_extra={"label": "Top P", "order": 9, "step": 0.05},
    )
    latency: str = Field(
        default="normal",
        description="延迟模式（low/normal/balanced）。",
        json_schema_extra={
            "label": "延迟模式", "order": 10,
            "hint": "normal 音质最稳；balanced / low 出声更快但可能略降音质。",
        },
    )
    normalize: bool = Field(
        default=True,
        description="是否让服务端把数字、日期、单位等转成读法。",
        json_schema_extra={"label": "文本规范化", "order": 11},
    )

    @field_validator("api_key", "reference_id", mode="before")
    @classmethod
    def _normalize_ids(cls, value: Any) -> str:
        return _normalize_string(value)


class SiliconFlowSTTConfig(PluginConfigBase):
    """SiliconFlow STT (SenseVoice) 配置。"""

    __ui_label__: ClassVar[str] = "SiliconFlow STT"
    __ui_order__: ClassVar[int] = 11
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (
        ("voice.enabled", True),
        ("voice.stt_provider", "siliconflow_sensevoice"),
    )

    api_key: str = Field(
        default="",
        description="SiliconFlow API 密钥。",
        json_schema_extra={"label": "API Key", "input_type": "password", "order": 0},
    )
    api_base: str = Field(
        default="https://api.siliconflow.cn/v1",
        description="SiliconFlow API 地址。",
        json_schema_extra={"label": "API 地址", "order": 1},
    )
    model: str = Field(
        default="FunAudioLLM/SenseVoiceSmall",
        description="STT 模型标识。",
        json_schema_extra={"label": "模型", "order": 2},
    )


class AliyunSTTConfig(PluginConfigBase):
    """阿里云 STT 配置。"""

    __ui_label__: ClassVar[str] = "阿里云语音识别"
    __ui_order__: ClassVar[int] = 12
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (("voice.enabled", True), ("voice.stt_provider", "aliyun"))

    access_key_id: str = Field(
        default="",
        description="阿里云 AccessKey ID。",
        json_schema_extra={"label": "AccessKey ID", "order": 0},
    )
    access_key_secret: str = Field(
        default="",
        description="阿里云 AccessKey Secret。",
        json_schema_extra={"label": "AccessKey Secret", "input_type": "password", "order": 1},
    )
    app_key: str = Field(
        default="",
        description="智能语音交互项目 App Key。",
        json_schema_extra={"label": "App Key", "order": 2},
    )
    region: str = Field(
        default="cn-shanghai",
        description="服务区域。",
        json_schema_extra={"label": "区域", "order": 3},
    )


class TencentSTTConfig(PluginConfigBase):
    """腾讯云 STT 配置。"""

    __ui_label__: ClassVar[str] = "腾讯云语音识别"
    __ui_order__: ClassVar[int] = 13
    __ui_visible_when__: ClassVar[UIVisibleWhen] = (("voice.enabled", True), ("voice.stt_provider", "tencent"))

    secret_id: str = Field(
        default="",
        description="腾讯云 SecretId。",
        json_schema_extra={"label": "SecretId", "order": 0},
    )
    secret_key: str = Field(
        default="",
        description="腾讯云 SecretKey。",
        json_schema_extra={"label": "SecretKey", "input_type": "password", "order": 1},
    )
    engine: str = Field(
        default="16k_zh",
        description="识别引擎类型。",
        json_schema_extra={"label": "引擎", "order": 2},
    )
    region: str = Field(
        default="ap-shanghai",
        description="服务区域。",
        json_schema_extra={"label": "区域", "order": 3},
    )


class ReminderConfig(PluginConfigBase):
    """定时提醒（原 luca.reminder 插件）。启用前请停用旧的定时提醒插件，提醒会自动搬过来。"""

    __ui_label__: ClassVar[str] = "定时提醒"
    __ui_order__: ClassVar[int] = 20

    enabled: bool = Field(
        default=True,
        description="允许麦麦设定、查看、取消提醒；关闭后麦麦看不到提醒工具，已设的提醒也暂停触发。",
        json_schema_extra={"label": "启用定时提醒", "order": 0},
    )
    drift_minutes: int = Field(
        default=1,
        ge=0,
        description="提醒飘移（分钟）：麦麦思考和回复需要时间，提前这么多分钟开始对话。",
        json_schema_extra={"label": "提前唤醒（分钟）", "order": 1, "step": 1},
    )
    max_per_chat: int = Field(
        default=10,
        ge=0,
        description="每个聊天同时待触发、有人请麦麦设的提醒数上限。",
        json_schema_extra={"label": "每个聊天提醒上限", "order": 2, "step": 1},
    )
    followup_enabled: bool = Field(
        default=True,
        description="允许麦麦顺手记下群友 24 小时内的安排，到时关心一下；关闭后麦麦看不到这个工具。",
        json_schema_extra={"label": "顺手记下", "order": 3},
    )
    followup_max_per_chat: int = Field(
        default=3,
        ge=0,
        description="每个聊天同时待触发的顺手记下数上限。",
        json_schema_extra={"label": "每个聊天顺手记下上限", "order": 4, "step": 1},
    )


class LifeConfig(PluginConfigBase):
    """日常生活：每天生成麦麦每小时的生活时程，定期按聊天调整，当前状态会注入对话并同步到 Discord 状态。"""

    __ui_label__: ClassVar[str] = "日常生活"
    __ui_order__: ClassVar[int] = 21

    enabled: bool = Field(
        default=False,
        description="是否启用日常生活时程。",
        json_schema_extra={"label": "启用日常生活", "order": 0, "hint": "会用到 LLM：每天生成一次，之后每隔一段时间调整一次。"},
    )
    life_setting: str = Field(
        default="",
        description="补充人设里没写的生活背景，例如身分、住哪、作息、爱好。",
        json_schema_extra={
            "label": "生活设定",
            "order": 1,
            "x-widget": "textarea",
            "placeholder": "例如：住台北的大学生，晚睡晚起，喜欢打音游和逛超商",
        },
    )
    generate_time: str = Field(
        default="00:00",
        description="每天生成当天时程的时间（HH:MM）；生成前沿用前一天同一时段。",
        json_schema_extra={"label": "每日生成时间", "order": 2, "placeholder": "00:00"},
    )
    adjust_interval_hours: int = Field(
        default=3,
        ge=0,
        description="每隔几小时根据最近的聊天调整之后的时段，0 表示不调整。",
        json_schema_extra={"label": "调整间隔（小时）", "order": 3, "step": 1},
    )
    sync_presence: bool = Field(
        default=True,
        description="换时段时把当前活动同步成 Discord 状态文字与在线状态。",
        json_schema_extra={"label": "同步 Discord 状态", "order": 4},
    )
    inject_prompt: bool = Field(
        default=True,
        description="把当前生活状态加进麦麦的对话上下文，让她聊天时知道自己正在做什么。",
        json_schema_extra={"label": "注入对话上下文", "order": 5},
    )
    model_task: Literal["replyer", "planner"] = Field(
        default="replyer",
        description="生成与调整时程沿用 MaiBot 哪个模型任务：replyer=回复模型, planner=规划模型。",
        json_schema_extra={"label": "使用模型", "order": 6},
    )

    @field_validator("generate_time", mode="before")
    @classmethod
    def _normalize_generate_time(cls, value: Any) -> str:
        return _normalize_string(value) or "00:00"


class TopicConfig(PluginConfigBase):
    """主动话题：按排程根据群的记忆和最近聊天找相关新闻或影片，唤醒麦麦分享到群里。"""

    __ui_label__: ClassVar[str] = "主动话题"
    __ui_order__: ClassVar[int] = 22

    enabled: bool = Field(
        default=False,
        description="是否启用主动话题。",
        json_schema_extra={"label": "启用主动话题", "order": 0},
    )
    channel_list: List[str] = Field(
        default_factory=list,
        description="要主动开话题的 Discord 频道 ID；频道里要先有人说过话，MaiBot 才认得这个聊天。",
        json_schema_extra={"label": "频道列表", "order": 1, "placeholder": "请输入频道 ID"},
    )
    cron: str = Field(
        default="0 12,20 * * *",
        description="什么时候开话题，5 段 cron（分 时 日 月 星期），服务器本地时间。",
        json_schema_extra={"label": "排程（cron）", "order": 2, "hint": "例：0 12,20 * * * 为每天 12 点和 20 点。", "placeholder": "0 12,20 * * *"},
    )
    context_hours: int = Field(
        default=24,
        ge=1,
        description="参考最近几小时的聊天来挑话题。",
        json_schema_extra={"label": "参考聊天（小时）", "order": 3, "step": 1},
    )
    news_enabled: bool = Field(
        default=True,
        description="搜索相关新闻分享；需要在 MaiBot 设置里启用联网搜索。",
        json_schema_extra={"label": "分享新闻", "order": 4},
    )
    video_enabled: bool = Field(
        default=False,
        description="请麦麦去 B 站或 X 找热门影片分享。",
        json_schema_extra={
            "label": "分享影片",
            "order": 5,
            "hint": "⚠ 需要另外安装能操作浏览器的插件，并且这台电脑能启动 Playwright / Chromium；没有浏览器工具时麦麦会放弃分享影片。",
        },
    )
    model_task: Literal["replyer", "planner"] = Field(
        default="planner",
        description="挑话题与挑新闻沿用 MaiBot 哪个模型任务：replyer=回复模型, planner=规划模型。",
        json_schema_extra={"label": "使用模型", "order": 6},
    )

    @field_validator("channel_list", mode="before")
    @classmethod
    def _normalize_channel_list(cls, value: Any) -> List[str]:
        return _normalize_string_list(value)

    @field_validator("cron", mode="before")
    @classmethod
    def _normalize_cron(cls, value: Any) -> str:
        return " ".join(_normalize_string(value).split()) or "0 12,20 * * *"


# WebUI 配置页标签分组：(标签 ID, 标题, 配置节列表)。
WEBUI_CONFIG_TABS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("basic", "基础设置", ("plugin", "connection", "platform")),
    ("chat", "聊天与过滤", ("chat", "filters")),
    ("daily", "日常", ("reminder", "life", "topics")),
    (
        "voice",
        "语音",
        (
            "voice",
            "siliconflow_tts",
            "gptsovits_tts",
            "minimax_tts",
            "fishaudio_tts",
            "siliconflow_stt",
            "aliyun_stt",
            "tencent_stt",
        ),
    ),
)


class DiscordPluginSettings(PluginConfigBase):
    """Discord 插件完整配置。"""

    plugin: DiscordPluginOptions = Field(default_factory=DiscordPluginOptions)
    connection: DiscordConnectionConfig = Field(default_factory=DiscordConnectionConfig)
    chat: DiscordChatConfig = Field(default_factory=DiscordChatConfig)
    platform: DiscordPlatformConfig = Field(default_factory=DiscordPlatformConfig)
    filters: DiscordFilterConfig = Field(default_factory=DiscordFilterConfig)
    voice: DiscordVoiceConfig = Field(default_factory=DiscordVoiceConfig)
    siliconflow_tts: SiliconFlowTTSConfig = Field(default_factory=SiliconFlowTTSConfig)
    gptsovits_tts: GPTSoVITSConfig = Field(default_factory=GPTSoVITSConfig)
    minimax_tts: MiniMaxTTSConfig = Field(default_factory=MiniMaxTTSConfig)
    fishaudio_tts: FishAudioTTSConfig = Field(default_factory=FishAudioTTSConfig)
    siliconflow_stt: SiliconFlowSTTConfig = Field(default_factory=SiliconFlowSTTConfig)
    aliyun_stt: AliyunSTTConfig = Field(default_factory=AliyunSTTConfig)
    tencent_stt: TencentSTTConfig = Field(default_factory=TencentSTTConfig)
    reminder: ReminderConfig = Field(default_factory=ReminderConfig)
    life: LifeConfig = Field(default_factory=LifeConfig)
    topics: TopicConfig = Field(default_factory=TopicConfig)

    def should_connect(self) -> bool:
        """委托 ``plugin`` 子配置判断是否应连接 Discord。

        Returns:
            bool: 与 ``DiscordPluginOptions.should_connect()`` 一致。
        """
        return self.plugin.should_connect()

    def validate_runtime_config(self, logger: Any) -> bool:
        """校验 ``plugin.config_version`` 是否已设置且与当前代码支持的版本一致。

        Args:
            logger: 用于输出错误信息的日志器。

        Returns:
            bool: 版本合法返回 True，否则返回 False 并已记录错误日志。
        """
        config_version = self.plugin.config_version
        if not config_version:
            logger.error(f"Discord 适配器配置缺少 plugin.config_version，要求版本 {SUPPORTED_CONFIG_VERSION}")
            return False
        if config_version != SUPPORTED_CONFIG_VERSION:
            logger.error(
                f"Discord 适配器配置版本不兼容: 当前为 {config_version}，要求 {SUPPORTED_CONFIG_VERSION}"
            )
            return False
        if not self.connection.token:
            logger.error("Discord 适配器缺少 connection.token，请在插件配置中填写 Bot Token")
            return False
        return True

    def get_intents_dict(self) -> Dict[str, bool]:
        """汇总连接配置中的 discord.py Intent 开关，供客户端初始化使用。

        Returns:
            Dict[str, bool]: 各 Intent 名称到是否启用的映射；消息相关显式拆分为
            ``guild_messages`` 与 ``dm_messages``，避免使用 discord.py 的聚合别名
            ``messages`` 造成语义混淆；``reactions`` 固定为 True。
        """
        return {
            "guild_messages": self.connection.intent_messages,
            "guilds": self.connection.intent_guilds,
            "dm_messages": self.connection.intent_dm_messages,
            "message_content": self.connection.intent_message_content,
            "voice_states": self.connection.intent_voice_states,
            "reactions": True,
        }


def _normalize_string(value: Any) -> str:
    """将输入规范化为去除首尾空白后的字符串；``None`` 视为空串。

    Args:
        value: 任意可转字符串的值。

    Returns:
        str: 规范化后的字符串。
    """
    return "" if value is None else str(value).strip()


def _normalize_string_list(value: Any) -> List[str]:
    """将列表元素逐项 ``_normalize_string`` 去重，跳过空串；非列表返回空列表。

    Args:
        value: 应为字符串列表的配置值。

    Returns:
        List[str]: 去重且非空的字符串列表。
    """
    if not isinstance(value, list):
        return []
    seen: set[str] = set()
    result: List[str] = []
    for item in value:
        text = _normalize_string(item)
        if text and text not in seen:
            seen.add(text)
            result.append(text)
    return result


def is_section_visible(config_class: Any, config_data: Mapping[str, Any]) -> bool:
    """按配置节的 ``__ui_visible_when__`` 条件判断该节是否应在 WebUI 中显示。

    Args:
        config_class: 配置节模型类。
        config_data: 已规范化的完整插件配置字典。

    Returns:
        bool: 未声明条件或全部条件满足时返回 True。
    """
    for path, expected in getattr(config_class, "__ui_visible_when__", ()):
        current: Any = config_data
        for key in path.split("."):
            current = current.get(key) if isinstance(current, Mapping) else None
        if current != expected:
            return False
    return True
