import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import asyncio

import discord

from test_typing_indicator import _load_adapter_module, _load_plugin_class

AUTHOR = SimpleNamespace(id=7, display_name="Daemi", nick=None, bot=False)


def _handler():
    handler_module = _load_adapter_module("src.recv_handler.message_handler")
    handler = handler_module.DiscordMessageHandler(
        logger=SimpleNamespace(warning=print, error=print, debug=print),
        platform_name="discord",
        chat_config=SimpleNamespace(),
    )

    async def fetch(url, size=48):
        return f"img:{url}:{size}"

    async def to_wav(data):
        return b"WAV" + data

    async def no_ffmpeg(*args):
        return b""

    handler._fetch_emoji_bytes = fetch
    handler._voice_message_to_wav = to_wav
    handler._run_ffmpeg = no_ffmpeg
    return handler


def _attachment(filename: str, content_type: str, voice: bool = False):
    async def read():
        return b"ogg"

    return SimpleNamespace(
        filename=filename,
        content_type=content_type,
        url=f"https://cdn.discordapp.com/attachments/1/2/{filename}",
        is_voice_message=lambda: voice,
        read=read,
    )


def _message(**overrides):
    fields = dict(
        id=99,
        author=AUTHOR,
        channel=SimpleNamespace(id=10, name="闲聊"),
        guild=SimpleNamespace(id=5, name="伺服器"),
        content="",
        attachments=[],
        stickers=[],
        embeds=[],
        poll=None,
        reference=None,
        message_snapshots=[],
        mentions=[],
        role_mentions=[],
        channel_mentions=[],
        flags=SimpleNamespace(suppress_embeds=False),
        created_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return SimpleNamespace(**fields)


def _convert(handler, message) -> dict:
    return handler.build_host_message_dict(asyncio.run(handler._convert_discord_to_maim(message)))


def test_forward_embeds_poll_and_attachments_reach_maibot() -> None:
    handler = _handler()
    snapshot = SimpleNamespace(content="原文", attachments=[], stickers=[], embeds=[])
    forward = _message(
        reference=SimpleNamespace(type=discord.MessageReferenceType.forward, message_id=1, cached_message=None),
        message_snapshots=[snapshot],
    )
    result = _convert(handler, forward)
    # 转发不是回复，不挂 reply，内容取自快照
    assert "reply_to" not in result and result["processed_plain_text"] == "[转发的消息]原文"

    rich = discord.Embed(title="排行", description="第一名", type="rich").add_field(name="分数", value="99")
    link = discord.Embed.from_dict({"type": "link", "provider": {"name": "YouTube"}, "title": "影片"})
    gif = discord.Embed.from_dict({"type": "gifv", "thumbnail": {"url": "https://t/x.png", "proxy_url": "https://p/x.png"}})
    poll = discord.Poll(question="吃什么", duration=timedelta(hours=1), multiple=True)
    poll.add_answer(text="拉面").add_answer(text="寿司")
    message = _message(
        content="看",
        embeds=[rich, link, gif],
        poll=poll,
        attachments=[
            _attachment("voice-message.ogg", "audio/ogg", voice=True),
            _attachment("a.zip", "application/zip"),
            _attachment("b.mp4", "video/mp4"),
        ],
    )
    raw = _convert(handler, message)["raw_message"]
    texts = [item["data"] for item in raw if item["type"] == "text"]
    assert texts == [
        "看",
        "[文件: a.zip]",
        "[视频: b.mp4]",
        "[卡片: 排行\n第一名\n分数: 99]",
        "[链接预览: YouTube\n影片]",
        "[投票: 吃什么｜选项: 拉面 / 寿司，可多选]",
    ]
    # 语音消息转成 wav 交给 MaiBot 转写；动图预览当表情，不加 size 参数
    assert {"type": "voice", "data": "", "binary_data_base64": "V0FWb2dn"} in raw
    assert {"type": "emoji", "data": "", "binary_data_base64": "img:https://p/x.png:None"} in raw


def test_voice_conversion_falls_back_to_original_without_ffmpeg(monkeypatch) -> None:
    handler_module = _load_adapter_module("src.recv_handler.message_handler")
    handler = handler_module.DiscordMessageHandler(
        logger=SimpleNamespace(warning=lambda *args: None), platform_name="discord", chat_config=SimpleNamespace()
    )

    async def missing(*args, **kwargs):
        raise FileNotFoundError("ffmpeg")

    monkeypatch.setattr(handler_module.asyncio, "create_subprocess_exec", missing)
    assert asyncio.run(handler._voice_message_to_wav(b"ogg")) == b"ogg"


def test_edit_and_delete_become_notices() -> None:
    handler = _handler()
    before, after = _message(content="今天下雨"), _message(content="今天放晴")

    edited = asyncio.run(handler.handle_message_change(after, before))
    assert edited["is_notify"] is True
    assert edited["processed_plain_text"] == "[事件-消息编辑] 「Daemi」把消息「今天下雨」改成了：今天放晴"
    assert edited["message_info"]["group_info"]["group_id"] == "10"

    deleted = asyncio.run(handler.handle_message_change(_message(stickers=[SimpleNamespace(name="wave")])))
    assert deleted["processed_plain_text"] == "[事件-消息撤回] 「Daemi」撤回了消息：[贴纸: wave]"
    assert deleted["message_id"].startswith("message_delete_99_")

    # 一般消息不是通知
    assert _convert(handler, _message(content="hi"))["is_notify"] is False


def test_message_change_is_filtered_like_messages() -> None:
    client_module = _load_adapter_module("src.recv_handler.discord_client")
    manager = client_module.DiscordClientManager.__new__(client_module.DiscordClientManager)
    routed: list[dict] = []

    async def route_message(gateway, message_dict, **kwargs):
        routed.append(message_dict)

    allowed = {"ok": True}
    manager.client = SimpleNamespace(user=SimpleNamespace(id=1))
    manager._filter_config = SimpleNamespace(ignore_self_message=True, ignore_bot_message=True)
    manager._chat_filter = SimpleNamespace(_config=None, is_allowed=lambda **kwargs: allowed["ok"])
    manager._message_handler = _handler()
    manager._gateway_capability = SimpleNamespace(route_message=route_message)
    manager._gateway_name = "discord"
    manager._logger = SimpleNamespace(error=print, debug=print)

    asyncio.run(manager._on_message_change(_message(content="x")))
    asyncio.run(manager._on_message_change(_message(author=SimpleNamespace(id=1, display_name="bot", nick=None, bot=True))))
    allowed["ok"] = False
    asyncio.run(manager._on_message_change(_message(content="y")))
    assert [item["processed_plain_text"] for item in routed] == ["[事件-消息撤回] 「Daemi」撤回了消息：x"]


def _manager(client_module, routed: list):
    manager = client_module.DiscordClientManager.__new__(client_module.DiscordClientManager)

    async def route_message(gateway, message_dict, **kwargs):
        routed.append(message_dict)

    manager.client = SimpleNamespace(user=SimpleNamespace(id=1))
    manager._filter_config = SimpleNamespace(ignore_self_message=True, ignore_bot_message=True)
    manager._chat_filter = SimpleNamespace(_config=None, is_allowed=lambda **kwargs: True)
    manager._message_handler = _handler()
    manager._thread_routing_manager = SimpleNamespace(clear_thread_context=lambda channel_id: None)
    manager._gateway_capability = SimpleNamespace(route_message=route_message)
    manager._gateway_name = "discord"
    manager._logger = SimpleNamespace(error=print, debug=lambda *args: None)
    manager._channel_locks = client_module.defaultdict(asyncio.Lock)
    return manager


def test_link_preview_images_reach_maibot(monkeypatch) -> None:
    client_module = _load_adapter_module("src.recv_handler.discord_client")
    monkeypatch.setattr(client_module, "LINK_PREVIEW_POLL_SECONDS", 0.01)
    routed: list[dict] = []
    manager = _manager(client_module, routed)

    message = _message(content="https://i.example/cat.png")
    preview = discord.Embed.from_dict({"type": "image", "thumbnail": {"url": "https://i.example/cat.png", "proxy_url": "https://p/cat.png"}})

    async def run() -> None:
        async def discord_unfurls_later() -> None:
            # discord.py 收到编辑事件时就地更新缓存里同一个 Message
            await asyncio.sleep(0.05)
            message.embeds.append(preview)

        unfurl = asyncio.create_task(discord_unfurls_later())
        await manager._on_message(message)
        await unfurl

    asyncio.run(run())
    assert routed[0]["is_picture"] is True
    assert {"type": "image", "data": "", "binary_data_base64": "img:https://p/cat.png:None"} in routed[0]["raw_message"]

    # 网页卡片（X、Pixiv、新闻）的大图也当图片，文字照旧
    card = discord.Embed.from_dict(
        {"type": "article", "title": "新番", "image": {"url": "https://x/a.jpg", "proxy_url": "https://p/a.jpg"}}
    )
    raw = _convert(_handler(), _message(content="https://x/post", embeds=[card]))["raw_message"]
    assert {"type": "text", "data": "[链接预览: 新番]"} in raw
    assert {"type": "image", "data": "", "binary_data_base64": "img:https://p/a.jpg:None"} in raw


def test_link_wait_keeps_channel_order_and_skips_suppressed_links(monkeypatch) -> None:
    client_module = _load_adapter_module("src.recv_handler.discord_client")
    monkeypatch.setattr(client_module, "LINK_PREVIEW_POLL_SECONDS", 0.01)
    monkeypatch.setattr(client_module, "LINK_PREVIEW_WAIT_SECONDS", 0.05)
    routed: list[dict] = []
    manager = _manager(client_module, routed)

    async def run(*messages) -> None:
        # 和 discord.py 一样，每则消息一个任务、按到达顺序建立
        await asyncio.gather(*(manager._on_message(message) for message in messages))

    # 网站没有预览时白等，但后面紧跟的 @ 不能抢先送到
    asyncio.run(run(_message(id=1, content="https://example.com/x"), _message(id=2, content="看這個")))
    assert [item["processed_plain_text"] for item in routed] == ["https://example.com/x", "看這個"]

    waited: list[int] = []

    async def record_wait(message) -> None:
        waited.append(message.id)

    manager._wait_for_link_preview = record_wait
    asyncio.run(run(
        _message(id=3, content="<https://example.com/x>"),
        _message(id=4, content="https://example.com/x", flags=SimpleNamespace(suppress_embeds=True)),
        _message(id=5, content="說 http 不是網址"),
    ))
    assert waited == [], "不会展开预览的消息不用等"


def _jpeg(color: str) -> bytes:
    from io import BytesIO

    from PIL import Image

    output = BytesIO()
    Image.new("RGB", (640, 360), color).save(output, format="JPEG")
    return output.getvalue()


def _video_handler(duration: str, interval: float = 2.0, broken_at: str = ""):
    handler = _handler()
    handler._chat_config = SimpleNamespace(video_frame_interval_sec=interval)
    calls: list[tuple] = []

    async def fake_ffmpeg(*args):
        calls.append(args)
        assert args[args.index("-protocol_whitelist") + 1] == "https,tls,tcp", "只准读 HTTPS，不准读本机文件"
        if args[0] == "ffprobe":
            return duration.encode()
        return b"" if args[args.index("-ss") + 1] == broken_at else _jpeg("red")

    handler._run_ffmpeg = fake_ffmpeg
    return handler, calls


def test_video_frames_become_one_contact_sheet() -> None:
    from io import BytesIO

    from PIL import Image

    handler, calls = _video_handler("7.0" + chr(10), broken_at="4.00")
    raw = _convert(handler, _message(attachments=[_attachment("cat.mp4", "video/mp4")]))
    seeks = [args[args.index("-ss") + 1] for args in calls if args[0] == "ffmpeg"]
    assert seeks == ["0.00", "2.00", "4.00", "6.00"], "每 2 秒一格，不截贴着结尾的那格"
    assert all(args[-1] == "pipe:1" or args[-1].startswith("https://cdn.discordapp.com/") for args in calls)
    assert raw["is_picture"] is True
    assert raw["raw_message"][0]["data"] == "[视频: cat.mp4，约 7 秒；下图是每 2 秒一格的画面，左上角是时间点]"
    sheet = Image.open(BytesIO(base64.b64decode(raw["raw_message"][1]["binary_data_base64"])))
    assert sheet.size == (648, 648), "读不到的那格跳过，剩 3 格拼成 2x2"

    # 太长就平均分布成 9 格
    handler, calls = _video_handler("60")
    raw = _convert(handler, _message(attachments=[_attachment("long.mp4", "video/mp4")]))
    seeks = [args[args.index("-ss") + 1] for args in calls if args[0] == "ffmpeg"]
    assert len(seeks) == 9 and seeks[1] == "6.67" and "每 6.7 秒" in raw["raw_message"][0]["data"]

    # 读不到长度（没装 FFmpeg、格式不支持）或关掉截图，只留文件名
    for handler, calls in (_video_handler(""), _video_handler("7", interval=0)):
        raw = _convert(handler, _message(attachments=[_attachment("x.mp4", "video/mp4")]))
        assert raw["processed_plain_text"] == "[视频: x.mp4]"
    assert calls == [], "关掉时不跑 FFmpeg"


def _tool_plugin(target: object):
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)

    async def find_message(msg_id, group_id, user_id):
        return target if msg_id == "555" else None

    plugin._thread_routing = SimpleNamespace(find_message=find_message)
    plugin._client_manager = SimpleNamespace(client=SimpleNamespace(user=SimpleNamespace(id=1)))
    plugin._content_builder = _load_adapter_module("src.send_handler.message_send_handler").DiscordContentBuilder()
    plugin._content_builder.output_script = "s2twp"
    return plugin


def test_edit_and_delete_tools_only_touch_own_messages() -> None:
    calls: list[object] = []

    async def edit(content):
        calls.append(("edit", content))

    async def delete():
        calls.append("delete")

    target = SimpleNamespace(author=SimpleNamespace(id=1), content="打措字", edit=edit, delete=delete)
    plugin = _tool_plugin(target)

    async def run() -> None:
        assert (await plugin.handle_discord_message_edit(msg_id="555", content="打错字"))["success"] is True
        assert "缺少修改后的内容" in (await plugin.handle_discord_message_edit(msg_id="555"))["error"]
        assert (await plugin.handle_discord_message_delete(msg_id="555"))["success"] is True
        assert "找不到" in (await plugin.handle_discord_message_delete(msg_id="404"))["error"]
        target.author = SimpleNamespace(id=2)
        assert "不是自己发的" in (await plugin.handle_discord_message_delete(msg_id="555"))["error"]
        assert "不是自己发的" in (await plugin.handle_discord_message_edit(msg_id="555", content="x"))["error"]

    asyncio.run(run())
    assert calls == [("edit", "打錯字"), "delete"], "改消息也照「输出转繁体」转"



def test_names_are_quoted_so_model_can_tell_them_from_content() -> None:
    handler = _handler()
    odd = SimpleNamespace(id=8, name="x", display_name="[GM] 小 明", nick="[GM] 小 明", global_name=None, bot=False, discriminator="0")
    result = _convert(handler, _message(author=odd, content="<@8> 早", mentions=[odd]))
    # ASCII 方括号会被 MaiBot 当成 [说话人] 的边界，换成全角；名字整体包进「」
    assert result["message_info"]["user_info"]["user_nickname"] == "「［GM］ 小 明」"
    assert result["message_info"]["user_info"]["user_cardname"] == "「［GM］ 小 明」"
    assert result["processed_plain_text"] == "@「［GM］ 小 明」 早"
    assert _convert(handler, _message(content="hi"))["message_info"]["user_info"]["user_cardname"] is None


def test_only_mentions_of_the_bot_itself_are_flagged() -> None:
    handler = _handler()
    handler.set_account_id("42")
    bot = SimpleNamespace(id=42, name="mai", display_name="麥麥", nick=None, global_name=None, bot=True, discriminator="0")
    friend = SimpleNamespace(id=8, name="x", display_name="小明", nick=None, global_name=None, bot=False, discriminator="0")
    flagged = _convert(handler, _message(content="<@42> 起床", mentions=[bot]))["message_info"]["additional_config"]
    other = _convert(handler, _message(content="<@8> 早", mentions=[friend]))["message_info"]["additional_config"]
    assert flagged["discord_mentions_bot"] is True and "discord_mentions_bot" not in other
