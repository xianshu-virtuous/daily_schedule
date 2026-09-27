"""daily_schedule 的完成度与情绪：判定只落在日程层，上层靠 50% 上卷。

为什么这么设计（主人定的口径）
------------------------------
「每一级下一部分完成 50% 以上就算上一级完成，省去了判定完成的 token，只需要判定日程。」

于是整套完成度是**算出来的，不是生成出来的**：

* 只有**日程层**做一次判定——那一天过得算不算成（一次掷骰，不调模型）；
* 周 = 本周已判定的日子里，成功日占比 > 50% 即完成；
* 月 = 该月已判定的周里，完成周占比 > 50%；年 = 该年已判定的月里，完成月占比 > 50%；
* 计划里的每条推进项同理：盯过它的那些天里，成功日占比 > 50% 就算它完成。

这样「年 / 月 / 周 / 项」四级完成度**零模型调用**，而且可解释：翻到哪一天的日志，
都能看出它当时算成还是没算成、因为什么。

忙碌等级怎么参与
----------------
主题里那条「很忙 / 较忙 / 空闲的等级会影响被打断后本日的成功判定」落在这里：

* 每个时段有自己的忙碌等级，一天取其中最忙的那档当 ``busy_peak``；
* 主人到场会让位（= 被打断），每次打断记一条结构化记录（含当时的忙碌等级）；
* **在忙的时段被打断**才扣成功概率（很忙扣得更狠），空闲时被打断不扣；
* 一整天都没忙过（``busy_peak == 0``）反而更容易算成成功日——那天本来就没什么会被耽误。

情绪周期锚在日程上
------------------
情绪不另做一套状态机，直接由最近几天的成功日比例推出来（顺 / 平常 / 有点背），
供日记与 ``/目标`` 展示；要不要注入对话由配置决定（默认不注入，省 token）。
"""

from __future__ import annotations

import random
import time
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from .config import DailyScheduleConfig
from .models import (
    PLAN_LAYER_MONTH,
    PLAN_LAYER_WEEK,
    PLAN_LAYER_YEAR,
    DailySchedule,
    PeriodPlan,
    week_key,
)

logger = get_logger("daily_schedule.progress")

#: 成功概率的钳制区间：再怎么样也别让某一天必成或必败。
_MIN_RATE = 0.05
_MAX_RATE = 0.95

#: 情绪三档的说法（顺 / 平常 / 有点背）。
MOOD_LEVELS = {
    "good": "最近挺顺",
    "normal": "最近平平",
    "bad": "最近有点背",
}


def _clamp(value: float, low: float = _MIN_RATE, high: float = _MAX_RATE) -> float:
    """把概率钳制在合理区间。"""
    return max(low, min(high, value))


def busy_of(schedule: DailySchedule | None, moment: datetime | None = None) -> int:
    """取某天最忙的那一档。

    Args:
        schedule: 某天的日程。
        moment: 只看这个时刻之后的部分（``None`` 表示整天）。

    Returns:
        0 / 1 / 2。
    """
    if schedule is None or not schedule.entries:
        return 0
    peak = 0
    for entry in schedule.entries:
        if moment is not None and entry.start < moment.strftime("%H:%M"):
            continue
        peak = max(peak, entry.busy)
    return peak


def judge_day(
    config: DailyScheduleConfig,
    *,
    day: str,
    schedule: DailySchedule | None,
    interrupts: list[dict[str, Any]] | None = None,
    now: datetime | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    """给某一天做一次成功判定（不调模型，纯本地）。

    成功概率 = 基准（``offline.roll_success_rate``，默认 0.8）
      － 忙时被打断的惩罚（很忙算两次）
      ＋ 一整天都没忙过的加成

    判定结果会连「怎么算出来的」一起返回，落盘后事后可核对——这也是
    「判断成功与否的主要部分是日程」的落点。

    Args:
        config: 插件配置。
        day: ``YYYY-MM-DD``。
        schedule: 那天的日程（提供忙碌等级）。
        interrupts: 那天的打断记录（每条含 ``busy``）。
        now: 判定时刻。
        rng: 随机源（测试用）。

    Returns:
        判定记录字典。
    """
    progress_cfg = config.progress
    base = _clamp(float(getattr(config.offline, "roll_success_rate", 0.8)))
    penalty = max(0.0, float(getattr(progress_cfg, "interrupt_penalty", 0.2)))
    idle_bonus = max(0.0, float(getattr(progress_cfg, "idle_bonus", 0.05)))

    peak = busy_of(schedule)
    items = [item for item in (interrupts or []) if isinstance(item, dict)]
    busy_interrupts = 0.0
    for item in items:
        try:
            level = int(item.get("busy") or 0)
        except (TypeError, ValueError):
            level = 0
        if level >= 2:
            busy_interrupts += 2.0
        elif level == 1:
            busy_interrupts += 1.0

    rate = base - penalty * busy_interrupts
    if peak == 0:
        rate += idle_bonus
    rate = _clamp(rate)

    source = rng or random.Random()
    roll = source.random()
    ok = roll < rate

    return {
        "ok": bool(ok),
        "rate": round(rate, 4),
        "roll": round(roll, 4),
        "base": round(base, 4),
        "busy_peak": peak,
        "interrupts": len(items),
        "busy_interrupts": int(busy_interrupts),
        "judged_at": (now or datetime.now()).timestamp(),
    }


def describe_outcome(outcome: dict[str, Any]) -> str:
    """把判定记录说成一句话（日记与命令共用）。"""
    if not outcome:
        return ""
    verdict = "算成了" if outcome.get("ok") else "没算成"
    peak = int(outcome.get("busy_peak") or 0)
    busy_label = {0: "空闲", 1: "较忙", 2: "很忙"}.get(peak, "空闲")
    broke = int(outcome.get("busy_interrupts") or 0)
    text = f"{verdict}（这天最忙的一档是{busy_label}，成功率 {outcome.get('rate')}）"
    if broke:
        text += f"，被搭话打断了 {broke} 次忙时"
    return text


# ── 完成度：下级 50% 上卷 ─────────────────────────────────────────────────────


def is_complete(ok_count: int, judged: int, *, threshold: float) -> bool:
    """下级占比是否过了阈值。

    Args:
        ok_count: 成功/完成的下级数量。
        judged: 已判定的下级数量。
        threshold: 阈值（默认 0.5，即「50% 以上算完成」）。

    Returns:
        是否算完成。
    """
    if judged <= 0:
        return False
    return (ok_count / judged) > threshold


def day_completion(outcomes: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """日的完成度（就是判定结果本身）。"""
    ok = sum(1 for item in outcomes.values() if item.get("ok"))
    judged = len(outcomes)
    return {
        "judged": judged,
        "ok": ok,
        "ratio": round(ok / judged, 4) if judged else 0.0,
        "complete": bool(judged and ok / judged > 0.5),
    }


def _days_of_week(day: date_cls) -> list[str]:
    """某天所在 ISO 周的七天日期。"""
    start = day - timedelta(days=day.weekday())
    return [(start + timedelta(days=offset)).isoformat() for offset in range(7)]


def _weeks_of_month(moment: datetime) -> list[date_cls]:
    """某个月覆盖到的所有 ISO 周的周一（只取与该月有交集的周）。

    Args:
        moment: 该月内的任意时刻。

    Returns:
        周一日期列表（升序）。
    """
    first = datetime(moment.year, moment.month, 1).date()
    if moment.month == 12:
        last = datetime(moment.year + 1, 1, 1).date() - timedelta(days=1)
    else:
        last = datetime(moment.year, moment.month + 1, 1).date() - timedelta(days=1)

    cursor = first - timedelta(days=first.weekday())
    weeks: list[date_cls] = []
    while cursor <= last:
        weeks.append(cursor)
        cursor += timedelta(days=7)
    return weeks


def _months_of_year(moment: datetime) -> list[datetime]:
    """某年里的十二个月（按月初定位）。"""
    return [datetime(moment.year, month, 1) for month in range(1, 13)]


def rollup(
    config: DailyScheduleConfig,
    layer: str,
    period: str,
    outcomes: dict[str, dict[str, Any]],
    *,
    week_cache: dict[str, dict[str, Any]] | None = None,
    month_cache: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """算某一层某个时期的完成度（下级 50% 上卷，零模型调用）。

    Args:
        config: 插件配置。
        layer: ``week`` / ``month`` / ``year``。
        period: 时期标识。
        outcomes: 已判定的日 → 判定记录。
        week_cache: 周的完成度缓存（算月时复用）。
        month_cache: 月的完成度缓存（算年时复用）。

    Returns:
        ``{"judged", "ok", "ratio", "complete", "unit"}``。
    """
    threshold = float(getattr(config.progress, "rollup_threshold", 0.5))

    if layer == PLAN_LAYER_WEEK:
        start = _week_start_of(period)
        if start is None:
            return {"judged": 0, "ok": 0, "ratio": 0.0, "complete": False, "unit": "日"}
        days = _days_of_week(start)
        inner = {day: outcomes[day] for day in days if day in outcomes}
        return {**day_completion(inner), "unit": "日"}

    if layer == PLAN_LAYER_MONTH:
        week_cache = week_cache if week_cache is not None else {}
        units: list[dict[str, Any]] = []
        for monday in _weeks_of_month(_period_month(period)):
            # 周这一层认的是 ISO 周标识（2026-W40），不是日期——这里要转一次
            key = week_key(datetime(monday.year, monday.month, monday.day))
            if key not in week_cache:
                week_cache[key] = rollup(config, PLAN_LAYER_WEEK, key, outcomes)
            unit = week_cache[key]
            if unit["judged"]:
                units.append(unit)
        ok = sum(1 for item in units if item["complete"])
        return {
            "judged": len(units),
            "ok": ok,
            "ratio": round(ok / len(units), 4) if units else 0.0,
            "complete": is_complete(ok, len(units), threshold=threshold),
            "unit": "周",
        }

    if layer == PLAN_LAYER_YEAR:
        month_cache = month_cache if month_cache is not None else {}
        units = []
        for month_moment in _months_of_year(_period_year(period)):
            key = f"{month_moment:%Y-%m}"
            if key not in month_cache:
                month_cache[key] = rollup(
                    config, PLAN_LAYER_MONTH, key, outcomes, week_cache=week_cache
                )
            unit = month_cache[key]
            if unit["judged"]:
                units.append(unit)
        ok = sum(1 for item in units if item["complete"])
        return {
            "judged": len(units),
            "ok": ok,
            "ratio": round(ok / len(units), 4) if units else 0.0,
            "complete": is_complete(ok, len(units), threshold=threshold),
            "unit": "月",
        }

    return {**day_completion(outcomes), "unit": "日"}


def _week_start_of(period: str) -> date_cls | None:
    """把 ``2026-W40`` 解析成那周的周一。"""
    text = str(period or "").strip()
    if "-W" not in text:
        return None
    year_text, _, week_text = text.partition("-W")
    try:
        return date_cls.fromisocalendar(int(year_text), int(week_text), 1)
    except (TypeError, ValueError):
        return None


def _period_month(period: str) -> datetime:
    """把 ``2026-09`` 解析成该月一号。"""
    try:
        year_text, _, month_text = str(period).partition("-")
        return datetime(int(year_text), int(month_text), 1)
    except (TypeError, ValueError):
        return datetime.now()


def _period_year(period: str) -> datetime:
    """把 ``2026`` 解析成那年一月。"""
    try:
        return datetime(int(str(period).strip()), 1, 1)
    except (TypeError, ValueError):
        return datetime(datetime.now().year, 1, 1)


def item_completion(
    plan: PeriodPlan | None,
    outcomes: dict[str, dict[str, Any]],
    focus_by_day: dict[str, str],
    *,
    threshold: float = 0.5,
) -> list[dict[str, Any]]:
    """算一份计划里每条推进项的完成度。

    一条推进项算完成的条件与上层一致：**盯过它的那些天里，成功日占比 > 50%**。
    「盯过它」＝当天抽取日程时周程点的重点就是它（记在当天日志的 ``focus`` 里）。

    Args:
        plan: 周程 / 月程。
        outcomes: 已判定的日 → 判定记录。
        focus_by_day: 日 → 当天的重点文本。
        threshold: 阈值。

    Returns:
        每条一项：``{"text", "judged", "ok", "ratio", "complete"}``。
    """
    if plan is None:
        return []
    result: list[dict[str, Any]] = []
    for item in plan.items:
        hits = [
            outcomes[day]
            for day, focus in focus_by_day.items()
            if focus and item.text and item.text in focus and day in outcomes
        ]
        ok = sum(1 for entry in hits if entry.get("ok"))
        result.append(
            {
                "text": item.text,
                "judged": len(hits),
                "ok": ok,
                "ratio": round(ok / len(hits), 4) if hits else 0.0,
                "complete": is_complete(ok, len(hits), threshold=threshold),
            }
        )
    return result


# ── 情绪：由最近几天的成功日比例推出来 ───────────────────────────────────────


def mood_of(
    config: DailyScheduleConfig,
    outcomes: dict[str, dict[str, Any]],
    *,
    today: date_cls | None = None,
) -> tuple[str, str]:
    """按最近几天的成功日比例取情绪档。

    Args:
        config: 插件配置。
        outcomes: 已判定的日 → 判定记录。
        today: 参考日期。

    Returns:
        ``(档位, 说法)``：``good`` / ``normal`` / ``bad`` 加一句口语描述。
    """
    days = max(1, int(getattr(config.progress, "mood_days", 3)))
    moment = today or date_cls.today()
    recent: list[dict[str, Any]] = []
    for offset in range(1, days + 2):
        key = (moment - timedelta(days=offset)).isoformat()
        if key in outcomes:
            recent.append(outcomes[key])
        if len(recent) >= days:
            break
    if not recent:
        return "normal", "还没有判定过的日子"

    ratio = sum(1 for item in recent if item.get("ok")) / len(recent)
    if ratio >= 0.7:
        return "good", f"最近 {len(recent)} 天里顺利居多"
    if ratio <= 0.3:
        return "bad", f"最近 {len(recent)} 天里不太顺"
    return "normal", f"最近 {len(recent)} 天里好坏参半"


def mood_line(level: str, note: str) -> str:
    """情绪的一句话（给日记与命令用）。"""
    return f"{MOOD_LEVELS.get(level, '最近平平')}（{note}）" if note else MOOD_LEVELS.get(level, "最近平平")


__all__ = [
    "MOOD_LEVELS",
    "busy_of",
    "day_completion",
    "describe_outcome",
    "is_complete",
    "item_completion",
    "judge_day",
    "mood_line",
    "mood_of",
    "rollup",
]
