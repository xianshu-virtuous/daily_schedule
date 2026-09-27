"""daily_schedule 的离线生活与 day 日记。

为什么要有这一层
----------------
日程本身是「计划」：模型每天排一次时间表，主人问起来它能说出「此刻在做什么」。
但计划不是经历——Bot 关掉的那几个小时里什么都没发生，被问到「昨晚干嘛了」
只能现编。**现编的东西无法延续**：下次再问，两次说辞对不上，人设就碎了。

所以离线生活这一层只做一件事：把「它不在的时候」变成**有锚的过去**。

三只锚
------
1. **时间锚**：离线跨度来自 ``time_sense`` 的启动结算（真实时间戳之差），
   不是让模型猜「大概睡了八小时」。
2. **框架锚**：离线期间原本排的是哪几段日程，从当天日程里取——生活节奏
   因此保持连贯，而不是每次重开一个新设定。
3. **记忆锚**：记忆服务里检索到的真事，允许日记提到主人、提到发生过的事。

模型只做**缝合**：把这三样真实材料拼成一段第一人称的「我那时在做什么」。
它不被允许发明新事实，只被允许把已知事实写得有人味。

闭环
----
缝合结果会落盘成 ``diary-YYYY-MM-DD``，并成为后续生成（日程 / 下一次离线日记）
的历史素材。**写下来的过去既是产出，也是下一次的输入**——这条回路才是
「延续」的来源：今天的编造被昨天的记录钉住，越写越像同一个人过的一条命。

存储
----
``data/json_storage/daily_schedule/diary-YYYY-MM-DD.json``，
默认**关闭**（``offline.enabled = false``），开启后才会记录。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import budget, llm, sources, store
from .config import DailyScheduleConfig
from .models import DailySchedule
from .persona import read_persona

logger = get_logger("daily_schedule.diary")

#: 一条日记正文的最大字符数。模型偶尔会写成长文，截断以免撑爆存储与回喂上下文。
_MAX_TEXT_CHARS = 600

#: 一天最多保留的离线条目数。超出时丢最早的（近的更能被后续生成延续）。
_MAX_ENTRIES_PER_DAY = 8

#: 单条日记最多记录的锚点条数（写入正文用于事后核对，不参与生成）。
_MAX_ANCHORS = 6


# ── 小工具 ────────────────────────────────────────────────────────────────────


def humanize_duration(seconds: float) -> str:
    """把秒数说成人话。

    本插件不依赖 time_sense 的模块（插件之间只能通过服务通信），
    所以这里自带一份实现，措辞与 time_sense 保持一致。

    Args:
        seconds: 时长（秒）。

    Returns:
        例如 ``不到一分钟`` / ``25 分钟`` / ``3 小时 20 分钟`` / ``2 天 5 小时``。
    """
    total = max(0.0, float(seconds))
    if total < 60:
        return "不到一分钟"
    minutes = int(total // 60)
    if minutes < 60:
        return f"{minutes} 分钟"
    hours, rest_minutes = divmod(minutes, 60)
    if hours < 24:
        if rest_minutes:
            return f"{hours} 小时 {rest_minutes} 分钟"
        return f"{hours} 小时"
    days, rest_hours = divmod(hours, 24)
    if rest_hours:
        return f"{days} 天 {rest_hours} 小时"
    return f"{days} 天"


def clock_of(timestamp: float) -> str:
    """把时间戳格式化成 ``HH:MM``。

    Args:
        timestamp: Unix 时间戳。

    Returns:
        时刻文本；非正数返回 ``未知``。
    """
    if timestamp <= 0:
        return "未知"
    return datetime.fromtimestamp(timestamp).strftime("%H:%M")


def date_of(timestamp: float) -> str:
    """把时间戳格式化成 ``YYYY-MM-DD``。

    Args:
        timestamp: Unix 时间戳。

    Returns:
        日期文本；非正数返回空字符串。
    """
    if timestamp <= 0:
        return ""
    return datetime.fromtimestamp(timestamp).date().isoformat()


def _as_float(value: Any, default: float = 0.0) -> float:
    """把任意值压成 float，失败给默认值。"""
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result:  # NaN
        return default
    return result


def _as_text(value: Any, limit: int = _MAX_TEXT_CHARS) -> str:
    """把任意值压成单行文本并截断。"""
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _as_str_list(value: Any, limit: int) -> list[str]:
    """把任意值压成字符串列表。"""
    if not isinstance(value, (list, tuple)):
        return []
    items: list[str] = []
    for item in value:
        text = _as_text(item, 120)
        if text:
            items.append(text)
        if len(items) >= limit:
            break
    return items


# ── 一条离线记录 ──────────────────────────────────────────────────────────────


@dataclass
class DiaryEntry:
    """一段离线生活。

    Attributes:
        from_ts: 离线开始时刻（上一次心跳）。
        to_ts: 离线结束时刻（本次启动结算时刻）。
        text: 第一人称的「那段时间在做什么」。
        at: 写入时间（排序与排查用）。
        source: 正文来源：``llm`` 模型缝合 / ``fact`` 只有事实 / ``fallback`` 兜底。
        model_tag: 实际生成用的模型标识。
        anchors: 本次缝合用到的真实素材摘要（事后可核对，见模块文档的「三只锚」）。
        outcomes: 本次离线结算时对「周程 / 月程推进项」的随机评估结果（留档可核对）。
        boot_count: time_sense 的启动序号，便于对齐两边日志。
    """

    from_ts: float = 0.0
    to_ts: float = 0.0
    text: str = ""
    at: float = 0.0
    source: str = "llm"
    model_tag: str = ""
    anchors: list[str] = field(default_factory=list)
    outcomes: list[str] = field(default_factory=list)
    boot_count: int = 0

    @property
    def seconds(self) -> float:
        """离线时长（秒），负数钳位为 0。"""
        return max(0.0, self.to_ts - self.from_ts)

    def span_text(self) -> str:
        """离线时长的可读文本。"""
        return humanize_duration(self.seconds)

    def clock_span(self) -> str:
        """``HH:MM-HH:MM`` 形式的时段文本。"""
        start = clock_of(self.from_ts)
        end = clock_of(self.to_ts)
        if start == "未知" or end == "未知":
            return "时间未知"
        return f"{start}-{end}"

    def render(self) -> str:
        """渲染成一行（命令展示用）。"""
        head = f"{self.clock_span()}（{self.span_text()}）"
        return f"{head} {self.text}" if self.text else head

    def anchor_line(self) -> str:
        """渲染锚点行，便于核对这段话是缝出来的还是编出来的。"""
        if not self.anchors:
            return ""
        return "锚点：" + " ｜ ".join(self.anchors)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "from_ts": self.from_ts,
            "to_ts": self.to_ts,
            "text": self.text,
            "at": self.at,
            "source": self.source,
            "model_tag": self.model_tag,
            "anchors": list(self.anchors),
            "outcomes": list(self.outcomes),
            "boot_count": self.boot_count,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DiaryEntry | None":
        """反序列化；数据不可用时返回 ``None``。

        Args:
            raw: 原始字典。

        Returns:
            条目实例或 ``None``。
        """
        if not isinstance(raw, dict):
            return None
        return cls(
            from_ts=_as_float(raw.get("from_ts")),
            to_ts=_as_float(raw.get("to_ts")),
            text=_as_text(raw.get("text")),
            at=_as_float(raw.get("at")),
            source=_as_text(raw.get("source"), 16) or "llm",
            model_tag=_as_text(raw.get("model_tag"), 64),
            anchors=_as_str_list(raw.get("anchors"), _MAX_ANCHORS),
            outcomes=_as_str_list(raw.get("outcomes"), _MAX_ANCHORS),
            boot_count=int(_as_float(raw.get("boot_count"))),
        )


# ── 一天 ──────────────────────────────────────────────────────────────────────


@dataclass
class DiaryDay:
    """某一天的日记（多段离线合并成一篇）。

    Attributes:
        date: ``YYYY-MM-DD``。
        entries: 当天的离线条目（按开始时间升序）。
        summary: 当天的一句话小结（由模型产出，可被后续生成覆盖）。
        updated_at: 最后写入时间。
    """

    date: str = ""
    entries: list[DiaryEntry] = field(default_factory=list)
    summary: str = ""
    updated_at: float = 0.0

    @property
    def is_empty(self) -> bool:
        """当天是否没有任何记录。"""
        return not self.entries

    def total_seconds(self) -> float:
        """当天所有离线时长的合计（秒）。"""
        return sum(entry.seconds for entry in self.entries)

    def append(self, entry: DiaryEntry) -> None:
        """追加一条记录并保持时间有序。

        超出条数上限时丢**最早**的：越近的记录越可能被后续生成延续，
        而更早的已经被写进模型回喂过的历史里了。

        Args:
            entry: 待追加条目。
        """
        self.entries.append(entry)
        self.entries.sort(key=lambda item: (item.from_ts, item.at))
        if len(self.entries) > _MAX_ENTRIES_PER_DAY:
            dropped = len(self.entries) - _MAX_ENTRIES_PER_DAY
            self.entries = self.entries[dropped:]
            logger.debug(f"[daily_schedule] 日记条目超过上限，丢弃最早 {dropped} 条")

    def text_block(self, *, with_anchors: bool = False) -> str:
        """把日记渲染成多行文本（命令展示与回喂共用）。

        Args:
            with_anchors: 是否附带锚点行（回喂时不要，展示时要）。

        Returns:
            多行文本；空日记返回空字符串。
        """
        if self.is_empty:
            return ""

        lines = [
            f"{self.date} 日记：{len(self.entries)} 段离线"
            f" ｜ 合计 {humanize_duration(self.total_seconds())}"
        ]
        if self.summary:
            lines.append(f"小结：{self.summary}")
        for entry in self.entries:
            lines.append(f"· {entry.render()}")
            if with_anchors:
                anchor = entry.anchor_line()
                if anchor:
                    lines.append(f"  {anchor}")
                if entry.outcomes:
                    lines.append("  这次评估：" + "；".join(entry.outcomes))
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "date": self.date,
            "entries": [entry.to_dict() for entry in self.entries],
            "summary": self.summary,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, raw: Any, *, date: str = "") -> "DiaryDay":
        """反序列化（数据不可用时得到一篇空日记，而不是报错）。

        Args:
            raw: 原始字典。
            date: 缺失时的日期兜底。

        Returns:
            日记实例。
        """
        if not isinstance(raw, dict):
            return cls(date=date)

        entries: list[DiaryEntry] = []
        raw_entries = raw.get("entries")
        if isinstance(raw_entries, list):
            for item in raw_entries:
                entry = DiaryEntry.from_dict(item)
                if entry is not None:
                    entries.append(entry)
        entries.sort(key=lambda item: (item.from_ts, item.at))

        return cls(
            date=_as_text(raw.get("date"), 16) or date,
            entries=entries,
            summary=_as_text(raw.get("summary"), 200),
            updated_at=_as_float(raw.get("updated_at")),
        )


async def load_day(date_str: str) -> DiaryDay:
    """读取某天的日记（不存在时返回空日记）。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        日记实例。
    """
    raw = await store.load_diary(date_str)
    return DiaryDay.from_dict(raw, date=date_str)


async def save_day(day: DiaryDay) -> bool:
    """写入某天的日记。

    Args:
        day: 日记实例。

    Returns:
        是否写入成功。
    """
    day.updated_at = time.time()
    return await store.save_diary(day.date, day.to_dict())


async def recent_days(limit: int = 3) -> list[DiaryDay]:
    """读取最近几天的日记（倒序）。

    Args:
        limit: 天数上限。

    Returns:
        日记列表；没有记录时返回空列表。
    """
    dates = await store.recent_diary_dates(limit)
    days: list[DiaryDay] = []
    for date_str in dates:
        day = await load_day(date_str)
        if not day.is_empty or day.summary:
            days.append(day)
    return days


# ── 框架锚：找出离开期间原本排了什么 ──────────────────────────────────────────

_DIARY_SYSTEM = """你负责给一个虚拟角色补写「它不在线的那段时间在做什么」。

【防背书 · 最重要的一条】你写出来的东西是给它**下次开口时当背景**用的，
不是给它照本宣科念出来的台词。所以：用你自己组织的句子写，不要复述材料里的原句，
也不要写成一句能被整句原样搬走的套话（比如「我正在专心修理一台旧收音机」这种现成腔）。
把它记成「它自己会记得的一件事」，而不是「一句准备好被念的话」。

严格规则：
1. 只使用用户消息里给出的材料：人设、真实时间跨度、它原本的安排、更早的日记、关于它的记忆。
2. 不要发明新的人物、地点、事件、对话或道具。材料里没有的，一律不写。
3. 材料不足时，宁可写得平淡，也不要为了好看补细节。
4. 用它的第一人称写，1-3 句，像随手记下的一笔；不要写成作文，不要抒情排比。
5. 语气与它的人设一致；它不在线的这段时间是「它的生活」，不是「停机维护」。
6. 只输出 JSON，不要任何多余文字。

输出格式：
{
  "text": "这段时间我在做什么（第一人称，1-3 句）",
  "summary": "今天到目前为止的一句话小结"
}
"""


def _minutes_of(hhmm: str) -> int | None:
    """把 ``HH:MM`` 转成当天分钟数。

    Args:
        hhmm: 时刻文本。

    Returns:
        0-1439 的分钟数；无法解析时返回 ``None``。
    """
    text = str(hhmm or "").strip()
    if ":" not in text:
        return None
    hour_text, _, minute_text = text.partition(":")
    try:
        hour = int(hour_text)
        minute = int(minute_text[:2])
    except (TypeError, ValueError):
        return None
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return hour * 60 + minute


def _minutes_at(timestamp: float) -> int:
    """取时间戳当天的分钟数。"""
    moment = datetime.fromtimestamp(timestamp)
    return moment.hour * 60 + moment.minute


def entries_within(
    schedule: DailySchedule | None,
    start_min: int,
    end_min: int,
    *,
    limit: int,
) -> list[str]:
    """取某天日程里与给定分钟区间相交的条目。

    Args:
        schedule: 某天的日程。
        start_min: 区间起点（当天分钟数）。
        end_min: 区间终点（当天分钟数）。
        limit: 最多返回几条。

    Returns:
        形如 ``04:00-07:00 睡眠`` 的条目文本列表。
    """
    if schedule is None or limit <= 0:
        return []

    lines: list[str] = []
    for entry in schedule.entries:
        begin = _minutes_of(entry.start)
        if begin is None:
            continue
        finish = _minutes_of(entry.end) if entry.end else None
        if finish is None or finish <= begin:
            finish = min(24 * 60, begin + 60)
        if finish <= start_min or begin >= end_min:
            continue
        busy = f"（忙 {entry.busy}）" if entry.busy else ""
        lines.append(f"{entry.start}-{entry.end or '??:??'} {entry.doing}{busy}")
        if len(lines) >= limit:
            break
    return lines


def describe_span_anchor(span: dict[str, Any]) -> str:
    """渲染时间锚文本。

    Args:
        span: ``time_sense.offline_span()`` 的返回。

    Returns:
        一行锚点描述。
    """
    seconds = _as_float(span.get("seconds"))
    days = int(_as_float(span.get("days")))
    text = _as_text(span.get("text"), 40) or humanize_duration(seconds)
    return f"time_sense 结算离线 {text}（跨 {days} 个自然日）"


def build_anchors(
    span: dict[str, Any],
    schedule_lines: list[str],
    memory_notes: list[str],
) -> list[str]:
    """汇总本次缝合用到的真实素材。

    Args:
        span: 时间锚。
        schedule_lines: 框架锚条目。
        memory_notes: 记忆锚条目。

    Returns:
        锚点文本列表。
    """
    anchors = [describe_span_anchor(span)]
    if schedule_lines:
        anchors.append(f"当天日程 {len(schedule_lines)} 段")
    if memory_notes:
        anchors.append(f"记忆 {len(memory_notes)} 条")
    return anchors[:_MAX_ANCHORS]


async def _history_block(exclude_date: str, limit_days: int) -> str:
    """拼出更早的日记，作为延续的把手。

    Args:
        exclude_date: 要排除的日期（通常是今天，正在写的那篇）。
        limit_days: 最多取几篇。

    Returns:
        多行文本；没有历史时返回空字符串。
    """
    if limit_days <= 0:
        return ""

    days = await recent_days(limit_days + 1)
    blocks = [
        day.text_block() for day in days if day.date != exclude_date and not day.is_empty
    ][:limit_days]
    return "\n\n".join(blocks)


def _memory_query(
    config: DailyScheduleConfig,
    schedule_lines: list[str],
    moment: datetime,
) -> str:
    """生成记忆检索关键词。

    Args:
        config: 插件配置。
        schedule_lines: 当时原本的安排。
        moment: 当前时间。

    Returns:
        检索关键词。
    """
    template = str(getattr(config.offline, "memory_query", "") or "").strip()
    if not template:
        return "最近的日常、正在做的事、和主人的约定"

    doing = ""
    if schedule_lines:
        first = schedule_lines[0]
        doing = first.split(" ", 1)[-1] if " " in first else first

    try:
        return template.format(
            day=moment.date().isoformat(),
            period=f"{moment.hour:02d}:{moment.minute:02d}",
            doing=doing,
        ).strip() or "最近的日常"
    except (KeyError, IndexError, ValueError):
        return template


def _build_prompt(
    config: DailyScheduleConfig,
    *,
    persona_block: str,
    from_ts: float,
    to_ts: float,
    span: dict[str, Any],
    schedule_lines: list[str],
    memory_notes: list[str],
    history: str,
    now: datetime,
    plan_block: str = "",
    outcome_lines: str = "",
) -> str:
    """组装缝合用的用户提示词。

    Args:
        config: 插件配置。
        persona_block: 人设文本块。
        from_ts: 离线开始时刻。
        to_ts: 离线结束时刻。
        span: 时间锚。
        schedule_lines: 框架锚条目。
        memory_notes: 记忆锚条目。
        history: 更早的日记文本。
        now: 当前时间。
        plan_block: 年 / 月 / 周程文本（它正在推进的事）。
        outcome_lines: 随机评估结果（这一周 / 这一月的推进顺利还是卡住了）。

    Returns:
        用户提示词。
    """
    seconds = _as_float(span.get("seconds"))
    parts: list[tuple[str, str]] = []

    if persona_block:
        parts.append(("人设", persona_block))

    parts.append(
        (
            "时间事实：真实发生，不可修改",
            f"它离开的时刻：{date_of(from_ts)} {clock_of(from_ts)}\n"
            f"它回来的时刻：{date_of(to_ts)} {clock_of(to_ts)}\n"
            f"中间隔了 {humanize_duration(seconds)}"
            f"（time_sense 结算值 {span.get('text') or humanize_duration(seconds)}，"
            f"跨 {int(_as_float(span.get('days')))} 个自然日）。\n"
            "这段时间它不在线上，需要你补写它当时在做什么。",
        )
    )

    if plan_block:
        parts.append(("它正在推进的事（年程 / 月程 / 周程）", plan_block))

    if outcome_lines:
        parts.append(
            (
                "日程判定：真实发生，不可修改",
                outcome_lines
                + "\n这是按「那天最忙的一档 + 有没有在忙的时候被搭话打断」算出来的结果："
                "算成了就写出一笔踏实或高兴，没算成要写出一点低落或不甘心——两种结果别写成同一种语气。"
                "顺便提一嘴当时忙不忙、有没有被叫走。",
            )
        )

    if schedule_lines:
        parts.append(
            ("它原本的安排（离开期间覆盖到的几段）", "\n".join(f"- {line}" for line in schedule_lines))
        )

    if memory_notes:
        parts.append(
            (
                "关于它的记忆（可能有噪音，只取用得上的）",
                "\n".join(f"- {note}" for note in memory_notes),
            )
        )

    if history:
        parts.append(("更早的日记（保持延续，不要重复已经写过的事）", history))

    parts.append(
        (
            "现在",
            f"{now.strftime('%Y-%m-%d %H:%M')}（本地时间）。请按要求补写这段时间，并只输出 JSON。",
        )
    )

    # 预算闸：时间事实、判定与本轮要求不许丢；更早的日记/记忆/日程材料按优先级丢
    text, notes = budget.fit(
        parts,
        limit=int(getattr(config.budget, "max_prompt_chars", 12000)),
        keep=("人设", "时间事实：真实发生，不可修改", "日程判定：真实发生，不可修改", "现在"),
    )
    for note in notes:
        logger.warning(f"[daily_schedule] 日记提示词{note}")
    return text


async def compose(
    config: DailyScheduleConfig,
    *,
    persona_block: str,
    from_ts: float,
    to_ts: float,
    span: dict[str, Any],
    schedule_lines: list[str],
    memory_notes: list[str],
    history: str,
    now: datetime,
    plan_block: str = "",
    outcome_lines: str = "",
) -> tuple[str, str, str]:
    """调用模型把真实材料缝成一段生活。

    Args:
        config: 插件配置。
        persona_block: 人设文本块。
        from_ts: 离线开始时刻。
        to_ts: 离线结束时刻。
        span: 时间锚。
        schedule_lines: 框架锚条目。
        memory_notes: 记忆锚条目。
        history: 更早的日记文本。
        now: 当前时间。

    Returns:
        ``(text, summary, model_tag)``；失败时 text 为空字符串。
    """
    prompt = _build_prompt(
        config,
        persona_block=persona_block,
        from_ts=from_ts,
        to_ts=to_ts,
        span=span,
        schedule_lines=schedule_lines,
        memory_notes=memory_notes,
        history=history,
        now=now,
        plan_block=plan_block,
        outcome_lines=outcome_lines,
    )
    if config.plugin.debug_log:
        logger.debug(f"[daily_schedule] 离线日记提示词：\n{prompt}")

    result = await llm.call(
        config, _DIARY_SYSTEM, prompt, request_name="daily_schedule_diary"
    )
    if not result.ok:
        logger.warning(f"[daily_schedule] 离线日记生成失败: {result.error}")
        return "", "", result.model_tag

    if config.plugin.debug_log:
        logger.debug(f"[daily_schedule] 离线日记原始返回：\n{result.text}")

    payload = llm.extract_json(result.text)
    if not isinstance(payload, dict):
        logger.warning(
            f"[daily_schedule] 离线日记无法解析 JSON"
            f"（{len(result.text)} 字，内容：{llm.excerpt(result.text)}）"
        )
        return "", "", result.model_tag

    text = _as_text(payload.get("text"))
    summary = _as_text(payload.get("summary"), 200)
    if not text:
        logger.warning("[daily_schedule] 离线日记缺少 text 字段，按只有事实处理")
    return text, summary, result.model_tag


# ── 主入口：把一次启动结算写成日记 ────────────────────────────────────────────


async def record_offline(
    config: DailyScheduleConfig,
    span: dict[str, Any],
    *,
    now: datetime | None = None,
    persona_block: str | None = None,
    plan_block: str = "",
    outcome_lines: str = "",
    outcomes: list[str] | None = None,
) -> DiaryEntry | None:
    """把一次启动结算出的离线跨度写成当天日记。

    这是本模块对外的唯一写入口，供启动流程调用。要点：

    - 默认关闭（``offline.enabled`` 为 false 时直接返回）；
    - 首次启动没有基线，不记（否则会写出一段假跨度）；
    - 跨度低于 ``min_seconds`` 不记（重启几秒钟不值得写进日记）；
    - 同一段离线只记一次（按起止时刻去重），插件被重复加载也不会写重。

    Args:
        config: 插件配置。
        span: ``time_sense.offline_span()`` 的返回。
        now: 结算时刻，默认当前时间。
        persona_block: 人设文本块；``None`` 表示自己读。
        plan_block: 年 / 月 / 周程文本（它正在推进的事）。
        outcome_lines: 随机评估结果的渲染文本（喂给缝合提示词）。
        outcomes: 随机评估结果的原始条目（留档在日记里，事后可核对）。

    Returns:
        写入的条目；未记录时返回 ``None``。
    """
    offline = config.offline
    if not getattr(offline, "enabled", False):
        return None

    if not isinstance(span, dict) or not span.get("has_baseline"):
        logger.debug("[daily_schedule] 首次启动没有时间基线，跳过离线日记")
        return None

    seconds = _as_float(span.get("seconds"))
    minimum = max(0.0, _as_float(offline.min_seconds))
    if seconds < minimum:
        logger.debug(
            f"[daily_schedule] 离线 {humanize_duration(seconds)}"
            f" 未达记录下限 {humanize_duration(minimum)}，跳过日记"
        )
        return None

    moment = now or datetime.now()
    to_ts = moment.timestamp()
    from_ts = to_ts - seconds
    day = moment.date().isoformat()

    diary = await load_day(day)
    for existing in diary.entries:
        if abs(existing.from_ts - from_ts) < 60 or abs(existing.to_ts - to_ts) < 60:
            logger.debug("[daily_schedule] 这段离线已经记过，跳过重复写入")
            return None

    # 框架锚：离开那天与回来那天各自覆盖到的日程段
    schedule_lines: list[str] = []
    if getattr(offline, "use_schedule_context", True):
        limit = max(1, int(getattr(offline, "max_schedule_lines", 6)))
        start_day = date_of(from_ts)
        start_min = _minutes_at(from_ts)
        end_min = _minutes_at(to_ts)
        if not start_day or start_day == day:
            schedule_lines = entries_within(
                await store.load_schedule(day), start_min, end_min, limit=limit
            )
        else:
            head = entries_within(
                await store.load_schedule(start_day), start_min, 24 * 60, limit=max(1, limit // 2)
            )
            tail = entries_within(
                await store.load_schedule(day), 0, end_min, limit=max(1, limit - len(head))
            )
            schedule_lines = head + tail

    # 记忆锚
    memory_notes: list[str] = []
    if getattr(offline, "use_memory", True):
        query = _memory_query(config, schedule_lines, moment)
        try:
            memory_notes = await sources.collect_memory_notes(config, query)
        except Exception as error:  # noqa: BLE001 - 记忆层不可用不影响记录
            logger.warning(f"[daily_schedule] 离线日记检索记忆失败: {error}")
            memory_notes = []
        memory_notes = memory_notes[: max(1, int(getattr(offline, "memory_top_k", 5)))]

    # 人设
    if persona_block is None:
        persona_block = ""
        try:
            snapshot = read_persona()
            if snapshot.is_usable:
                persona_block = snapshot.to_prompt_block()
        except Exception as error:  # noqa: BLE001 - 读不到人设就少一层材料
            logger.debug(f"[daily_schedule] 读取人设失败，离线日记降级: {error}")

    history = await _history_block(day, max(0, int(getattr(offline, "history_days", 2))))

    text = ""
    summary = ""
    model_tag = ""
    source = "fact"
    if getattr(offline, "generate_text", True):
        text, summary, model_tag = await compose(
            config,
            persona_block=persona_block,
            from_ts=from_ts,
            to_ts=to_ts,
            span=span,
            schedule_lines=schedule_lines,
            memory_notes=memory_notes,
            history=history,
            now=moment,
            plan_block=plan_block,
            outcome_lines=outcome_lines,
        )
        if text:
            source = "llm"

    entry = DiaryEntry(
        from_ts=from_ts,
        to_ts=to_ts,
        text=text,
        at=time.time(),
        source=source,
        model_tag=model_tag,
        anchors=build_anchors(span, schedule_lines, memory_notes),
        outcomes=list(outcomes or [])[:_MAX_ANCHORS],
        boot_count=int(_as_float(span.get("boot_count"))),
    )

    diary.append(entry)
    if summary:
        diary.summary = summary
    await save_day(diary)

    # 新日记写好了：把「可以注入几轮」的计数器上膛（限次注入，见 service.diary_injection）
    try:
        inject_turns = max(0, int(getattr(offline, "inject_turns", 3)))
        if inject_turns > 0 and bool(getattr(offline, "inject_enabled", False)):
            state = await store.load_state()
            state.diary_inject_left = inject_turns
            await store.save_state(state)
    except Exception as error:  # noqa: BLE001 - 计数器写不进去不该影响日记
        logger.warning(f"[daily_schedule] 记录日记注入轮数失败: {error}")

    removed = await store.prune_diaries(int(getattr(offline, "keep_days", 30)))
    if removed:
        logger.debug(f"[daily_schedule] 已回收 {len(removed)} 篇过期日记")

    logger.info(
        f"[daily_schedule] 已记录离线日记 {day}：{entry.clock_span()}"
        f"（{entry.span_text()}，来源 {source}，锚点 {len(entry.anchors)} 项）"
    )
    return entry


__all__ = [
    "DiaryDay",
    "DiaryEntry",
    "build_anchors",
    "clock_of",
    "compose",
    "date_of",
    "describe_span_anchor",
    "entries_within",
    "humanize_duration",
    "load_day",
    "recent_days",
    "record_offline",
    "save_day",
]
