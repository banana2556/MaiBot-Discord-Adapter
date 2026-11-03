from types import SimpleNamespace
import asyncio

import discord
import pytest

from test_typing_indicator import _load_adapter_module, _load_plugin_class


def test_build_presence_maps_config_to_discord_objects() -> None:
    build = _load_adapter_module("src.recv_handler.discord_client").build_presence

    activity, status = build("在想晚餐", "custom", "idle")
    assert isinstance(activity, discord.CustomActivity) and activity.name == "在想晚餐"
    assert status is discord.Status.idle

    activity, _ = build("Minecraft", "playing")
    assert activity.type is discord.ActivityType.playing and activity.name == "Minecraft"

    assert build("   ") == (None, discord.Status.online)
    assert len(build("字" * 200)[0].name) == 128
    with pytest.raises(ValueError):
        build("x", "streaming")
    with pytest.raises(ValueError):
        build("x", status="away")


def test_set_presence_applies_now_and_survives_reconnect() -> None:
    client_module = _load_adapter_module("src.recv_handler.discord_client")
    manager = client_module.DiscordClientManager.__new__(client_module.DiscordClientManager)
    calls: list[dict] = []

    async def change_presence(**kwargs):
        calls.append(kwargs)

    manager.client = SimpleNamespace(change_presence=change_presence)
    manager.is_connected = False
    asyncio.run(manager.set_presence("离线时设的"))
    # 没连上时只记下来，等重建客户端登录时带上
    assert calls == [] and manager._presence[0].name == "离线时设的"

    manager.is_connected = True
    asyncio.run(manager.set_presence("", status="dnd"))
    assert calls == [{"activity": None, "status": discord.Status.dnd}]


def test_presence_config_defaults_and_trims_text() -> None:
    config_module = _load_adapter_module("config")
    settings = config_module.DiscordPluginSettings()
    assert (settings.presence.status, settings.presence.activity_type, settings.presence.activity_text) == (
        "online",
        "custom",
        "",
    )
    assert len(config_module.DiscordPresenceConfig(activity_text="字" * 200).activity_text) == 128


def test_presence_api_reports_bad_values() -> None:
    plugin_cls = _load_plugin_class()
    plugin = plugin_cls.__new__(plugin_cls)
    plugin._client_manager = None
    assert asyncio.run(plugin.set_presence("x"))["success"] is False

    client_module = _load_adapter_module("src.recv_handler.discord_client")
    manager = client_module.DiscordClientManager.__new__(client_module.DiscordClientManager)
    manager.client, manager.is_connected = None, False
    plugin._client_manager = manager
    assert "显示方式" in asyncio.run(plugin.set_presence("x", activity_type="streaming"))["error"]
    assert asyncio.run(plugin.set_presence("x", activity_type="watching"))["success"] is True
