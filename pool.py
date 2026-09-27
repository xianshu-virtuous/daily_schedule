"""daily_schedule 的日程池：编几套「日型」，每天抽一套，每周重编。

为什么要池子
------------
每天让模型重写一遍「今天」，它会把回喂给它的昨天当成模板来换词。实测（同一台 bot 连续三天）：

======================================  ======================================
09-23                                   09-24
======================================  ======================================
在食堂咬烤面包片，翻昨晚没回完的消息     在食堂咬烤面包，把昨天的视频翻出来看
坐矮墙上喝水，鞋带解开又系上             靠矮墙灌水，鞋带又散了
和同学打闹，分享海豹涂鸦                 和同学打打闹闹，举海豹涂鸦问像不像
回宿舍擦药膏，对着躯壳发呆               回宿舍擦药膏，对着躯壳发呆
======================================  ======================================

13 段逐段对应，细节都在重复——**单调不是"每天只生成一次"的锅，而是"把昨天回喂给模型
又要求它接得上"的锅**。池子把这两件事拆开：

- **变化**来自「日型轮换 + 时段变体」的**抽取**，不花模型调用；
- **连续**来自日记与前几天**实际过过的**日程（回喂的是事实，不是让它接的模板）；
- 生成从「每天一次」变成「每周几次」，顺带更省。

三层结构（日型 → 时段 → 变体）
------------------------------
变体必须**属于同一个日型**：只按时段乱抽会出现「上午在图书馆、下午在床上、中间没有
移动过程」的混乱。所以一个日型就是一套自洽的完整骨架，变体只在"同一个场地/主题下的
动作细节"上不同（换动作、换对象、换细节，不许只换措辞）。

写坏率怎么压
------------
池子一次写坏 = 坏一整周，所以这里比单次生成多设了几道闸：

1. **一次只编一个日型**（输出小、截断风险低），失败可单独重试并**把问题反馈**给模型；
2. 每个日型过 :func:`models.validate_archetype`（覆盖 00:00-23:59、首尾相接、条数、
   变体重复、禁词），不合格**不进池**；
3. **增量落盘**到 ``pool-staging``：编好一个存一个，重启能接着编；
4. **只有整池达标才替换** ``pool-current``——刷池失败时旧池子原封不动；
5. 池子不可用/抽取失败 → 按配置**回退到 daily 模式**现生成一份，绝不让今天没日程。
"""

from __future__ import annotations

import hashlib
import random
import time
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import budget, generator, llm, sources, store
from .config import DailyScheduleConfig
from .models import (
    ARCHETYPE_WEEKEND,
    ARCHETYPE_WORKDAY,
    Archetype,
    DailySchedule,
    ScheduleEntry,
    SchedulePool,
    validate_archetype,
    validate_pool,
)
from .persona import PersonaSnapshot, read_persona

logger = get_logger("daily_schedule.pool")

#: 编日型时的系统提示词。``{...}`` 占位符由 :func:`_system_prompt` 填。
_ARCHETYPE_SYSTEM = """\
你在为一个虚拟角色编「日型」——也就是它会反复过的一种日子。你写的是**模板**，不是某一天的流水账。

【防背书】所有文本都是给它**当背景**用的，不是让它照念的台词：用你自己组织的说法写，
不要复述人设材料里的原句，也不要写成一句能被整句原样搬走的套话。

硬性要求：
1. 只输出一个 JSON 对象，不要解释、不要代码围栏、不要多余文字。
2. slots 必须完整覆盖 00:00 到 23:59：第一段 start 为 "00:00"，最后一段 end 为 "23:59"，
   相邻两段首尾相接（前一段的 end 就是后一段的 start），不留空档、不重叠。时间格式固定 "HH:MM"。
   段落数在 {min_entries}-{max_entries} 之间。
3. 每段给 {want_variants} 个变体。**变体之间必须场景一致、动作不同**：同一个时段的地点和大主题不变，
   但具体在做什么要明显不一样（换动作、换对象、换细节），不许只换措辞、调语序或换同义词。
   每个变体的 doing 是「它此刻正在做的事」：第一人称、现在进行时、10-30 字、具体、带一点动作或细节。
4. 每个变体带 busy（0=闲着或休息，1=有点忙但在做，2=很忙顾不上别的）与 hint
   （该时段被搭话时的自然反应倾向，一句话、可留空）。睡觉、发呆一律 busy=0。
5. 这是「{kind_label}」的日型：{kind_hint}
6. 另外给 5-6 条 yield_lines：主人在这个时候找它时，它自己心甘情愿放下手上事情的心情。
   第一人称、每条 8-20 字、口语化、各不相同；绝对不能出现「日程」「让位」「系统」「规则」
   「优先级」这类词，也不要写成「主人来了所以我要让出时间」——写成「是我自己更想陪你」。
7. 不要写成任务清单或时间表标题（不要「午饭」这种短词），也不要写成解释或心理独白。
8. 另外给这一套日子 2-4 个 tags：这一整天最突出的关键词（如「训练」「外出」「上课」「宅着」），
   每条 2-4 字、不要重复。日程抽取时会靠它对齐「这一周该干什么」。
9. 已经编过的日型：{existing}。这一套要**明显不是同一类日子**（换节奏、换场地、换活动重心），
   但必须仍然符合这个人设的身份、习惯与所在环境。

输出格式：
{
  "name": "日型名（4-8 字，例如 训练日 / 闲散日 / 外出日）",
  "mood": "这一整天的一句话基调（会被当成当天的小结留存）",
  "tags": ["训练", "上课"],
  "slots": [
    {"start": "00:00", "end": "06:30", "variants": [
      {"doing": "……", "busy": 0, "hint": ""},
      {"doing": "……", "busy": 0, "hint": ""},
      {"doing": "……", "busy": 0, "hint": ""}
    ]}
  ],
  "yield_lines": ["……", "……", "……", "……", "……"]
}
"""

_KIND_HINTS = {
    ARCHETYPE_WORKDAY: (
        "工作日。有固定要去的地方（上学 / 上班 / 训练 / 看店之类）、有必须完成的事，"
        "节奏偏紧，白天多半在外面"
    ),
    ARCHETYPE_WEEKEND: (
        "休息日。没有必须去的地方，节奏松，活动偏向自己喜欢的事，可以睡懒觉、"
        "可以出门逛，也可以一整天窝着"
    ),
}

_KIND_LABELS = {ARCHETYPE_WORKDAY: "工作日", ARCHETYPE_WEEKEND: "休息日"}


# ── 小工具 ────────────────────────────────────────────────────────────────────


def _seed(*parts: str) -> int:
    """由若干字符串算一个稳定的种子。

    刻意用 ``hashlib`` 而不是内置 ``hash()``：后者带进程随机化，同一个日期
    在不同进程里会抽出不同的组合——"今天她到底在做什么"必须可复现。

    Args:
        *parts: 参与计算的字符串。

    Returns:
        64 位整数种子。
    """
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).digest()
    return int.from_bytes(digest[:8], "big")


def _keywords_of(text: str) -> list[str]:
    """把一句「今天要做什么」拆成几个可匹配的关键词。

    只做最朴素的切分（按标点与常见连接词断开、取长度 2 起的片段），
    目的是把周程的话和日型的 tags 对上；对不上就退回正常抽取，不会出错。

    Args:
        text: 周程里的推进项文本。

    Returns:
        关键词列表。
    """
    cleaned = "".join(
        " " if char in "，。、；：！？（）【】《》 \t" else char for char in str(text or "")
    )
    words: list[str] = []
    for chunk in cleaned.split(" "):
        chunk = chunk.strip()
        if len(chunk) >= 2 and chunk not in words:
            words.append(chunk)
    # 再补上「去/做/练/写/看/学/买/修」这类动词后面的宾语片段（如「出门办事」→「出门」）
    for verb in ("出门", "复习", "考试", "上课", "打工", "实习", "旅行", "搬家", "看病", "训练"):
        if verb in cleaned and verb not in words:
            words.append(verb)
    return words[:6]


def _unique_lines(items: Any, *, limit: int = 6) -> list[str]:
    """把模型给的字符串数组压成去重、限长的心声列表。"""
    if not isinstance(items, list):
        return []
    lines: list[str] = []
    for item in items:
        text = " ".join(str(item).split()).strip()
        if not text or text in lines:
            continue
        lines.append(text[:80])
        if len(lines) >= limit:
            break
    return lines


def pool_id_for(moment: datetime, fingerprint: str, *, salt: str = "") -> str:
    """算池子标识：刷新日期 + 人设指纹前缀（可选再加一段盐）。

    换人设（指纹变）就等于换池子，老池子自然作废；同一天内重启，
    id 不变，所以 ``pool-staging`` 里的半成品能接着编。手动「刷池」会带盐，
    拿到一个新 id —— 那样才是真的重编，而不是接着编上次没编完的那几个。

    Args:
        moment: 刷新开始时间。
        fingerprint: 人设指纹。
        salt: 附加的干扰段（手动重编时用，可空）。

    Returns:
        形如 ``2026-09-28-a1b2c3d4`` 的标识。
    """
    base = f"{moment:%Y-%m-%d}-{(fingerprint or 'nofp')[:8]}"
    return f"{base}-{salt}" if salt else base


def pool_is_usable(
    pool: SchedulePool | None,
    *,
    fingerprint: str,
    now_ts: float,
) -> bool:
    """池子现在能不能用来抽今天的日程。

    Args:
        pool: 池子。
        fingerprint: 当前人设指纹。
        now_ts: 当前时间戳。

    Returns:
        可用返回 True。
    """
    if pool is None or not pool.archetypes:
        return False
    if pool.persona_fingerprint and fingerprint and pool.persona_fingerprint != fingerprint:
        return False
    return not pool.is_expired(now_ts)


# ── 生成一个日型 ──────────────────────────────────────────────────────────────


def _system_prompt(config: DailyScheduleConfig, *, kind: str, existing: list[str]) -> str:
    """按配置与已有日型拼出系统提示词。"""
    pool = config.pool
    return _ARCHETYPE_SYSTEM.format(
        min_entries=max(2, int(config.schedule.min_entries)),
        max_entries=max(2, int(config.schedule.max_entries)),
        want_variants=max(2, min(4, int(pool.variants_per_slot))),
        kind_label=_KIND_LABELS.get(kind, "工作日"),
        kind_hint=_KIND_HINTS.get(kind, _KIND_HINTS[ARCHETYPE_WORKDAY]),
        existing="、".join(existing) if existing else "（还没有，这是第一套）",
    )


def _user_prompt(
    config: DailyScheduleConfig,
    snapshot: PersonaSnapshot,
    profile: Any,
    bundle: sources.SourceBundle,
    *,
    kind: str,
    existing: list[str],
    now: datetime,
    feedback: str = "",
    plan_block: str = "",
) -> str:
    """拼出「编一个日型」的用户提示词。"""
    parts: list[tuple[str, str]] = []

    persona_block = bundle.persona_block or snapshot.to_prompt_block()
    if persona_block:
        parts.append(("人设", persona_block))

    profile_block = generator.profile_block(profile)
    if profile_block:
        parts.append(("人设梳理", profile_block))

    if plan_block:
        parts.append(("它的年程 / 月程 / 周程（这一套日子要贴着它来编）", plan_block))

    memory_block = bundle.memory_block()
    if memory_block:
        parts.append(("关于它的记忆（可能有噪音，只取用得上的）", memory_block))

    internet_block = bundle.internet_block()
    if internet_block:
        parts.append(("联网查到的参考（只取与角色日常相关的信息）", internet_block))

    parts.append(
        (
            "要编的日型",
            f"{_KIND_LABELS.get(kind, '工作日')} 的第 "
            f"{len(existing) + 1} 套（已经编过：{'、'.join(existing) if existing else '无'}）。"
            f"参考日期 {now:%Y-%m-%d}（只用来判断季节与作息氛围，不要写进正文）。",
        )
    )
    if feedback:
        parts.append(("上一次的问题，务必修正", feedback.replace("；", "\n- ")))

    parts.append(
        (
            "格式",
            f"请按格式输出这个日型：{max(2, int(config.schedule.min_entries))}-"
            f"{max(2, int(config.schedule.max_entries))} 段、每段 "
            f"{max(2, min(4, int(config.pool.variants_per_slot)))} 个场景一致的变体，"
            "并带上 5-6 条 yield_lines。",
        )
    )

    # 预算闸：人设和格式要求不许丢，联网/记忆/规划材料按优先级丢
    text, notes = budget.fit(
        parts,
        limit=int(getattr(config.budget, "max_prompt_chars", 12000)),
        keep=("人设", "人设梳理", "要编的日型", "格式"),
    )
    for note in notes:
        logger.warning(f"[daily_schedule] 日型提示词{note}")
    return text


async def build_archetype(
    config: DailyScheduleConfig,
    snapshot: PersonaSnapshot,
    profile: Any,
    bundle: sources.SourceBundle,
    *,
    kind: str,
    existing: list[str],
    now: datetime,
    feedback: str = "",
    plan_block: str = "",
) -> tuple[Archetype | None, str, list[str], list[str]]:
    """编一个日型（一次模型调用 + 机械校验）。

    Args:
        config: 插件配置。
        snapshot: 人设快照。
        profile: 人设判定结果。
        bundle: 素材包（整轮刷池只收集一次，多个日型共用）。
        kind: ``workday`` / ``weekend``。
        existing: 已编好的日型名（让模型避开同类）。
        now: 参考时间。
        feedback: 上一次失败的问题摘要（重试时带上）。
        plan_block: 年 / 月 / 周程文本（周程定义日程该干什么，日型要贴着它编）。

    Returns:
        ``(日型, 模型标识, 问题清单, 心声)``；编不出来时日型为 ``None``。
    """
    result = await llm.call(
        config,
        _system_prompt(config, kind=kind, existing=existing),
        _user_prompt(
            config,
            snapshot,
            profile,
            bundle,
            kind=kind,
            existing=existing,
            now=now,
            feedback=feedback,
            plan_block=plan_block,
        ),
        max_tokens=max(1000, int(config.pool.max_tokens)),
        request_name="daily_schedule_pool",
    )
    if not result.ok:
        return None, result.model_tag, [result.error or "模型调用失败"], []

    if config.plugin.debug_log:
        logger.debug(f"[daily_schedule] 日型原始返回（{kind}）：\n{result.text}")

    payload = llm.extract_json(result.text)
    if payload is None:
        await store.save_raw_failure(
            result.text, model_tag=result.model_tag, error="pool: unparsable json"
        )
        return None, result.model_tag, ["无法解析 JSON"], []

    archetype = Archetype.from_dict({**payload, "kind": kind})
    if archetype is None:
        return None, result.model_tag, ["结构不完整（缺少 slots 或变体）"], []

    problems = validate_archetype(
        archetype,
        min_entries=int(config.schedule.min_entries),
        max_entries=int(config.schedule.max_entries),
        want_variants=int(config.pool.variants_per_slot),
    )
    lines = _unique_lines(payload.get("yield_lines"))
    if problems:
        await store.save_raw_failure(
            result.text, model_tag=result.model_tag, error="pool: " + "；".join(problems[:3])
        )
        return None, result.model_tag, problems, lines

    return archetype, result.model_tag, [], lines


# ── 刷池 ──────────────────────────────────────────────────────────────────────


def _jobs(config: DailyScheduleConfig) -> list[tuple[str, int]]:
    """算出这次要编哪些日型（工作日 N 个 + 休息日 M 个）。"""
    workdays = max(1, min(6, int(config.pool.archetypes)))
    weekends = max(0, min(4, int(config.pool.weekend_archetypes)))
    jobs: list[tuple[str, int]] = [(ARCHETYPE_WORKDAY, index) for index in range(workdays)]
    jobs.extend((ARCHETYPE_WEEKEND, index) for index in range(weekends))
    return jobs


async def refresh_pool(
    config: DailyScheduleConfig,
    plugin: Any,
    *,
    now: datetime | None = None,
    snapshot: PersonaSnapshot | None = None,
    profile: Any = None,
    force: bool = False,
    plan_block: str = "",
) -> SchedulePool | None:
    """刷一遍池子：逐日型生成、校验、增量落盘，达标才换掉当前池。

    这是"写坏率"的主战场：任何一个日型失败都只是少一个日型，**不会**毁掉
    已经在用的池子；只有工作日日型达到 ``pool.min_archetypes`` 才做替换。

    Args:
        config: 插件配置。
        plugin: 插件实例（互联网层调用工具要用）。
        now: 参考时间。
        snapshot: 人设快照（缺省自己读）。
        profile: 人设判定结果（缺省自己判）。
        force: 已有可用池子时是否也重刷（``/日程 刷池`` 用）。
        plan_block: 年 / 月 / 周程文本（让日型贴着本周的走向编）。

    Returns:
        达到可用的池子；这一轮还没编够时返回 ``None``（staging 留着下次接着编）。
    """
    moment = now or datetime.now()
    snapshot = snapshot or read_persona()
    if not snapshot.is_usable:
        logger.warning("[daily_schedule] 人设不可用，跳过刷池")
        return None

    # 手动「刷池」带一段盐：一个崭新的 pool_id，半成品不会被当成"已编好"接着用
    salt = str(int(moment.timestamp()) % 100_000) if force else ""
    pool_id = pool_id_for(moment, snapshot.fingerprint, salt=salt)

    current = await store.load_pool()
    if (
        not force
        and current is not None
        and current.pool_id == pool_id
        and not current.is_expired(moment.timestamp())
    ):
        logger.info("[daily_schedule] 池子还在有效期内，跳过刷池")
        return current

    if force:
        logger.info("[daily_schedule] 手动重编池子，丢弃未完成的半成品")
        await store.delete_pool_staging()

    profile = profile or await generator.ensure_persona_profile(config, snapshot)

    staging = await store.load_pool_staging()
    if staging is None or staging.pool_id != pool_id:
        staging = SchedulePool(
            pool_id=pool_id,
            created_at=time.time(),
            persona_fingerprint=snapshot.fingerprint,
            persona_kind=getattr(profile, "kind", ""),
            persona_name=getattr(profile, "character_name", "") or snapshot.nickname,
        )
    staging.persona_fingerprint = snapshot.fingerprint

    # 素材整轮收集一次，所有日型共用（记忆/联网都只花一次）
    bundle = await sources.collect(
        config,
        plugin,
        snapshot.to_prompt_block(),
        getattr(profile, "character_name", "") or snapshot.nickname,
        getattr(profile, "occupation", ""),
        getattr(profile, "source_work", ""),
    )
    staging.sources_used = list(bundle.used)

    done_keys = {item.key for item in staging.archetypes}
    failures: list[str] = []
    model_tag = staging.model_tag

    for kind, index in _jobs(config):
        key = f"{kind}-{index}"
        if key in done_keys:
            continue

        existing = [item.label for item in staging.archetypes]
        feedback = ""
        archetype: Archetype | None = None
        problems: list[str] = []
        attempts = max(1, int(config.pool.retry_times) + 1)

        for attempt in range(attempts):
            archetype, tag, problems, lines = await build_archetype(
                config,
                snapshot,
                profile,
                bundle,
                kind=kind,
                existing=existing,
                now=moment,
                feedback=feedback,
                plan_block=plan_block,
            )
            if tag:
                model_tag = tag
            if archetype is not None:
                break
            feedback = "；".join(problems[:4])
            logger.warning(
                f"[daily_schedule] 日型 {key} 第 {attempt + 1}/{attempts} 次生成不合格：{feedback}"
            )

        if archetype is None:
            failures.append(f"{_KIND_LABELS.get(kind, kind)} #{index + 1}：{feedback or '生成失败'}")
            continue

        archetype.key = key
        staging.archetypes.append(archetype)
        for line in lines:
            if line not in staging.yield_lines:
                staging.yield_lines.append(line)
        staging.yield_lines = staging.yield_lines[:8]
        staging.model_tag = model_tag
        done_keys.add(key)

        # 编好一个立刻落盘：重启不必从头再来
        await store.save_pool_staging(staging)
        logger.info(
            f"[daily_schedule] 日型已编好：{key} {archetype.label}"
            f"（{len(archetype.slots)} 段，变体 "
            f"{min(len(item.variants) for item in archetype.slots)}-"
            f"{max(len(item.variants) for item in archetype.slots)} 个）"
        )

    staging.failures = failures[:8]

    problems = validate_pool(
        staging,
        min_archetypes=int(config.pool.min_archetypes),
        want_weekend=int(config.pool.weekend_archetypes) > 0,
    )
    if problems:
        await store.save_pool_staging(staging)
        logger.warning(
            "[daily_schedule] 池子这一轮还没编够（"
            + "；".join(problems)
            + "），保留旧池子，下轮接着编"
        )
        return None

    staging.refresh_at = moment.timestamp() + max(1, int(config.pool.refresh_days)) * 86400.0
    staging.created_at = time.time()
    staging.model_tag = model_tag
    await store.save_pool(staging)
    await store.delete_pool_staging()

    logger.info(
        f"[daily_schedule] 池子已刷新：{staging.pool_id}"
        f"（{len(staging.archetypes)} 个日型，{len(staging.yield_lines)} 条心声，"
        f"素材 {','.join(staging.sources_used) or '无'}，模型 {staging.model_tag}，"
        f"有效期至 {datetime.fromtimestamp(staging.refresh_at):%m-%d %H:%M}）"
    )
    return staging


# ── 每天从池子里抽一套 ────────────────────────────────────────────────────────


def pick_archetype(
    pool: SchedulePool,
    *,
    day: str,
    weekend: bool,
    recent_keys: list[str],
    exclude_keys: list[str] | None = None,
) -> Archetype | None:
    """挑一个日型：优先用「最久没用过」的，同久时用日期做确定性打破平局。

    Args:
        pool: 池子。
        day: ``YYYY-MM-DD``。
        weekend: 是否周六周日。
        recent_keys: 最近用过的日型 key（最近的在前）。
        exclude_keys: 本次不要选的 key（``/日程 重生成`` 换一套时用）。

    Returns:
        选中的日型；一个都没有时返回 ``None``。
    """
    candidates = [
        item for item in pool.bucket(weekend=weekend) if item.key not in (exclude_keys or [])
    ]
    if not candidates:
        candidates = list(pool.bucket(weekend=weekend))
    if not candidates:
        return None

    def rank(item: Archetype) -> tuple[int, int]:
        # ``recent_keys[0]`` 是**最近**用过的，所以下标越大 = 越久没用 = 越该先用；
        # 完全没用过的给一个极大的下标，最优先。
        position = recent_keys.index(item.key) if item.key in recent_keys else 10_000
        return (-position, _seed(day, item.key) % 1000)

    return sorted(candidates, key=rank)[0]


def compose_day(
    pool: SchedulePool,
    *,
    moment: datetime,
    recent: list[DailySchedule] | None = None,
    exclude_keys: list[str] | None = None,
    focus_text: str = "",
) -> DailySchedule | None:
    """从池子里抽出一整天的日程（不调用模型）。

    抽取是确定性的：同一个日期 + 同一个日型 + 同一个时段，永远抽到同一个变体
    （种子来自 :func:`_seed`），所以"今天她在做什么"在一整天里前后一致，
    进程重启也不会变。同时会避开昨天同一时段的同一句 doing，让相邻两天不重样。

    ``focus_text`` 是周程里"今天该偏重的那件事"：如果它和某个日型的 ``tags``
    对得上，就优先抽那个日型——这样「周程定义日程该干什么」真的落到日程上了，
    而且是零调用的字符串匹配，不是再打一次模型。

    Args:
        pool: 池子。
        moment: 参考时间。
        recent: 最近几天的日程（最近的在前），用于避开刚用过的日型与句子。
        exclude_keys: 不要选的日型 key。
        focus_text: 今天该偏重的事（周程给的），可为空。

    Returns:
        今天的日程；池子里没有可用日型时返回 ``None``。
    """
    day = moment.date().isoformat()
    week = moment.weekday() >= 5
    recent = recent or []
    recent_keys = [item.archetype for item in recent if item.archetype]

    archetype: Archetype | None = None
    if focus_text:
        keywords = _keywords_of(focus_text)
        bucket = pool.bucket(weekend=week)
        matched = [
            item
            for item in bucket
            if item.key not in (exclude_keys or [])
            and any(keyword in tag for tag in item.tags for keyword in keywords)
        ]
        if matched:
            archetype = sorted(
                matched, key=lambda item: _seed(day, item.key) % 1000
            )[0]
            logger.debug(
                f"[daily_schedule] 周程「{focus_text}」命中日型 {archetype.label}，按它来排今天"
            )

    if archetype is None:
        archetype = pick_archetype(
            pool,
            day=day,
            weekend=week,
            recent_keys=recent_keys,
            exclude_keys=exclude_keys,
        )
    if archetype is None:
        return None

    yesterday_doing: dict[str, str] = {}
    if recent:
        for entry in recent[0].entries:
            yesterday_doing[entry.start] = entry.doing

    entries: list[ScheduleEntry] = []
    for slot in archetype.slots:
        if not slot.variants:
            continue
        index = _seed(day, archetype.key, slot.start) % len(slot.variants)
        variant = slot.pick(index)
        # 和昨天同一时段撞了同一句就顺移一个变体——轮换的意义就在这儿
        if variant is not None and yesterday_doing.get(slot.start) == variant.doing:
            shifted = slot.pick(index + 1)
            variant = shifted or variant
        if variant is None:
            continue
        entries.append(
            ScheduleEntry(
                start=slot.start,
                end=slot.end,
                doing=variant.doing,
                busy=variant.busy,
                hint=variant.hint,
            )
        )

    if not entries:
        return None

    return DailySchedule(
        date=day,
        entries=entries,
        yield_lines=list(pool.yield_lines),
        yesterday_summary=archetype.mood,
        generated_at=time.time(),
        model_tag=f"pool:{pool.pool_id}",
        persona_kind=pool.persona_kind,
        persona_name=pool.persona_name,
        sources_used=["pool"],
        archetype=archetype.key,
        pool_id=pool.pool_id,
        focus=focus_text,
    )


def summary_lines(pool: SchedulePool | None) -> list[str]:
    """把池子渲染成几行（命令展示用）。

    Args:
        pool: 池子。

    Returns:
        文本行列表。
    """
    if pool is None:
        return ["  池子：还没有（第一轮刷池完成后才有）"]
    lines = [
        f"  池子：{pool.pool_id}"
        f"（{len(pool.archetypes)} 个日型，{len(pool.yield_lines)} 条心声）",
        f"  生成：{datetime.fromtimestamp(pool.created_at):%Y-%m-%d %H:%M}"
        f" ｜ 模型：{pool.model_tag or '-'}"
        f" ｜ 素材：{'、'.join(pool.sources_used) or '-'}",
    ]
    if pool.refresh_at:
        lines.append(f"  有效期至：{datetime.fromtimestamp(pool.refresh_at):%Y-%m-%d %H:%M}")
    for item in pool.archetypes:
        variants = [len(slot.variants) for slot in item.slots] or [0]
        lines.append(
            f"    · {item.label}（{_KIND_LABELS.get(item.kind, item.kind)}，"
            f"{len(item.slots)} 段，变体 {min(variants)}-{max(variants)} 个）"
            + (f"：{item.mood}" if item.mood else "")
        )
    if pool.failures:
        lines.append("  最近失败：" + "；".join(pool.failures[:3]))
    return lines


__all__ = [
    "build_archetype",
    "compose_day",
    "pick_archetype",
    "pool_id_for",
    "pool_is_usable",
    "refresh_pool",
    "summary_lines",
]