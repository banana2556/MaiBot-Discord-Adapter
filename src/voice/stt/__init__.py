"""STT 提供商实现。"""

from .aliyun_stt import AliyunSTTProvider
from .gemini_stt import GeminiSTTProvider
from .siliconflow_stt import SiliconFlowSTTProvider
from .tencent_stt import TencentSTTProvider

__all__ = [
    "AliyunSTTProvider",
    "GeminiSTTProvider",
    "SiliconFlowSTTProvider",
    "TencentSTTProvider",
]
