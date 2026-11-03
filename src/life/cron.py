"""5 段 cron 与时间解析工具，由定时提醒、日常生活与主动话题共用。

移植自 maibot-reminder-plugin，语义保持不变。
"""

from datetime import date, datetime, time, timedelta
from typing import List, Set

TIME_FORMAT = "%Y-%m-%d %H:%M"
WEEKDAYS = "一二三四五六日"
# 分 时 日 月 星期 各段的取值范围；星期 0 和 7 都是周日
CRON_RANGES = ((0, 59), (0, 23), (1, 31), (1, 12), (0, 7))
# 2 月 29 日这类表达式最多四年才触发一次
SEARCH_DAYS = 366 * 5


def parse_cron(expr: str) -> List[Set[int]]:
    """把 5 段 cron 解析成每段允许的取值集合，支持 * 、a-b、a,b 和 /步长。"""
    parts = expr.split()
    if len(parts) != 5:
        raise ValueError(f"cron {expr!r} 必須是 5 段：分 時 日 月 星期")
    fields: List[Set[int]] = []
    for part, (low, high) in zip(parts, CRON_RANGES, strict=True):
        values: Set[int] = set()
        for item in part.split(","):
            body, _, step = item.partition("/")
            try:
                if body == "*":
                    start, end = low, high
                elif "-" in body:
                    start, end = (int(value) for value in body.split("-", 1))
                else:
                    start = int(body)
                    # 「5/10」表示从 5 开始每 10 个取一次
                    end = high if step else start
                values.update(range(start, end + 1, int(step) if step else 1))
            except ValueError:
                raise ValueError(f"無法解析 cron 段 {item!r}") from None
            if not low <= start <= end <= high:
                raise ValueError(f"cron 段 {item!r} 超出範圍 {low}-{high}")
        fields.append(values)
    if 7 in fields[4]:
        fields[4].add(0)
    return fields


def matches_day(fields: List[Set[int]], day: date) -> bool:
    """按标准 cron 语义判断日期：日和星期都被限定时满足其一即可，否则两者都要满足。"""
    _, _, days, months, weekdays = fields
    if day.month not in months:
        return False
    day_ok = day.day in days
    weekday_ok = day.isoweekday() % 7 in weekdays
    if len(days) == 31 or set(range(7)) <= weekdays:
        return day_ok and weekday_ok
    return day_ok or weekday_ok


def next_run(fields: List[Set[int]], start: datetime) -> datetime:
    """返回不早于 start 的第一个匹配时刻（整分钟）。"""
    day = start.date()
    for _ in range(SEARCH_DAYS):
        if matches_day(fields, day):
            for hour in sorted(fields[1]):
                for minute in sorted(fields[0]):
                    candidate = datetime.combine(day, time(hour, minute))
                    if candidate >= start:
                        return candidate
        day += timedelta(days=1)
    raise ValueError("這個 cron 五年內都不會觸發")


def parse_at(now: datetime, at: str) -> datetime:
    """把 HH:MM（下一个该时刻）或 YYYY-MM-DD HH:MM 换算成时刻，已经过去时抛出 ValueError。"""
    at = at.strip()
    try:
        moment = datetime.strptime(at, TIME_FORMAT)
    except ValueError:
        try:
            clock = datetime.strptime(at, "%H:%M").time()
        except ValueError:
            raise ValueError(f"無法解析時間 {at!r}，請用 HH:MM 或 YYYY-MM-DD HH:MM") from None
        moment = datetime.combine(now.date(), clock)
        if moment <= now:
            moment += timedelta(days=1)
    if moment <= now:
        raise ValueError(f"{moment.strftime(TIME_FORMAT)} 已經過去")
    return moment


def describe(moment: datetime) -> str:
    return f"{moment.strftime(TIME_FORMAT)} 星期{WEEKDAYS[moment.weekday()]}"
