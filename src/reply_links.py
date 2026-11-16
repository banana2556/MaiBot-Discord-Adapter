"""回覆前文：一則訊息在接前面哪幾則。

麥麥只要把一則訊息回好，不需要把整個群分成話題，只要知道那則在接哪幾則：

- 明確連結不用猜：回覆了哪則（沿回覆鏈往上）、@ 的人剛說的話（叫她的算 @ 她）、同一人剛連發的。
- 沒有明確連結的，問決策模型（表情決策那一次請求順便問，不多打一次）：「#N 在接前面哪一則？」，
  選項是它前面的訊息，外加「沒在接誰」；照機率取過門檻的前 MAX_LINKS 則。

Dialogue disentanglement 的研究（Takada & Mori, 2026）發現 LLM 很難一次挑對「回應哪一則」；
這裡只拿它當沒有明確連結時的補充，而且看機率決定採不採用。
"""

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

NONE = "none"
SELF_OPTION = "you"
# 一次請求最多問幾則；每題最多給前面幾則當選項
LINK_TARGETS = 4
LINK_OPTIONS = 8
MAX_LINKS = 2
# 同一人在這段時間內連發的算前文
SAME_SPEAKER_SECONDS = 120
REPLY_CHAIN_DEPTH = 3


def plain(name: str) -> str:
    """發話人名字去掉外框的「」，planner 上下文和 @ 裡的寫法才對得上。"""
    return name.strip().strip("「」")


def link_questions(
    window: Sequence[Mapping[str, Any]], numbers: Mapping[int, str], remembered: Mapping[str, Mapping[str, Any]]
) -> Tuple[Dict[str, Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """沒有明確回覆的新訊息，各出一題「在接前面哪一則」。

    叫她的（她上次說話之後）先問，再補最近的；回覆了某則的不問，回覆鏈就是它的前文。

    Args:
        window: 送給決策模型的聊天（planner_messages 的格式），照時間排。
        numbers: id(訊息) -> 狀態文字裡的 # 編號（只有別人的訊息有）。
        remembered: 這個聊天收訊時記下的 _inbound（看 addressed）。

    Returns:
        (要加進請求的 questions, 題目 ID -> {"target": msg_id, "options": {選項: msg_id}})。
    """
    last_self = max((index for index, message in enumerate(window) if message["self"]), default=-1)
    candidates = [index for index, message in enumerate(window) if not message["self"] and not message["quote"]]
    calling = [
        index for index in candidates if index > last_self and (remembered.get(window[index]["msg_id"]) or {}).get("addressed")
    ]
    chosen = list(dict.fromkeys([*reversed(calling), *reversed(candidates)]))[:LINK_TARGETS]
    questions: Dict[str, Dict[str, Any]] = {}
    keys: Dict[str, Dict[str, Any]] = {}
    for index in sorted(chosen):
        target = window[index]
        options: Dict[str, str] = {}
        criteria: Dict[str, str] = {}
        for message in reversed(window[:index]):
            if len(options) >= LINK_OPTIONS:
                break
            # 她說過的只給最近一句，免得選項被她自己的話塞滿
            key = SELF_OPTION if message["self"] else numbers[id(message)]
            if key in options:
                continue
            options[key] = message["msg_id"]
            who = "（你）" if message["self"] else f"「{message['user']}」"
            criteria[key] = f"{who}：{message['text']}"
        if not options:  # 前面沒有訊息；選擇題至少要兩個選項
            continue
        criteria = dict(reversed(criteria.items()))
        criteria[NONE] = "開了新話題，或沒有在接前面任何一則"
        number = numbers[id(target)]
        question_id = f"link_{number}"
        questions[question_id] = {
            "type": "choice",
            "instructions": f"#{number}「{target['user']}」：{target['text']}\n這則在接前面哪一則的話？",
            "criteria": criteria,
        }
        keys[question_id] = {"target": target["msg_id"], "number": number, "options": options}
    return questions, keys


def parse_links(
    answers: Optional[Mapping[str, Any]], keys: Mapping[str, Mapping[str, Any]], threshold: float
) -> Tuple[Dict[str, List[str]], str]:
    """決策模型的答案 -> {目標 msg_id: 前文 msg_id（照機率高到低）}，和頁面顯示用的摘要。

    沒過門檻或選「沒在接誰」的不連；拿不到答案的那題略過。
    """
    links: Dict[str, List[str]] = {}
    details: List[str] = []
    for question_id, key in keys.items():
        answer = (answers or {}).get(question_id)
        if not isinstance(answer, Mapping):
            continue
        probabilities = answer.get("probabilities")
        if not isinstance(probabilities, Mapping):
            # 沒給機率時只信它選的那個，信心用 confidence
            probabilities = {str(answer.get("choice") or ""): float(answer.get("confidence") or 1.0)}
        ranked = sorted(
            ((str(option), float(value)) for option, value in probabilities.items() if isinstance(value, (int, float))),
            key=lambda item: item[1],
            reverse=True,
        )
        chosen = [(option, value) for option, value in ranked if option in key["options"] and value >= threshold]
        chosen = chosen[:MAX_LINKS]
        links[key["target"]] = [key["options"][option] for option, _ in chosen]
        if chosen:
            shown = "、".join(f"{'你' if option == SELF_OPTION else '#' + option}（{value:.2f}）" for option, value in chosen)
        else:
            none_probability = dict(ranked).get(NONE)
            shown = f"沒在接誰（{none_probability:.2f}）" if none_probability is not None else "都沒過門檻"
        details.append(f"#{key['number']}→{shown}")
    return links, "；".join(details)


def context_ids(
    messages: Sequence[Mapping[str, Any]],
    target_id: str,
    inbound: Mapping[str, Any],
    judged: Sequence[str],
) -> List[str]:
    """target 的前文 msg_id，照時間排；target 不在 messages 裡時回傳空清單。

    Args:
        messages: planner / replyer 上下文裡的聊天訊息（planner_messages 的格式）。
        target_id: 要回的那則（上下文裡顯示的 msg_id）。
        inbound: 這則收訊時記下的 _inbound（mentions、addressed）；她自己的訊息或沒記到時是空的。
        judged: 決策模型判斷這則在接的 msg_id。
    """
    position = {message["msg_id"]: index for index, message in enumerate(messages)}
    if target_id not in position:
        return []
    index = position[target_id]
    target = messages[index]
    before = messages[:index]
    related = set(judged)
    # 回覆鏈：回覆的那則、那則又回覆的
    current = target
    for _ in range(REPLY_CHAIN_DEPTH):
        if current["quote"] not in position:
            break
        related.add(current["quote"])
        current = messages[position[current["quote"]]]
    # @ 的人剛說的；叫她的接她上一句
    names = {plain(name) for name in inbound.get("mentions", [])}
    for name in names:
        latest = next((m for m in reversed(before) if not m["self"] and plain(m["user"]) == name), None)
        if latest is not None:
            related.add(latest["msg_id"])
    if inbound.get("addressed"):
        latest_self = next((m for m in reversed(before) if m["self"]), None)
        if latest_self is not None:
            related.add(latest_self["msg_id"])
    # 同一人剛連發的（時間不明時當作連發）
    for message in reversed(before):
        if message["self"] != target["self"] or plain(message["user"]) != plain(target["user"]):
            continue
        if message["time"] and target["time"] and target["time"] - message["time"] > SAME_SPEAKER_SECONDS:
            break
        related.add(message["msg_id"])
    return [message["msg_id"] for message in before if message["msg_id"] in related]
