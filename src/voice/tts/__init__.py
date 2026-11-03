"""TTS 提供商实现。"""

from .fishaudio_tts import FishAudioTTSProvider
from .gptsovits_tts import GPTSoVITSTTSProvider
from .minimax_tts import MiniMaxTTSProvider
from .siliconflow_tts import SiliconFlowTTSProvider

__all__ = ["FishAudioTTSProvider", "GPTSoVITSTTSProvider", "MiniMaxTTSProvider", "SiliconFlowTTSProvider"]
