"""daily_schedule 生成层。

包含两次（且每天最多两次）模型调用：

1. **人设类型判定**：区分「扮演已有角色」与「原创 OC」，结果按人设指纹缓存，
   人设不变就不再重复调用；
2. **日程生成**：产出当天时段表、每段的「此刻在做什么」、忙碌等级与反应倾向，
   并顺带产出让位心声池和前一天的小结（写进日志，避免额外调用）。

生成结果会落盘到 ``schedule-YYYY-MM-DD``，同时把完整记录写进 ``log-YYYY-MM-DD``。
"""

from __future__ import annotations

import time
from datetime import date as date_cls
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import llm, sources, store
from .config import DailyScheduleConfig
from .models import (
    PERSONA_KIND_ORIGINAL,
    DailySchedule,
    PersonaProfile,
    ScheduleEntry,
)
from .persona import PersonaSnapshot

logger = get_logger("daily_schedule.generator")

#: 人设类型判定的系统提示词。
_JUDGE_SYSTEM = """\
你是一个设定分析助手。给你一段聊天机器人的「人设」文本，你只做客观梳理，不做创作。

判断规则：
- kind = "roleplay"：人设明显来自已有的动漫 / 游戏 / 小说 / 影视等作品，或在扮演某个有出处的
  既有角色（文本里出现作品名、官方设定、已存在的角色名等线索）。
- kind = "original_oc"：人设是原创设定，没有可考的作品出处。

只输出一个 JSON 对象，不要任何解释、不要代码围栏。字段：

{
  "kind": "roleplay" 或 "original_oc",
  "character_name": "角色名（取不到就填昵称）",
  "source_work": "出处作品名（原创 OC 填空字符串）",
  "world": "一句话世界观，比如「近未来都市」「奇幻大陆」（拿不准就按人设推测一句）",
  "occupation": "该角色平日的身份/职业，用于安排日常（如 学生 / 舰船指挥官 / 女仆 / 咖啡店店员 / 无固定职业）",
  "anchors": ["该角色日常里说得通的固定事项，3-6 条，每条不超过 12 字，如「整理舰装」「去学校」"]
}
"""

#: 日程生成的系统提示词。
_GENERATE_SYSTEM = """\
你正在为一个人格化的聊天机器人安排「今天这一天」。你写的是它自己的生活，
不是给用户看的剧本，也不是任务清单。

硬性要求：
1. 只输出一个 JSON 对象，不要解释、不要代码围栏、不要多余文字。
2. entries 必须完整覆盖 00:00 到 23:59：第一条 start 为 "00:00"，最后一条 end 为 "23:59"，
   相邻条目首尾相接、不留空档、不重叠。时间格式固定为 "HH:MM"。
3. 每条 doing 是「它此刻正在做的事」，用第一人称、现在进行时的一句话，具体、生活化、
   带一点动作或细节（例如「在窗边擦杯子，水汽把玻璃糊住了一半」），10-30 字，
   不要写成日程标题式短词（不要「午饭」这种），也不要写成解释或心理独白。
4. 内容必须贴合给定的人设与世界观：角色的身份、习惯、所处的环境都要对得上，
   不要塞进与设定冲突的东西。
5. busy 是忙碌等级：0=闲着或休息，1=有点忙但在做，2=很忙、顾不上别的。
   只在真的忙的时候给 1 或 2，睡觉、发呆、休息一律给 0。
6. hint 是该时段被搭话时的自然反应倾向，一句话、可留空；它是倾向不是拒绝，
   不要写成「不许打扰」这类硬规则。
7. yield_lines 是 3-5 条心里话，用于「主人在这个时候找它」的场景：
   写它自己心甘情愿把手上的事放下、想先陪对方的心情。
   要求：第一人称、每条 8-20 字、口语化、有温度、各不相同。
   绝对不能出现「日程」「让位」「系统」「规则」「检测到」「优先级」这类词，
   也不要写成「主人来了所以我要让出时间」——要写成「是我自己更想陪你」。
8. 如果给了「前几天的记录」，就让今天和它自然接得上（但不必重复昨天）。

输出格式：
{
  "entries": [
    {"start": "00:00", "end": "06:40", "doing": "……", "busy": 0, "hint": ""},
    ...
  ],
  "yield_lines": ["……", "……", "……"],
  "yesterday_summary": "对前一天的一句话小结；没有前一天记录就填空字符串"
}
"""


def _format_entries(entries: list[ScheduleEntry]) -> str:
    """把日程条目渲染成紧凑文本（回喂与日志共用）。

    Args:
        entries: 日程条目。

    Returns:
        多行文本；无条目时返回空字符串。
    """
    lines: list[str] = []
    for entry in entries:
        span = f"{entry.start}-{entry.end or '??:??'}"
        busy = f" busy={entry.busy}" if entry.busy else ""
        lines.append(f"{span} {entry.doing}{busy}")
    return "\n".join(lines)


async def _history_block(config: DailyScheduleConfig, today: date_cls) -> str:
    """拼出前几天的日程与日志，作为生成时的连续性参考。

    Args:
        config: 插件配置。
        today: 今天（用于跳过当天记录）。

    Returns:
        多行文本；没有历史记录时返回空字符串。
    """
    lookback = max(0, config.schedule.lookback_days)
    if lookback <= 0:
        return ""

    blocks: list[str] = []
    for offset in range(1, lookback + 1):
        day = (today - timedelta(days=offset)).isoformat()
        schedule = await store.load_schedule(day)
        log_payload = await store.load_log(day)

        section: list[str] = []
        if schedule is not None and not schedule.is_empty:
            section.append(_format_entries(schedule.entries))
        if isinstance(log_payload, dict):
            summary = str(log_payload.get("yesterday_summary") or "").strip()
            if summary:
                section.append(f"（小结：{summary}）")
            events = log_payload.get("events")
            if isinstance(events, list) and events:
                recent = [
                    str(item).strip() for item in events[-3:] if str(item).strip()
                ]
                if recent:
                    section.append("（当时发生：" + "；".join(recent) + "）")

        if section:
            blocks.append(f"【{day}】\n" + "\n".join(section))

    return "\n\n".join(blocks)


def _build_user_prompt(
    config: DailyScheduleConfig,
    snapshot: PersonaSnapshot,
    profile: PersonaProfile,
    bundle: sources.SourceBundle,
    history: str,
    now: datetime,
) -> str:
    """组装日程生成的用户提示词。

    Args:
        config: 插件配置。
        snapshot: 人设快照。
        profile: 人设判定结果。
        bundle: 素材包。
        history: 前几天记录文本。
        now: 当前时间。

    Returns:
        用户提示词。
    """
    parts: list[str] = []

    persona_block = bundle.persona_block or snapshot.to_prompt_block()
    if persona_block:
        parts.append("【人设】\n" + persona_block)

    profile_lines = [
        f"类型：{'扮演已有角色' if profile.is_roleplay else '原创角色（原创 OC）'}"
    ]
    if profile.character_name:
        profile_lines.append(f"角色名：{profile.character_name}")
    if profile.source_work:
        profile_lines.append(f"出处：{profile.source_work}")
    if profile.world:
        profile_lines.append(f"世界观：{profile.world}")
    if profile.occupation:
        profile_lines.append(f"平日身份：{profile.occupation}")
    if profile.anchors:
        profile_lines.append("日常锚点：" + "、".join(profile.anchors))
    parts.append("【人设梳理】\n" + "\n".join(profile_lines))

    memory_block = bundle.memory_block()
    if memory_block:
        parts.append("【关于它的记忆（可能有噪音，只取用得上的）】\n" + memory_block)

    internet_block = bundle.internet_block()
    if internet_block:
        parts.append("【联网查到的参考（只取与角色日常相关的信息）】\n" + internet_block)

    if history:
        parts.append("【前几天的记录】\n" + history)

    parts.append(
        f"【今天】{now.strftime('%Y-%m-%d')}，现在是 {now.strftime('%H:%M')}（本地时间）。"
    )
    parts.append(
        f"请安排 {config.schedule.min_entries}-{config.schedule.max_entries} 条条目，"
        "完整覆盖今天 00:00 到 23:59，并按格式输出 JSON。"
    )

    return "\n\n".join(parts)


def _parse_schedule(
    payload: dict[str, Any],
    *,
    max_entries: int,
) -> tuple[list[ScheduleEntry], list[str], str]:
    """解析模型返回的日程 JSON。

    Args:
        payload: 解析出的 JSON 对象。
        max_entries: 条目数上限。

    Returns:
        ``(entries, yield_lines, yesterday_summary)``。
    """
    entries: list[ScheduleEntry] = []
    raw_entries = payload.get("entries")
    if isinstance(raw_entries, list):
        for item in raw_entries:
            entry = ScheduleEntry.from_dict(item)
            if entry is not None:
                entries.append(entry)

    entries.sort(key=lambda item: item.start)
    entries = entries[:max_entries]

    yield_lines: list[str] = []
    raw_lines = payload.get("yield_lines")
    if isinstance(raw_lines, list):
        for item in raw_lines:
            text = " ".join(str(item).split()).strip()
            if text:
                yield_lines.append(text[:80])

    summary = " ".join(str(payload.get("yesterday_summary") or "").split()).strip()[:400]
    return entries, yield_lines[:6], summary


async def ensure_persona_profile(
    config: DailyScheduleConfig, snapshot: PersonaSnapshot
) -> PersonaProfile:
    """确保拿到人设判定结果（带指纹缓存）。

    人设指纹未变时直接复用缓存，不额外调用模型；判定失败时退回
    「原创 OC + 用昵称当角色名」的保守结果，保证流程可以继续。

    Args:
        config: 插件配置。
        snapshot: 人设快照。

    Returns:
        人设判定结果。
    """
    cached = await store.load_persona_profile()
    if cached is not None and cached.fingerprint == snapshot.fingerprint:
        return cached

    logger.info("[daily_schedule] 人设指纹变化或首次运行，开始判定人设类型")
    result = await llm.call(
        config,
        _JUDGE_SYSTEM,
        "【人设文本】\n" + (snapshot.to_prompt_block() or "（人设为空）"),
        temperature=0.2,
        max_tokens=600,
        request_name="daily_schedule_persona",
    )

    profile: PersonaProfile | None = None
    if result.ok:
        payload = llm.extract_json(result.text)
        if payload is not None:
            profile = PersonaProfile.from_dict({**payload, "fingerprint": snapshot.fingerprint})
            if profile is not None:
                profile.checked_at = time.time()

    if profile is None:
        logger.warning(
            f"[daily_schedule] 人设判定失败，退回保守结果: {result.error or 'no json'}"
        )
        profile = PersonaProfile(
            fingerprint=snapshot.fingerprint,
            kind=PERSONA_KIND_ORIGINAL,
            character_name=snapshot.nickname,
            occupation="",
            checked_at=time.time(),
        )

    if not profile.character_name:
        profile.character_name = snapshot.nickname

    await store.save_persona_profile(profile)
    logger.info(
        f"[daily_schedule] 人设判定完成: kind={profile.kind} "
        f"name={profile.character_name} work={profile.source_work or '-'}"
    )
    return profile


async def generate_daily_schedule(
    config: DailyScheduleConfig,
    plugin: Any,
    snapshot: PersonaSnapshot,
    profile: PersonaProfile,
    *,
    day: date_cls | None = None,
    now: datetime | None = None,
) -> DailySchedule | None:
    """生成一天的日程并落盘。

    Args:
        config: 插件配置。
        plugin: 插件实例（互联网层调用工具需要）。
        snapshot: 人设快照。
        profile: 人设判定结果。
        day: 目标日期，默认今天。
        now: 当前时间，默认取系统时间。

    Returns:
        生成成功的日程；失败返回 ``None``。
    """
    moment = now or datetime.now()
    target_day = day or moment.date()

    bundle = await sources.collect(
        config,
        plugin,
        snapshot.to_prompt_block(),
        profile.character_name or snapshot.nickname,
        profile.occupation,
        profile.source_work,
    )

    history = await _history_block(config, target_day)
    user_prompt = _build_user_prompt(config, snapshot, profile, bundle, history, moment)

    if config.plugin.debug_log:
        logger.debug(f"[daily_schedule] 日程生成提示词：\n{user_prompt}")

    result = await llm.call(
        config,
        _GENERATE_SYSTEM,
        user_prompt,
        request_name="daily_schedule_generate",
    )
    if not result.ok:
        await _record_failure(result.error)
        return None

    if config.plugin.debug_log:
        logger.debug(f"[daily_schedule] 日程生成原始返回：\n{result.text}")

    payload = llm.extract_json(result.text)
    if payload is None:
        await store.save_raw_failure(
            result.text, model_tag=result.model_tag, error="unparsable json"
        )
        await _record_failure("模型未返回可解析的 JSON")
        logger.warning(
            f"[daily_schedule] 日程生成失败：无法解析 JSON"
            f"（{len(result.text)} 字，内容：{llm.excerpt(result.text)}；"
            f"原始返回见 {store.RAW_FAILURE_KEY}.json）"
        )
        return None

    entries, yield_lines, summary = _parse_schedule(
        payload, max_entries=config.schedule.max_entries
    )
    if len(entries) < config.schedule.min_entries:
        await store.save_raw_failure(
            result.text, model_tag=result.model_tag, error=f"entries={len(entries)}"
        )
        await _record_failure(f"条目不足（{len(entries)}）")
        logger.warning(
            f"[daily_schedule] 日程生成失败：条目仅 {len(entries)} 条，"
            f"低于下限 {config.schedule.min_entries}"
        )
        return None

    schedule = DailySchedule(
        date=target_day.isoformat(),
        entries=entries,
        yield_lines=yield_lines,
        yesterday_summary=summary,
        generated_at=time.time(),
        model_tag=result.model_tag,
        persona_kind=profile.kind,
        persona_name=profile.character_name,
        sources_used=list(bundle.used),
    )

    await store.save_schedule(schedule)
    if config.log.enabled:
        await store.save_log(
            schedule.date,
            {
                "date": schedule.date,
                "generated_at": schedule.generated_at,
                "model_tag": schedule.model_tag,
                "persona_kind": schedule.persona_kind,
                "persona_name": schedule.persona_name,
                "sources_used": schedule.sources_used,
                "entries": [entry.to_dict() for entry in schedule.entries],
                "yield_lines": schedule.yield_lines,
                "yesterday_summary": schedule.yesterday_summary,
                "events": [],
            },
        )

    await _record_success()
    logger.info(
        f"[daily_schedule] 已生成 {schedule.date} 日程：{len(entries)} 条，"
        f"素材层={','.join(bundle.used) or '无'}，模型={result.model_tag}"
    )
    return schedule


async def append_log_event(day: str, text: str) -> None:
    """往某天日志里追加一条事件记录。

    Args:
        day: ``YYYY-MM-DD``。
        text: 事件描述（如让位、被搭话）。
    """
    payload = await store.load_log(day)
    if not isinstance(payload, dict):
        payload = {"date": day}
    events = payload.get("events")
    if not isinstance(events, list):
        events = []
    events.append(text[:200])
    payload["events"] = events[-30:]
    await store.save_log(day, payload)


async def _record_failure(reason: str) -> None:
    """把生成失败原因写进运行时状态，便于命令排查。

    Args:
        reason: 失败原因。
    """
    state = await store.load_state()
    state.last_generate_at = time.time()
    state.last_error = (reason or "unknown")[:200]
    await store.save_state(state)


async def _record_success() -> None:
    """记录一次成功生成，清掉上一次的失败摘要。"""
    state = await store.load_state()
    state.last_generate_at = time.time()
    state.last_error = ""
    await store.save_state(state)


__all__ = [
    "append_log_event",
    "ensure_persona_profile",
    "generate_daily_schedule",
]
