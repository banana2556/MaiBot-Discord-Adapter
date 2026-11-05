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


def test_planner_emote_nudge_follows_chance_and_only_when_tool_offered() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls()

    def run(chance, tool_names):
        plugin.set_plugin_config({"plugin": {"config_version": "1.0.0"}, "chat": {"emote_chance": chance}})
        items = [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "人設"}]}]
        tools = [{"type": "function", "function": {"name": name}} for name in tool_names]
        asyncio.run(plugin.nudge_emote_in_planner(items=items, tool_definitions=tools, session_id="s1"))
        return "discord_emote" in items[0]["parts"][-1]["text"]

    assert run(1, ["reply", "discord_emote"])
    assert not run(0, ["reply", "discord_emote"]), "机率 0 不提醒"
    assert not run(1, ["reply"]), "非 Discord 聊天没有这个工具，不提醒"


def _decision_plugin(monkeypatch, answers, **decision):
    """决策模型开着的插件；ask 被换成假的，记下被问了什么。"""
    plugin_module = _load_adapter_module("plugin")
    plugin = _load_plugin_class()()
    plugin.set_plugin_config(
        {
            "plugin": {"config_version": "1.0.0"},
            "chat": {"emote_chance": 0},
            "decision": {"enabled": True, "api_key": "k", **decision},
        }
    )

    async def build_readable(messages, **kwargs):
        return {"success": True, "text": "小明：我抽到限定了！！"}

    plugin._ctx = SimpleNamespace(
        message=SimpleNamespace(build_readable=build_readable), logger=SimpleNamespace(debug=print, warning=print)
    )
    asked = []

    async def fake_ask(config, state, questions, logger):
        asked.append((config.model, state, questions))
        return answers

    monkeypatch.setattr(plugin_module, "ask_decision", fake_ask)
    return plugin, plugin_module, asked


def test_decision_model_picks_whether_and_how_to_emote(monkeypatch) -> None:
    def nudge(probabilities, **decision):
        plugin, module, asked = _decision_plugin(
            monkeypatch, {"emote": {"type": "choice", "probabilities": probabilities}} if probabilities else None, **decision
        )
        result = asyncio.run(plugin._emote_nudge("s1"))
        return result, module, asked

    text, module, asked = nudge({"none": 0.2, "reaction": 0.7, "sticker": 0.1})
    assert text == module.EMOTE_NUDGE and asked[0][1] == "小明：我抽到限定了！！"
    assert set(asked[0][2]["emote"]["criteria"]) == {"none", "reaction", "sticker"}
    assert nudge({"none": 0.3, "reaction": 0.2, "sticker": 0.5})[0] == module.STICKER_NUDGE
    assert nudge({"none": 0.6, "reaction": 0.3, "sticker": 0.1})[0] == "", "值得的机率 0.4 没过默认门槛 0.5"
    assert nudge({"none": 0.6, "reaction": 0.3, "sticker": 0.1}, emote_threshold=0.3)[0] == module.EMOTE_NUDGE
    assert nudge(None)[0] == "", "请求失败退回随机，emote_chance=0 就不提醒"


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
