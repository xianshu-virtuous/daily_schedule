"""daily_schedule 场景注入装配层。

把「当前时段 + 让位状态」翻译成一段可以塞进 prompt 的旁白文本。

三条自我约束（来自插件需求）：

- **不强制**：文本只描述「此刻在做什么」，不要求主模型照做；
- **不补充、不强调**：不加标题、不做展开，末尾用 ``scene.note`` 明确说它只是背景；
- **只留出推脱的余地**：忙碌时给的是「可以说等会儿」的许可，不是「必须拒绝」的规则。

让位文案强调「是我自己更想陪你」，因此优先使用生成日程时一并产出的心声
（``yield_lines``），而不是模板硬编的句子。
"""

from __future__ import annotations

import random
from datetime import datetime
from typing import Any

from .config import DailyScheduleConfig
from .models import DailySchedule, RuntimeState

#: 心声池为空时的兜底让位句（仍保持「自己愿意」的语气）。
_FALLBACK_YIELD_LINE = "手头这些先放一放吧，你说，我听着。"

#: 让位时取不到「原本在做的事」的兜底描述。
_FALLBACK_DOING = "手上原本有事"


def _fill(template: str, mapping: dict[str, Any]) -> str:
    """按占位符逐项替换模板文本。

    使用简单替换而非 ``str.format``：模板里出现未知占位符或花括号时不会抛异常。

    Args:
        template: 模板文本。
        mapping: 占位符名到值的映射。

    Returns:
        替换后的文本。
    """
    result = template
    for key, value in mapping.items():
        result = result.replace("{" + key + "}", str(value))
    return result.strip()


def pick_yield_line(
    config: DailyScheduleConfig,
    schedule: DailySchedule | None,
    state: RuntimeState,
) -> str:
    """挑一条让位心声。

    优先用触发让位那一刻选定的心声（``state.yield_line``），
    这样同一段让位期间措辞稳定；没有时从日程心声池随机取。

    Args:
        config: 插件配置。
        schedule: 当天日程。
        state: 运行时状态。

    Returns:
        让位心声文本。
    """
    if state.yield_line:
        return state.yield_line
    if schedule is not None and schedule.yield_lines:
        return random.choice(schedule.yield_lines)
    return _FALLBACK_YIELD_LINE


def build_scene_line(
    config: DailyScheduleConfig,
    schedule: DailySchedule | None,
    state: RuntimeState,
    now: datetime,
) -> str:
    """构造当下这一刻的场景行（不含 note）。

    Args:
        config: 插件配置。
        schedule: 当天日程。
        state: 运行时状态。
        now: 当前时间。

    Returns:
        场景行文本；无可用内容时返回空字符串。
    """
    scene = config.scene

    if state.is_yielding(now.timestamp()):
        doing = state.yield_doing or _FALLBACK_DOING
        return _fill(
            scene.yield_template,
            {
                "line": pick_yield_line(config, schedule, state),
                "doing": doing,
                "master": state.yield_master or "你",
            },
        )

    if schedule is None or schedule.is_empty:
        return scene.fallback.strip()

    entry = schedule.entry_at(now)
    if entry is None:
        return scene.fallback.strip()

    line = _fill(
        scene.template,
        {
            "doing": entry.doing,
            "busy": entry.busy,
            "busy_label": entry.busy_label,
            "hint": entry.hint,
        },
    )

    if entry.busy >= scene.busy_min_level and scene.busy_suffix:
        line = f"{line}{scene.busy_suffix}"

    if entry.hint and scene.hint_template:
        line = f"{line}\n{_fill(scene.hint_template, {'hint': entry.hint})}"

    return line


def build_injection(
    config: DailyScheduleConfig,
    schedule: DailySchedule | None,
    state: RuntimeState,
    now: datetime,
) -> str:
    """构造最终注入到 prompt 的完整文本块。

    Args:
        config: 插件配置。
        schedule: 当天日程。
        state: 运行时状态。
        now: 当前时间。

    Returns:
        注入文本；任何一层为空时返回空字符串（宁可不注入也不硬编场景）。
    """
    if not config.plugin.enabled or not config.scene.enabled:
        return ""

    line = build_scene_line(config, schedule, state, now)
    if not line:
        return ""

    note = config.scene.note.strip()
    if note and note not in line:
        return f"{line}\n{note}"
    return line


def describe_now(
    schedule: DailySchedule | None, now: datetime
) -> tuple[str, int]:
    """取当前时段的描述与忙碌等级（命令展示与日志用）。

    Args:
        schedule: 当天日程。
        now: 当前时间。

    Returns:
        ``(doing, busy)``；无日程时返回 ``("", 0)``。
    """
    if schedule is None or schedule.is_empty:
        return "", 0
    entry = schedule.entry_at(now)
    if entry is None:
        return "", 0
    return entry.doing, entry.busy


__all__ = [
    "build_injection",
    "build_scene_line",
    "describe_now",
    "pick_yield_line",
]
