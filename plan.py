"""daily_schedule 的三层规划：年程 → 月程 → 周程（日程在 :mod:`pool` / :mod:`generator`）。

为什么分三层
------------
日程要同时满足两件相反的事：**稳定**（她说的话前后一致、像个同一个人）和
**新鲜**（不会连着三天做同一件事）。把这两件事压在一层里，就会像早期那版：
每天重写一遍，结果既不稳定（前言不搭后语）又没变化（抄昨天骨架换词）。

所以按「稳定 → 活」排成四层，每层围绕上一层生成，变化程度逐层放大：

============================  ==========================================  ==========
层                            它回答什么                                  变化程度
============================  ==========================================  ==========
年程 ``year``                 这一年准备怎么过（大方向）                  最低
月程 ``month``                这个月的目标；分忙碌 / 休息（寒暑假）        低
周程 ``week``                 这周该干什么（把月目标落到周内节奏）        中
日程 ``day``（:mod:`pool`）   每天每段具体做什么；随机评估的判落点        最高
============================  ==========================================  ==========

两层模式与日程层一致：

- ``pool``（默认）：编几套**型**（几套"这类时期通常怎么过"的模板），到期时抽一套，
  再用上层目标**规则化填充**——抽取与填充都不调模型，于是"编一次用很久"。
- ``direct``：到期现场生成一份。月/年层一次调用本来就不贵，想要更贴合当下就选它。

失效与回退
----------
规划**不设"有效期天数"，而是跟自然边界走**：周程周一作废、月程月初作废、年程元旦作废——
免得「这周的安排」用到下周三还挂着。任一层缺失或过期时，只补那一层；补不出来就直接跳过，
**下层照常生成**（月程没有也能有周程，周程没有也能有日程），绝不让规划链断掉就没了生活。

事实优先
--------
计划是**期望**，日记和日程是**事实**。下一轮生成上层规划时，事实（日记里真的发生过什么、
推进项被随机评估成什么结果）会作为材料喂回去纠正计划——没有这条，三层会互相背书：
计划写在那儿，模型就照着念。
"""

from __future__ import annotations

import hashlib
import time
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import budget, generator, llm, sources, store
from .config import DailyScheduleConfig
from .models import (
    PLAN_KIND_BUSY,
    PLAN_KIND_REST,
    PLAN_LAYER_MONTH,
    PLAN_LAYER_LABELS,
    PLAN_LAYER_WEEK,
    PLAN_LAYER_YEAR,
    PLAN_LAYERS,
    PeriodPlan,
    PlanItem,
    PlanPool,
    period_expiry,
    period_key,
    validate_period_plan,
)
from .persona import PersonaSnapshot, read_persona

logger = get_logger("daily_schedule.plan")

#: 各层的系统提示词。``{...}`` 占位符由 :func:`_system_prompt` 填。
_SYSTEM = {
    PLAN_LAYER_YEAR: """\
你在为一个人格化的聊天机器人安排「这一年」。你写的是它自己的生活计划，不是给用户看的任务清单。

【防背书】所有文本都是给它**当背景**用的，不是让它照念的台词：用你自己组织的说法写，
不要复述人设材料里的原句，也不要写成一句能被整句原样搬走的套话（比如「今年要努力学习」）。

硬性要求：
1. 只输出一个 JSON 对象，不要解释、不要代码围栏。
2. 一年只需要**一个走向**：这一年它整体在过什么日子（学业年 / 工作年 / 边工作边考试的一年……）。
3. kind 填 "busy" 或 "rest"：整体偏忙填 busy，整体宽松（比如毕业后的空档年、休养年）填 rest。
4. items 给 2-4 条**这一年真正会推进的事**，每条 text ≤ 20 字、why ≤ 30 字（为什么做）。
   不要写"保持好心情"这种谁都能写的话，要贴着人设的身份与所在环境。
5. 不要出现「日程」「让位」「系统」「规则」「优先级」这些词。
6. 只写这个人设说得通的事，不要发明设定里没有的身份、地点与人物。

输出格式：
{"title": "这一年的说法（4-8 字）", "mood": "这一整年的一句话基调", "kind": "busy",
 "items": [{"text": "……", "why": "……", "kind": "work"}, …],
 "note": "给这一年的提醒（一句话，可空）"}
""",
    PLAN_LAYER_MONTH: """\
你在为一个人格化的聊天机器人安排「这个月」。它围绕**年程**展开，你写的是它自己的生活。

【防背书】所有文本都是给它**当背景**用的，不是让它照念的台词：不要复述材料里的原句。

硬性要求：
1. 只输出一个 JSON 对象，不要解释、不要代码围栏。
2. kind 填 "busy" 或 "rest"：开学月、考试月、赶工月是 busy；
   寒暑假、长假、休养月是 rest。**这一条很重要**：休息月就该是休息月的节奏。
3. items 给 1-3 条**这个月要推进的事**（从年程的方向里挑，别另起炉灶），
   每条 text ≤ 20 字、why ≤ 30 字。
4. mood 与 title 要能看出这个月的性质（如「开学月」「期末冲刺」「暑假放空」）。
5. 不要出现「日程」「让位」「系统」「规则」「优先级」这些词。

输出格式：
{"title": "这个月的说法（4-8 字）", "mood": "这个月的一句话基调", "kind": "busy",
 "items": [{"text": "……", "why": "……", "kind": "work"}, …],
 "note": "给这个月的提醒（一句话，可空）"}
""",
    PLAN_LAYER_WEEK: """\
你在为一个人格化的聊天机器人安排「这一周」。它围绕**月程**展开，
你写的是「这周的每一天该干什么」的指导——**日程会照着你写的东西来安排**。

【防背书】所有文本都是给它**当背景**用的，不是让它照念的台词：不要复述材料里的原句。

硬性要求：
1. 只输出一个 JSON 对象，不要解释、不要代码围栏。
2. items 给 1-3 条**这周要推进的事**，必须来自月程（可以更具体一点）。
   每条可以带 focus_days：偏重哪几天，取值只能是「周一」「周二」…「周日」，
   例如 ["周三", "周六"]；不指定就留空数组。
3. mood 写这一周的节奏（比如「前半周赶课，后半周松下来」）。
4. kind 填 "busy" 或 "rest"：这一周整体是紧的还是松的。
5. 不要出现「日程」「让位」「系统」「规则」「优先级」这些词。

输出格式：
{"title": "这一周的说法（4-8 字）", "mood": "这一周的一句话节奏", "kind": "busy",
 "items": [{"text": "……", "why": "……", "kind": "work", "focus_days": ["周三"]}, …],
 "note": "给这一周的提醒（一句话，可空）"}
""",
}

#: 各层的「写几套」提示（池子模式下一次只编一套型，此处描述这一套的位置）。
_KIND_HINT = {
    PLAN_LAYER_YEAR: "这一年整体偏忙还是偏松",
    PLAN_LAYER_MONTH: "这个月是忙碌月还是休息月（学生：学期月 vs 寒暑假月）",
    PLAN_LAYER_WEEK: "这一周偏紧还是偏松",
}


def _seed(*parts: str) -> int:
    """稳定的种子（不用内置 ``hash()``：它带进程随机化，抽取必须可复现）。"""
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _cfg_value(config: DailyScheduleConfig, name: str, layer: str, default: Any) -> Any:
    """读「按层分」的配置项。

    配置写成 ``year_mode`` / ``month_mode`` / ``week_mode`` 这种逐层字段最直观；
    这里也接受 ``mode = {year = "pool", …}` 的字典写法，两种都不会读崩。

    Args:
        config: 插件配置。
        name: 配置项基名（``mode`` / ``refresh_days``）。
        layer: 规划层。
        default: 都取不到时的默认值。

    Returns:
        配置值。
    """
    plan_cfg = getattr(config, "plan", None)
    if plan_cfg is None:
        return default
    mapping = getattr(plan_cfg, name, None)
    if isinstance(mapping, dict):
        return mapping.get(layer, default)
    return getattr(plan_cfg, f"{name}_{layer}", default)


# ── 上层材料 ──────────────────────────────────────────────────────────────────


def upper_block(plans: dict[str, PeriodPlan | None], layer: str) -> str:
    """把「上一层（以及更上层的目标）」渲染成提示词片段。

    Args:
        plans: 已有的各层计划。
        layer: 当前要生成的层。

    Returns:
        多行文本；没有上层材料时返回空字符串。
    """
    order = [PLAN_LAYER_YEAR, PLAN_LAYER_MONTH, PLAN_LAYER_WEEK]
    index = order.index(layer) if layer in order else 0
    parts: list[str] = []
    for name in order[:index]:
        plan = plans.get(name)
        if plan is None:
            continue
        parts.append(f"【{PLAN_LAYER_LABELS[name]}】\n{plan.text_block()}")
    return "\n\n".join(parts)


def plan_block(plans: dict[str, PeriodPlan | None]) -> str:
    """把三层计划渲染成给日程层（生成 / 抽取 / 日记）的素材块。

    Args:
        plans: 各层计划。

    Returns:
        多行文本；一层都没有时返回空字符串。
    """
    blocks = [
        plan.text_block()
        for name in PLAN_LAYERS
        if (plan := plans.get(name)) is not None
    ]
    return "\n\n".join(blocks)


def today_focus(plans: dict[str, PeriodPlan | None], moment: datetime) -> PlanItem | None:
    """取今天该偏重的那件事（优先看周程，其次月程）。

    Args:
        plans: 各层计划。
        moment: 参考时间。

    Returns:
        命中的推进项，或 ``None``。
    """
    for layer in (PLAN_LAYER_WEEK, PLAN_LAYER_MONTH):
        plan = plans.get(layer)
        if plan is None:
            continue
        item = plan.focus_for_day(moment)
        if item is not None:
            return item
    return None


async def _user_prompt(
    config: DailyScheduleConfig,
    snapshot: PersonaSnapshot,
    profile: Any,
    bundle: sources.SourceBundle,
    plans: dict[str, PeriodPlan | None],
    layer: str,
    *,
    mood_hint: str = "",
    now: datetime,
) -> str:
    """拼出生成某一层规划的用户提示词（异步：要读实际发生过的事实）。"""
    parts: list[tuple[str, str]] = []

    persona_block = bundle.persona_block or snapshot.to_prompt_block()
    if persona_block:
        parts.append(("人设", persona_block))

    profile_block = generator.profile_block(profile)
    if profile_block:
        parts.append(("人设梳理", profile_block))

    memory_block = bundle.memory_block()
    if memory_block:
        parts.append(("关于它的记忆（可能有噪音，只取用得上的）", memory_block))

    upper = upper_block(plans, layer)
    if upper:
        parts.append(("上层规划（这一层要围绕它展开）", upper))
    else:
        parts.append(("上层规划", "（这是最上面一层，围绕人设与所在环境来写）"))

    # 事实优先：把实际发生过的事喂回去，纠正计划里的想当然
    history = await _recent_blocks(config, layer, now)
    if history:
        parts.append(("实际发生过的（计划要跟它对齐，别装作没发生）", history))

    if mood_hint:
        parts.append(("这一套的走向", mood_hint))

    parts.append(
        (
            "时间",
            f"现在 {now:%Y-%m-%d %H:%M}；这一层是"
            f"{PLAN_LAYER_LABELS.get(layer, layer)}"
            f"（{_KIND_HINT.get(layer, '')}）。请按格式输出 JSON。",
        )
    )
    # 预算闸：人设与时间要求不许丢，材料按优先级丢（超限会记一条 WARNING）
    text, notes = budget.fit(
        parts,
        limit=int(getattr(config.budget, "max_prompt_chars", 12000)),
        keep=("人设", "人设梳理", "时间"),
    )
    for note in notes:
        logger.warning(f"[daily_schedule] {layer} 规划提示词{note}")
    return text


async def _recent_blocks(config: DailyScheduleConfig, layer: str, now: datetime) -> str:
    """取最近的实际记录（日程 + 日记），给上层规划当"事实"。"""
    blocks: list[str] = []
    try:
        days = await store.recent_schedules(3 if layer == PLAN_LAYER_WEEK else 7)
        for schedule in days:
            if not schedule.entries:
                continue
            sample = "；".join(entry.doing for entry in schedule.entries[:: max(1, len(schedule.entries) // 4)][:4])
            blocks.append(f"{schedule.date}：{sample}")
    except Exception as error:  # noqa: BLE001 - 事实材料取不到不影响生成
        logger.debug(f"[daily_schedule] 取日程事实失败: {error}")

    try:
        from . import diary  # 延迟导入：diary 不依赖 plan，这里只是顺手用

        for day in await diary.recent_days(3):
            if day.is_empty:
                continue
            blocks.append(day.text_block())
    except Exception as error:  # noqa: BLE001 - 日记取不到不影响生成
        logger.debug(f"[daily_schedule] 取日记事实失败: {error}")

    return "\n\n".join(blocks[:6])


def _system_prompt(layer: str, *, existing: list[str]) -> str:
    """按层取系统提示词，并把"已经编过哪些型"带上。"""
    base = _SYSTEM.get(layer, _SYSTEM[PLAN_LAYER_WEEK])
    if not existing:
        return base
    return (
        base
        + "\n补充：已经编过的同层模板有："
        + "、".join(existing)
        + "。这一套要明显不是同一种日子（换节奏、换重心），但仍要符合这个人设。"
    )


# ── 生成一份规划（direct 模式，也是池子模式下的"编一套型"） ────────────────────


async def build_plan(
    config: DailyScheduleConfig,
    snapshot: PersonaSnapshot,
    profile: Any,
    plans: dict[str, PeriodPlan | None],
    layer: str,
    *,
    bundle: sources.SourceBundle | None = None,
    existing: list[str] | None = None,
    mood_hint: str = "",
    now: datetime,
    request_name: str = "daily_schedule_plan",
) -> tuple[PeriodPlan | None, str, list[str]]:
    """调用模型生成一层规划（一次调用）。

    Args:
        config: 插件配置。
        snapshot: 人设快照。
        profile: 人设判定结果。
        plans: 已有的各层计划（上层材料）。
        layer: 要生成的层。
        bundle: 素材包（缺省时由调用方保证已收集）。
        existing: 已编过的同层模板名（池子模式用，避免雷同）。
        mood_hint: 这一套的走向提示（池子编多套时用来区分忙碌/休息）。
        now: 参考时间。
        request_name: 请求名（写进 LLM 统计）。

    Returns:
        ``(计划, 模型标识, 问题清单)``；失败时计划为 ``None``。
    """
    if bundle is None:
        bundle = await sources.collect(
            config,
            None,
            snapshot.to_prompt_block(),
            getattr(profile, "character_name", "") or snapshot.nickname,
            getattr(profile, "occupation", ""),
            getattr(profile, "source_work", ""),
        )

    result = await llm.call(
        config,
        _system_prompt(layer, existing=existing or []),
        await _user_prompt(
            config,
            snapshot,
            profile,
            bundle,
            plans,
            layer,
            mood_hint=mood_hint,
            now=now,
        ),
        temperature=max(0.2, min(1.0, float(config.model.temperature))),
        max_tokens=max(800, int(config.plan.max_tokens)),
        request_name=request_name,
    )
    if not result.ok:
        return None, result.model_tag, [result.error or "模型调用失败"]

    if config.plugin.debug_log:
        logger.debug(f"[daily_schedule] {layer} 规划原始返回：\n{result.text}")

    payload = llm.extract_json(result.text)
    if payload is None:
        await store.save_raw_failure(
            result.text, model_tag=result.model_tag, error=f"plan:{layer} unparsable"
        )
        return None, result.model_tag, ["无法解析 JSON"]

    plan = PeriodPlan.from_dict({**payload, "layer": layer})
    if plan is None:
        return None, result.model_tag, ["结构不完整"]

    plan.layer = layer
    plan.mode = "direct"
    plan.model_tag = result.model_tag
    plan.persona_fingerprint = snapshot.fingerprint
    plan.source = ",".join(bundle.used) or "persona"
    problems = validate_period_plan(plan, min_items=max(1, int(config.plan.min_items)))
    if problems:
        await store.save_raw_failure(
            result.text, model_tag=result.model_tag, error="plan: " + "；".join(problems[:3])
        )
        return None, result.model_tag, problems
    return plan, result.model_tag, []


# ── 型池：编几套模板，抽一套 + 规则填充 ───────────────────────────────────────


def _wanted_kinds(config: DailyScheduleConfig, layer: str) -> list[str]:
    """池子要编哪几种走向（忙碌 / 休息）。"""
    if layer == PLAN_LAYER_WEEK:
        # 周层的忙碌/休息由月程决定，池子里两种都备着
        return [PLAN_KIND_BUSY, PLAN_KIND_REST]
    if layer == PLAN_LAYER_MONTH:
        return [PLAN_KIND_BUSY, PLAN_KIND_REST]
    # 年层：一年一个走向，忙碌为主，顺带备一份宽松的
    return [PLAN_KIND_BUSY, PLAN_KIND_REST]


async def refresh_pool(
    config: DailyScheduleConfig,
    plugin: Any,
    layer: str,
    *,
    plans: dict[str, PeriodPlan | None] | None = None,
    now: datetime | None = None,
    snapshot: PersonaSnapshot | None = None,
    profile: Any = None,
    force: bool = False,
) -> PlanPool | None:
    """刷新某一层的型池：逐套生成、校验、增量落盘，够了才替换。

    与日程池同一套写法（小步生成 / 机械校验 / 增量落盘 / 整池才换），
    因为上层的写坏代价更大：一套月程型会被用上一个月。

    Args:
        config: 插件配置。
        plugin: 插件实例。
        layer: 要刷的层。
        plans: 已有的各层计划（编型时的上层材料）。
        now: 参考时间。
        snapshot: 人设快照。
        profile: 人设判定结果。
        force: 是否无视现有池子重编。

    Returns:
        达到可用的型池；没编够时返回 ``None``（半成品留着下次接着编）。
    """
    moment = now or datetime.now()
    snapshot = snapshot or read_persona()
    if not snapshot.is_usable:
        logger.warning(f"[daily_schedule] 人设不可用，跳过 {layer} 型池刷新")
        return None

    plans = plans or {}
    salt = str(int(moment.timestamp()) % 100_000) if force else ""
    pool_id = f"{layer}-{moment:%Y-%m-%d}-{snapshot.fingerprint[:8]}" + (f"-{salt}" if salt else "")

    current = await store.load_plan_pool(layer)
    if (
        not force
        and current is not None
        and current.pool_id == pool_id
        and not current.is_expired(moment.timestamp())
    ):
        return current
    if force:
        await store.delete_plan_pool_staging(layer)

    staging = await store.load_plan_pool_staging(layer)
    if staging is None or staging.pool_id != pool_id:
        staging = PlanPool(
            layer=layer,
            pool_id=pool_id,
            created_at=time.time(),
            persona_fingerprint=snapshot.fingerprint,
        )

    profile = profile or await generator.ensure_persona_profile(config, snapshot)
    bundle = await sources.collect(
        config,
        plugin,
        snapshot.to_prompt_block(),
        getattr(profile, "character_name", "") or snapshot.nickname,
        getattr(profile, "occupation", ""),
        getattr(profile, "source_work", ""),
    )
    staging.sources_used = list(bundle.used)

    wanted = _wanted_kinds(config, layer)
    kinds_todo = [
        kind
        for kind in wanted
        if not any(item.kind == kind for item in staging.archetypes)
    ]

    failures: list[str] = []
    model_tag = staging.model_tag
    for kind in kinds_todo:
        existing = [item.label for item in staging.archetypes]
        hint = f"这一套是「{'忙碌' if kind == PLAN_KIND_BUSY else '休息'}」走向：{_KIND_HINT.get(layer, '')}"
        plan: PeriodPlan | None = None
        problems: list[str] = []
        feedback = ""
        attempts = max(1, int(config.plan.retry_times) + 1)
        for attempt in range(attempts):
            plan, tag, problems = await build_plan(
                config,
                snapshot,
                profile,
                plans,
                layer,
                bundle=bundle,
                existing=existing + ([kind] if feedback else []),
                mood_hint=hint + (f"；上次的问题：{feedback}" if feedback else ""),
                now=moment,
                request_name="daily_schedule_plan_pool",
            )
            if tag:
                model_tag = tag
            if plan is not None:
                break
            feedback = "；".join(problems[:3])
            logger.warning(
                f"[daily_schedule] {layer} 型 {kind} 第 {attempt + 1}/{attempts} 次不合格：{feedback}"
            )

        if plan is None:
            failures.append(f"{PLAN_LAYER_LABELS.get(layer, layer)}/{kind}：{feedback or '生成失败'}")
            continue

        plan.kind = kind
        plan.mode = "pool"
        plan.pool_id = pool_id
        staging.archetypes.append(plan)
        staging.model_tag = model_tag
        await store.save_plan_pool_staging(staging)
        logger.info(
            f"[daily_schedule] {PLAN_LAYER_LABELS.get(layer, layer)}型已编好："
            f"{plan.label}（{kind}，{len(plan.items)} 条推进项）"
        )

    staging.failures = failures[:6]
    if not staging.archetypes:
        await store.save_plan_pool_staging(staging)
        logger.warning(
            f"[daily_schedule] {layer} 型池一套都没编出来，保留旧池子，下次再试"
        )
        return None

    staging.refresh_at = moment.timestamp() + max(
        1, int(_cfg_value(config, "refresh_days", layer, 30))
    ) * 86400.0
    staging.created_at = time.time()
    await store.save_plan_pool(staging)
    await store.delete_plan_pool_staging(layer)
    logger.info(
        f"[daily_schedule] {layer} 型池已刷新：{pool_id}"
        f"（{len(staging.archetypes)} 套，有效期至 "
        f"{datetime.fromtimestamp(staging.refresh_at):%Y-%m-%d}）"
    )
    return staging


def _fill_from_upper(plan: PeriodPlan, upper: PeriodPlan | None) -> PeriodPlan:
    """把上层目标规则化填进抽出来的模板（不调模型）。

    模板给的是「这类时期通常怎么过」，上层给的是「这个时期真正要达到什么」。
    填充规则：上层的事项**必须出现**——同名的保留模板里的细节，没出现过的补进去。

    Args:
        plan: 抽出来的模板（会被复制后修改）。
        upper: 上层计划（可空）。

    Returns:
        填充后的计划。
    """
    if upper is None or not upper.items:
        return plan

    existing_texts = {item.text for item in plan.items}
    for item in upper.items:
        if item.text in existing_texts:
            continue
        plan.items.append(
            PlanItem(
                text=item.text,
                why=item.why,
                kind=item.kind,
                focus_days=[],
            )
        )
    plan.items = plan.items[:6]
    if upper.note and not plan.note:
        plan.note = upper.note
    return plan


def compose_from_pool(
    pool: PlanPool,
    *,
    period: str,
    upper: PeriodPlan | None,
    moment: datetime,
    recent_kinds: list[str] | None = None,
) -> PeriodPlan | None:
    """从型池里抽一套模板，填上上层目标，得到这个时期的计划（不调模型）。

    Args:
        pool: 型池。
        period: 时期标识。
        upper: 上层计划。
        moment: 参考时间。
        recent_kinds: 最近用过哪些走向（优先用没怎么用过的）。

    Returns:
        填充后的计划；池子空时返回 ``None``。
    """
    if not pool.archetypes:
        return None

    recent_kinds = recent_kinds or []
    # 上层是休息期就优先抽休息型，反之亦然；同向里优先用最久没用过的
    want_kind = upper.kind if upper is not None else PLAN_KIND_BUSY
    candidates = [item for item in pool.archetypes if item.kind == want_kind] or list(
        pool.archetypes
    )

    def rank(item: PeriodPlan) -> tuple[int, int]:
        position = recent_kinds.index(item.kind) if item.kind in recent_kinds else 10_000
        return (-position, _seed(period, item.label) % 1000)

    chosen = sorted(candidates, key=rank)[0]
    filled = PeriodPlan.from_dict(chosen.to_dict())
    if filled is None:  # pragma: no cover - from_dict 只在结构损坏时返回 None
        return None

    filled = _fill_from_upper(filled, upper)
    filled.layer = pool.layer
    filled.period = period
    filled.mode = "pool"
    filled.pool_id = pool.pool_id
    filled.model_tag = pool.model_tag
    filled.persona_fingerprint = pool.persona_fingerprint
    filled.created_at = time.time()
    filled.expires_at = period_expiry(pool.layer, moment)
    return filled


# ── 链条：缺哪层补哪层 ────────────────────────────────────────────────────────


async def _load_or_build(
    config: DailyScheduleConfig,
    plugin: Any,
    plans: dict[str, PeriodPlan | None],
    layer: str,
    *,
    moment: datetime,
    snapshot: PersonaSnapshot,
    profile: Any,
    force: bool = False,
) -> PeriodPlan | None:
    """取某一层当前的计划；没有或过期就生成（池子抽或直生成）。"""
    period = period_key(layer, moment)
    existing = await store.load_plan(layer, period) if not force else None
    if existing is not None and not existing.is_expired(moment.timestamp()):
        plans[layer] = existing
        return existing

    mode = str(_cfg_value(config, "mode", layer, "pool") or "pool").strip().lower()
    plan: PeriodPlan | None = None

    if mode == "pool":
        pool = await store.load_plan_pool(layer)
        if pool is not None and not pool.is_expired(moment.timestamp()):
            recent = await _recent_kinds(plans, layer)
            plan = compose_from_pool(
                pool,
                period=period,
                upper=plans.get(_upper_of(layer)),
                moment=moment,
                recent_kinds=recent,
            )
        if plan is None:
            # 池子没有/过期/抽不出来：编型池留给下次，这次直生成保底
            logger.info(f"[daily_schedule] {layer} 型池不可用，改为直生成这一期")
    if plan is None:
        bundle = await sources.collect(
            config,
            plugin,
            snapshot.to_prompt_block(),
            getattr(profile, "character_name", "") or snapshot.nickname,
            getattr(profile, "occupation", ""),
            getattr(profile, "source_work", ""),
        )
        built, _, problems = await build_plan(
            config,
            snapshot,
            profile,
            plans,
            layer,
            bundle=bundle,
            now=moment,
        )
        if built is None:
            logger.warning(
                f"[daily_schedule] {PLAN_LAYER_LABELS.get(layer, layer)}生成失败："
                f"{'；'.join(problems[:3]) or '未知原因'}"
            )
            plans[layer] = None
            return None
        plan = built

    plan.layer = layer
    plan.period = period
    plan.expires_at = period_expiry(layer, moment)
    await store.save_plan(plan)
    plans[layer] = plan
    logger.info(
        f"[daily_schedule] 已生成{PLAN_LAYER_LABELS.get(layer, layer)} "
        f"{period}：「{plan.label}」（{plan.kind}，{len(plan.items)} 条推进项，"
        f"模式 {plan.mode}）"
    )
    return plan


def _upper_of(layer: str) -> str:
    """取上一层的名字（年程的上一层是它自己，表示没有）。"""
    order = list(PLAN_LAYERS)
    index = order.index(layer) if layer in order else 0
    return order[index - 1] if index > 0 else layer


async def _recent_kinds(plans: dict[str, PeriodPlan | None], layer: str) -> list[str]:
    """取最近几期用过的走向（避免连着抽同一种）。"""
    kinds: list[str] = []
    for offset in range(1, 4):
        previous = await store.load_previous_plan(layer, offset)
        if previous is not None:
            kinds.append(previous.kind)
    return kinds


async def ensure_chain(
    config: DailyScheduleConfig,
    plugin: Any,
    *,
    now: datetime | None = None,
    force: bool = False,
) -> dict[str, PeriodPlan | None]:
    """确保年程 / 月程 / 周程都在有效期内，缺哪层补哪层。

    这是规划层的唯一入口：由上到下依次取（年 → 月 → 周），
    上层拿不到不影响下层——下层照样生成，只是少了点依据。

    Args:
        config: 插件配置。
        plugin: 插件实例。
        now: 参考时间。
        force: 是否强制重生成三层（``/日程 规划 重生成`` 用）。

    Returns:
        三层计划的字典（可能含 ``None``）。
    """
    plans: dict[str, PeriodPlan | None] = {name: None for name in PLAN_LAYERS}
    if not bool(getattr(config.plan, "enabled", True)):
        return plans

    moment = now or datetime.now()
    snapshot = read_persona()
    if not snapshot.is_usable:
        logger.debug("[daily_schedule] 人设不可用，跳过规划层")
        return plans

    profile = await generator.ensure_persona_profile(config, snapshot)
    for layer in PLAN_LAYERS:
        try:
            await _load_or_build(
                config,
                plugin,
                plans,
                layer,
                moment=moment,
                snapshot=snapshot,
                profile=profile,
                force=force,
            )
        except Exception as error:  # noqa: BLE001 - 某一层失败不该拖垮整条链
            logger.warning(
                f"[daily_schedule] {PLAN_LAYER_LABELS.get(layer, layer)}生成异常: {error}"
            )
            plans[layer] = None
    return plans


async def load_current_chain(now: datetime | None = None) -> dict[str, PeriodPlan | None]:
    """只读地取当前三层计划（不触发任何生成）。

    Args:
        now: 参考时间。

    Returns:
        三层计划的字典。
    """
    moment = now or datetime.now()
    plans: dict[str, PeriodPlan | None] = {}
    for layer in PLAN_LAYERS:
        plans[layer] = await store.load_plan(layer, period_key(layer, moment))
    return plans


# ── 完成度：判定只落在日程层，这里只负责把它接进规划 ─────────────────────────
#
# 主人定的口径：「每一级下一部分完成 50% 以上就算上一级完成，省去了判定完成的 token，
# 只需要判定日程。」所以本模块**不做任何判定**——判定在 :mod:`progress`（日程层，
# 一次掷骰、零模型调用），这里只把结果读出来给规划用：
#
# * 生成新一期规划时，把上一期的完成情况当「实际发生过的事」喂回去；
# * 命令展示时显示各级完成度。


async def completion_snapshot(
    config: DailyScheduleConfig,
    plans: dict[str, PeriodPlan | None],
    *,
    moment: datetime,
) -> dict[str, Any]:
    """取当前三层的完成度（下级 50% 上卷，零模型调用）。

    Args:
        config: 插件配置。
        plans: 三层计划。
        moment: 参考时间。

    Returns:
        ``{层: 完成度}`` 加 ``items``（周程推进项各自的完成度）。
    """
    from . import progress as progress_module

    outcomes = await store.day_outcomes()
    snapshot: dict[str, Any] = {}
    week_cache: dict[str, Any] = {}
    for layer in PLAN_LAYERS:
        plan = plans.get(layer)
        if plan is None:
            snapshot[layer] = {"judged": 0, "ok": 0, "ratio": 0.0, "complete": False, "unit": ""}
            continue
        try:
            snapshot[layer] = progress_module.rollup(
                config, layer, plan.period, outcomes, week_cache=week_cache
            )
        except Exception as error:  # noqa: BLE001 - 完成度算不出来不该影响别的
            logger.debug(f"[daily_schedule] 算 {layer} 完成度失败: {error}")
            snapshot[layer] = {"judged": 0, "ok": 0, "ratio": 0.0, "complete": False, "unit": ""}

    week = plans.get(PLAN_LAYER_WEEK)
    if week is not None:
        try:
            week_days = [
                (moment - timedelta(days=offset)).date().isoformat() for offset in range(0, 7)
            ]
            focus = await store.focus_by_day(week_days)
            snapshot["items"] = progress_module.item_completion(
                week,
                outcomes,
                focus,
                threshold=float(getattr(config.progress, "rollup_threshold", 0.5)),
            )
        except Exception as error:  # noqa: BLE001 - 单项完成度失败不影响整体
            logger.debug(f"[daily_schedule] 算推进项完成度失败: {error}")
            snapshot["items"] = []
    else:
        snapshot["items"] = []
    return snapshot


def completion_line(info: dict[str, Any]) -> str:
    """把一层完成度说成一句话。"""
    judged = int(info.get("judged") or 0)
    if not judged:
        return "还没判定过"
    mark = "已完成" if info.get("complete") else "未完成"
    return f"{mark}（{info.get('ok')}/{judged} {info.get('unit') or ''}）"


def summary_lines(
    plans: dict[str, PeriodPlan | None],
    *,
    pool_lines: dict[str, list[str]] | None = None,
    completion: dict[str, Any] | None = None,
) -> list[str]:
    """把三层规划渲染成命令展示用的文本。

    Args:
        plans: 各层计划。
        pool_lines: 各层型池的附加说明。
        completion: 各级完成度（见 :func:`completion_snapshot`）。

    Returns:
        文本行列表。
    """
    lines: list[str] = []
    for layer in PLAN_LAYERS:
        label = PLAN_LAYER_LABELS.get(layer, layer)
        plan = plans.get(layer)
        if plan is None:
            lines.append(f"  {label}：还没有（下次生成时会补）")
        else:
            head = plan.text_block().splitlines()[0]
            info = (completion or {}).get(layer)
            if info:
                head += f" ｜ {completion_line(info)}"
            lines.append(f"  {label}：{head}")
            for extra in plan.text_block().splitlines()[1:]:
                lines.append(f"  {extra}")
            expire = (
                datetime.fromtimestamp(plan.expires_at).strftime("%m-%d %H:%M")
                if plan.expires_at
                else "-"
            )
            lines.append(f"    （模式 {plan.mode}，到期 {expire}）")
        for extra in (pool_lines or {}).get(layer, []):
            lines.append(f"    {extra}")

    for item in (completion or {}).get("items") or []:
        flag = "✔" if item.get("complete") else "·"
        lines.append(
            f"    {flag} 本周推进：{item.get('text')}（{item.get('ok')}/{item.get('judged')} 天顺）"
        )
    return lines


__all__ = [
    "build_plan",
    "completion_line",
    "completion_snapshot",
    "compose_from_pool",
    "ensure_chain",
    "load_current_chain",
    "plan_block",
    "refresh_pool",
    "summary_lines",
    "today_focus",
    "upper_block",
]
