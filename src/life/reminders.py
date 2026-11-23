"""定时提醒：到点唤醒麦麦在聊天里说话，提醒保存在插件数据目录的 reminders.json，重启后继续生效。

移植自 maibot-reminder-plugin（luca.reminder），工具名、指令和提示词保持不变：
- 被要求的提醒（reminder_set）：标准 5 段 cron，一次性或重复，到点一定提醒。
- 顺手记下（reminder_followup）：群友随口说到自己之后的安排时，麦麦自己记一个一次性提醒，到时说不说由麦麦决定。
  单独做成工具而不是 reminder_set 的参数，否则模型会把它当成「是不是正式提醒请求」二选一而放弃。
到点时通过 Maisaka 主动任务唤醒麦麦，由麦麦结合人设和上下文自己说出来；离线期间错过的提醒在下次启动后补发一次。
提醒可以带一条目标消息（msg_id）：到点前把它以真实 ID 放回上下文，麦麦就能用 attach_at @ 对方（attach_at 按消息找发送者）。
模型和用户看到的文字（工具说明、唤醒意图、指令回复、WebUI）一律用繁体中文，保持简短。
"""

from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

import json

from maibot_sdk import API, Command, Tool

from ...config import PAGE_FIELDS
from .common import save_state
from .cron import describe, next_run, parse_at, parse_cron

FOLLOWUP = "順手記下"
FOLLOWUP_WINDOW = timedelta(days=7)
FOLLOWUP_TOOL = "reminder_followup"
REMINDER_TOOLS = ("reminder_set", FOLLOWUP_TOOL, "reminder_list", "reminder_cancel")
LEGACY_PLUGIN_ID = "luca.reminder"
COMMANDS = [
    {"usage": "/reminders", "description": "列出目前聊天待觸發的提醒"},
    {"usage": "/reminder_cancel <id>", "description": "取消目前聊天的一則提醒"},
]
TOOLS = [
    {"name": "reminder_set", "description": "有人請麥麥提醒時設定，可一次或重複"},
    {"name": "reminder_followup", "description": "群友聊到未來一週內的安排（含模糊時間）時順手記下；有人要求提醒則只用 reminder_set"},
    {"name": "reminder_list / reminder_cancel", "description": "麥麥查看、取消目前聊天的提醒"},
]


def make_reminder(
    stream_id: str,
    content: str,
    due: datetime,
    *,
    cron: str = "",
    recurring: bool = False,
    auto: bool = False,
    target: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """生成一条提醒记录；没有聊天或内容为空时抛出 ValueError。"""
    if not stream_id:
        raise ValueError("無法確定目前聊天")
    if not content.strip():
        raise ValueError("提醒內容不能為空")
    return {
        "id": uuid4().hex[:6],
        "stream_id": stream_id,
        "cron": cron.strip(),
        "recurring": recurring,
        "auto": auto,
        "content": content.strip(),
        "due": due.isoformat(timespec="seconds"),
        "target": target,
    }


class ReminderMixin:
    """定时提醒的工具、指令、WebUI 接口与到期触发；由插件主类混入，轮询由插件的日常后台任务驱动。"""

    ctx: Any
    config: Any
    _reminders: List[Dict[str, Any]]
    _reminder_delivery: Dict[str, Dict[str, Any]]

    @property
    def _reminders_path(self) -> Path:
        return self.ctx.paths.data_dir / "reminders.json"

    def _load_reminders(self) -> None:
        """读回已保存的提醒；本插件还没有提醒文件时，从旧的定时提醒插件数据目录搬过来一次。"""
        path = self._reminders_path
        if not path.exists():
            legacy = path.parent.parent / LEGACY_PLUGIN_ID / "reminders.json"
            if legacy.exists():
                # 只复制不删除，旧插件的数据留着；两个插件同时启用会重复提醒，日志里提醒停用旧插件
                path.write_text(legacy.read_text(encoding="utf-8"), encoding="utf-8")
                self.ctx.logger.warning(
                    f"已从旧的定时提醒插件搬入提醒：{legacy}；请停用 {LEGACY_PLUGIN_ID}，否则提醒会重复触发"
                )
        self._reminders = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
        # 舊版本可能保存超出目前順手記下範圍的記錄；只清理超過一週的順手記下。
        latest = datetime.now() + FOLLOWUP_WINDOW
        stale = [r for r in self._reminders if r.get("auto") and datetime.fromisoformat(r["due"]) > latest]
        if stale:
            self._reminders = [r for r in self._reminders if r not in stale]
            self._save_reminders()
            self.ctx.logger.warning(
                f"順手記下只收一週內，已移除 {len(stale)} 則超出範圍的舊紀錄："
                + "；".join(f"{r['due']} {r['content']}" for r in stale)
            )

    async def _apply_reminder_switches(self) -> None:
        # 关闭时直接禁用工具，麦麦在 Planner 里就看不到它，不会白白调用一次再被拒绝
        reminder_config = self._load_settings().reminder
        switches = {name: reminder_config.enabled for name in REMINDER_TOOLS}
        switches[FOLLOWUP_TOOL] = reminder_config.enabled and reminder_config.followup_enabled
        component = self.ctx.component
        for name, enabled in switches.items():
            toggle = component.enable_component if enabled else component.disable_component
            result = await toggle(f"{self.ctx.plugin_id}.{name}", "tool")
            if not result["success"]:
                self.ctx.logger.warning(f"切换 {name} 工具失败：{result.get('error')}")

    async def _target(self, msg_id: str, stream_id: str) -> Optional[Dict[str, Any]]:
        """查出 msg_id 的发送者和原文，没给 msg_id 时返回 None，查不到时抛出 ValueError 让模型改正。"""
        if not msg_id:
            return None
        message = await self.ctx.message.get_by_id(msg_id, stream_id=stream_id)
        if not isinstance(message, dict) or "success" in message:  # None 为查无此消息，带 success 的是调用失败
            raise ValueError(f"找不到訊息 {msg_id}")
        user_info = message["message_info"]["user_info"]
        return {
            "msg_id": message["message_id"],
            "name": user_info["user_cardname"] or user_info["user_nickname"],
            "user_info": user_info,
            "text": message.get("processed_plain_text") or "",
        }

    async def _restore_target(self, reminder: Dict[str, Any]) -> Optional[str]:
        """把目标消息以真实 ID 放回上下文供 attach_at 解析，原消息可能已离开 Maisaka 历史或经过重启；成功时返回对方名字。"""
        target = reminder.get("target")  # 1.1 及更早保存的提醒没有 target
        if not target:
            return None
        result = await self.ctx.maisaka.context.append(
            reminder["stream_id"],
            [{"type": "text", "content": target["text"] or reminder["content"]}],
            message_id=target["msg_id"],
            replyable=True,
            user_info=target["user_info"],
            source_kind="reminder_target",
        )
        if not result["success"]:
            self.ctx.logger.warning(f"提醒 {reminder['id']} 的目标消息放回上下文失败，这次不 @：{result.get('error')}")
            return None
        return target["name"]

    async def fire_due_reminders(self, now: datetime) -> None:
        """唤醒麦麦发出所有到期提醒：重复的排到下一个匹配时刻，一次性的删除；调用异常的留到下次轮询重试。

        提前 drift_minutes 分钟唤醒，抵消麦麦思考和回复的耗时。
        """
        drift = timedelta(minutes=self._load_settings().reminder.drift_minutes)
        due = [reminder for reminder in self._reminders if datetime.fromisoformat(reminder["due"]) - drift <= now]
        for reminder in due:
            scheduled = datetime.fromisoformat(reminder["due"])
            try:
                name = await self._restore_target(reminder)
                if reminder["auto"]:
                    # 没人要求过，说不说、要不要 @ 都交给麦麦
                    intent = f"你之前順手記下：{reminder['content']}\n現在到時間了，覺得合適就自然地提起或關心一下，不合適也可以不說。"
                    if name:
                        intent += f"\n需要的話可以用 attach_at @ {name}（上面是對方之前的訊息）。"
                else:
                    # 指定對象的正式提醒必須 @，使用真實來源訊息 ID，而非只寫名字。
                    intent = f"到了設定的提醒時間，請現在就在聊天裡發出這則提醒：{reminder['content']}"
                    if name:
                        intent += (f"\n務必標注 {name}：在 reply 工具使用 attach_at=[\"{reminder['target']['msg_id']}\"]，"
                                   "不能只寫名字或只回覆訊息；上面已放回原訊息供解析。")
                    intent += "\n說清楚是誰請你提醒、提醒什麼、為什麼，別只丟一句沒頭沒腦的話。"
                late_minutes = int((now - scheduled).total_seconds() // 60)
                if late_minutes >= 1:
                    intent += f"\n原定 {describe(scheduled)}，已經晚了 {late_minutes} 分鐘。"
                # 睡觉时段也要叫醒她来提醒，否则静默中的聊天会把这次触发吞掉
                await self._wake_up()
                self._reminder_delivery[reminder["stream_id"]] = {"content": reminder["content"], "topic": None}
                self._focus.pop(reminder["stream_id"], None)
                result = await self.ctx.maisaka.proactive.trigger(
                    reminder["stream_id"], intent, reason=FOLLOWUP if reminder["auto"] else "定時提醒"
                )
                if not result["success"]:
                    self._reminder_delivery.pop(reminder["stream_id"], None)
            except Exception:
                self.ctx.logger.exception(f"提醒 {reminder['id']} 触发异常，稍后重试")
                continue
            if not result["success"]:
                self.ctx.logger.warning(
                    f"提醒 {reminder['id']} 无法触发，已删除：{result.get('error')}；内容：{reminder['content']}"
                )
            if reminder not in self._reminders:  # 触发期间已被取消
                continue
            if result["success"] and reminder["recurring"]:
                # 提前唤醒时 now 早于原定时刻，下一次要从原定时刻之后算起，否则同一时刻会再触发一次
                following = max(scheduled, now.replace(second=0, microsecond=0)) + timedelta(minutes=1)
                reminder["due"] = next_run(parse_cron(reminder["cron"]), following).isoformat(timespec="seconds")
            else:
                self._reminders.remove(reminder)
        if due:
            self._save_reminders()

    def _save_reminders(self) -> None:
        save_state(self._reminders_path, self._reminders)

    def _same_occurrence(self, stream_id: str, due: datetime, target: Optional[Dict[str, Any]], content: str) -> Optional[Dict[str, Any]]:
        """同一聊天、同一到期時刻且同一來源訊息（或相同內容）就是同一件事。"""
        msg_id = (target or {}).get("msg_id")
        for existing in self._reminders:
            if existing["stream_id"] != stream_id or existing["due"] != due.isoformat(timespec="seconds"):
                continue
            prior_id = (existing.get("target") or {}).get("msg_id")
            if (msg_id and prior_id == msg_id) or existing["content"].strip() == content.strip():
                return existing
        return None

    def _add_reminder(self, reminder: Dict[str, Any], now: datetime) -> Dict[str, Any]:
        existing = self._same_occurrence(reminder["stream_id"], datetime.fromisoformat(reminder["due"]), reminder.get("target"), reminder["content"])
        if existing:
            # 正式要求優先；先順手記下後收到正式要求時原地升級，不產生兩次觸發。
            if not reminder["auto"] and existing["auto"]:
                existing.update({key: reminder[key] for key in ("auto", "content", "cron", "recurring", "target")})
                self._save_reminders()
            return {"success": True, "id": existing["id"], "already_set": True,
                    "next_run": describe(datetime.fromisoformat(existing["due"])), "now": describe(now)}
        self._reminders.append(reminder)
        self._save_reminders()
        return {
            "success": True,
            "id": reminder["id"],
            "next_run": describe(datetime.fromisoformat(reminder["due"])),
            "now": describe(now),
        }

    def _cancel_reminder(self, reminder_id: str, stream_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """取消并返回提醒；给了 stream_id 时只能取消该聊天的。"""
        for reminder in self._reminders:
            if reminder["id"] == reminder_id and stream_id in (None, reminder["stream_id"]):
                self._reminders.remove(reminder)
                self._save_reminders()
                return reminder
        return None

    def _reminder_rows(self, stream_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """按下次提醒时间排好的展示行；给了 stream_id 时只列该聊天的。"""
        reminders = sorted(self._reminders, key=lambda reminder: reminder["due"])
        return [
            {
                "id": reminder["id"],
                "stream_id": reminder["stream_id"],
                "kind": FOLLOWUP if reminder["auto"] else "被要求",
                "schedule": f"重複 · {reminder['cron']}" if reminder["recurring"] else "一次",
                "next_run": describe(datetime.fromisoformat(reminder["due"])),
                "content": reminder["content"],
                "target": (reminder.get("target") or {}).get("name") or "（無）",
            }
            for reminder in reminders
            if stream_id in (None, reminder["stream_id"])
        ]

    # ===== 工具 =====

    @Tool(
        "reminder_set",
        description="有人請你提醒時使用，到時你會被喚醒，在目前聊天發出提醒。指定提醒必須填要標注者的真實 msg_id；"
        "如果有人請你提醒大家，填提出要求者的訊息。對方只給模糊時段時自己挑合理時刻；"
        "完全沒說時間就先問清楚再設，別亂猜。cron 是伺服器本地時間的 5 段表達式"
        "（分 時 日 月 星期），recurring=false 時只在下一次符合時提醒一次。",
        parameters={
            "type": "object",
            "properties": {
                "content": {
                    "type": "string",
                    "description": "誰請你提醒誰、什麼事、為什麼，到時才看得懂前因後果（例：小明請你提醒小華交報告，老師下午收）",
                },
                "cron": {"type": "string"},
                "recurring": {"type": "boolean"},
                "msg_id": {"type": "string", "description": "要 @ 的人的一則訊息，通常是請你提醒的那則"},
            },
            "required": ["content", "cron", "recurring", "msg_id"],
            "additionalProperties": False,
        },
        visibility="visible",
    )
    async def handle_reminder_set(
        self, content: str, cron: str, recurring: bool, msg_id: str = "", stream_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """设置被要求的提醒；参数错误或超过每个聊天的上限时作为失败结果交给模型改正。"""
        del kwargs
        now = datetime.now()
        limit = self._load_settings().reminder.max_per_chat
        try:
            if not self._load_settings().reminder.enabled:
                raise ValueError("定時提醒已在設定中關閉")
            if sum(not r["auto"] and r["stream_id"] == stream_id for r in self._reminders) >= limit:
                raise ValueError(f"這個聊天已經有 {limit} 則提醒，先取消一些再設")
            if type(recurring) is not bool:
                raise ValueError("recurring 必須是布林值")
            # 从当前这一分钟算起，模型按当前时刻写出的一次性 cron 也能立即触发
            due = next_run(parse_cron(cron), now.replace(second=0, microsecond=0))
            target = await self._target(msg_id, stream_id)
            if not target:
                raise ValueError("指定提醒必須提供要標注者的 msg_id；若是提醒大家，請選一則提出要求者的訊息")
            reminder = make_reminder(stream_id, content, due, cron=cron, recurring=recurring, target=target)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "now": describe(now)}
        return self._add_reminder(reminder, now)

    @Tool(
        FOLLOWUP_TOOL,
        description="順手記下未來一週內的安排，只用於沒有人明確要求提醒的閒聊；已經有人要求提醒就只用 reminder_set，兩個工具不可同時記同一件事。"
        "對話文字、圖片或截圖有提及將來時間（明確時刻或明天、週末、下週、晚點等模糊時段）就判斷是否有值得關心的安排；"
        "只要確有安排就呼叫，不必等『提醒我』，也不要因時間模糊而漏記。圖片上的聊天時間戳與已發生的事不是安排。"
        "只有模糊時段時自己選合理時刻；完全無法推算時間時再詢問。at 用伺服器本地時間 HH:MM 或 YYYY-MM-DD HH:MM。"
        "記下後照常回覆或不回覆，不必宣布提醒。",
        parameters={
            "type": "object",
            "properties": {
                "content": {"type": "string", "description": "注意到的事和相關的人"},
                "at": {"type": "string"},
                "msg_id": {"type": "string", "description": "相關的人的一則訊息，可不填"},
            },
            "required": ["content", "at"],
            "additionalProperties": False,
        },
        visibility="visible",
    )
    async def handle_reminder_followup(
        self, content: str, at: str, msg_id: str = "", stream_id: str = "", **kwargs: Any
    ) -> Dict[str, Any]:
        """順手記下一件事；超過每個聊天的上限或超出未來一週時拒絕。"""
        del kwargs
        now = datetime.now()
        reminder_config = self._load_settings().reminder
        limit = reminder_config.followup_max_per_chat
        try:
            if not (reminder_config.enabled and reminder_config.followup_enabled):
                raise ValueError("順手記下已在設定中關閉")
            if sum(r["auto"] and r["stream_id"] == stream_id for r in self._reminders) >= limit:
                raise ValueError(f"這個聊天已經記了 {limit} 件事，先不要再記了")
            moment = parse_at(now, at)
            if moment - now > FOLLOWUP_WINDOW:
                raise ValueError("超過一週的事不記")
            cron = f"{moment.minute} {moment.hour} {moment.day} {moment.month} *"
            target = await self._target(msg_id, stream_id)
            reminder = make_reminder(stream_id, content, moment, cron=cron, auto=True, target=target)
        except ValueError as exc:
            return {"success": False, "error": str(exc), "now": describe(now)}
        return self._add_reminder(reminder, now)

    @Tool(
        "reminder_list",
        description="查看、列出目前聊天還沒觸發的提醒和定時排程",
        parameters={"type": "object", "properties": {}, "additionalProperties": False},
    )
    async def handle_reminder_list(self, stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """只列出当前聊天的提醒。"""
        del kwargs
        return {"success": True, "now": describe(datetime.now()), "reminders": self._reminder_rows(stream_id)}

    @Tool(
        "reminder_cancel",
        description="依 id 取消、刪除目前聊天的一則提醒或定時排程",
        parameters={
            "type": "object",
            "properties": {"reminder_id": {"type": "string"}},
            "required": ["reminder_id"],
            "additionalProperties": False,
        },
    )
    async def handle_reminder_cancel(self, reminder_id: str, stream_id: str = "", **kwargs: Any) -> Dict[str, Any]:
        """只能取消当前聊天的提醒。"""
        del kwargs
        reminder = self._cancel_reminder(reminder_id, stream_id)
        if reminder is None:
            return {"success": False, "error": f"目前聊天沒有 id 為 {reminder_id} 的提醒"}
        return {"success": True, "cancelled": reminder["content"]}

    # ===== 聊天指令 =====

    @Command("reminders", description="列出目前聊天待觸發的提醒", pattern=r"^/reminders$")
    async def handle_reminder_list_command(self, stream_id: str = "", **kwargs: Any):
        """列出当前聊天的提醒。"""
        del kwargs
        rows = self._reminder_rows(stream_id)
        if rows:
            lines = [f"[{row['id']}] {row['next_run']} · {row['kind']} · {row['schedule']}\n{row['content']}" for row in rows]
            text = "⏰ 待觸發的提醒：\n" + "\n".join(lines) + "\n取消：/reminder_cancel <id>"
        else:
            text = "這個聊天沒有待觸發的提醒"
        await self.ctx.send.text(text, stream_id)
        return True, f"列出了 {len(rows)} 則提醒", True

    # 组件按全名注册不分类型，指令不能和 reminder_cancel 工具同名
    @Command(
        "reminder_cancel_command",
        description="取消目前聊天的一則提醒",
        pattern=r"^/reminder_cancel\s+(?P<reminder_id>\w+)$",
    )
    async def handle_reminder_cancel_command(self, stream_id: str = "", **kwargs: Any):
        """按 id 取消当前聊天的提醒。"""
        reminder_id = kwargs["matched_groups"]["reminder_id"]
        reminder = self._cancel_reminder(reminder_id, stream_id)
        text = f"已取消：{reminder['content']}" if reminder else f"這個聊天沒有 id 為 {reminder_id} 的提醒"
        await self.ctx.send.text(text, stream_id)
        return reminder is not None, text, True

    # ===== WebUI 页面 API =====

    async def _webui_chat_names(self) -> Dict[str, str]:
        """聊天流 ID -> 真实聊天名称：群名，私聊显示「某某的私聊」；读不到时返回空字典，页面改显示 ID。"""
        streams = await self.ctx.chat.get_all_streams(platform="all_platforms")
        names: Dict[str, str] = {}
        if isinstance(streams, list):
            for stream in streams:
                person = stream["user_cardname"] or stream["user_nickname"]
                names[stream["stream_id"]] = stream["group_name"] or f"{person}的私聊"
        else:
            self.ctx.logger.warning(f"读取聊天列表失败，WebUI 改为显示聊天 ID：{streams.get('error')}")
        return names

    @API("webui_summary", description="讀取所有聊天待觸發的提醒、指令和工具說明", version="1")
    async def webui_summary(self) -> Dict[str, Any]:
        """WebUI 显示真实聊天名称：群名，私聊显示「某某的私聊」。"""
        names = await self._webui_chat_names()
        rows = [{**row, "chat": names.get(row["stream_id"], row["stream_id"])} for row in self._reminder_rows()]
        enabled = self._load_settings().reminder.enabled
        return {
            "notice": "" if enabled else "定時提醒未啟用：麥麥看不到提醒工具，已設的提醒也暫停觸發；在下方「設定」開啟。",
            "settings": self._setting_rows("reminder", PAGE_FIELDS["reminder"]),
            "now": describe(datetime.now()),
            "total": len(rows),
            "followup_count": sum(row["kind"] == FOLLOWUP for row in rows),
            "recurring_count": sum(row["schedule"] != "一次" for row in rows),
            "reminders": rows,
            "commands": COMMANDS,
            "tools": TOOLS,
        }

    @API("webui_reminders_save", description="儲存定時提醒的設定", version="1")
    async def webui_reminders_save(self, **fields: Optional[str]) -> Dict[str, Any]:
        """只改有填的項目；開關改了要馬上讓麥麥看得到或看不到提醒工具。"""
        saved = self._save_page_fields("reminder", fields)
        await self._apply_reminder_switches()
        return {"saved": saved}

    @API("webui_cancel", description="取消任意聊天的一則提醒", version="1")
    async def webui_cancel(self, reminder_id: str) -> Dict[str, Any]:
        """WebUI 管理用，不限聊天。"""
        if self._cancel_reminder(reminder_id) is None:
            raise ValueError("提醒不存在或已經觸發，請重新整理頁面")
        return {"cancelled": reminder_id}
