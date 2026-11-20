"""待辦：她在各話題串裡還沒做完的事，決定每輪的焦點，沒輪到的各給 planner 一行。

每個聊天一次只跑一輪 planner、一輪只顧一串（真人也是一次回一句），但真人會記得手上還有什麼事。
焦點每輪會換串，跨好幾輪的事（有人叫她、她派子代理去查）不能跟著換串就忘掉：

- 待辦從話題串推出來（topics.add_member 存的 items），不另外記狀態；存在 threads.json，重啟也還在：
  - called：有人叫她（@、回覆她、喊名字），她在那串之後還沒說話；
  - result：她派的子代理查完了，她在那串之後還沒說話；
  - searching：子代理還在查；
  - asked：她在那串問了問題，還沒人接。
  超過 OPEN_SECONDS 的不算。她往那串說了話，那串的 called、result 就結束。
- 選焦點：叫她還沒回的（最新的）優先，再來是查完還沒回的；都沒有就照原本的（最新一則別人說的）。
  當過一次焦點的事記在 handled：她那輪選擇不回，就不再一直搶焦點，只留一行提醒。
- 每輪開始（上一輪以 wait 或 reply 收尾之後的第一次 planner 請求）都重選焦點；沒有新訊息的輪次
  （子代理結論送回、這裡叫醒的）也會換到該做的那串。
- 一輪結束時還有人叫她沒回、而且沒有新訊息會開下一輪：等一下用 maisaka.proactive.trigger 叫醒她做下一件，
  每件只叫一次（她不回就算了）。
"""

import asyncio
import re
import time
from typing import Any, Dict, List, Mapping, Optional, Sequence

from maibot_sdk import HookHandler

# 待辦多久之後不算（叫她半小時沒回，再提就怪了）
OPEN_SECONDS = 1800
HANDLED_LIMIT = 100
# 一輪結束後等幾秒再叫醒她做下一件（replyer 可能還在寫上一則）
NUDGE_DELAY_SECONDS = 10
AGENDA_NOTE = "【你手上還沒做完的】（別的話題串，這輪看不到內容；不用每件都馬上處理，該回的之後會再輪到）\n"
_QUESTION = re.compile(r"[?？]")
_TASK_ID = re.compile(r"任务 id：([\w-]+)")
_RESULT_ID = re.compile(r'<subagent_result id="([^"]+)"')


def open_items(store: Mapping[str, Any], now: float) -> List[Dict[str, Any]]:
    """每串至多一件還沒做完的事，照時間排（舊的在前）。"""
    found = []
    for topic_id, topic in store["topics"].items():
        items = topic.get("items", [])
        if not items:
            continue
        her = max((item.get("at", 0.0) for item in items if item.get("self")), default=0.0)
        others = [item for item in items if not item.get("self")]
        base = {"topic": topic_id, "title": (others or items)[0]["text"][:30], "msg_id": (others or items)[-1].get("msg_id", "")}
        calls = [item for item in others if item.get("addressed") and item.get("at", 0.0) > her and now - item["at"] < OPEN_SECONDS]
        result_at = float(topic.get("result_at") or 0.0)
        last = items[-1]
        if calls:
            call = calls[-1]
            found.append({**base, "kind": "called", "key": call["msg_id"], "msg_id": call["msg_id"], "at": call["at"], "who": call["who"], "text": call["text"]})
        elif result_at > her and now - result_at < OPEN_SECONDS:
            found.append({**base, "kind": "result", "key": f"result-{topic_id}-{int(result_at)}", "at": result_at})
        elif topic.get("searching"):
            found.append({**base, "kind": "searching", "key": f"searching-{topic_id}", "at": float(topic.get("last") or 0.0)})
        elif last.get("self") and _QUESTION.search(last["text"]) and now - last.get("at", 0.0) < OPEN_SECONDS:
            found.append({**base, "kind": "asked", "key": last["msg_id"], "at": last["at"], "text": last["text"]})
    return sorted(found, key=lambda item: item["at"])


def pick(store: Mapping[str, Any], now: float, among: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
    """這輪該做的事：叫她還沒回的（最新的）優先，再來是查完還沒回的；當過焦點的（handled）不再搶。

    Args:
        among: 只挑焦點在這些 msg_id 裡的（話題分析要拿焦點那則跑聯想，只能挑這輪看得到的）。
    """
    handled = set(store.get("handled", []))
    pending = [item for item in open_items(store, now) if item["key"] not in handled and (among is None or item["msg_id"] in among)]
    for kind in ("called", "result"):
        if found := [item for item in pending if item["kind"] == kind]:
            return found[-1]
    return None


def mark_handled(store: Dict[str, Any], topic_id: Optional[int], now: float) -> None:
    """焦點那串的 called、result 算處理過了：她這輪回不回都是她決定的，之後不再搶焦點。"""
    keys = [item["key"] for item in open_items(store, now) if item["topic"] == topic_id and item["kind"] in ("called", "result")]
    handled = store.setdefault("handled", [])
    handled.extend(key for key in keys if key not in handled)
    del handled[:-HANDLED_LIMIT]


def agenda_note(store: Mapping[str, Any], now: float, focus_topic: Optional[int]) -> str:
    """焦點以外還沒做完的事，每件一行。"""
    lines = []
    for item in open_items(store, now):
        if item["topic"] == focus_topic:
            continue
        minutes = max(0, round((now - item["at"]) / 60))
        where = f"（在聊「{item['title']}」，{minutes} 分鐘前）"
        lines.append({
            "called": f"- 「{item.get('who')}」叫你還沒回{where}：{item.get('text')}",
            "result": f"- 你派去查的結論回來了，還沒跟大家說{where}",
            "searching": f"- 你說要查的還在查{where}",
            "asked": f"- 你問了「{item.get('text')}」還沒人回{where}",
        }[item["kind"]])
    return AGENDA_NOTE + "\n".join(lines) if lines else ""


def ends_round(output_items: Any) -> bool:
    """planner 這次回應會不會結束這一輪：wait，或 reply 沒選 after_reply=continue。"""
    for item in output_items if isinstance(output_items, list) else []:
        call = item.get("tool_call") if isinstance(item, Mapping) else None
        if not isinstance(call, Mapping):
            continue
        args = call.get("args") if isinstance(call.get("args"), Mapping) else {}
        if call.get("func_name") == "wait" or (call.get("func_name") == "reply" and args.get("after_reply") != "continue"):
            return True
    return False


class AgendaMixin:
    """需要主插件提供 ctx、_append_system_text，和 EmoteMixin 的 _topics、_focus、_unanalyzed、_save_threads。"""

    def _init_agenda_state(self) -> None:
        # 子代理：派出時那一輪焦點所在的串（planner 的 call_id -> 串 ID），派出後拿到任務 id（任務 id -> 串 ID）
        self._subagent_calls: Dict[str, int] = {}
        self._subagent_tasks: Dict[str, int] = {}
        # 上一輪已經結束的聊天：下一次 planner 請求是新的一輪，要重選焦點
        self._round_over: set[str] = set()
        # 叫醒過的待辦（每件只叫一次）
        self._nudged: set[str] = set()
        # 等著叫醒的背景工作（留參照，免得還沒跑完就被回收）
        self._nudge_tasks: set[asyncio.Task] = set()

    def _agenda_focus(self, session_id: str, among: Optional[Sequence[str]] = None) -> Optional[Dict[str, Any]]:
        """照待辦挑這輪的焦點；挑到的那串記成處理過。"""
        store = self._topics.get(session_id)
        if not store:
            return None
        now = time.time()
        item = pick(store, now, among)
        if item:
            mark_handled(store, item["topic"], now)
            self.ctx.logger.info(f"待辦 [{session_id}]：這輪先做串 {item['topic']} 的 {item['kind']}（{item['title']}）")
        return item

    def _new_round_focus(self, session_id: str) -> None:
        """沒有新訊息的新一輪（子代理結論送回、叫醒她做下一件）：照待辦換焦點。"""
        if item := self._agenda_focus(session_id):
            self._focus[session_id] = item["msg_id"]
            self._save_threads()

    def _agenda_note(self, session_id: str) -> str:
        store = self._topics.get(session_id)
        if not store:
            return ""
        return agenda_note(store, time.time(), store["of"].get(self._focus.get(session_id, "")))

    def _track_subagents(self, session_id: str, items: Sequence[Mapping[str, Any]]) -> None:
        """派出子代理那輪記下的串（_subagent_calls）對上任務 id，標成還在查；結論第一次出現時改成查完了。"""
        store = self._topics.get(session_id) or {"topics": {}}
        for item in items:
            if not isinstance(item, Mapping):
                continue
            if item.get("item_type") == "FunctionCallOutputItem" and str(item.get("call_id")) in self._subagent_calls:
                topic_id = self._subagent_calls.pop(str(item.get("call_id")))
                if (found := _TASK_ID.search(str(item.get("output") or ""))) and topic_id in store["topics"]:
                    self._subagent_tasks[found.group(1)] = topic_id
                    store["topics"][topic_id].setdefault("searching", []).append(found.group(1))
            parts = item.get("parts") if isinstance(item.get("parts"), list) else []
            text = str(parts[0].get("text") or "") if parts and isinstance(parts[0], Mapping) else ""
            if (found := _RESULT_ID.search(text)) and found.group(1) in self._subagent_tasks:
                topic = store["topics"].get(self._subagent_tasks.pop(found.group(1)))
                if topic is not None:
                    topic["searching"] = [task for task in topic.get("searching", []) if task != found.group(1)]
                    topic["result_at"] = time.time()
                    self._save_threads()
                    self.ctx.logger.info(f"子代理 {found.group(1)} 的結論送回 [{session_id}]：記成那串的待辦")

    @HookHandler(
        "maisaka.planner.after_response",
        name="discord_agenda_after_planner",
        mode="observe",
        order="late",
        timeout_ms=1000,
    )
    async def after_planner_response(self, **kwargs: Any) -> Dict[str, Any]:
        """記下派出子代理時的那串；這一輪結束時，下一次請求要重選焦點，還有人叫她沒回就等一下叫醒她。"""
        session_id = str(kwargs.get("session_id") or "")
        store = self._topics.get(session_id)
        topic_id = store["of"].get(self._focus.get(session_id, "")) if store else None
        output = kwargs.get("output_items")
        for item in output if isinstance(output, list) else []:
            call = item.get("tool_call") if isinstance(item, Mapping) else None
            if topic_id is not None and isinstance(call, Mapping) and call.get("func_name") == "subagent":
                self._subagent_calls[str(call.get("call_id"))] = topic_id
        if session_id and ends_round(output):
            self._round_over.add(session_id)
            if store:
                task = asyncio.create_task(self._nudge_later(session_id))
                self._nudge_tasks.add(task)
                task.add_done_callback(self._nudge_tasks.discard)
        return {"action": "continue"}

    async def _nudge_later(self, session_id: str) -> None:
        """一輪結束後還有人叫她沒回、也沒有新訊息會開下一輪：叫醒她做下一件。"""
        await asyncio.sleep(NUDGE_DELAY_SECONDS)
        store = self._topics.get(session_id)
        if session_id in self._unanalyzed or not store:
            return  # 有新訊息，MaiBot 本來就會再跑一輪，那輪的話題分析會照待辦選焦點
        item = pick(store, time.time())
        if not item or item["kind"] != "called" or item["key"] in self._nudged:
            return
        self._nudged.add(item["key"])
        try:
            result = await self.ctx.maisaka.proactive.trigger(
                session_id, f"「{item['who']}」剛剛叫你還沒回：{item['text']}", reason="話題串的待辦"
            )
        except Exception as exc:  # noqa: BLE001 - 叫不醒就等下一則訊息
            self.ctx.logger.warning(f"待辦 [{session_id}]：叫醒失敗 {exc!r}")
            return
        if isinstance(result, Mapping) and not result.get("success", True):
            self.ctx.logger.warning(f"待辦 [{session_id}]：叫醒失敗 {result.get('error')}")
