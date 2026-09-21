"""daily_schedule 数据模型。

集中定义日程、人设判定结果与运行时状态的结构，并提供容错的解析方法。

模型返回的 JSON 不保证干净（可能缺字段、类型不符、时间格式不规范），
因此所有 ``from_dict`` 都以「尽力解析、坏数据丢弃」为原则，绝不抛异常。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date as date_cls
from datetime import datetime, time
from typing import Any

#: 人设类型：扮演已有角色（来自作品/设定）
PERSONA_KIND_ROLEPLAY = "roleplay"
#: 人设类型：原创 OC（无出处，纯原创设定）
PERSONA_KIND_ORIGINAL = "original_oc"

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
        last_generate_at: 最近一次生成尝试的时间戳。
        last_error: 最近一次生成的错误摘要（排查用）。
    """

    yield_until: float = 0.0
    yield_line: str = ""
    yield_master: str = ""
    yield_stream_id: str = ""
    yield_doing: str = ""
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
            last_generate_at=_number("last_generate_at"),
            last_error=_coerce_text(raw.get("last_error"), 200),
        )


__all__ = [
    "BUSY_LABELS",
    "PERSONA_KIND_ORIGINAL",
    "PERSONA_KIND_ROLEPLAY",
    "DailySchedule",
    "PersonaProfile",
    "RuntimeState",
    "ScheduleEntry",
    "normalize_hhmm",
]
