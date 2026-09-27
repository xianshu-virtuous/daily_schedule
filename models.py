"""daily_schedule 数据模型。

集中定义日程、人设判定结果与运行时状态的结构，并提供容错的解析方法。

模型返回的 JSON 不保证干净（可能缺字段、类型不符、时间格式不规范），
因此所有 ``from_dict`` 都以「尽力解析、坏数据丢弃」为原则，绝不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time, timedelta
from typing import Any

#: 人设类型：扮演已有角色（来自作品/设定）
PERSONA_KIND_ROLEPLAY = "roleplay"
#: 人设类型：原创 OC（无出处，纯原创设定）
PERSONA_KIND_ORIGINAL = "original_oc"

#: 日型归属：工作日用的日型。
ARCHETYPE_WORKDAY = "workday"
#: 日型归属：休息日（周六周日）用的日型。
ARCHETYPE_WEEKEND = "weekend"

#: 日型里禁止出现的词：这些词一进文本，模型就会开始"念设定"。
_FORBIDDEN_WORDS = ("日程", "让位", "系统", "规则", "优先级", "检测到", "模板", "设定")

#: 池子里每个时段最少要有的变体数（只有一个变体就谈不上轮换）。
MIN_VARIANTS_PER_SLOT = 2

#: 规划层：这一年的规划（最高层，顶层围绕人设生成）。
PLAN_LAYER_YEAR = "year"
#: 规划层：这个月的目标（围绕年程生成；分忙碌 / 休息）。
PLAN_LAYER_MONTH = "month"
#: 规划层：这一周怎么过（围绕月程生成，定义日程该干什么）。
PLAN_LAYER_WEEK = "week"

#: 规划层的顺序（由高到低），链条生成与展示都用它。
PLAN_LAYERS = (PLAN_LAYER_YEAR, PLAN_LAYER_MONTH, PLAN_LAYER_WEEK)

#: 时期类型：忙碌期（学期 / 工作月）。
PLAN_KIND_BUSY = "busy"
#: 时期类型：休息期（寒暑假 / 休息月）。
PLAN_KIND_REST = "rest"

#: 规划层的中文名（日志与命令展示用）。
PLAN_LAYER_LABELS = {
    PLAN_LAYER_YEAR: "年程",
    PLAN_LAYER_MONTH: "月程",
    PLAN_LAYER_WEEK: "周程",
}

#: 随机评估的结果：顺利。
PLAN_PROGRESS_OK = "ok"
#: 随机评估的结果：卡住了。
PLAN_PROGRESS_STUCK = "stuck"

_TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2})$")


def normalize_hhmm(value: Any) -> str:
    """把任意时间写法规范成 ``HH:MM`` 字符串。

    支持 ``"7:30"`` / ``"07:30"`` / ``"0730"`` / ``"7"`` 等宽松写法，
    以及 ``datetime`` / ``time`` 实例。

    Args:
        value: 待规范化的时间值。

    Returns:
        规范化后的 ``HH:MM``；无法解析时返回空字符串。
    """
    if isinstance(value, datetime):
        return f"{value.hour:02d}:{value.minute:02d}"
    if isinstance(value, time):
        return f"{value.hour:02d}:{value.minute:02d}"

    text = str(value or "").strip()
    if not text:
        return ""

    match = _TIME_PATTERN.match(text)
    if match:
        hour, minute = int(match.group(1)), int(match.group(2))
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
        return ""

    if text.isdigit() and len(text) in (3, 4):
        hour, minute = int(text[:-2]), int(text[-2:])
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return f"{hour:02d}:{minute:02d}"
    return ""


def _coerce_busy(value: Any) -> int:
    """把任意忙碌标记收敛到 0 / 1 / 2。"""
    try:
        busy = int(float(str(value).strip()))
    except (TypeError, ValueError):
        return 0
    return max(0, min(2, busy))


def _coerce_text(value: Any, limit: int) -> str:
    """把任意值转成裁剪后的单行文本。"""
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    if not text:
        return ""
    return text[:limit]


BUSY_LABELS: dict[int, str] = {0: "空闲", 1: "有点忙", 2: "很忙"}


@dataclass
class ScheduleEntry:
    """日程中的单个时段。

    Attributes:
        start: 起始时刻，``HH:MM``。
        end: 结束时刻，``HH:MM``。
        doing: 这一时段在做什么（第一人称、现在进行时的一句话）。
        busy: 忙碌等级，0=空闲 / 1=有点忙 / 2=很忙。
        hint: 被打扰时的自然反应倾向（可空）。
    """

    start: str
    end: str
    doing: str
    busy: int = 0
    hint: str = ""

    @property
    def busy_label(self) -> str:
        """忙碌等级的中文文字。"""
        return BUSY_LABELS.get(self.busy, "空闲")

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "start": self.start,
            "end": self.end,
            "doing": self.doing,
            "busy": self.busy,
            "hint": self.hint,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "ScheduleEntry | None":
        """从字典解析时段；缺少必要信息时返回 ``None``。

        Args:
            raw: 模型返回的原始条目。

        Returns:
            解析成功的时段，或 ``None``。
        """
        if not isinstance(raw, dict):
            return None
        start = normalize_hhmm(raw.get("start") or raw.get("begin") or raw.get("time"))
        doing = _coerce_text(
            raw.get("doing") or raw.get("activity") or raw.get("content"), 120
        )
        if not start or not doing:
            return None
        end = normalize_hhmm(raw.get("end") or raw.get("finish"))
        return cls(
            start=start,
            end=end,
            doing=doing,
            busy=_coerce_busy(raw.get("busy", 0)),
            hint=_coerce_text(raw.get("hint") or raw.get("interrupt_hint"), 60),
        )


@dataclass
class DailySchedule:
    """某一天的日程。

    Attributes:
        date: 归属日期，``YYYY-MM-DD``。
        entries: 按 ``start`` 升序排列的时段列表。
        yield_lines: 让位心声池（主人到场时随机取一条）。
        yesterday_summary: 生成时对前一日的小结，写入日志。
        generated_at: 生成时间戳（Unix 秒）。
        model_tag: 实际使用的模型标识，便于排查。
        persona_kind: 当次判定的人设类型。
        persona_name: 当次判定的角色名。
        sources_used: 本次生成实际用上的素材层（persona/memory/internet）。
        archetype: 这份日程来自哪个日型（池子模式才有；daily 模式为空）。
        pool_id: 来源池子的标识（池子模式才有）。
        focus: 抽取这份日程时，周程点的「今天偏重」那件事；用于事后算这条推进项的完成度。
    """

    date: str
    entries: list[ScheduleEntry] = field(default_factory=list)
    yield_lines: list[str] = field(default_factory=list)
    yesterday_summary: str = ""
    generated_at: float = 0.0
    model_tag: str = ""
    persona_kind: str = ""
    persona_name: str = ""
    sources_used: list[str] = field(default_factory=list)
    archetype: str = ""
    pool_id: str = ""
    focus: str = ""

    def entry_at(self, moment: datetime | time | str) -> ScheduleEntry | None:
        """取指定时刻所处的时段。

        区间语义为 ``[start, end)``；``end`` 缺失时视为延续到下一条开始。
        若时刻不在任何区间内（例如凌晨的空档），返回 ``None``。

        Args:
            moment: 查询时刻，支持 ``datetime`` / ``time`` / ``"HH:MM"``。

        Returns:
            命中的时段，或 ``None``。
        """
        current = normalize_hhmm(moment)
        if not current or not self.entries:
            return None

        for index, entry in enumerate(self.entries):
            end = entry.end
            if not end:
                end = (
                    self.entries[index + 1].start
                    if index + 1 < len(self.entries)
                    else "23:59"
                )
            if end <= entry.start:  # 跨天时段（如 23:30-06:00）用简单包含关系兜底
                if current >= entry.start or current < end:
                    return entry
                continue
            if entry.start <= current < end:
                return entry
        return None

    @property
    def is_empty(self) -> bool:
        """是否没有任何可用时段。"""
        return not self.entries

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "date": self.date,
            "entries": [entry.to_dict() for entry in self.entries],
            "yield_lines": list(self.yield_lines),
            "yesterday_summary": self.yesterday_summary,
            "generated_at": self.generated_at,
            "model_tag": self.model_tag,
            "persona_kind": self.persona_kind,
            "persona_name": self.persona_name,
            "sources_used": list(self.sources_used),
            "archetype": self.archetype,
            "pool_id": self.pool_id,
            "focus": self.focus,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "DailySchedule | None":
        """从字典解析日程；结构不可用时返回 ``None``。

        Args:
            raw: 存储或模型返回的原始字典。

        Returns:
            解析成功的日程，或 ``None``。
        """
        if not isinstance(raw, dict):
            return None

        day = _coerce_text(raw.get("date"), 10)
        if not day:
            day = date_cls.today().isoformat()

        entries: list[ScheduleEntry] = []
        raw_entries = raw.get("entries")
        if isinstance(raw_entries, list):
            for item in raw_entries:
                entry = ScheduleEntry.from_dict(item)
                if entry is not None:
                    entries.append(entry)
        entries.sort(key=lambda item: item.start)

        lines_raw = raw.get("yield_lines")
        yield_lines: list[str] = []
        if isinstance(lines_raw, list):
            for item in lines_raw:
                text = _coerce_text(item, 80)
                if text:
                    yield_lines.append(text)

        sources_raw = raw.get("sources_used")
        sources = (
            [str(item) for item in sources_raw if str(item).strip()]
            if isinstance(sources_raw, list)
            else []
        )

        try:
            generated_at = float(raw.get("generated_at") or 0.0)
        except (TypeError, ValueError):
            generated_at = 0.0

        return cls(
            date=day,
            entries=entries,
            yield_lines=yield_lines,
            yesterday_summary=_coerce_text(raw.get("yesterday_summary"), 400),
            generated_at=generated_at,
            model_tag=_coerce_text(raw.get("model_tag"), 80),
            persona_kind=_coerce_text(raw.get("persona_kind"), 32),
            persona_name=_coerce_text(raw.get("persona_name"), 60),
            sources_used=sources,
            archetype=_coerce_text(raw.get("archetype"), 40),
            pool_id=_coerce_text(raw.get("pool_id"), 80),
            focus=_coerce_text(raw.get("focus"), 80),
        )


# ── 日程池：日型 / 时段 / 变体 ────────────────────────────────────────────────
#
# 为什么要有池子：每天让模型重写一遍"今天"，它就会把回喂给它的昨天当成模板来换词，
# 三天下来连「鞋带解了又系」这种细节都在重复（实测）。改成"一周编几套日型、
# 每天从中抽一套"，变化来自抽取而不是每天重写，而且抽取不花模型调用。
#
# 数据结构刻意做成三层（日型 → 时段 → 变体）而不是平铺的条目列表：
# 变体必须**属于同一个日型**才能保证抽出来的那天场景连贯——只按时段乱抽，
# 会出现"上午在图书馆、下午在床上、中间没有移动过程"的混乱。


@dataclass
class ScheduleVariant:
    """某个时段里的一种"在做这件事"的写法。

    Attributes:
        doing: 此刻在做的事（第一人称、现在进行时的一句话）。
        busy: 忙碌等级，0=空闲 / 1=有点忙 / 2=很忙。
        hint: 被打扰时的自然反应倾向（可空）。
    """

    doing: str = ""
    busy: int = 0
    hint: str = ""

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {"doing": self.doing, "busy": self.busy, "hint": self.hint}

    @classmethod
    def from_dict(cls, raw: Any) -> "ScheduleVariant | None":
        """从字典（或裸字符串）解析一个变体。

        模型有时会把 ``variants`` 写成字符串数组（只给 doing），这里一并兼容：
        宁可少一个字段，也别因为格式偷懒把整段变体丢掉。

        Args:
            raw: 原始值，字典或字符串。

        Returns:
            变体实例；doing 为空时返回 ``None``。
        """
        if isinstance(raw, str):
            doing = _coerce_text(raw, 120)
            if not doing:
                return None
            return cls(doing=doing)
        if not isinstance(raw, dict):
            return None

        doing = _coerce_text(
            raw.get("doing") or raw.get("activity") or raw.get("content"), 120
        )
        if not doing:
            return None
        return cls(
            doing=doing,
            busy=_coerce_busy(raw.get("busy", 0)),
            hint=_coerce_text(raw.get("hint") or raw.get("interrupt_hint"), 60),
        )


@dataclass
class PoolSlot:
    """日型里的一个时段，带多个可选变体。

    Attributes:
        start: 起始时刻 ``HH:MM``。
        end: 结束时刻 ``HH:MM``。
        variants: 这个时段的可选写法（至少 :data:`MIN_VARIANTS_PER_SLOT` 个）。
    """

    start: str = ""
    end: str = ""
    variants: list[ScheduleVariant] = field(default_factory=list)

    def doing_set(self) -> set[str]:
        """这一段的变体文本集合（查重与「别和昨天一样」用）。"""
        return {item.doing for item in self.variants if item.doing}

    def pick(self, index: int) -> ScheduleVariant | None:
        """按序号取一个变体（越界时取模，保证一定拿得到）。"""
        if not self.variants:
            return None
        return self.variants[index % len(self.variants)]

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "start": self.start,
            "end": self.end,
            "variants": [item.to_dict() for item in self.variants],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PoolSlot | None":
        """从字典解析一个时段；缺少起止时间时返回 ``None``。"""
        if not isinstance(raw, dict):
            return None
        start = normalize_hhmm(raw.get("start") or raw.get("begin") or raw.get("time"))
        end = normalize_hhmm(raw.get("end") or raw.get("finish"))
        if not start or not end:
            return None

        raw_variants = raw.get("variants")
        if raw_variants is None:
            # 容忍"没写 variants 只写了一个 doing"的偷懒输出
            raw_variants = [raw]
        variants: list[ScheduleVariant] = []
        if isinstance(raw_variants, list):
            for item in raw_variants:
                variant = ScheduleVariant.from_dict(item)
                if variant is not None:
                    variants.append(variant)
        if not variants:
            return None
        return cls(start=start, end=end, variants=variants)


@dataclass
class Archetype:
    """一套「日型」：某种它反复会过的日子。

    Attributes:
        key: 唯一键（落盘与"最近用过没"都靠它）。
        name: 日型名（如「训练日」）。
        kind: ``workday`` / ``weekend``。
        mood: 这一整天的一句话基调（当作当天的小结写进日志）。
        tags: 这一套日子的关键词（如 ``["训练","外出"]``）——周程说「今天偏重
            出门办事」时，抽取会优先挑标签能对上的日型。空列表表示不参与匹配。
        slots: 完整覆盖 00:00-23:59 的时段与变体。
    """

    key: str = ""
    name: str = ""
    kind: str = ARCHETYPE_WORKDAY
    mood: str = ""
    tags: list[str] = field(default_factory=list)
    slots: list[PoolSlot] = field(default_factory=list)

    @property
    def label(self) -> str:
        """展示名（没有名字就用 key）。"""
        return self.name or self.key

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "key": self.key,
            "name": self.name,
            "kind": self.kind,
            "mood": self.mood,
            "tags": list(self.tags),
            "slots": [slot.to_dict() for slot in self.slots],
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "Archetype | None":
        """从字典解析一个日型；结构不可用时返回 ``None``。"""
        if not isinstance(raw, dict):
            return None

        slots: list[PoolSlot] = []
        raw_slots = raw.get("slots")
        if isinstance(raw_slots, list):
            for item in raw_slots:
                slot = PoolSlot.from_dict(item)
                if slot is not None:
                    slots.append(slot)
        if not slots:
            return None

        slots.sort(key=lambda item: item.start)
        kind = _coerce_text(raw.get("kind"), 16).lower()
        if kind not in (ARCHETYPE_WORKDAY, ARCHETYPE_WEEKEND):
            kind = ARCHETYPE_WORKDAY
        name = _coerce_text(raw.get("name"), 40)
        key = _coerce_text(raw.get("key"), 60) or name

        raw_tags = raw.get("tags")
        tags: list[str] = []
        if isinstance(raw_tags, list):
            for entry in raw_tags:
                tag = _coerce_text(entry, 12)
                if tag and tag not in tags:
                    tags.append(tag)

        return cls(
            key=key,
            name=name or key,
            kind=kind,
            mood=_coerce_text(raw.get("mood"), 200),
            tags=tags[:6],
            slots=slots,
        )


def validate_archetype(
    archetype: Archetype,
    *,
    min_entries: int,
    max_entries: int,
    want_variants: int,
) -> list[str]:
    """校验一个日型是否可用，返回问题清单（空清单 = 通过）。

    这是「写坏率」的第一道闸：模型返回的东西先过这里，不合格就不进池子。
    只做**能机械判定**的事（覆盖、衔接、条数、重复、禁词），不评价写得好不好。

    Args:
        archetype: 待校验的日型。
        min_entries: 时段数下限。
        max_entries: 时段数上限。
        want_variants: 期望的变体数（实际不少于 :data:`MIN_VARIANTS_PER_SLOT` 即可）。

    Returns:
        问题描述列表。
    """
    problems: list[str] = []

    if not archetype.slots:
        return ["没有任何时段"]

    count = len(archetype.slots)
    if count < max(1, min_entries):
        problems.append(f"时段只有 {count} 段，少于下限 {min_entries}")
    if count > max(1, max_entries):
        problems.append(f"时段有 {count} 段，超过上限 {max_entries}")

    if archetype.slots[0].start != "00:00":
        problems.append(f"第一段从 {archetype.slots[0].start} 开始，必须从 00:00 开始")

    last_end = archetype.slots[-1].end
    if last_end not in ("23:59", "24:00"):
        problems.append(f"最后一段到 {last_end} 结束，必须到 23:59")

    for index in range(len(archetype.slots) - 1):
        current = archetype.slots[index]
        following = archetype.slots[index + 1]
        if current.end != following.start:
            problems.append(
                f"第 {index + 1} 段结束于 {current.end}，下一段却从 {following.start} 开始（有空档或重叠）"
            )

    need_variants = max(MIN_VARIANTS_PER_SLOT, min(4, want_variants))
    for slot in archetype.slots:
        if len(slot.variants) < need_variants:
            problems.append(f"{slot.start} 段只有 {len(slot.variants)} 个变体，至少要 {need_variants} 个")
        if len(slot.doing_set()) < len(slot.variants):
            problems.append(f"{slot.start} 段有完全重复的变体")
        for variant in slot.variants:
            if len(variant.doing) < 4:
                problems.append(f"{slot.start} 段的变体太短：{variant.doing!r}")
            for word in _FORBIDDEN_WORDS:
                if word in variant.doing or word in variant.hint:
                    problems.append(f"{slot.start} 段出现不该出现的词「{word}」")

    return problems[:12]


@dataclass
class SchedulePool:
    """一周（或配置的天数）内可用的日型池。

    Attributes:
        pool_id: 池子标识（日期 + 人设指纹前缀），换人设即换 id。
        created_at: 生成时间戳。
        refresh_at: 到期时间戳（到了就重刷）。
        persona_fingerprint: 生成时的人设指纹。
        persona_kind: 生成时判定的人设类型（写进当天日志用）。
        persona_name: 生成时判定的角色名（写进当天日志用）。
        model_tag: 生成用的模型标识。
        archetypes: 全部日型（工作日与周末混存，按 kind 分桶）。
        yield_lines: 让位心声池（池子生成时一并产出，抽取时不再调模型）。
        failures: 最近一轮生成里失败的日型与原因（命令展示用）。
        sources_used: 池子生成时用上的素材层。
    """

    pool_id: str = ""
    created_at: float = 0.0
    refresh_at: float = 0.0
    persona_fingerprint: str = ""
    persona_kind: str = ""
    persona_name: str = ""
    model_tag: str = ""
    archetypes: list[Archetype] = field(default_factory=list)
    yield_lines: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    sources_used: list[str] = field(default_factory=list)

    def is_expired(self, now_ts: float) -> bool:
        """池子是否过期（``refresh_at`` 为 0 表示永不过期）。"""
        return bool(self.refresh_at) and now_ts >= self.refresh_at

    def bucket(self, *, weekend: bool) -> list[Archetype]:
        """取某类日子可用的日型。

        周末优先用 ``weekend`` 桶；没编周末日型时退回工作日桶——
        「周末还在上班」比「周末用工作日模板」更假，但「没有池子」最假。

        Args:
            weekend: 是否周六周日。

        Returns:
            日型列表。
        """
        if not weekend:
            return [item for item in self.archetypes if item.kind != ARCHETYPE_WEEKEND]
        weekend_items = [item for item in self.archetypes if item.kind == ARCHETYPE_WEEKEND]
        if weekend_items:
            return weekend_items
        return [item for item in self.archetypes if item.kind != ARCHETYPE_WEEKEND]

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "pool_id": self.pool_id,
            "created_at": self.created_at,
            "refresh_at": self.refresh_at,
            "persona_fingerprint": self.persona_fingerprint,
            "persona_kind": self.persona_kind,
            "persona_name": self.persona_name,
            "model_tag": self.model_tag,
            "archetypes": [item.to_dict() for item in self.archetypes],
            "yield_lines": list(self.yield_lines),
            "failures": list(self.failures),
            "sources_used": list(self.sources_used),
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "SchedulePool | None":
        """从字典解析池子；结构不可用时返回 ``None``。"""
        if not isinstance(raw, dict):
            return None

        archetypes: list[Archetype] = []
        raw_items = raw.get("archetypes")
        if isinstance(raw_items, list):
            for item in raw_items:
                archetype = Archetype.from_dict(item)
                if archetype is not None:
                    archetypes.append(archetype)

        def _number(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        raw_lines = raw.get("yield_lines")
        lines: list[str] = []
        if isinstance(raw_lines, list):
            for item in raw_lines:
                text = _coerce_text(item, 80)
                if text:
                    lines.append(text)

        failures = [
            _coerce_text(item, 200) for item in (raw.get("failures") or []) if _coerce_text(item, 200)
        ] if isinstance(raw.get("failures"), list) else []

        return cls(
            pool_id=_coerce_text(raw.get("pool_id"), 80),
            created_at=_number("created_at"),
            refresh_at=_number("refresh_at"),
            persona_fingerprint=_coerce_text(raw.get("persona_fingerprint"), 64),
            persona_kind=_coerce_text(raw.get("persona_kind"), 32),
            persona_name=_coerce_text(raw.get("persona_name"), 60),
            model_tag=_coerce_text(raw.get("model_tag"), 80),
            archetypes=archetypes,
            yield_lines=lines[:8],
            failures=failures[:8],
            sources_used=[
                _coerce_text(item, 24)
                for item in (raw.get("sources_used") or [])
                if _coerce_text(item, 24)
            ]
            if isinstance(raw.get("sources_used"), list)
            else [],
        )


def validate_pool(
    pool: SchedulePool,
    *,
    min_archetypes: int,
    want_weekend: bool,
) -> list[str]:
    """校验池子整体是否够用。

    比逐个日型更宽：只要**工作日日型够数**就算可用（周末可以退回工作日桶），
    这样"周末日型没编出来"不至于让整池作废。

    Args:
        pool: 待校验的池子。
        min_archetypes: 工作日日型数下限。
        want_weekend: 是否要求周末日型。

    Returns:
        问题描述列表；空清单表示可用。
    """
    problems: list[str] = []
    workday = [item for item in pool.archetypes if item.kind != ARCHETYPE_WEEKEND]
    weekend = [item for item in pool.archetypes if item.kind == ARCHETYPE_WEEKEND]

    if len(workday) < max(1, min_archetypes):
        problems.append(f"工作日日型只有 {len(workday)} 个，少于下限 {min_archetypes}")
    if want_weekend and not weekend:
        problems.append("缺少周末日型")
    return problems


# ── 三层规划：年程 / 月程 / 周程 ──────────────────────────────────────────────
#
# 规划是分层的，每层围绕上一层生成，而且**变化程度逐层放大**：
#
#   年程（这一年怎么走）      —— 最稳，一年一个走向
#     月程（这个月的目标）    —— 分忙碌 / 休息（学期月 vs 寒暑假月）
#       周程（这周该干什么）  —— 定义日程该干什么
#         日程（每天怎么过）  —— 最活，每天不重样；也是随机评估的判落点
#
# 这么分是为了让「稳定」与「新鲜」各归其位：越高层越不该乱变（人设一致性靠它），
# 越底层越该有变化（不重复靠它）。把两者混在一层里，就是早期那版日程的毛病——
# 每天重写一遍，既不稳定（前言不搭后语）又没变化（抄昨天）。


def week_key(moment: datetime) -> str:
    """取 ISO 周标识。

    Args:
        moment: 参考时间。

    Returns:
        形如 ``2026-W40`` 的周标识。
    """
    year, week, _ = moment.isocalendar()
    return f"{year}-W{week:02d}"


def period_key(layer: str, moment: datetime) -> str:
    """取某一层的时期标识。

    Args:
        layer: ``year`` / ``month`` / ``week``。
        moment: 参考时间。

    Returns:
        年 ``2026``；月 ``2026-09``；周 ``2026-W40``。
    """
    if layer == PLAN_LAYER_YEAR:
        return f"{moment:%Y}"
    if layer == PLAN_LAYER_MONTH:
        return f"{moment:%Y-%m}"
    if layer == PLAN_LAYER_WEEK:
        return week_key(moment)
    return f"{moment:%Y-%m-%d}"


def period_start(layer: str, moment: datetime) -> datetime:
    """取某层所在时期的起点（自然边界）。

    Args:
        layer: 规划层。
        moment: 参考时间。

    Returns:
        该时期的起点（年/月/周的零点）。
    """
    if layer == PLAN_LAYER_YEAR:
        return datetime(moment.year, 1, 1)
    if layer == PLAN_LAYER_MONTH:
        return datetime(moment.year, moment.month, 1)
    # ISO 周：周一为起点
    start = moment - timedelta(days=moment.weekday())
    return datetime(start.year, start.month, start.day)


def period_expiry(layer: str, moment: datetime) -> float:
    """取某层所在时期的下一个自然边界（到期时间戳）。

    规划不设"有效期天数"，而是**跟着自然边界走**：周程周一作废、月程月初作废、
    年程元旦作废。这样「这周的安排」不会用到下周三还留着。

    Args:
        layer: 规划层。
        moment: 参考时间。

    Returns:
        到期时间戳。
    """
    if layer == PLAN_LAYER_YEAR:
        return datetime(moment.year + 1, 1, 1).timestamp()
    if layer == PLAN_LAYER_MONTH:
        if moment.month == 12:
            return datetime(moment.year + 1, 1, 1).timestamp()
        return datetime(moment.year, moment.month + 1, 1).timestamp()
    start = period_start(PLAN_LAYER_WEEK, moment)
    return (start + timedelta(days=7)).timestamp()


@dataclass
class PlanItem:
    """计划里的一条「要推进的事」。

    Attributes:
        text: 要做什么（一句话）。
        why: 为什么做（给下层当依据，也是日记里"惦记的事"）。
        kind: ``work`` / ``life`` / ``rest``。
        focus_days: 周程用：偏重哪几天（如 ``["周三","周六"]``；空＝不限）。
        progress: 随机评估结果：``""`` 未评估 / ``ok`` 顺利 / ``stuck`` 卡住了。
        progress_at: 最近一次评估时间戳。
    """

    text: str = ""
    why: str = ""
    kind: str = "work"
    focus_days: list[str] = field(default_factory=list)
    progress: str = ""
    progress_at: float = 0.0

    @property
    def is_open(self) -> bool:
        """还没被评估过的推进项。"""
        return not self.progress

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "text": self.text,
            "why": self.why,
            "kind": self.kind,
            "focus_days": list(self.focus_days),
            "progress": self.progress,
            "progress_at": self.progress_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PlanItem | None":
        """从字典（或裸字符串）解析一条推进项。"""
        if isinstance(raw, str):
            text = _coerce_text(raw, 80)
            return cls(text=text) if text else None
        if not isinstance(raw, dict):
            return None

        text = _coerce_text(raw.get("text") or raw.get("what") or raw.get("doing"), 80)
        if not text:
            return None

        kind = _coerce_text(raw.get("kind"), 16).lower() or "work"
        if kind not in ("work", "life", "rest"):
            kind = "work"

        raw_days = raw.get("focus_days")
        days: list[str] = []
        if isinstance(raw_days, list):
            for item in raw_days:
                day = _coerce_text(item, 8)
                if day and day not in days:
                    days.append(day)

        progress = _coerce_text(raw.get("progress"), 16).lower()
        if progress not in (PLAN_PROGRESS_OK, PLAN_PROGRESS_STUCK):
            progress = ""

        try:
            progress_at = float(raw.get("progress_at") or 0.0)
        except (TypeError, ValueError):
            progress_at = 0.0

        return cls(
            text=text,
            why=_coerce_text(raw.get("why"), 120),
            kind=kind,
            focus_days=days[:4],
            progress=progress,
            progress_at=progress_at,
        )


@dataclass
class PeriodPlan:
    """某一层某个时期的计划。

    Attributes:
        layer: ``year`` / ``month`` / ``week``。
        period: 时期标识（``2026`` / ``2026-09`` / ``2026-W40``）。
        title: 这个时期的说法（如「大二上学期」「开学月」「推进周」）。
        mood: 一句话基调（会被下层当素材，也当那周/那天的小结）。
        kind: ``busy`` / ``rest``（忙碌期 / 休息期，年与月层用）。
        items: 要推进的事。
        note: 给下层的提醒（自由文本）。
        created_at / expires_at: 生成时间与自然到期时间。
        mode: 这份计划是 ``pool`` 抽的还是 ``direct`` 生成的。
        source: 素材来源描述（日志用）。
        pool_id: 来自哪个型池（direct 为空）。
        model_tag: 生成用的模型标识。
        persona_fingerprint: 生成时的人设指纹。
    """

    layer: str = PLAN_LAYER_WEEK
    period: str = ""
    title: str = ""
    mood: str = ""
    kind: str = PLAN_KIND_BUSY
    items: list[PlanItem] = field(default_factory=list)
    note: str = ""
    created_at: float = 0.0
    expires_at: float = 0.0
    mode: str = "daily"
    source: str = ""
    pool_id: str = ""
    model_tag: str = ""
    persona_fingerprint: str = ""

    @property
    def label(self) -> str:
        """展示名（没有 title 就用时期）。"""
        return self.title or self.period

    def is_expired(self, now_ts: float) -> bool:
        """是否已过自然边界。"""
        return bool(self.expires_at) and now_ts >= self.expires_at

    def open_items(self) -> list[PlanItem]:
        """还没被评估过的推进项。"""
        return [item for item in self.items if item.is_open]

    def focus_for_day(self, moment: datetime) -> PlanItem | None:
        """取指定日期该偏重的那件事（周程用）。

        先看有没有明确标了今天的；没有就看有没有标了星期几的；都没有就不指定。

        Args:
            moment: 参考时间。

        Returns:
            命中的推进项，或 ``None``。
        """
        weekday_names = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")
        today_name = weekday_names[moment.weekday()]
        for item in self.items:
            if today_name in item.focus_days:
                return item
        for item in self.items:
            if item.focus_days:
                continue
            # 没标日子的事项：只在「本周主线」意义上算今天的重点（取第一条）
            return item
        return None

    def text_block(self) -> str:
        """渲染成给下层 / 命令看的文本块。"""
        lines = [f"{self.period}｜{self.label}（{self.kind}）：{self.mood}" if self.mood else f"{self.period}｜{self.label}"]
        for item in self.items:
            mark = {"ok": "✔", "stuck": "✘"}.get(item.progress, "·")
            focus = f"（{('、'.join(item.focus_days))}）" if item.focus_days else ""
            why = f"　——{item.why}" if item.why else ""
            lines.append(f"  {mark} {item.text}{focus}{why}")
        if self.note:
            lines.append(f"  提醒：{self.note}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "layer": self.layer,
            "period": self.period,
            "title": self.title,
            "mood": self.mood,
            "kind": self.kind,
            "items": [item.to_dict() for item in self.items],
            "note": self.note,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "mode": self.mode,
            "source": self.source,
            "pool_id": self.pool_id,
            "model_tag": self.model_tag,
            "persona_fingerprint": self.persona_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PeriodPlan | None":
        """从字典解析计划；结构不可用时返回 ``None``。"""
        if not isinstance(raw, dict):
            return None

        layer = _coerce_text(raw.get("layer"), 16).lower()
        if layer not in PLAN_LAYERS:
            layer = PLAN_LAYER_WEEK

        items: list[PlanItem] = []
        raw_items = raw.get("items")
        if isinstance(raw_items, list):
            for entry in raw_items:
                item = PlanItem.from_dict(entry)
                if item is not None:
                    items.append(item)

        def _number(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        kind = _coerce_text(raw.get("kind"), 16).lower()
        if kind not in (PLAN_KIND_BUSY, PLAN_KIND_REST):
            kind = PLAN_KIND_BUSY

        period = _coerce_text(raw.get("period"), 16)
        if not period:
            return None

        return cls(
            layer=layer,
            period=period,
            title=_coerce_text(raw.get("title"), 40),
            mood=_coerce_text(raw.get("mood"), 200),
            kind=kind,
            items=items[:8],
            note=_coerce_text(raw.get("note"), 200),
            created_at=_number("created_at"),
            expires_at=_number("expires_at"),
            mode=_coerce_text(raw.get("mode"), 16) or "daily",
            source=_coerce_text(raw.get("source"), 60),
            pool_id=_coerce_text(raw.get("pool_id"), 80),
            model_tag=_coerce_text(raw.get("model_tag"), 80),
            persona_fingerprint=_coerce_text(raw.get("persona_fingerprint"), 64),
        )


def validate_period_plan(plan: PeriodPlan, *, min_items: int = 1) -> list[str]:
    """校验一份规划是否可用（机械判定，不合格就不落盘）。

    Args:
        plan: 待校验的计划。
        min_items: 推进项数量下限。

    Returns:
        问题清单；空清单表示可用。
    """
    problems: list[str] = []
    if not plan.period:
        problems.append("没有时期标识")
    if not plan.title:
        problems.append("没有说法（title）")
    if len(plan.items) < max(1, min_items):
        problems.append(f"推进项只有 {len(plan.items)} 条，少于下限 {min_items}")
    for item in plan.items:
        for word in _FORBIDDEN_WORDS:
            if word in item.text or word in item.why:
                problems.append(f"推进项出现不该出现的词「{word}」")
                break
    return problems[:8]


@dataclass
class PlanPool:
    """某一层的「型池」：几套可复用的计划模板。

    为什么上层也要池子：直生成是「到期打一次模型」，池子是「一次编几套、
    之后按需抽取」——抽出来的模板再被上层目标**规则化填充**，所以抽一次不花钱。
    但上层的池子必须编得更稳：一层模板会被用上一年/一个月，写坏的代价比日型大。

    Attributes:
        layer: 属于哪一层。
        pool_id: 池子标识。
        created_at / refresh_at: 生成时间与到期时间。
        archetypes: 几套模板（``PeriodPlan``，``period`` 是 ``template``）。
        failures: 最近一轮编失败的原因。
        model_tag / sources_used / persona_fingerprint: 排查与失效判据。
    """

    layer: str = PLAN_LAYER_WEEK
    pool_id: str = ""
    created_at: float = 0.0
    refresh_at: float = 0.0
    archetypes: list[PeriodPlan] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    model_tag: str = ""
    sources_used: list[str] = field(default_factory=list)
    persona_fingerprint: str = ""

    def is_expired(self, now_ts: float) -> bool:
        """池子是否过期。"""
        return bool(self.refresh_at) and now_ts >= self.refresh_at

    def to_dict(self) -> dict[str, Any]:
        """序列化。"""
        return {
            "layer": self.layer,
            "pool_id": self.pool_id,
            "created_at": self.created_at,
            "refresh_at": self.refresh_at,
            "archetypes": [item.to_dict() for item in self.archetypes],
            "failures": list(self.failures),
            "model_tag": self.model_tag,
            "sources_used": list(self.sources_used),
            "persona_fingerprint": self.persona_fingerprint,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PlanPool | None":
        """从字典解析型池。"""
        if not isinstance(raw, dict):
            return None

        layer = _coerce_text(raw.get("layer"), 16).lower()
        if layer not in PLAN_LAYERS:
            layer = PLAN_LAYER_WEEK

        archetypes: list[PeriodPlan] = []
        raw_items = raw.get("archetypes")
        if isinstance(raw_items, list):
            for entry in raw_items:
                plan = PeriodPlan.from_dict(entry)
                if plan is not None:
                    archetypes.append(plan)

        def _number(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        failures = raw.get("failures")
        return cls(
            layer=layer,
            pool_id=_coerce_text(raw.get("pool_id"), 80),
            created_at=_number("created_at"),
            refresh_at=_number("refresh_at"),
            archetypes=archetypes,
            failures=[
                _coerce_text(item, 200) for item in failures if _coerce_text(item, 200)
            ][:6]
            if isinstance(failures, list)
            else [],
            model_tag=_coerce_text(raw.get("model_tag"), 80),
            sources_used=[
                _coerce_text(item, 24)
                for item in (raw.get("sources_used") or [])
                if _coerce_text(item, 24)
            ]
            if isinstance(raw.get("sources_used"), list)
            else [],
            persona_fingerprint=_coerce_text(raw.get("persona_fingerprint"), 64),
        )


@dataclass
class PersonaProfile:
    """人设判定结果（带指纹缓存）。

    Attributes:
        fingerprint: 人设文本指纹；变化即触发重新判定。
        kind: ``roleplay``（扮演已有角色）或 ``original_oc``（原创 OC）。
        character_name: 角色名。
        source_work: 出处作品（原创 OC 为空）。
        world: 世界观一句话。
        occupation: 身份/职业，用于日程取材。
        anchors: 该角色日常里说得通的固定事项（工作、作息、习惯）。
        checked_at: 判定时间戳。
    """

    fingerprint: str = ""
    kind: str = PERSONA_KIND_ORIGINAL
    character_name: str = ""
    source_work: str = ""
    world: str = ""
    occupation: str = ""
    anchors: list[str] = field(default_factory=list)
    checked_at: float = 0.0

    @property
    def is_roleplay(self) -> bool:
        """是否为扮演已有角色。"""
        return self.kind == PERSONA_KIND_ROLEPLAY

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "fingerprint": self.fingerprint,
            "kind": self.kind,
            "character_name": self.character_name,
            "source_work": self.source_work,
            "world": self.world,
            "occupation": self.occupation,
            "anchors": list(self.anchors),
            "checked_at": self.checked_at,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "PersonaProfile | None":
        """从字典解析人设判定结果。

        Args:
            raw: 存储中的原始字典。

        Returns:
            解析成功的结果，或 ``None``。
        """
        if not isinstance(raw, dict):
            return None

        kind = _coerce_text(raw.get("kind"), 32).lower()
        if kind not in (PERSONA_KIND_ROLEPLAY, PERSONA_KIND_ORIGINAL):
            kind = PERSONA_KIND_ORIGINAL

        anchors_raw = raw.get("anchors")
        anchors: list[str] = []
        if isinstance(anchors_raw, list):
            for item in anchors_raw:
                text = _coerce_text(item, 60)
                if text:
                    anchors.append(text)

        try:
            checked_at = float(raw.get("checked_at") or 0.0)
        except (TypeError, ValueError):
            checked_at = 0.0

        return cls(
            fingerprint=_coerce_text(raw.get("fingerprint"), 64),
            kind=kind,
            character_name=_coerce_text(raw.get("character_name"), 60),
            source_work=_coerce_text(raw.get("source_work"), 80),
            world=_coerce_text(raw.get("world"), 200),
            occupation=_coerce_text(raw.get("occupation"), 60),
            anchors=anchors[:8],
            checked_at=checked_at,
        )


@dataclass
class RuntimeState:
    """插件运行时状态（让位与生成节流）。

    Attributes:
        yield_until: 让位状态的到期时间戳（Unix 秒）；0 表示未让位。
        yield_line: 本次让位取用的心声。
        yield_master: 触发让位的主人显示名。
        yield_stream_id: 触发让位的聊天流 ID。
        yield_doing: 让位时原本在做的事。
        diary_inject_left: 日记还能注入几轮（每注入一次减一，减到 0 就不再发）。
        last_roll_date: 最近做过日程判定的日期（``YYYY-MM-DD``，每天最多判一次）。
        last_stream_id: 最近一次主人开口的会话（年目标主动分享用）。
        mood: 情绪档（``good`` / ``normal`` / ``bad``，由最近几天的成功日比例推出）。
        mood_note: 情绪的一句话说明。
        last_generate_at: 最近一次生成尝试的时间戳。
        last_error: 最近一次生成的错误摘要（排查用）。
    """

    yield_until: float = 0.0
    yield_line: str = ""
    yield_master: str = ""
    yield_stream_id: str = ""
    yield_doing: str = ""
    diary_inject_left: int = 0
    last_roll_date: str = ""
    last_stream_id: str = ""
    mood: str = ""
    mood_note: str = ""
    last_generate_at: float = 0.0
    last_error: str = ""

    def is_yielding(self, now_ts: float) -> bool:
        """当前是否处于让位状态。

        Args:
            now_ts: 当前时间戳。

        Returns:
            是否正在让位。
        """
        return self.yield_until > 0 and now_ts < self.yield_until

    def to_dict(self) -> dict[str, Any]:
        """序列化为可 JSON 化的字典。"""
        return {
            "yield_until": self.yield_until,
            "yield_line": self.yield_line,
            "yield_master": self.yield_master,
            "yield_stream_id": self.yield_stream_id,
            "yield_doing": self.yield_doing,
            "diary_inject_left": self.diary_inject_left,
            "last_roll_date": self.last_roll_date,
            "last_stream_id": self.last_stream_id,
            "mood": self.mood,
            "mood_note": self.mood_note,
            "last_generate_at": self.last_generate_at,
            "last_error": self.last_error,
        }

    @classmethod
    def from_dict(cls, raw: Any) -> "RuntimeState":
        """从字典解析运行时状态；缺失字段使用默认值。

        Args:
            raw: 存储中的原始字典。

        Returns:
            运行时状态实例。
        """
        if not isinstance(raw, dict):
            return cls()

        def _number(key: str) -> float:
            try:
                return float(raw.get(key) or 0.0)
            except (TypeError, ValueError):
                return 0.0

        return cls(
            yield_until=_number("yield_until"),
            yield_line=_coerce_text(raw.get("yield_line"), 80),
            yield_master=_coerce_text(raw.get("yield_master"), 60),
            yield_stream_id=_coerce_text(raw.get("yield_stream_id"), 80),
            yield_doing=_coerce_text(raw.get("yield_doing"), 120),
            diary_inject_left=max(0, int(_number("diary_inject_left"))),
            last_roll_date=_coerce_text(raw.get("last_roll_date"), 10),
            last_stream_id=_coerce_text(raw.get("last_stream_id"), 80),
            mood=_coerce_text(raw.get("mood"), 16),
            mood_note=_coerce_text(raw.get("mood_note"), 120),
            last_generate_at=_number("last_generate_at"),
            last_error=_coerce_text(raw.get("last_error"), 200),
        )


__all__ = [
    "ARCHETYPE_WEEKEND",
    "ARCHETYPE_WORKDAY",
    "BUSY_LABELS",
    "MIN_VARIANTS_PER_SLOT",
    "PERSONA_KIND_ORIGINAL",
    "PERSONA_KIND_ROLEPLAY",
    "PLAN_KIND_BUSY",
    "PLAN_KIND_REST",
    "PLAN_LAYERS",
    "PLAN_LAYER_LABELS",
    "PLAN_LAYER_MONTH",
    "PLAN_LAYER_WEEK",
    "PLAN_LAYER_YEAR",
    "PLAN_PROGRESS_OK",
    "PLAN_PROGRESS_STUCK",
    "Archetype",
    "DailySchedule",
    "PeriodPlan",
    "PersonaProfile",
    "PlanItem",
    "PlanPool",
    "PoolSlot",
    "RuntimeState",
    "ScheduleEntry",
    "SchedulePool",
    "ScheduleVariant",
    "normalize_hhmm",
    "period_expiry",
    "period_key",
    "period_start",
    "validate_archetype",
    "validate_period_plan",
    "validate_pool",
    "week_key",
]
