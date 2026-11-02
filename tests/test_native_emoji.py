from pathlib import Path
from types import SimpleNamespace
import base64
import importlib
import importlib.util
import sys

from maim_message import Seg


def _load_adapter_module(module_name: str):
    package_name = "maibot_discord_adapter_under_test"
    package_root = Path(__file__).resolve().parents[1]
    if package_name not in sys.modules:
        spec = importlib.util.spec_from_file_location(
            package_name,
            package_root / "__init__.py",
            submodule_search_locations=[str(package_root)],
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("无法加载 Discord 适配器测试包")
        module = importlib.util.module_from_spec(spec)
        sys.modules[package_name] = module
        spec.loader.exec_module(module)
    return importlib.import_module(f"{package_name}.{module_name}")


def _b64(payload: bytes) -> str:
    return base64.b64encode(b"\x89PNG\r\n\x1a\n" + payload).decode()


class _FakeEmoji:
    def __init__(self, emoji_id: int, guild_id: int) -> None:
        self.id = emoji_id
        self.guild_id = guild_id

    def is_usable(self) -> bool:
        return True

    def __str__(self) -> str:
        return f"<:fake:{self.id}>"


SMILE, OTHER_SERVER_EMOJI, LOCAL_STICKER, FOREIGN_STICKER, UNKNOWN = (
    _b64(b"smile"),
    _b64(b"other"),
    _b64(b"local-sticker"),
    _b64(b"foreign-sticker"),
    _b64(b"unknown"),
)


def test_registry_persists_and_reloads(tmp_path: Path) -> None:
    registry_module = _load_adapter_module("src.native_emoji")
    path = tmp_path / "native_emoji.json"
    registry_module.NativeEmojiRegistry(path).remember(SMILE, "emoji", "111")

    reloaded = registry_module.NativeEmojiRegistry(path)
    assert reloaded.lookup(SMILE) == ("emoji", 111)
    assert reloaded.lookup(UNKNOWN) is None
    assert reloaded.lookup("not base64!") is None


def test_builder_restores_native_emoji_and_stickers() -> None:
    registry_module = _load_adapter_module("src.native_emoji")
    builder_module = _load_adapter_module("src.send_handler.message_send_handler")

    registry = registry_module.NativeEmojiRegistry()
    registry.remember(SMILE, "emoji", 111)
    registry.remember(OTHER_SERVER_EMOJI, "emoji", 222)
    registry.remember(LOCAL_STICKER, "sticker", 333)
    registry.remember(FOREIGN_STICKER, "sticker", 444)

    emojis = {111: _FakeEmoji(111, guild_id=1), 222: _FakeEmoji(222, guild_id=2)}
    local_sticker = SimpleNamespace(id=333, guild_id=1, available=True)
    stickers = {333: local_sticker, 444: SimpleNamespace(id=444, guild_id=2, available=True)}
    client = SimpleNamespace(get_emoji=emojis.get, get_sticker=stickers.get)
    channel = SimpleNamespace(
        guild=SimpleNamespace(id=1, me=object()),
        permissions_for=lambda _member: SimpleNamespace(use_external_emojis=False),
    )

    builder = builder_module.DiscordContentBuilder(native_emoji=registry)
    segments = [Seg(type="emoji", data=data) for data in (SMILE, OTHER_SERVER_EMOJI, LOCAL_STICKER, FOREIGN_STICKER)]
    segments.append(Seg(type="emoji", data=UNKNOWN))
    content, files, sent_stickers = builder.build(Seg(type="seglist", data=segments), client, channel)

    # 同服务器表情 → 原生；外部表情无权限、外服贴纸、未知表情 → 退回上传图片
    assert content == "<:fake:111>"
    assert sent_stickers == [local_sticker]
    assert len(files) == 3

    # 没有 client（如语音分支）时全部按图片上传
    content, files, sent_stickers = builder.build(Seg(type="emoji", data=SMILE))
    assert content is None and len(files) == 1 and sent_stickers == []
