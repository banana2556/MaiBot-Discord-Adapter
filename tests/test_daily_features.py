from datetime import datetime, timedelta
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

    def __init__(self, data_dir, llm_replies=None, search_results=None):
        self.plugin_id = "maibot-discord-adapter"
        self.paths = SimpleNamespace(data_dir=data_dir)
        self.logger = logging.getLogger("test_daily_features")
        self.llm_replies = list(llm_replies or [])
        self.prompts: list = []
        self.triggered: list = []
        self.toggles: list = []
        self.searches: list = []
        self.search_results = search_results or []
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
        self.web = SimpleNamespace(search=self._search)
        self.personality = SimpleNamespace(get_current=self._persona)
        self.component = SimpleNamespace(enable_component=self._toggle(True), disable_component=self._toggle(False))
        self.chat = SimpleNamespace(get_stream_by_group_id=self._stream_by_group)

    async def _trigger(self, stream_id, intent, **kwargs):
        self.triggered.append((stream_id, intent, kwargs.get("reason")))
        return {"success": True}

    async def _append(self, stream_id, segments, **kwargs):
        return {"success": True}

    async def _get_by_id(self, message_id, stream_id=""):
        return self.messages.get(message_id)

    async def _build_readable(self, messages, **kwargs):
        return {"success": True, "text": "小明：晚上九點一起打遊戲？\n麥麥：好啊"}

    async def _generate(self, prompt, model=""):
        self.prompts.append(prompt)
        reply = self.llm_replies.pop(0) if self.llm_replies else ""
        return {"success": True, "response": reply if isinstance(reply, str) else json.dumps(reply, ensure_ascii=False)}

    async def _search(self, query, max_results=5):
        self.searches.append(query)
        return {"results": self.search_results}

    async def _persona(self):
        return {"name": "麥麥", "personality": "活潑的女大學生"}

    async def _stream_by_group(self, group_id, platform="qq"):
        return {"stream_id": f"s-{group_id}"} if group_id == "111" else None

    async def call_capability(self, capability, **kwargs):
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


def _scout(tmp_path, replies, search_results):
    topics_module = _load_adapter_module("src.life.topics")
    ctx = FakeCtx(tmp_path, llm_replies=replies, search_results=search_results)

    async def resolve(channel_id):
        return await ctx._stream_by_group(channel_id) and f"s-{channel_id}"

    return topics_module.TopicScout(ctx, tmp_path / "topics.json", resolve), ctx


def _topic_config(**overrides):
    config_module = _load_adapter_module("config")
    return config_module.TopicConfig(enabled=True, channel_list=["111", "222"], cron="0 12 * * *", **overrides)


def test_topics_wait_for_cron_then_share_news(tmp_path) -> None:
    results = [
        {"title": "音遊新曲上線", "url": "https://example.com/a", "snippet": "新曲"},
        {"title": "貓咪咖啡廳開幕", "url": "https://example.com/b", "snippet": "貓"},
    ]
    replies = [{"topics": [{"keyword": "音遊", "why": "大家常聊"}]}, {"index": 1, "summary": "新的貓咪咖啡廳"}]
    scout, ctx = _scout(tmp_path, replies, results)
    config = _topic_config()

    _run(scout.tick(NOW - timedelta(minutes=30), config))
    assert scout.next_due == datetime(2026, 11, 2, 12, 0) and not ctx.triggered, "第一次只排程，不立刻发"

    _run(scout.tick(NOW, config))
    assert ctx.searches == ["音遊 新聞"]
    assert len(ctx.triggered) == 1, "频道 222 没有会话，跳过"
    stream_id, intent, reason = ctx.triggered[0]
    assert stream_id == "s-111" and "https://example.com/b" in intent and reason == "主動話題・新聞"
    assert "大家喜歡音遊和貓" in ctx.prompts[0]
    assert scout.next_due == datetime(2026, 11, 3, 12, 0)
    saved = json.loads((tmp_path / "topics.json").read_text(encoding="utf-8"))
    assert saved["history"]["s-111"][0]["url"] == "https://example.com/b"


def test_topics_video_asks_bot_to_use_browser_tool(tmp_path) -> None:
    scout, ctx = _scout(tmp_path, [{"topics": [{"keyword": "貓咪影片"}]}], [])
    shared = _run(scout.run_channel("111", NOW, _topic_config(news_enabled=False, video_enabled=True)))
    assert shared == "影片：貓咪影片"
    assert "瀏覽器" in ctx.triggered[0][1] and "B 站或 X" in ctx.triggered[0][1]
    assert not ctx.searches


def test_topics_skip_when_model_rejects_all_news(tmp_path) -> None:
    scout, ctx = _scout(tmp_path, [{"topics": [{"keyword": "音遊"}]}, {"index": -1}], [{"title": "廣告", "url": "https://x"}])
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
