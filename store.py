"""daily_schedule 存储层。

统一封装插件对 JSON 存储的读写，命名空间固定为 ``daily_schedule``，
落盘位置为 ``data/json_storage/daily_schedule/``。

键名约定：

- ``schedule-YYYY-MM-DD``：某天的日程
- ``log-YYYY-MM-DD``：某天的日志（生成记录 + 前一日小结）
- ``persona-profile``：人设类型判定结果（带指纹）
- ``runtime-state``：让位与生成节流状态
- ``raw-failure-last``：最近一次「模型返回解析不出 JSON」的原始文本留档

读写均按「失败降级」处理：解析或 IO 出错时返回空值并记日志，
绝不让存储问题打断对话主流程。
"""

from __future__ import annotations

import time
from typing import Any

from src.app.plugin_system.api import storage_api
from src.app.plugin_system.api.log_api import get_logger

from .models import DailySchedule, PersonaProfile, RuntimeState

logger = get_logger("daily_schedule.store")

#: JSON 存储命名空间。
STORE_NAME = "daily_schedule"

#: 人设判定缓存的存储键。
PERSONA_KEY = "persona-profile"

#: 运行时状态的存储键。
STATE_KEY = "runtime-state"

#: 解析失败的原始返回留档键（只保留最近一次）。
RAW_FAILURE_KEY = "raw-failure-last"

#: 日志键前缀。
LOG_PREFIX = "log-"

#: 日程键前缀。
SCHEDULE_PREFIX = "schedule-"

#: day 日记键前缀。
DIARY_PREFIX = "diary-"

#: 当前生效的日程池键。
POOL_KEY = "pool-current"

#: 正在编、还没编完的日程池键（增量落盘，重启接着编）。
POOL_STAGING_KEY = "pool-staging"


def schedule_key(date_str: str) -> str:
    """生成某天日程的存储键。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        存储键名。
    """
    return f"{SCHEDULE_PREFIX}{date_str}"


def log_key(date_str: str) -> str:
    """生成某天日志的存储键。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        存储键名。
    """
    return f"{LOG_PREFIX}{date_str}"


async def _load_raw(name: str) -> dict[str, Any] | None:
    """读取原始字典，任何异常都降级为 ``None``。

    Args:
        name: 存储键名。

    Returns:
        数据字典，或 ``None``。
    """
    try:
        return await storage_api.load_json(STORE_NAME, name)
    except Exception as error:  # noqa: BLE001 - 存储异常不应影响对话主流程
        logger.warning(f"[daily_schedule] 读取 {name} 失败，按缺失处理: {error}")
        return None


async def _save_raw(name: str, data: dict[str, Any]) -> bool:
    """写入字典，任何异常都降级为 ``False``。

    Args:
        name: 存储键名。
        data: 待写入数据。

    Returns:
        是否写入成功。
    """
    try:
        await storage_api.save_json(STORE_NAME, name, data)
        return True
    except Exception as error:  # noqa: BLE001 - 存储异常不应影响对话主流程
        logger.warning(f"[daily_schedule] 写入 {name} 失败: {error}")
        return False


async def load_json_raw(name: str) -> dict[str, Any] | None:
    """读任意存储键的原始字典（用量统计这类"插件自己的小账本"用）。

    与 :func:`_load_raw` 的区别：这个是**对外**的，别的模块不用再自己碰 ``_load_raw``。

    Args:
        name: 存储键名。

    Returns:
        数据字典，或 ``None``。
    """
    return await _load_raw(name)


async def save_json_raw(name: str, data: dict[str, Any]) -> bool:
    """写任意存储键。

    Args:
        name: 存储键名。
        data: 待写入数据。

    Returns:
        是否写入成功。
    """
    if not isinstance(data, dict):
        return False
    return await _save_raw(name, data)


async def load_schedule(date_str: str) -> DailySchedule | None:
    """读取某天的日程。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        日程实例；不存在或数据不可用时返回 ``None``。
    """
    return DailySchedule.from_dict(await _load_raw(schedule_key(date_str)))


async def save_schedule(schedule: DailySchedule) -> bool:
    """保存某天的日程。

    Args:
        schedule: 日程实例。

    Returns:
        是否保存成功。
    """
    return await _save_raw(schedule_key(schedule.date), schedule.to_dict())


async def load_log(date_str: str) -> dict[str, Any] | None:
    """读取某天的日志。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        日志字典，或 ``None``。
    """
    return await _load_raw(log_key(date_str))


async def save_raw_failure(
    text: str, *, model_tag: str = "", error: str = "", slot: str = ""
) -> bool:
    """把一次解析失败的模型原始返回留档，方便事后排查。

    只保留最近一次：解析失败本来就不常见，留档是为了能直接看到模型到底
    吐了什么（被截断？写成散文？），而不是只能盯着「无法解析 JSON」。

    Args:
        text: 模型原始返回。
        model_tag: 实际使用的模型标识。
        error: 失败原因摘要。
        slot: 失败来源（``pool`` / ``plan`` / ``generate``）。给了就**额外**写一份
            ``raw-failure-<slot>.json``：``raw-failure-last`` 会被后发生的失败覆盖，
            而刷池一轮要重试好几次、又和规划生成交错跑，原来的现场很容易被别的
            阶段挤掉——分来源留档才查得动。

    Returns:
        是否写入成功。
    """
    payload = {
        "at": time.time(),
        "model_tag": model_tag,
        "error": error,
        "text": str(text or "")[:20000],
    }
    ok = await _save_raw(RAW_FAILURE_KEY, payload)
    if slot:
        await _save_raw(f"{RAW_FAILURE_KEY}-{slot}", payload)
    return ok


async def load_raw_failure() -> dict[str, Any] | None:
    """读取最近一次解析失败的留档。

    Returns:
        留档字典，或 ``None``。
    """
    return await _load_raw(RAW_FAILURE_KEY)


async def save_log(date_str: str, payload: dict[str, Any]) -> bool:
    """保存某天的日志。

    Args:
        date_str: ``YYYY-MM-DD``。
        payload: 日志内容。

    Returns:
        是否保存成功。
    """
    return await _save_raw(log_key(date_str), payload)


async def recent_log_dates(limit: int) -> list[str]:
    """列出最近的日志日期。

    Args:
        limit: 返回条数上限（按日期倒序）。

    Returns:
        日期字符串列表（``YYYY-MM-DD``，倒序）。
    """
    try:
        names = await storage_api.list_json(STORE_NAME)
    except Exception as error:  # noqa: BLE001 - 存储异常不应影响对话主流程
        logger.warning(f"[daily_schedule] 列出存储键失败: {error}")
        return []

    dates = sorted(
        (name[len(LOG_PREFIX) :] for name in names if name.startswith(LOG_PREFIX)),
        reverse=True,
    )
    return dates[: max(0, limit)]


async def recent_schedule_dates(limit: int) -> list[str]:
    """列出最近有日程的日期。

    Args:
        limit: 返回条数上限（按日期倒序）。

    Returns:
        日期字符串列表（``YYYY-MM-DD``，倒序）。
    """
    try:
        names = await storage_api.list_json(STORE_NAME)
    except Exception as error:  # noqa: BLE001 - 存储异常不应影响对话主流程
        logger.warning(f"[daily_schedule] 列出存储键失败: {error}")
        return []

    dates = sorted(
        (name[len(SCHEDULE_PREFIX) :] for name in names if name.startswith(SCHEDULE_PREFIX)),
        reverse=True,
    )
    return dates[: max(0, limit)]


# ── 日程池 ────────────────────────────────────────────────────────────────────
#
# 两个键：``pool-current`` 是正在用的池子，``pool-staging`` 是正在编的半成品。
# 分开的理由是"写坏率"：刷池失败时旧池子必须原封不动，编了一半也得能接着编。


async def load_pool() -> "SchedulePool | None":
    """读取当前生效的日程池。

    Returns:
        池子实例；不存在或数据不可用时返回 ``None``。
    """
    from .models import SchedulePool

    return SchedulePool.from_dict(await _load_raw(POOL_KEY))


async def save_pool(pool: "SchedulePool") -> bool:
    """写入当前生效的日程池。"""
    return await _save_raw(POOL_KEY, pool.to_dict())


async def load_pool_staging() -> "SchedulePool | None":
    """读取正在编的日程池半成品。"""
    from .models import SchedulePool

    return SchedulePool.from_dict(await _load_raw(POOL_STAGING_KEY))


async def save_pool_staging(pool: "SchedulePool") -> bool:
    """写入日程池半成品（每编好一个日型就落一次，重启接着编）。"""
    return await _save_raw(POOL_STAGING_KEY, pool.to_dict())


async def delete_pool_staging() -> bool:
    """删掉半成品（池子正式替换后调用）。"""
    try:
        await storage_api.delete_json(STORE_NAME, POOL_STAGING_KEY)
        return True
    except Exception as error:  # noqa: BLE001 - 删不掉只是多占一个键
        logger.warning(f"[daily_schedule] 清理 {POOL_STAGING_KEY} 失败: {error}")
        return False


async def recent_schedules(limit: int) -> list["DailySchedule"]:
    """读取最近几天的日程（最近的在前）。

    抽取日型时要靠它避开"刚用过的日型"，也要靠它避开"昨天同一时段的同一句话"。

    Args:
        limit: 天数上限。

    Returns:
        日程列表；没有记录时返回空列表。
    """
    days: list[DailySchedule] = []
    for date_str in await recent_schedule_dates(limit):
        schedule = await load_schedule(date_str)
        if schedule is not None and not schedule.is_empty:
            days.append(schedule)
    return days


# ── 三层规划（年 / 月 / 周） ──────────────────────────────────────────────────
#
# 每层每个时期一个键（``plan-week-2026-W40``），型池两个键（``plan-pool-week`` /
# ``plan-pool-week-staging``）。为什么不像日程那样只留"当前"：规划链要能回溯——
# 生成下一期时要看上一期实际怎么样（以及随机评估的结果），所以按时期存。


#: 规划层键前缀。
PLAN_PREFIX = "plan-"

#: 规划型池键前缀。
PLAN_POOL_PREFIX = "plan-pool-"


def plan_key(layer: str, period: str) -> str:
    """生成某一层某个时期的存储键。

    Args:
        layer: ``year`` / ``month`` / ``week``。
        period: 时期标识。

    Returns:
        存储键名。
    """
    return f"{PLAN_PREFIX}{layer}-{period}"


def plan_pool_key(layer: str, *, staging: bool = False) -> str:
    """生成某一层型池的存储键。"""
    suffix = "-staging" if staging else ""
    return f"{PLAN_POOL_PREFIX}{layer}{suffix}"


async def load_plan(layer: str, period: str) -> "PeriodPlan | None":
    """读取某一层某个时期的计划。"""
    from .models import PeriodPlan

    return PeriodPlan.from_dict(await _load_raw(plan_key(layer, period)))


async def save_plan(plan: "PeriodPlan") -> bool:
    """写入某一层某个时期的计划。"""
    return await _save_raw(plan_key(plan.layer, plan.period), plan.to_dict())


async def load_previous_plan(layer: str, offset: int) -> "PeriodPlan | None":
    """读取这一层往前数第 N 期的计划（offset=1 即上一期）。

    用来做两件事：看上一期实际怎样（给下一期当材料）、以及避开连着抽同一种走向。

    Args:
        layer: 规划层。
        offset: 往前数几期（从 1 开始）。

    Returns:
        计划实例；没有时返回 ``None``。
    """
    from .models import PeriodPlan

    dates = await recent_plan_periods(layer, offset + 1)
    if len(dates) <= offset:
        return None
    return PeriodPlan.from_dict(await _load_raw(plan_key(layer, dates[offset])))


async def recent_plan_periods(layer: str, limit: int) -> list[str]:
    """列出某一层最近有记录的时期（倒序）。

    Args:
        layer: 规划层。
        limit: 条数上限。

    Returns:
        时期标识列表（倒序）。
    """
    prefix = f"{PLAN_PREFIX}{layer}-"
    try:
        names = await storage_api.list_json(STORE_NAME)
    except Exception as error:  # noqa: BLE001 - 存储异常不应影响对话主流程
        logger.warning(f"[daily_schedule] 列出规划键失败: {error}")
        return []

    periods = sorted(
        (name[len(prefix) :] for name in names if name.startswith(prefix)),
        reverse=True,
    )
    return periods[: max(0, limit)]


async def load_plan_pool(layer: str) -> "PlanPool | None":
    """读取某一层的型池。"""
    from .models import PlanPool

    return PlanPool.from_dict(await _load_raw(plan_pool_key(layer)))


async def save_plan_pool(pool: "PlanPool") -> bool:
    """写入某一层的型池。"""
    return await _save_raw(plan_pool_key(pool.layer), pool.to_dict())


async def load_plan_pool_staging(layer: str) -> "PlanPool | None":
    """读取某一层正在编的型池半成品。"""
    from .models import PlanPool

    return PlanPool.from_dict(await _load_raw(plan_pool_key(layer, staging=True)))


async def save_plan_pool_staging(pool: "PlanPool") -> bool:
    """写入某一层型池的半成品（编好一套落一次）。"""
    return await _save_raw(plan_pool_key(pool.layer, staging=True), pool.to_dict())


async def delete_plan_pool_staging(layer: str) -> bool:
    """删掉某一层型池的半成品（整池替换后调用）。"""
    try:
        await storage_api.delete_json(STORE_NAME, plan_pool_key(layer, staging=True))
        return True
    except Exception as error:  # noqa: BLE001 - 删不掉只是多占一个键
        logger.warning(f"[daily_schedule] 清理 {layer} 型池半成品失败: {error}")
        return False


async def load_persona_profile() -> PersonaProfile | None:
    """读取人设判定缓存。

    Returns:
        人设判定结果，或 ``None``。
    """
    return PersonaProfile.from_dict(await _load_raw(PERSONA_KEY))


async def load_logs(dates: list[str]) -> dict[str, dict[str, Any]]:
    """批量读取若干天的日志（完成度计算用一次读一批，不要一天一个来回）。

    Args:
        dates: 日期列表（``YYYY-MM-DD``）。

    Returns:
        ``{日期: 日志内容}``；读不到的日期不出现。
    """
    result: dict[str, dict[str, Any]] = {}
    for date_str in dates:
        payload = await _load_raw(log_key(date_str))
        if isinstance(payload, dict):
            result[date_str] = payload
    return result


async def day_outcomes(dates: list[str] | None = None, *, limit: int = 0) -> dict[str, dict]:
    """取已判定过的日 → 判定记录。

    Args:
        dates: 指定日期；``None`` 表示扫最近 ``limit`` 天。
        limit: ``dates`` 为 ``None`` 时的天数上限。

    Returns:
        ``{日期: 判定记录}``。
    """
    if dates is None:
        dates = await recent_log_dates(limit or 40)
    logs = await load_logs(list(dates))
    return {
        day: payload["outcome"]
        for day, payload in logs.items()
        if isinstance(payload.get("outcome"), dict)
    }


async def focus_by_day(dates: list[str]) -> dict[str, str]:
    """取「日期 → 当天抽取日程时周程点的重点」。

    Args:
        dates: 日期列表。

    Returns:
        ``{日期: 重点文本}``（没有重点的日期不出现）。
    """
    logs = await load_logs(list(dates))
    return {
        day: str(payload.get("focus") or "")
        for day, payload in logs.items()
        if str(payload.get("focus") or "").strip()
    }


async def save_persona_profile(profile: PersonaProfile) -> bool:
    """写入人设判定缓存。

    Args:
        profile: 人设判定结果。

    Returns:
        是否保存成功。
    """
    return await _save_raw(PERSONA_KEY, profile.to_dict())


async def load_state() -> RuntimeState:
    """读取运行时状态（缺失时返回默认状态）。

    Returns:
        运行时状态实例。
    """
    return RuntimeState.from_dict(await _load_raw(STATE_KEY))


async def save_state(state: RuntimeState) -> bool:
    """写入运行时状态。

    Args:
        state: 运行时状态。

    Returns:
        是否保存成功。
    """
    return await _save_raw(STATE_KEY, state.to_dict())


# ── day 日记（离线生活） ─────────────────────────────────────────────────────
#
# 日记刻意用独立的键，不和 schedule- / log- 混在一起：日程是「计划」，
# 日志是「生成记录」，日记是「已经发生过的事实」。三者语义不同，
# 混在一个键里迟早会把「计划」当成「发生过的」回喂给模型——那正是幻觉的来源。
#
# 这里只做 dict 的存取，不认识 DiaryDay 模型，避免 store 与 diary 互相导入。


def diary_key(date_str: str) -> str:
    """生成某天日记的存储键。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        存储键名。
    """
    return f"{DIARY_PREFIX}{date_str}"


async def load_diary(date_str: str) -> dict[str, Any] | None:
    """读取某天的日记。

    Args:
        date_str: ``YYYY-MM-DD``。

    Returns:
        日记字典，或 ``None``。
    """
    return await _load_raw(diary_key(date_str))


async def save_diary(date_str: str, payload: dict[str, Any]) -> bool:
    """保存某天的日记。

    Args:
        date_str: ``YYYY-MM-DD``。
        payload: 日记内容。

    Returns:
        是否保存成功。
    """
    return await _save_raw(diary_key(date_str), payload)


async def recent_diary_dates(limit: int) -> list[str]:
    """列出最近有日记的日期。

    Args:
        limit: 返回条数上限（按日期倒序）。

    Returns:
        日期字符串列表（``YYYY-MM-DD``，倒序）。
    """
    try:
        names = await storage_api.list_json(STORE_NAME)
    except Exception as error:  # noqa: BLE001 - 存储异常不应影响对话主流程
        logger.warning(f"[daily_schedule] 列出存储键失败: {error}")
        return []

    dates = sorted(
        (name[len(DIARY_PREFIX) :] for name in names if name.startswith(DIARY_PREFIX)),
        reverse=True,
    )
    return dates[: max(0, limit)]


async def prune_diaries(keep_days: int) -> list[str]:
    """删掉超出保留期的日记。

    只删 diary- 键，绝不碰 schedule- / log-：那两份是生成素材，
    删了会让日程失去连续性，而日记的定位是「可读的过去」，过期即可回收。

    Args:
        keep_days: 保留最近多少天；小于等于 0 表示永久保留，不删。

    Returns:
        被删除的日期列表（倒序）。
    """
    if keep_days <= 0:
        return []

    dates = await recent_diary_dates(10_000)
    doomed = dates[max(0, keep_days) :]
    if not doomed:
        return []

    removed: list[str] = []
    for date_str in doomed:
        try:
            await storage_api.delete_json(STORE_NAME, diary_key(date_str))
            removed.append(date_str)
        except Exception as error:  # noqa: BLE001 - 删不掉只是占点空间
            logger.warning(f"[daily_schedule] 删除旧日记 {date_str} 失败: {error}")
    return removed


__all__ = [
    "DIARY_PREFIX",
    "LOG_PREFIX",
    "PERSONA_KEY",
    "PLAN_POOL_PREFIX",
    "PLAN_PREFIX",
    "POOL_KEY",
    "POOL_STAGING_KEY",
    "SCHEDULE_PREFIX",
    "STATE_KEY",
    "STORE_NAME",
    "delete_plan_pool_staging",
    "delete_pool_staging",
    "day_outcomes",
    "diary_key",
    "focus_by_day",
    "load_diary",
    "load_json_raw",
    "load_log",
    "load_logs",
    "load_persona_profile",
    "load_plan",
    "load_plan_pool",
    "load_plan_pool_staging",
    "load_pool",
    "load_pool_staging",
    "load_previous_plan",
    "load_schedule",
    "load_state",
    "log_key",
    "plan_key",
    "plan_pool_key",
    "prune_diaries",
    "recent_diary_dates",
    "recent_log_dates",
    "recent_plan_periods",
    "recent_schedule_dates",
    "recent_schedules",
    "save_diary",
    "save_json_raw",
    "save_log",
    "save_persona_profile",
    "save_plan",
    "save_plan_pool",
    "save_plan_pool_staging",
    "save_pool",
    "save_pool_staging",
    "save_schedule",
    "save_state",
    "schedule_key",
]
