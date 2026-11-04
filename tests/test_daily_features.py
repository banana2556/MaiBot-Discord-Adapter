from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
import asyncio
import json
import logging

import pytest

from test_typing_indicator import _load_adapter_module, _load_plugin_class

NOW = datetime(2026, 11, 2, 12, 0)  # 星期一


def _run(coro):
    return asyncio.run(coro)


def _hours(prefix: str, start: int = 0, end: int = 24) -> list:
    return [
        {"hour": h, "activity": f"{prefix}{h}", "mood": "普通", "status_text": f"✨ {prefix}{h}", "online_status": "online"}
        for h in range(start, end)
    ]


class FakeCtx:
    """记录调用的假插件上下文；llm_replies 依次作为每次 LLM 回复。"""

    def __init__(self, data_dir, llm_replies=None):
        self.plugin_id = "maibot-discord-adapter"
        self.paths = SimpleNamespace(data_dir=data_dir)
        self.logger = logging.getLogger("test_daily_features")
        self.llm_replies = list(llm_replies or [])
        self.prompts: list = []
        self.tasks: list = []
        self.triggered: list = []
        self.toggles: list = []
        self.messages = {
            "m1": {
                "message_id": "m1",
                "message_info": {"user_info": {"user_id": "u1", "user_nickname": "小明", "user_cardname": None}},
                "processed_plain_text": "9 點提醒我開會",
            }
        }
        self.maisaka = SimpleNamespace(
            proactive=SimpleNamespace(trigger=self._trigger), context=SimpleNamespace(append=self._append)
        )
        self.message = SimpleNamespace(get_by_id=self._get_by_id, build_readable=self._build_readable)
        self.llm = SimpleNamespace(generate=self._generate)
        self.personality = SimpleNamespace(get_current=self._persona)
        self.component = SimpleNamespace(enable_component=self._toggle(True), disable_component=self._toggle(False))
        self.chat = SimpleNamespace(get_stream_by_group_id=self._stream_by_group)
        self.adjusts: list = []

    async def _trigger(self, stream_id, intent, **kwargs):
        self.triggered.append((stream_id, intent, kwargs.get("reason")))
        return {"success": True}

    async def _append(self, stream_id, segments, **kwargs):
        return {"success": True}

    async def _get_by_id(self, message_id, stream_id=""):
        return self.messages.get(message_id)

    async def _build_readable(self, messages, **kwargs):
        return {"success": True, "text": "小明：晚上九點一起打遊戲？\n麥麥：好啊"}

    async def _generate(self, prompt, task_name=None):
        self.prompts.append(prompt)
        self.tasks.append(task_name)
        reply = self.llm_replies.pop(0) if self.llm_replies else ""
        return {"success": True, "response": reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)}

    async def _persona(self):
        return {"name": "麥麥", "personality": "活潑的女大學生"}

    async def _stream_by_group(self, group_id, platform="qq"):
        return {"stream_id": f"s-{group_id}"} if group_id == "111" else None

    async def call_capability(self, capability, **kwargs):
        # 部署环境的旧版 SDK 没有 ctx.web / ctx.frequency，这个假上下文也不提供，只能经 call_capability 调
        if capability == "frequency.set_adjust":
            self.adjusts.append((kwargs["chat_id"], kwargs["value"]))
            return {"success": True}
        assert capability == "knowledge.search" and kwargs["chat_id"] == "s-111"
        return {"success": True, "content": "你知道這些知識: 大家喜歡音遊和貓"}

    def _toggle(self, enabled):
        async def run(name, component_type):
            self.toggles.append((name, enabled))
            return {"success": True}

        return run


def _cfg(sections):
    return {"plugin": {"config_version": "1.0.0"}, **sections}


def _plugin(tmp_path, config=None):
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls()
    plugin.set_plugin_config(_cfg(config or {}))
    ctx = FakeCtx(tmp_path)
    plugin._ctx = ctx
    return plugin, ctx


# ===== cron =====


def test_cron_matches_reminder_plugin_semantics() -> None:
    cron = _load_adapter_module("src.life.cron")
    cases = {
        "0 12 2 11 *": datetime(2026, 11, 2, 12, 0),
        "0 9 2 11 *": datetime(2027, 11, 2, 9, 0),
        "0 9 * * 1-5": datetime(2026, 11, 3, 9, 0),
        "0 9 * * 7": datetime(2026, 11, 8, 9, 0),
        "0 9 13 * 5": datetime(2026, 11, 6, 9, 0),
        "0 0 29 2 *": datetime(2028, 2, 29, 0, 0),
        "5/20 12 * * *": datetime(2026, 11, 2, 12, 5),
    }
    for expr, expected in cases.items():
        assert cron.next_run(cron.parse_cron(expr), NOW) == expected, expr
    for expr in ("0 9 * *", "60 * * * *", "*/0 * * * *", "0 9 30 2 *"):
        with pytest.raises(ValueError):
            cron.next_run(cron.parse_cron(expr), NOW)
    assert cron.parse_at(NOW, "09:00") == datetime(2026, 11, 3, 9, 0)


# ===== 定时提醒 =====


def test_reminder_set_fire_and_recurring(tmp_path) -> None:
    plugin, ctx = _plugin(tmp_path)
    plugin._load_reminders()
    once = _run(plugin.handle_reminder_set("小明請你提醒他開會", "0 9 1 1 *", False, msg_id="m1", stream_id="g1"))
    daily = _run(plugin.handle_reminder_set("提醒大家喝水", "0 9 * * *", True, stream_id="g1"))
    assert once["success"] and daily["success"]
    assert not _run(plugin.handle_reminder_cancel(once["id"], stream_id="g2"))["success"]

    new_year = datetime.fromisoformat(plugin._reminders[0]["due"])
    _run(plugin.fire_due_reminders(new_year))

    intents = [intent for _, intent, _ in ctx.triggered]
    assert any("@ 小明" in intent for intent in intents)
    assert [r["content"] for r in plugin._reminders] == ["提醒大家喝水"]
    assert datetime.fromisoformat(plugin._reminders[0]["due"]) > new_year
    saved = json.loads((tmp_path / "reminders.json").read_text(encoding="utf-8"))
    assert [r["content"] for r in saved] == ["提醒大家喝水"]


def test_followup_limits_and_switches(tmp_path) -> None:
    plugin, ctx = _plugin(tmp_path, {"reminder": {"followup_max_per_chat": 1}})
    plugin._load_reminders()
    assert not _run(plugin.handle_reminder_followup("下週聚餐", "2099-01-01 20:00", stream_id="g1"))["success"]
    ok = _run(plugin.handle_reminder_followup("小明 5 點考試", (datetime.now() + timedelta(hours=1)).strftime("%H:%M"), stream_id="g1"))
    assert ok["success"]
    assert not _run(plugin.handle_reminder_followup("再一件", (datetime.now() + timedelta(hours=2)).strftime("%H:%M"), stream_id="g1"))["success"]

    plugin.set_plugin_config(_cfg({"reminder": {"followup_enabled": False}}))
    _run(plugin._apply_reminder_switches())
    assert ("maibot-discord-adapter.reminder_followup", False) in ctx.toggles
    assert ("maibot-discord-adapter.reminder_set", True) in ctx.toggles


def test_reminders_migrate_from_legacy_plugin(tmp_path) -> None:
    data_dir = tmp_path / "maibot-discord-adapter"
    legacy_dir = tmp_path / "luca.reminder"
    data_dir.mkdir()
    legacy_dir.mkdir()
    (legacy_dir / "reminders.json").write_text(json.dumps([{"id": "abc123", "content": "舊的"}]), encoding="utf-8")
    plugin, _ = _plugin(data_dir)
    plugin._load_reminders()
    assert plugin._reminders[0]["id"] == "abc123"
    assert (data_dir / "reminders.json").exists() and (legacy_dir / "reminders.json").exists()


def test_out_of_window_followups_are_dropped_on_load(tmp_path) -> None:
    soon = (datetime.now() + timedelta(hours=3)).isoformat(timespec="seconds")
    later = (datetime.now() + timedelta(days=180)).isoformat(timespec="seconds")
    reminders = [
        {"id": "ok1", "auto": True, "due": soon, "content": "今晚打遊戲"},
        {"id": "bad", "auto": True, "due": later, "content": "半年後的演唱會"},
        {"id": "ask", "auto": False, "due": later, "content": "有人請麥麥半年後提醒"},
    ]
    (tmp_path / "reminders.json").write_text(json.dumps(reminders, ensure_ascii=False), encoding="utf-8")
    plugin, _ = _plugin(tmp_path)
    plugin._load_reminders()
    assert [r["id"] for r in plugin._reminders] == ["ok1", "ask"], "只清掉超过 24 小时的顺手记下"
    assert [r["id"] for r in json.loads((tmp_path / "reminders.json").read_text(encoding="utf-8"))] == ["ok1", "ask"]


# ===== 日常生活 =====


def _life(tmp_path, replies, presence_calls):
    daily_module = _load_adapter_module("src.life.daily_life")
    ctx = FakeCtx(tmp_path, llm_replies=replies)

    async def set_presence(text, activity_type, status):
        presence_calls.append((text, status))
        return {"success": True}

    async def recent_chats(since, until):
        return "小明：晚上九點一起打遊戲？\n麥麥：好啊"

    return daily_module.DailyLife(ctx, tmp_path / "life.json", set_presence, recent_chats), ctx


def _life_config(**overrides):
    config_module = _load_adapter_module("config")
    return config_module.LifeConfig(enabled=True, **overrides)


def test_life_generates_injects_and_pushes_presence(tmp_path) -> None:
    presence: list = []
    hours = _hours("日常")
    hours[12] = {**hours[12], "activity": "在學餐吃午飯", "online_status": "idle"}
    del hours[5]  # 缺的时段沿用前一个
    life, ctx = _life(tmp_path, ["```json\n" + json.dumps(hours, ensure_ascii=False) + "\n```"], presence)

    _run(life.tick(NOW, _life_config()))
    assert life.state["date"] == "2026-11-02" and life.state["hours"][5]["activity"] == "日常4"
    assert presence == [("✨ 日常12", "idle")]
    assert "在學餐吃午飯" in life.prompt_text(NOW) and "13 點 日常13" in life.prompt_text(NOW)
    assert ctx.tasks == ["replyer"], "日常生活默认沿用 replyer 模型任务"

    _run(life.tick(NOW + timedelta(minutes=5), _life_config()))
    assert len(presence) == 1 and len(ctx.prompts) == 1, "同一时段不重复推送、不重复生成"


def test_life_adjusts_only_future_hours(tmp_path) -> None:
    presence: list = []
    revised = {"changed": True, "reason": "答應晚上九點一起打遊戲", "hours": [{"hour": 21, "activity": "和小明打遊戲"}]}
    life, ctx = _life(tmp_path, [_hours("原本"), revised], presence)
    config = _life_config(adjust_interval_hours=3)
    _run(life.tick(NOW, config))
    _run(life.tick(NOW + timedelta(hours=3), config))

    assert "晚上九點一起打遊戲" in ctx.prompts[1]
    assert life.state["hours"][21]["activity"] == "和小明打遊戲"
    assert life.state["hours"][20]["activity"] == "原本20" and life.state["hours"][3]["activity"] == "原本3"
    assert life.state["note"] == "答應晚上九點一起打遊戲"


def test_life_generation_failure_backs_off(tmp_path) -> None:
    life, ctx = _life(tmp_path, ["不是 JSON"], [])
    _run(life.tick(NOW, _life_config()))
    _run(life.tick(NOW + timedelta(minutes=1), _life_config()))
    assert len(ctx.prompts) == 1 and life.current(NOW) is None


def test_life_state_is_injected_into_system_prompt(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path, {"life": {"enabled": True}})
    daily_module = _load_adapter_module("src.life.daily_life")
    plugin._daily_life = daily_module.DailyLife(plugin._ctx, tmp_path / "life.json", None, None)
    plugin._daily_life._state = {"date": "x", "hours": _hours("正在")}
    items = [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "人設"}]}, {"item_type": "UserMessageItem"}]

    result = _run(plugin.inject_life_state_into_planner(items=items, session_id="s1"))
    assert "modified_kwargs" in result and "你現在的生活狀態" in items[0]["parts"][-1]["text"]

    plugin.set_plugin_config(_cfg({"life": {"enabled": False}}))
    assert _run(plugin.inject_life_state_into_replyer(items=[], session_id="s1")) == {"action": "continue"}


# ===== 主动话题 =====


def _scout(tmp_path, replies):
    topics_module = _load_adapter_module("src.life.topics")
    ctx = FakeCtx(tmp_path, llm_replies=replies)

    async def resolve(channel_id):
        return await ctx._stream_by_group(channel_id) and f"s-{channel_id}"

    return topics_module.TopicScout(ctx, tmp_path / "topics.json", resolve), ctx


def _topic_config(**overrides):
    config_module = _load_adapter_module("config")
    return config_module.TopicConfig(enabled=True, channel_list=["111", "222"], cron="0 12 * * *", **overrides)


def test_topics_wait_for_cron_then_ask_bot_to_find_and_share(tmp_path) -> None:
    replies = [{"topics": [{"keyword": "音遊", "why": "大家常聊"}, {"keyword": "貓咪咖啡廳"}]}]
    scout, ctx = _scout(tmp_path, replies)
    config = _topic_config()

    _run(scout.tick(NOW - timedelta(minutes=30), config))
    assert scout.next_due == datetime(2026, 11, 2, 12, 0) and not ctx.triggered, "第一次只排程，不立刻发"

    _run(scout.tick(NOW, config))
    assert len(ctx.triggered) == 1, "频道 222 没有会话，跳过"
    stream_id, intent, reason = ctx.triggered[0]
    assert stream_id == "s-111" and reason == "主動話題"
    assert "音遊：大家常聊" in intent and "貓咪咖啡廳" in intent and "最近的新聞" in intent, "影片默认关闭"
    assert "自己決定用什麼搜尋或瀏覽工具" in intent, "不替她指定搜索工具"
    assert "大家喜歡音遊和貓" in ctx.prompts[0] and set(ctx.tasks) == {"planner"}
    assert scout.next_due == datetime(2026, 11, 3, 12, 0)
    saved = json.loads((tmp_path / "topics.json").read_text(encoding="utf-8"))
    assert saved["history"]["s-111"][0]["topic"] == "音遊、貓咪咖啡廳"


def test_topics_kind_follows_settings_and_avoids_recent(tmp_path) -> None:
    scout, ctx = _scout(tmp_path, [{"topics": [{"keyword": "貓咪影片"}]}, {"topics": [{"keyword": "新番"}]}])
    video_only = _topic_config(news_enabled=False, video_enabled=True)
    assert _run(scout.run_channel("111", NOW, video_only)) == "貓咪影片"
    assert "影片" in ctx.triggered[0][1] and "新聞" not in ctx.triggered[0][1]

    _run(scout.run_channel("111", NOW, _topic_config()))
    assert "貓咪影片" in ctx.prompts[1], "挑话题时避开最近开过的"


def test_topics_skip_when_no_topic_picked(tmp_path) -> None:
    scout, ctx = _scout(tmp_path, [{"topics": []}])
    assert _run(scout.run_channel("111", NOW, _topic_config())) is None
    assert not ctx.triggered


# ===== 后台轮询 =====


def test_daily_loop_fires_reminders_and_runs_life(tmp_path, monkeypatch) -> None:
    plugin_module = _load_adapter_module("plugin")
    monkeypatch.setattr(plugin_module, "DAILY_POLL_SECONDS", 0)
    plugin, ctx = _plugin(tmp_path, {"life": {"enabled": True, "sync_presence": False}})
    ctx.llm_replies.append(_hours("日常"))
    (tmp_path / "reminders.json").write_text(
        json.dumps([{"id": "r1", "stream_id": "g1", "cron": "", "recurring": False, "auto": False,
                     "content": "交報告", "due": "2000-01-01T00:00:00", "target": None}]),
        encoding="utf-8",
    )

    async def scenario():
        await plugin._start_daily_features()
        for _ in range(50):
            await asyncio.sleep(0)
            if plugin._daily_life.state.get("hours") and not plugin._reminders:
                break
        plugin._daily_task.cancel()

    _run(scenario())
    assert ctx.triggered and "交報告" in ctx.triggered[0][1]
    assert plugin._daily_life.state["hours"][0]["activity"] == "日常0"


# ===== WebUI =====


def test_webui_pages_show_life_schedule_and_topic_history(tmp_path) -> None:
    plugin, ctx = _plugin(
        tmp_path, {"life": {"enabled": True}, "topics": {"enabled": True, "channel_list": ["111", "222"]}}
    )

    async def all_streams(platform="all_platforms"):
        return [{"stream_id": "s-111", "group_name": "聊天大廳", "user_cardname": None, "user_nickname": "x"}]

    ctx.chat.get_all_streams = all_streams
    now = datetime.now()
    plugin._daily_life = _load_adapter_module("src.life.daily_life").DailyLife(ctx, tmp_path / "life.json", None, None)
    plugin._daily_life._state = {
        "date": now.date().isoformat(),
        "adjusted_at": "2026-11-02T09:00:00",
        "hours": _hours("正在"),
        "note": "答應晚上打遊戲",
    }
    plugin._topic_scout = _load_adapter_module("src.life.topics").TopicScout(ctx, tmp_path / "topics.json", None)
    old = {"time": "2026-11-01T12:00:00", "source": "news", "topic": "音遊", "title": "舊的", "url": "https://a"}
    new = {"time": "2026-11-02T12:00:00", "source": "video", "topic": "貓", "title": "影片：貓", "url": ""}
    plugin._topic_scout._state = {"history": {"s-111": [old, new]}}

    life = _run(plugin.webui_life())
    assert life["notice"] == "" and life["current"] == f"正在{now.hour}（普通）"
    assert len(life["hours"]) == 24 and life["hours"][now.hour]["time"].startswith("▶")
    assert life["adjusted_at"] == "11-02 09:00" and life["note"] == "答應晚上打遊戲"

    topics = _run(plugin.webui_topics())
    assert [row["title"] for row in topics["history"]] == ["影片：貓", "舊的"], "新的在前"
    assert topics["history"][0]["chat"] == "聊天大廳" and topics["history"][0]["kind"] == "影片"
    assert topics["channels"][0]["chat"] == "聊天大廳" and "不認得" in topics["channels"][1]["chat"]
    assert topics["next_due"] == "—" and topics["shared_count"] == 2

    plugin.set_plugin_config(_cfg({}))
    assert "未啟用" in _run(plugin.webui_life())["notice"]
    assert "未啟用" in _run(plugin.webui_topics())["notice"]


# ===== 睡觉时不回复 =====


def _sleeping_plugin(tmp_path, asleep=True):
    plugin, ctx = _plugin(tmp_path, {"life": {"enabled": True}})
    hours = _hours("醒著")
    hours[datetime.now().hour] = {**hours[datetime.now().hour], "activity": "睡覺", "asleep": asleep}
    plugin._daily_life = _load_adapter_module("src.life.daily_life").DailyLife(ctx, tmp_path / "life.json", None, None)
    plugin._daily_life._state = {"date": datetime.now().date().isoformat(), "hours": hours}
    return plugin, ctx


def _planner_kwargs(*tool_names):
    return {
        "items": [{"item_type": "SystemMessageItem", "parts": [{"type": "text", "text": "人設"}]}],
        "tool_definitions": [{"type": "function", "function": {"name": name}} for name in tool_names],
        "session_id": "s1",
    }


def test_asleep_flag_is_parsed_and_old_schedules_count_as_awake() -> None:
    daily_module = _load_adapter_module("src.life.daily_life")
    hours = daily_module.normalize_hours(
        [{"hour": 0, "activity": "睡覺", "asleep": True}, {"hour": 1, "activity": "熟睡", "asleep": "true"}, {"hour": 8, "activity": "起床"}]
    )
    assert [hours[h]["asleep"] for h in (0, 1, 8)] == [True, True, False]
    assert "💤" in daily_module.format_hours(hours).splitlines()[0], "调整时程时模型看得到哪些时段在睡觉"
    life = daily_module.DailyLife(None, Path("unused.json"), None, None)
    life._state = {"hours": [{**entry, "asleep": False} for entry in hours]}
    life._state["hours"][3].pop("asleep")  # 旧时程没有 asleep
    assert life.asleep(NOW.replace(hour=3)) is False


def test_sleep_silences_discord_planner_until_woken(tmp_path) -> None:
    plugin, ctx = _sleeping_plugin(tmp_path)

    kwargs = _planner_kwargs("reply", "stop", "discord_emote")
    result = _run(plugin.silence_planner_while_asleep(**kwargs))
    assert [t["function"]["name"] for t in result["modified_kwargs"]["tool_definitions"]] == ["stop"]
    assert "睡眠中" in kwargs["items"][0]["parts"][-1]["text"]
    assert ctx.adjusts == [("s1", 0)] and plugin._silenced_sessions == {"s1"}

    assert _run(plugin.silence_planner_while_asleep(**_planner_kwargs("reply", "stop"))) == {"action": "continue"}, (
        "不是 Discord 聊天不管"
    )

    _run(plugin._wake_up())
    assert ctx.adjusts[-1] == ("s1", 1.0) and not plugin._silenced_sessions
    assert _run(plugin.silence_planner_while_asleep(**_planner_kwargs("reply", "discord_emote"))) == {
        "action": "continue"
    }, "叫醒后照常回复"


def test_sleep_silence_respects_setting_and_awake_hours(tmp_path) -> None:
    plugin, _ = _sleeping_plugin(tmp_path, asleep=False)
    assert not plugin._sleeping(datetime.now())
    plugin, _ = _sleeping_plugin(tmp_path)
    plugin.set_plugin_config(_cfg({"life": {"enabled": True, "sleep_silence": False}}))
    assert not plugin._sleeping(datetime.now())
    assert "照常回覆" in _run(plugin.webui_life())["sleep_state"]


# ===== 页面设置 =====


def test_page_settings_migrate_from_config_then_override_it(tmp_path) -> None:
    plugin, _ = _plugin(
        tmp_path, {"life": {"life_setting": "住台北"}, "topics": {"channel_list": ["111"], "cron": "0 9 * * *"}}
    )
    path = tmp_path / "daily_settings.json"
    plugin._load_page_settings(path)
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["life"] == {"life_setting": "住台北"}
    assert saved["topics"] == {"channel_list": ["111"], "cron": "0 9 * * *"}
    assert saved["voice"]["idle_timeout_sec"] == 300, "语音页面管的字段也一起搬过去"

    _run(plugin.webui_life_save_setting("  住高雄  "))
    _run(plugin.webui_topics_add_channel("222"))
    _run(plugin.webui_topics_save_cron("0  20 * * *"))
    _run(plugin.webui_topics_remove_channel("111"))
    settings = plugin._load_settings()
    assert settings.life.life_setting == "住高雄"
    assert settings.topics.channel_list == ["222"] and settings.topics.cron == "0 20 * * *"

    # 重新加载配置（例如在插件设置页存档）后，页面设置仍然生效
    plugin.set_plugin_config(_cfg({"life": {"life_setting": "舊的"}}))
    assert plugin._load_settings().life.life_setting == "住高雄"
    for bad in (lambda: plugin.webui_topics_add_channel("abc"), lambda: plugin.webui_topics_save_cron("每天")):
        with pytest.raises(ValueError):
            _run(bad())

    schema = plugin.get_webui_config_schema()
    assert "life_setting" not in schema["sections"]["life"]["fields"]
    assert not {"channel_list", "cron"} & set(schema["sections"]["topics"]["fields"])


# ===== 语音页面 =====


def test_voice_page_saves_only_filled_fields_and_reconnects(tmp_path) -> None:
    plugin, _ = _plugin(tmp_path, {"voice": {"enabled": True, "idle_timeout_sec": 120}})
    plugin._load_page_settings(tmp_path / "daily_settings.json")
    restarts: list = []

    async def restart():
        restarts.append(True)

    plugin._restart_if_needed = restart

    status = _run(plugin.webui_voice())
    assert status["current_channel"] == "未連線" and status["connected"] is False
    assert {"item": "空閒退出（秒）", "value": "120"} in status["settings"]

    _run(plugin.webui_voice_save(voice_mode="fixed", fixed_channel_id="123", enable_vad="false", idle_timeout_sec=" 60 "))
    voice = plugin._load_settings().voice
    assert (voice.voice_mode, voice.fixed_channel_id, voice.enable_vad, voice.idle_timeout_sec) == ("fixed", "123", False, 60)
    assert voice.vad_threshold_db == -50.0, "没填的不动"
    _run(plugin.webui_voice_add_channel("456"))
    assert plugin._load_settings().voice.auto_channel_list == ["456"]
    _run(plugin.webui_voice_remove_channel("456"))
    assert plugin._load_settings().voice.auto_channel_list == [] and len(restarts) == 3

    for bad in (
        lambda: plugin.webui_voice_save(idle_timeout_sec="很久"),
        lambda: plugin.webui_voice_save(),
        lambda: plugin.webui_voice_save(fixed_channel_id="abc"),
    ):
        with pytest.raises(ValueError):
            _run(bad())
    assert len(restarts) == 3 and plugin._load_settings().voice.idle_timeout_sec == 60, "格式不对时不存也不重连"

    schema = plugin.get_webui_config_schema()
    assert set(schema["sections"]["voice"]["fields"]) == {"enabled", "tts_provider", "stt_provider"}
