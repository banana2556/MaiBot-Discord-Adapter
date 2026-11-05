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
        message=SimpleNamespace(build_readable=build_readable),
        logger=SimpleNamespace(debug=print, info=print, warning=print),
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


def test_decision_page_counts_nudges_adoptions_and_failures(monkeypatch) -> None:
    sticker = {"emote": {"type": "choice", "probabilities": {"none": 0.1, "reaction": 0.2, "sticker": 0.7}}}
    plugin, module, _ = _decision_plugin(monkeypatch, sticker)

    async def all_streams(platform="all_platforms"):
        return [{"stream_id": "s1", "group_name": "聊天大廳", "user_cardname": None, "user_nickname": "x"}]

    plugin._ctx.chat = SimpleNamespace(get_all_streams=all_streams)

    async def scenario():
        assert await plugin._emote_nudge("s1") == module.STICKER_NUDGE
        # MaiBot 自己用 send_emoji 发了表情包，discord_emote 看不到，靠 after_send 记采纳
        await plugin.stop_discord_typing_after_send(
            message={"session_id": "s1", "raw_message": [{"type": "emoji"}]}, sent=True
        )
        first = await plugin.webui_decision()

        async def broken(config, state, questions, logger):
            return None

        monkeypatch.setattr(module, "ask_decision", broken)
        await plugin._emote_nudge("s1")
        return first, await plugin.webui_decision()

    first, second = asyncio.run(scenario())
    assert first["notice"] == "" and first["today_calls"] == 1
    assert first["today_nudge_rate"] == "100%（1/1）" and first["today_adopt_rate"] == "100%（1/1）"
    assert first["today_split"] == "表情 0・發圖 1・不提醒 0"
    assert first["recent"][0]["chat"] == "聊天大廳" and first["recent"][0]["result"] == "提醒發圖"
    assert first["recent"][0]["adopted"] == "貼圖或表情包" and first["recent"][0]["message"] == "小明：我抽到限定了！！"
    assert first["daily"][0]["calls"] == 1 and first["daily"][0]["adopted"] == 1
    assert second["today_failed"] == 1 and second["today_calls"] == 2 and second["recent"][0]["reaction"] == "—"
    assert second["today_latency"] == first["today_latency"], "失败的请求不算进平均延迟"


def test_adoption_only_counts_the_latest_nudge_in_that_chat_within_three_minutes() -> None:
    from datetime import datetime, timedelta

    stats = _load_adapter_module("src.decision").DecisionStats(None)
    start = datetime(2026, 11, 5, 12, 0)
    stats.record(start, "s1", "好耶", "reaction", 200, 0.8, 0.1)
    stats.adopt(start + timedelta(minutes=4), "s1", "表情回應")
    stats.adopt(start, "s2", "表情回應")
    assert stats.state["recent"][0]["adopted"] == "", "超过 3 分钟、别的聊天都不算"
    stats.record(start, "s1", "嗯", "skip", 200)
    stats.adopt(start, "s1", "表情回應")
    assert [item["adopted"] for item in stats.state["recent"]] == ["", ""], "最近一次是不提醒，就不算采纳"
    stats.record(start, "s1", "哈哈", "reaction", 200, 0.9, 0)
    stats.adopt(start + timedelta(minutes=1), "s1", "表情回應")
    stats.adopt(start + timedelta(minutes=2), "s1", "表情")
    assert stats.state["recent"][0]["adopted"] == "表情回應" and stats.state["days"]["2026-11-05"]["adopted"] == 1


def test_decision_test_button_reports_the_failure_reason(monkeypatch) -> None:
    plugin, module, _ = _decision_plugin(
        monkeypatch, {"emote": {"type": "choice", "probabilities": {"none": 0.5, "reaction": 0.4, "sticker": 0.1}}}
    )
    assert asyncio.run(plugin.webui_decision_test())["ok"]
    assert plugin._decision_stats.state["last_test"]["ok"] is True

    async def unauthorized(config, state, questions, logger):
        logger.warning("决策模型返回 HTTP 401：invalid key")
        return None

    monkeypatch.setattr(module, "ask_decision", unauthorized)
    try:
        asyncio.run(plugin.webui_decision_test())
    except ValueError as exc:
        assert "401" in str(exc)
    else:
        raise AssertionError("测试失败要让页面显示错误")
    assert plugin._decision_stats.state["last_test"]["ok"] is False
