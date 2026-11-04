"""日常生活时程：每天生成一次麦麦每小时在做什么，定期按最近聊天调整，并把当前状态注入对话和 Discord 状态。

状态保存在插件数据目录的 life_schedule.json：
``{"date": "YYYY-MM-DD", "generated_at": iso, "adjusted_at": iso, "hours": [24 个时段], "note": 最近一次调整原因}``
每个时段 ``{"hour", "activity", "mood", "status_text", "online_status", "asleep"}``；旧时程没有 asleep 时视为醒着。
模型看到的文字用繁体中文。
"""

from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict, List, Optional

from .common import generate_json, load_state, persona_text, save_state

ONLINE_STATUSES = ("online", "idle", "dnd")
STATUS_TEXT_LIMIT = 40
# 注入时带上接下来几个时段，麦麦被问「等等要干嘛」时答得出来
UPCOMING_HOURS = 3
RETRY_DELAY = timedelta(minutes=10)

HOUR_FORMAT = (
    '每個時段是 {"hour": 0-23, "activity": "在做什麼（具體、生活化，15 字內）", "mood": "心情（5 字內）", '
    '"status_text": "Discord 狀態文字，開頭一個 emoji，20 字內", "online_status": "online / idle / dnd 其一，'
    '睡覺或不方便回訊息用 idle，專心做事不想被打擾用 dnd", "asleep": true / false（在睡覺時 true，含午睡和補眠；睡前滑手機這類還醒著的是 false）}'
)


def normalize_hours(raw: Any, fallback: Optional[List[Dict[str, Any]]] = None) -> Optional[List[Dict[str, Any]]]:
    """把模型给的时段列表整理成按小时排好的 24 项；缺的小时沿用 fallback 或前一个时段，完全不可用时返回 None。"""
    if isinstance(raw, dict):
        raw = raw.get("hours")
    if not isinstance(raw, list):
        return None
    by_hour: Dict[int, Dict[str, Any]] = {}
    for item in raw:
        if not isinstance(item, dict):
            continue
        try:
            hour = int(item.get("hour"))
        except (TypeError, ValueError):
            continue
        activity = str(item.get("activity") or "").strip()
        if not 0 <= hour <= 23 or not activity:
            continue
        status = str(item.get("online_status") or "online").strip().lower()
        by_hour[hour] = {
            "hour": hour,
            "activity": activity,
            "mood": str(item.get("mood") or "").strip(),
            "status_text": str(item.get("status_text") or activity).strip()[:STATUS_TEXT_LIMIT],
            "online_status": status if status in ONLINE_STATUSES else "online",
            "asleep": str(item.get("asleep")).strip().lower() in ("true", "1", "yes"),
        }
    if not by_hour:
        return None
    hours: List[Dict[str, Any]] = []
    for hour in range(24):
        entry = by_hour.get(hour)
        if entry is None:
            previous = fallback[hour] if fallback else (hours[-1] if hours else next(iter(by_hour.values())))
            entry = {**previous, "hour": hour}
        hours.append(entry)
    return hours


def format_hours(hours: List[Dict[str, Any]], start: int = 0) -> str:
    return "\n".join(
        f"{entry['hour']:02d}:00 {entry['activity']}（{entry['mood'] or '—'}）[{entry['online_status']}] {entry['status_text']}"
        f"{' 💤睡覺中' if entry.get('asleep') else ''}"
        for entry in hours[start:]
    )


class DailyLife:
    """管理当天的生活时程：生成、调整、查询当前状态并同步 Discord 状态。"""

    def __init__(
        self,
        ctx: Any,
        path: Path,
        set_presence: Callable[[str, str, str], Awaitable[Dict[str, Any]]],
        recent_chats: Callable[[datetime, datetime], Awaitable[str]],
    ) -> None:
        """
        Args:
            ctx: 插件上下文，用到 llm、personality 和 logger。
            path: 状态文件路径。
            set_presence: ``(文字, 显示方式, 在线状态)`` 改 Discord 状态。
            recent_chats: ``(起, 止)`` 返回这段时间各聊天的可读记录，用于调整时程。
        """
        self._ctx = ctx
        self._path = path
        self._set_presence = set_presence
        self._recent_chats = recent_chats
        self._state: Dict[str, Any] = load_state(path)
        # 已推送到 Discord 的「日期 小时」，换时段或时程变动后才重新推送
        self._pushed_key = ""
        # 生成失败后等一阵再试，避免每次轮询都打一次 LLM
        self._retry_at: Optional[datetime] = None

    @property
    def state(self) -> Dict[str, Any]:
        return self._state

    def current(self, now: datetime) -> Optional[Dict[str, Any]]:
        """当前时段；还没生成过时程时返回 None。生成前沿用前一天同一时段。"""
        hours = self._state.get("hours")
        if not isinstance(hours, list) or len(hours) != 24:
            return None
        return hours[now.hour]

    def asleep(self, now: datetime) -> bool:
        """时程上现在是不是在睡觉；没有时程时当作醒着。"""
        entry = self.current(now)
        return bool(entry and entry.get("asleep"))

    def prompt_text(self, now: datetime) -> str:
        """注入对话的当前生活状态，没有时程时返回空字符串。"""
        entry = self.current(now)
        if entry is None:
            return ""
        hours = self._state["hours"]
        upcoming = "；".join(
            f"{(now.hour + offset) % 24:02d} 點 {hours[(now.hour + offset) % 24]['activity']}"
            for offset in range(1, UPCOMING_HOURS + 1)
        )
        mood = f"（心情：{entry['mood']}）" if entry["mood"] else ""
        return (
            f"【你現在的生活狀態】現在是 {now.strftime('%H:%M')}，你正在{entry['activity']}{mood}。接下來：{upcoming}。"
            "這是你自己的生活，聊天時可以自然帶到，但不用每句都提，也別照抄這段。"
        )

    async def tick(self, now: datetime, config: Any) -> None:
        """后台轮询调用：到点生成当天时程、隔一段时间按聊天调整、换时段时同步 Discord 状态。"""
        today = now.date().isoformat()
        if self._state.get("date") != today and now.time() >= _parse_clock(config.generate_time):
            if self._retry_at is None or now >= self._retry_at:
                self._retry_at = None if await self.generate(now, config) else now + RETRY_DELAY
        elif self._state.get("date") == today and self._adjust_due(now, config):
            await self.adjust(now, config)
        if config.sync_presence:
            await self._push_presence(now)

    def _adjust_due(self, now: datetime, config: Any) -> bool:
        if config.adjust_interval_hours <= 0:
            return False
        last = self._state.get("adjusted_at") or self._state.get("generated_at")
        return not last or now - datetime.fromisoformat(last) >= timedelta(hours=config.adjust_interval_hours)

    async def generate(self, now: datetime, config: Any) -> bool:
        """生成今天 24 个时段；失败时保留旧时程，下次轮询再试。"""
        persona = await persona_text(self._ctx)
        prompt = (
            "你要替一個聊天機器人角色安排今天的生活，像真人一樣有作息、吃飯、休閒和偶爾的小意外。\n"
            f"今天是 {now.strftime('%Y-%m-%d')} 星期{'一二三四五六日'[now.weekday()]}。\n"
            f"角色人設：\n{persona or '（未提供）'}\n"
            f"生活設定：{config.life_setting or '（未提供，按人設合理推想）'}\n\n"
            f"請輸出 JSON 陣列，剛好 24 個時段（hour 0 到 23）。{HOUR_FORMAT}\n只輸出 JSON。"
        )
        hours = normalize_hours(await generate_json(self._ctx, prompt, config.model_task))
        if hours is None:
            self._ctx.logger.warning("日常生活时程生成失败，稍后重试")
            return False
        stamp = now.isoformat(timespec="seconds")
        self._state = {"date": now.date().isoformat(), "generated_at": stamp, "adjusted_at": stamp, "hours": hours, "note": "今日初始安排"}
        self._save()
        self._ctx.logger.info(f"已生成今天的日常生活时程，现在：{hours[now.hour]['activity']}")
        return True

    async def adjust(self, now: datetime, config: Any) -> bool:
        """按上次调整以来的聊天调整之后的时段；聊天里没提到相关安排时只更新调整时间。"""
        last = datetime.fromisoformat(self._state.get("adjusted_at") or self._state["generated_at"])
        chats = await self._recent_chats(last, now)
        self._state["adjusted_at"] = now.isoformat(timespec="seconds")
        if not chats:
            self._save()
            return False
        hours = self._state["hours"]
        prompt = (
            "以下是一個聊天機器人角色今天的生活時程，以及它最近參與的聊天。\n"
            f"現在是 {now.strftime('%H:%M')}。\n\n今天剩下的時程：\n{format_hours(hours, now.hour)}\n\n"
            f"最近的聊天：\n{chats}\n\n"
            "如果聊天裡有影響它接下來安排的事（答應了別人什麼、約了要一起做什麼、被提醒要做什麼、自己說了等等要去幹嘛），"
            "就調整從現在這個小時開始的時段；沒有的話不要改。\n"
            f'輸出 JSON：{{"changed": true/false, "reason": "一句話說明", "hours": [從 {now.hour} 點到 23 點的時段]}}。'
            f"{HOUR_FORMAT}\n只輸出 JSON。"
        )
        result = await generate_json(self._ctx, prompt, config.model_task)
        if not isinstance(result, dict) or not result.get("changed"):
            self._save()
            return False
        revised = normalize_hours(result, fallback=hours)
        if revised is None:
            self._save()
            return False
        # 已经过去的时段不改，只换现在和之后的
        self._state["hours"] = hours[: now.hour] + revised[now.hour :]
        self._state["note"] = str(result.get("reason") or "").strip()
        self.invalidate_presence()
        self._save()
        self._ctx.logger.info(f"日常生活时程已按聊天调整：{self._state['note']}")
        return True

    def invalidate_presence(self) -> None:
        """Discord 客户端重建后调用，下次轮询重新推送当前时段的状态。"""
        self._pushed_key = ""

    async def _push_presence(self, now: datetime) -> None:
        entry = self.current(now)
        key = f"{self._state.get('date')} {now.hour}"
        if entry is None or key == self._pushed_key:
            return
        result = await self._set_presence(entry["status_text"], "custom", entry["online_status"])
        if result.get("success"):
            self._pushed_key = key
        else:
            # Discord 还没连上时也会失败，下次轮询再试
            self._ctx.logger.debug(f"同步日常状态到 Discord 失败：{result.get('error')}")

    def _save(self) -> None:
        save_state(self._path, self._state)


def _parse_clock(value: str) -> time:
    try:
        return datetime.strptime(str(value).strip(), "%H:%M").time()
    except ValueError:
        return time(0, 0)
