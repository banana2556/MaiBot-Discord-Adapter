from types import SimpleNamespace
import asyncio
import logging

from test_typing_indicator import _load_adapter_module


def _connect(members):
    """自动模式连进有这些成员的频道，返回连线后空闲计时是否在跑。"""
    vm_module = _load_adapter_module("src.voice.voice_manager")
    vm = vm_module.VoiceManager(
        bot=SimpleNamespace(), logger=logging.getLogger("test_voice_idle"), auto_channel_list=["1"]
    )
    voice_client = SimpleNamespace(is_connected=lambda: True)

    async def channel_connect(**kwargs):
        return voice_client

    channel = SimpleNamespace(
        id=1, name="語音", guild=SimpleNamespace(name="g"), members=members, connect=channel_connect
    )

    async def resolve(channel_id):
        return channel

    vm._resolve_voice_channel = resolve
    vm._get_channel_permissions = lambda channel: None
    vm._describe_channel_permissions = lambda channel: ""

    async def run():
        assert await vm.connect(1)
        task = vm._idle_timer_task
        alive = task is not None and not task.done()
        vm._cancel_idle_timer()
        return alive

    return asyncio.run(run())


def test_connect_into_empty_channel_starts_idle_timer() -> None:
    assert _connect([SimpleNamespace(bot=True)]), "只剩自己时要开始计时，否则永远不会离开"


def test_connect_with_people_does_not_start_idle_timer() -> None:
    assert not _connect([SimpleNamespace(bot=True), SimpleNamespace(bot=False)])
