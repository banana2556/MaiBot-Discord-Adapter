from types import SimpleNamespace
import asyncio

from test_typing_indicator import _load_adapter_module, _load_plugin_class


class _Emoji:
    def __init__(self, name: str, emoji_id: int, usable: bool = True) -> None:
        self.name, self.id, self._usable = name, emoji_id, usable

    def is_usable(self) -> bool:
        return self._usable

    def __str__(self) -> str:
        return f"<:{self.name}:{self.id}>"


GUILD = SimpleNamespace(emojis=[_Emoji("pepe", 111), _Emoji("gone", 222, usable=False)])


def test_replyer_hook_allows_server_emojis_only_in_discord_sessions() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._discord_session_channels = {"s-guild": 10}
    items = [
        {"item_type": "SystemMessageItem", "meta": {}, "parts": [{"type": "text", "text": "不要输出表情包"}]},
        {"item_type": "UserMessageItem", "meta": {}, "parts": [{"type": "text", "text": "hi"}]},
    ]
    result = asyncio.run(plugin.inject_discord_emoji_replyer_hint(session_id="s-guild", items=items))
    system_parts = result["modified_kwargs"]["items"][0]["parts"]
    assert system_parts[1]["text"] == plugin_cls._REPLYER_EMOJI_HINT
    assert asyncio.run(plugin.inject_discord_emoji_replyer_hint(session_id="qq-session", items=items)) == {
        "action": "continue"
    }


def test_outbound_text_turns_emoji_names_into_native() -> None:
    builder_cls = _load_adapter_module("src.send_handler.message_send_handler").DiscordContentBuilder
    channel = SimpleNamespace(guild=GUILD)
    render = builder_cls._render_emoji_names
    assert render("笑死:pepe:你", channel) == "笑死<:pepe:111>你"
    # 已成形的表情、不可用或不存在的名称、时间格式都不动
    assert render("<:pepe:111> <a:pepe:111>", channel) == "<:pepe:111> <a:pepe:111>"
    assert render(":gone: :nope: 10:30:00", channel) == ":gone: :nope: 10:30:00"
    assert render(":pepe:", SimpleNamespace()) == ":pepe:"


def test_decision_request_uses_system_one_format() -> None:
    from aiohttp import web

    decision = _load_adapter_module("src.decision")
    received = {}

    async def systemone(request):
        received["auth"] = request.headers["Authorization"]
        received["body"] = await request.json()
        return web.json_response({"answers": {"emote": {"type": "choice", "choice": "none"}}, "model": "m"})

    async def scenario():
        app = web.Application()
        app.router.add_post("/v1/systemone", systemone)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        config = SimpleNamespace(base_url=f"http://127.0.0.1:{port}/", api_key=" key ", model="vllm-sr/d3-flash")
        logger = SimpleNamespace(warning=print)
        try:
            ok = await decision.ask(config, "聊天", {"emote": {"type": "noul", "instructions": "?"}}, logger)
            missing = await decision.ask(
                SimpleNamespace(base_url=f"http://127.0.0.1:{port}/nope", api_key="", model="x"), "s", {}, logger
            )
        finally:
            await runner.cleanup()
        return ok, missing

    ok, missing = asyncio.run(scenario())
    assert ok == {"emote": {"type": "choice", "choice": "none"}} and missing is None
    assert received["auth"] == "Bearer key"
    assert received["body"] == {
        "model": "vllm-sr/d3-flash",
        "state": "聊天",
        "questions": {"emote": {"type": "noul", "instructions": "?"}},
    }


def test_outbound_text_can_be_converted_to_traditional() -> None:
    builder_cls = _load_adapter_module("src.send_handler.message_send_handler").DiscordContentBuilder
    Seg = __import__("maim_message").Seg
    builder = builder_cls()
    message = Seg(type="text", data="我们用软件看视频:pepe: https://zh.wikipedia.org/wiki/软件")
    assert builder.build(message, channel=SimpleNamespace(guild=GUILD))[0] == message.data.replace(":pepe:", "<:pepe:111>")
    builder.output_script = "s2twp"
    content = builder.build(message, channel=SimpleNamespace(guild=GUILD))[0]
    assert content == "我們用軟體看影片<:pepe:111> https://zh.wikipedia.org/wiki/软件", "网址不转"
    assert builder.to_output_script("后来") == "後來"
