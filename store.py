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


async def save_raw_failure(text: str, *, model_tag: str = "", error: str = "") -> bool:
    """把一次解析失败的模型原始返回留档，方便事后排查。

    只保留最近一次：解析失败本来就不常见，留档是为了能直接看到模型到底
    吐了什么（被截断？写成散文？），而不是只能盯着「无法解析 JSON」。

    Args:
        text: 模型原始返回。
        model_tag: 实际使用的模型标识。
        error: 失败原因摘要。

    Returns:
        是否写入成功。
    """
    payload = {
        "at": time.time(),
        "model_tag": model_tag,
        "error": error,
        "text": str(text or "")[:20000],
    }
    return await _save_raw(RAW_FAILURE_KEY, payload)


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


async def load_persona_profile() -> PersonaProfile | None:
    """读取人设判定缓存。

    Returns:
        人设判定结果，或 ``None``。
    """
    return PersonaProfile.from_dict(await _load_raw(PERSONA_KEY))


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
    "SCHEDULE_PREFIX",
    "STATE_KEY",
    "STORE_NAME",
    "diary_key",
    "load_diary",
    "load_log",
    "load_persona_profile",
    "load_schedule",
    "load_state",
    "log_key",
    "prune_diaries",
    "recent_diary_dates",
    "recent_log_dates",
    "save_diary",
    "save_log",
    "save_persona_profile",
    "save_schedule",
    "save_state",
    "schedule_key",
]
