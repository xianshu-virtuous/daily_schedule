# -*- coding: utf-8 -*-
r"""逻辑自检：不依赖框架运行时，把「最容易悄悄出错」的几处逐个钉住。

覆盖（每块都对应一次真实踩过的坑）：

* **场景装配**：让位行与场景行拆得开、让位期间不追加忙碌后缀、闲时不附反应倾向；
* **按流隔离**：本流拿到让位行、别的流只拿到场景行、认不出会话时退化为全局注入；
* **日记注入**：默认限次（每次记完日记只在之后几轮注入）、字符上限、开关三态；
* **注入通道**：`reminder_first` 不再把同一段文本写两遍，reminder 写失败必须退回 extra；
* **日程池**：日型的机械校验八种不合格情形、池子可用性、抽取确定性与 LRU、
  与前一天同一时段不重样、刷池失败不替换旧池子；
* **三层规划**：ISO 周期边界、规划校验、型池抽取 + 上层规则填充、随机评估的收敛与幂等、链条生成；
* **周程 → 日程**：周程点到今天的重点时，按日型标签优先抽取（零额外调用）。

用法（用实例自带 venv，别用系统 python）：

    $env:NEO_ROOT="<你的 Neo-MoFox 框架根目录>"
    & "<框架根>\.venv\Scripts\python.exe" <插件目录>\tests\check_logic.py

不设 `NEO_ROOT` 时会自动从「插件装在 `<框架根>\plugins\<插件>`」这个位置倒推两级。
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time
import types
from datetime import datetime
from pathlib import Path

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
#: 框架根：优先用环境变量；装进实例时（``<根>/plugins/<插件>``）可以倒推两级。
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global checks
    checks += 1
    if condition:
        print(f"  OK    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def section(title: str) -> None:
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


from daily_schedule.config import DailyScheduleConfig  # noqa: E402

print(f"框架根  : {NEO_ROOT}  存在={NEO_ROOT.exists()}")
print(f"插件目录: {PLUGIN_DIR}")

config = DailyScheduleConfig()

# ── 1. 场景装配 ───────────────────────────────────────────────────────────────
section("1. scene：让位行与场景行拆得开")

from daily_schedule import scene, service as service_mod  # noqa: E402
from daily_schedule.models import DailySchedule, RuntimeState, ScheduleEntry  # noqa: E402

NOW = datetime(2026, 9, 28, 14, 30)

schedule = DailySchedule(
    date="2026-09-28",
    entries=[
        ScheduleEntry(start="13:00", end="18:00", doing="在店里修一台旧收音机", busy=2, hint=""),
    ],
    yield_lines=["刀先放着，我更想听你说话。"],
    generated_at=NOW.timestamp(),
)

yielding_state = RuntimeState(
    yield_until=NOW.timestamp() + 1800,
    yield_line="刀先放着，我更想听你说话。",
    yield_master="飞行雪绒",
    yield_stream_id="stream-B",
    yield_doing="在店里修一台旧收音机",
)

busy_line = scene.build_scene_line(config, schedule, RuntimeState(), NOW)
check("非让位：忙碌时追加 busy_suffix", "手上正忙" in busy_line, busy_line)

not_yield = scene.build_scene_line(
    config, schedule, yielding_state, NOW, allow_yield=False, busy_suffix=False
)
check("让位期间：场景行不含让位心声", "刀先放着" not in not_yield, not_yield)
check("让位期间：也不含 busy_suffix", "手上正忙" not in not_yield, not_yield)
check("让位期间：仍然说得出「此刻在做什么」", "旧收音机" in not_yield, not_yield)

old_style = scene.build_scene_line(config, schedule, yielding_state, NOW)
check("旧行为（allow_yield=True）：场景行就是让位行", old_style == "（刀先放着，我更想听你说话。）", old_style)

check("build_yield_line 未让位时返回空", scene.build_yield_line(config, schedule, RuntimeState(), NOW) == "")
check(
    "build_yield_line 让位时返回让位行",
    scene.build_yield_line(config, schedule, yielding_state, NOW) == "（刀先放着，我更想听你说话。）",
)

# ── 2. service：按流切分 ──────────────────────────────────────────────────────
section("2. service.injection_texts：全局段 / 本流让位段")

ScheduleService = service_mod.ScheduleService
fake_plugin = types.SimpleNamespace(config=config)
svc = ScheduleService(fake_plugin)

# 屏蔽会真的去跑任务 / 读盘的依赖
async def _fake_current_time(*args, **kwargs):
    return NOW


async def _fake_diary_injection():
    return ""


async def _fake_load_schedule(day):
    return schedule


async def _fake_load_state():
    return yielding_state


svc.current_time = _fake_current_time  # type: ignore[method-assign]
svc.diary_injection = _fake_diary_injection  # type: ignore[method-assign]
svc.request_generation = lambda **kwargs: False  # type: ignore[method-assign]
service_mod.store.load_schedule = _fake_load_schedule  # type: ignore[assignment]
service_mod.store.load_state = _fake_load_state  # type: ignore[assignment]


def run(coro):
    return asyncio.run(coro)


texts_other = run(svc.injection_texts(stream_id="stream-A"))
check("别的会话：base 不含让位心声", "刀先放着" not in texts_other.base, texts_other.base)
check("别的会话：拿到的是场景行", "旧收音机" in texts_other.base, texts_other.base)
check("别的会话：stream 段为空（不会被注入让位）", texts_other.stream == "", texts_other.stream)
check("别的会话：base 里也没有 busy_suffix", "手上正忙" not in texts_other.base, texts_other.base)

texts_target = run(svc.injection_texts(stream_id="stream-B"))
check("触发会话：stream 段就是让位行", texts_target.stream == "（刀先放着，我更想听你说话。）", texts_target.stream)
check("触发会话：base 仍不含让位行（写全局桶的那份）", "刀先放着" not in texts_target.base, texts_target.base)
check("触发会话：full = base + 让位行", texts_target.full.endswith(texts_target.stream), texts_target.full)
check("触发会话：full 里两者都在", "旧收音机" in texts_target.full and "刀先放着" in texts_target.full)

texts_unknown = run(svc.injection_texts(stream_id=None))
check(
    "拿不到 stream_id：退回旧行为（让位行进 base）",
    "刀先放着" in texts_unknown.base and texts_unknown.stream == "",
    texts_unknown.base,
)

# 旧版状态文件里没有 yield_stream_id：认不出触发流，也该照旧注入，而不是哪都不出现
legacy_state = RuntimeState(
    yield_until=NOW.timestamp() + 1800,
    yield_line="刀先放着，我更想听你说话。",
    yield_master="主人",
    yield_stream_id="",
    yield_doing="在店里修一台旧收音机",
)


async def _fake_load_state_legacy():
    return legacy_state


service_mod.store.load_state = _fake_load_state_legacy  # type: ignore[assignment]
texts_legacy_state = run(svc.injection_texts(stream_id="stream-A"))
check(
    "旧状态没记触发流：退化为全局注入，不静默丢失让位",
    "刀先放着" in texts_legacy_state.base and texts_legacy_state.stream == "",
    texts_legacy_state.base,
)
service_mod.store.load_state = _fake_load_state  # type: ignore[assignment]

old_combined = run(svc.get_injection(stream_id="stream-B"))
check("get_injection 合并形态仍可用", "刀先放着" in old_combined, old_combined)

config.scene.yield_stream_scope = False
texts_legacy = run(svc.injection_texts(stream_id="stream-A"))
check(
    "关掉 yield_stream_scope：退回全局注入让位",
    "刀先放着" in texts_legacy.base and texts_legacy.stream == "",
    texts_legacy.base,
)
config.scene.yield_stream_scope = True

# ── 3. 日记注入截断 ───────────────────────────────────────────────────────────
section("3. diary_injection：注入默认开且有字符闸")

from daily_schedule import diary as diary_mod  # noqa: E402


class _FakeDay:
    def __init__(self, text: str) -> None:
        self._text = text
        self.is_empty = False

    def text_block(self) -> str:
        return self._text


async def _fake_recent_days(limit):
    return [_FakeDay("2026-09-27 日记：1 段离线\n" + "我在补觉。" * 200)]


diary_mod.recent_days = _fake_recent_days  # type: ignore[assignment]
config.offline.enabled = True
config.offline.inject_enabled = True
config.offline.inject_turns = 3
# 注意：上面为了绕开真实读盘把 svc.diary_injection 换成了假实现，
# 这里要测真的那个，所以直接走类方法（实例属性会把它挡住）。
diary_injection = ScheduleService.diary_injection

yielding_state.diary_inject_left = 3
block = run(diary_injection(svc))
check("日记注入开启后有内容", block.startswith("【它不在线的时候"), block[:30])
check("注入一次后计数减一", yielding_state.diary_inject_left == 2, str(yielding_state.diary_inject_left))
check("日记注入按 inject_max_chars 截断", len(block) <= int(config.offline.inject_max_chars) + 1, str(len(block)))
check("截断处有省略号", block.endswith("…"), block[-10:])

yielding_state.diary_inject_left = 1
check("还剩一轮时仍注入", run(diary_injection(svc)).startswith("【它不在线的时候"))
check("用完那一轮后归零", yielding_state.diary_inject_left == 0, str(yielding_state.diary_inject_left))
check("计数归零后不再注入（省 token 的关键）", run(diary_injection(svc)) == "")

yielding_state.diary_inject_left = 0
config.offline.inject_turns = 0
check("inject_turns=0 退回「每轮都注入」", run(diary_injection(svc)).startswith("【它不在线的时候"))
config.offline.inject_turns = 3

yielding_state.diary_inject_left = 3
config.offline.inject_max_chars = 0
block_full = run(diary_injection(svc))
check("inject_max_chars=0 表示不截断", len(block_full) > 600, str(len(block_full)))
config.offline.inject_max_chars = 600

config.offline.enabled = False
check("离线生活关掉后不注入", run(diary_injection(svc)) == "")
config.offline.enabled = True
config.offline.inject_enabled = False
check("单关 inject_enabled 后不注入", run(diary_injection(svc)) == "")
config.offline.inject_enabled = True
yielding_state.diary_inject_left = 3

# ── 3b. 反应倾向只在忙时附加 ───────────────────────────────────────────────────
section("3b. scene：闲时不附加反应倾向（省一行 token）")

hint_schedule = DailySchedule(
    date="2026-09-28",
    entries=[
        ScheduleEntry(start="13:00", end="18:00", doing="在店里修收音机", busy=2, hint="手上占着，回话会慢半拍"),
        ScheduleEntry(start="18:00", end="23:59", doing="在窗边发呆", busy=0, hint="闲着呢，随时可以说"),
    ],
    generated_at=NOW.timestamp(),
)
busy_line = scene.build_scene_line(config, hint_schedule, RuntimeState(), NOW)
check("忙时附加反应倾向", "回话会慢半拍" in busy_line, busy_line)

evening = datetime(2026, 9, 28, 20, 0)
idle_line = scene.build_scene_line(config, hint_schedule, RuntimeState(), evening)
check("闲时默认不附加反应倾向", "随时可以说" not in idle_line, idle_line)
config.scene.hint_only_when_busy = False
idle_old = scene.build_scene_line(config, hint_schedule, RuntimeState(), evening)
check("关掉 hint_only_when_busy 后退回旧行为", "随时可以说" in idle_old, idle_old)
config.scene.hint_only_when_busy = True

# ── 4. 事件处理器：分桶写入 ───────────────────────────────────────────────────
section("4. scene_injector：日程行写全局桶、让位行写流私有桶")

from daily_schedule.handlers.scene_injector import SceneInjectorHandler  # noqa: E402


class _FakePromptApi:
    def __init__(self) -> None:
        self.global_writes: list[tuple[str, str, str]] = []
        self.stream_writes: list[tuple[str, str, str, str]] = []
        self.stream_deletes: list[tuple[str, str, str]] = []

    def add_system_reminder(self, bucket, name, content, insert_type=None, consume=None):
        self.global_writes.append((bucket, name, content))

    def add_stream_reminder(self, stream_id, bucket, name, content, insert_type=None, consume=None):
        self.stream_writes.append((stream_id, bucket, name, content))

    def delete_stream_reminder(self, stream_id, bucket, name):
        self.stream_deletes.append((stream_id, bucket, name))
        return True


import src.app.plugin_system.api as api_pkg  # noqa: E402

fake_api = _FakePromptApi()
api_pkg.prompt_api = fake_api  # type: ignore[attr-defined]

check("_stream_id_of 能读出 stream_id", SceneInjectorHandler._stream_id_of({"stream_id": "s1"}) == "s1")
check("_stream_id_of 空值安全", SceneInjectorHandler._stream_id_of({}) == "")
check("_stream_id_of 非 dict 安全", SceneInjectorHandler._stream_id_of(None) == "")

SceneInjectorHandler._sync_reminders(None, config, service_mod.SceneTexts(base="BASE", stream="YIELD"), "s1")
check("全局桶收到日程行", fake_api.global_writes == [(config.scene.reminder_bucket, config.scene.reminder_name, "BASE")], str(fake_api.global_writes))
check("流私有桶收到让位行", fake_api.stream_writes == [("s1", config.scene.reminder_bucket, config.scene.reminder_name, "YIELD")], str(fake_api.stream_writes))

fake_api.global_writes.clear()
fake_api.stream_writes.clear()
SceneInjectorHandler._sync_reminders(None, config, service_mod.SceneTexts(base="BASE", stream=""), "s1")
check("不再让位时清掉该流的让位行", fake_api.stream_deletes == [("s1", config.scene.reminder_bucket, config.scene.reminder_name)], str(fake_api.stream_deletes))

fake_api.global_writes.clear()
fake_api.stream_writes.clear()
fake_api.stream_deletes.clear()
SceneInjectorHandler._sync_reminders(None, config, service_mod.SceneTexts(base="BASE", stream="YIELD"), "")
check("没有 stream_id 时不写流私有桶", fake_api.stream_writes == [] and fake_api.stream_deletes == [], str(fake_api.stream_writes))

# ── 4b. 注入通道：默认只走一条，不再把同一段文本付两遍 ────────────────────────
section("4b. scene_injector：reminder_first 不再重复写 extra")

from daily_schedule.handlers import scene_injector as injector_mod  # noqa: E402

injector_mod.service_api.get_service = lambda signature: svc  # type: ignore[assignment]

handler_self = types.SimpleNamespace(plugin=fake_plugin)
handler_self._sync_reminders = SceneInjectorHandler._sync_reminders.__get__(handler_self, SceneInjectorHandler)
handler_self._get_service = lambda: svc
handler_self._stream_id_of = SceneInjectorHandler._stream_id_of
handler_self._is_cache_safe_template = SceneInjectorHandler._is_cache_safe_template


def build_params(texts_base: str = "BASE", texts_stream: str = "") -> dict:
    return {
        "name": "default_chatter_user_prompt",
        "values": {"extra": "", "stream_id": "s1"},
    }


async def _fake_injection_texts(*, stream_id=None, now=None):
    return service_mod.SceneTexts(base="BASE", stream="")


svc.injection_texts = _fake_injection_texts  # type: ignore[method-assign]

config.scene.channel = "reminder_first"
fake_api.global_writes.clear()
fake_api.stream_writes.clear()
params = build_params()
run(SceneInjectorHandler.execute(handler_self, "on_prompt_build", params))
check("reminder_first：写了 reminder", len(fake_api.global_writes) == 1, str(fake_api.global_writes))
check("reminder_first：不再往 extra 追加（省一半）", params["values"]["extra"] == "", repr(params["values"]["extra"]))

config.scene.channel = "both"
fake_api.global_writes.clear()
params = build_params()
run(SceneInjectorHandler.execute(handler_self, "on_prompt_build", params))
check("both：两条路都写", len(fake_api.global_writes) == 1 and "BASE" in params["values"]["extra"])

config.scene.channel = "extra_only"
fake_api.global_writes.clear()
params = build_params()
run(SceneInjectorHandler.execute(handler_self, "on_prompt_build", params))
check("extra_only：完全不碰 reminder", fake_api.global_writes == [], str(fake_api.global_writes))
check("extra_only：走 extra", "BASE" in params["values"]["extra"])

# reminder 写失败必须退回 extra，否则这段背景就彻底消失了
class _BrokenPromptApi:
    def add_system_reminder(self, *args, **kwargs):
        raise RuntimeError("store down")

    def add_stream_reminder(self, *args, **kwargs):
        raise RuntimeError("store down")

    def delete_stream_reminder(self, *args, **kwargs):
        raise RuntimeError("store down")


config.scene.channel = "reminder_first"
api_pkg.prompt_api = _BrokenPromptApi()  # type: ignore[attr-defined]
params = build_params()
run(SceneInjectorHandler.execute(handler_self, "on_prompt_build", params))
check("reminder 写失败时退回 extra（不丢注入）", "BASE" in params["values"]["extra"], repr(params["values"]["extra"]))

# ── 5. 日程池：校验 / 抽取 / 写坏率 ───────────────────────────────────────────
section("5. 日程池：机械校验、确定性抽取、刷池失败不毁旧池")

from daily_schedule import pool as pool_mod  # noqa: E402
from daily_schedule.models import (  # noqa: E402
    ARCHETYPE_WEEKEND,
    ARCHETYPE_WORKDAY,
    Archetype,
    PoolSlot,
    SchedulePool,
    ScheduleVariant,
    validate_archetype,
    validate_pool,
)


def slots_of(count: int, *, variants: int = 3, doing_prefix: str = "在做点事") -> list[PoolSlot]:
    """造一套从 00:00 到 23:59 首尾相接的时段（每段 variants 个变体）。"""
    span = 24 * 60 // count
    out: list[PoolSlot] = []
    for index in range(count):
        begin = index * span
        end = 24 * 60 - 1 if index == count - 1 else (index + 1) * span
        start_text = f"{begin // 60:02d}:{begin % 60:02d}"
        end_text = f"{end // 60:02d}:{end % 60:02d}"
        out.append(
            PoolSlot(
                start=start_text,
                end=end_text,
                variants=[
                    ScheduleVariant(doing=f"{doing_prefix}{index}-{n}", busy=n % 3, hint="")
                    for n in range(variants)
                ],
            )
        )
    return out


def make_archetype(key: str, *, kind: str = ARCHETYPE_WORKDAY, count: int = 13, variants: int = 3) -> Archetype:
    return Archetype(
        key=key,
        name=key.upper(),
        kind=kind,
        mood=f"{key} 的基调",
        slots=slots_of(count, variants=variants, doing_prefix=key),
    )


def make_plan(title: str, *, kind: str = "busy", items: list[str] | None = None, layer: str = "week"):
    """造一份规划（测试用）。"""
    from daily_schedule.models import PeriodPlan, PlanItem

    return PeriodPlan(
        layer=layer,
        period=f"test-{title}",
        title=title,
        mood=f"{title} 的基调",
        kind=kind,
        items=[PlanItem(text=text) for text in (items or ["推进一件事"])],
        expires_at=time.time() + 86400,
    )


good = make_archetype("workday-0")
check(
    "合格的日型通过校验",
    validate_archetype(good, min_entries=6, max_entries=14, want_variants=3) == [],
    str(validate_archetype(good, min_entries=6, max_entries=14, want_variants=3)),
)

bad_start = make_archetype("bad-start")
bad_start.slots[0].start = "00:30"
check("首段不从 00:00 开始 → 不合格", any("00:00" in item for item in validate_archetype(bad_start, min_entries=6, max_entries=14, want_variants=3)))

bad_end = make_archetype("bad-end")
bad_end.slots[-1].end = "23:00"
check("末段不到 23:59 → 不合格", any("23:59" in item for item in validate_archetype(bad_end, min_entries=6, max_entries=14, want_variants=3)))

bad_gap = make_archetype("bad-gap")
bad_gap.slots[3].start = "08:00"  # 与上一段的 end 断开
check("中间有空档 → 不合格", any("空档" in item for item in validate_archetype(bad_gap, min_entries=6, max_entries=14, want_variants=3)))

too_few = make_archetype("too-few", count=3)
check("时段太少 → 不合格", any("少于下限" in item for item in validate_archetype(too_few, min_entries=6, max_entries=14, want_variants=3)))

dup = make_archetype("dup")
dup.slots[0].variants[1].doing = dup.slots[0].variants[0].doing
check("同段变体完全重复 → 不合格", any("重复" in item for item in validate_archetype(dup, min_entries=6, max_entries=14, want_variants=3)))

few_var = make_archetype("few-var", variants=1)
check("变体太少 → 不合格", any("变体" in item for item in validate_archetype(few_var, min_entries=6, max_entries=14, want_variants=3)))

forbidden = make_archetype("forbidden")
forbidden.slots[0].variants[0].doing = "在写今天的日程"
check("出现禁词 → 不合格", any("日程" in item for item in validate_archetype(forbidden, min_entries=6, max_entries=14, want_variants=3)))

workdays_only = SchedulePool(pool_id="p1", archetypes=[make_archetype("w0"), make_archetype("w1")])
check("两个工作日日型 → 池子可用", validate_pool(workdays_only, min_archetypes=2, want_weekend=False) == [])
check(
    "要周末日型却没有 → 池子不可用",
    validate_pool(workdays_only, min_archetypes=2, want_weekend=True) != [],
)
check(
    "工作日日型不够 → 池子不可用",
    validate_pool(SchedulePool(pool_id="p2", archetypes=[make_archetype("w0")]), min_archetypes=2, want_weekend=False) != [],
)

pool_ok = SchedulePool(
    pool_id="2026-09-28-abcdef01",
    created_at=time.time(),
    refresh_at=time.time() + 7 * 86400,
    persona_fingerprint="abcdef01",
    persona_kind="original_oc",
    persona_name="测试角色",
    archetypes=[
        make_archetype("workday-0"),
        make_archetype("workday-1"),
        make_archetype("workday-2"),
        make_archetype("weekend-0", kind=ARCHETYPE_WEEKEND),
        make_archetype("weekend-1", kind=ARCHETYPE_WEEKEND),
    ],
    yield_lines=["刀先放着，我更想听你说话。"],
)

check("未过期 + 指纹一致 → 可用", pool_mod.pool_is_usable(pool_ok, fingerprint="abcdef01", now_ts=time.time()))
check("过期 → 不可用", not pool_mod.pool_is_usable(pool_ok, fingerprint="abcdef01", now_ts=time.time() + 8 * 86400))
check("人设变了 → 不可用", not pool_mod.pool_is_usable(pool_ok, fingerprint="ffffffff", now_ts=time.time()))
check("空池 → 不可用", not pool_mod.pool_is_usable(SchedulePool(), fingerprint="abcdef01", now_ts=time.time()))

MONDAY = datetime(2026, 9, 28, 14, 30)   # 周一
SATURDAY = datetime(2026, 10, 3, 14, 30)  # 周六

# 工作日优先用「最久没用过」的：最近用过 workday-0、workday-1 → 该抽 workday-2
picked = pool_mod.pick_archetype(
    pool_ok, day="2026-09-28", weekend=False, recent_keys=["workday-1", "workday-0"]
)
check("优先抽最久没用过的日型", picked is not None and picked.key == "workday-2", getattr(picked, "key", None))
# 全部用过 → 用最久远的那个（列表尾部）
picked_old = pool_mod.pick_archetype(
    pool_ok, day="2026-09-29", weekend=False, recent_keys=["workday-2", "workday-1", "workday-0"]
)
check("全用过时抽最久远的那个", picked_old is not None and picked_old.key == "workday-0", getattr(picked_old, "key", None))
picked_weekend = pool_mod.pick_archetype(pool_ok, day="2026-10-03", weekend=True, recent_keys=[])
check("周末从周末桶里抽", picked_weekend is not None and picked_weekend.kind == ARCHETYPE_WEEKEND, getattr(picked_weekend, "key", None))

day_a = pool_mod.compose_day(pool_ok, moment=MONDAY, recent=[])
day_b = pool_mod.compose_day(pool_ok, moment=MONDAY, recent=[])
check("同一天抽取可复现（不依赖进程随机）", day_a is not None and day_b is not None and [e.doing for e in day_a.entries] == [e.doing for e in day_b.entries])
check("抽取结果覆盖整天", day_a is not None and day_a.entries[0].start == "00:00" and day_a.entries[-1].end == "23:59")
check(
    "抽取结果标了日型与池子",
    day_a is not None
    and day_a.archetype in {"workday-0", "workday-1", "workday-2"}
    and day_a.pool_id == pool_ok.pool_id,
    getattr(day_a, "archetype", None),
)
check(
    "抽取带上了心声与基调",
    day_a is not None
    and day_a.yield_lines == pool_ok.yield_lines
    and day_a.yesterday_summary == f"{day_a.archetype} 的基调",
    getattr(day_a, "yesterday_summary", None),
)

# 同一天再抽一次，若把第一次的结果当成"昨天"，每段都不该撞同一句
day_same_slot = pool_mod.compose_day(pool_ok, moment=MONDAY, recent=[day_a])
check(
    "与前一天同一时段不重样",
    day_same_slot is not None
    and all(a.doing != b.doing for a, b in zip(day_a.entries, day_same_slot.entries)),
)

# 刷池：失败不毁旧池、成功才替换
_mem: dict[str, object] = {"current": pool_ok, "staging": None}


async def _fake_load_pool():
    return _mem["current"]


async def _fake_save_pool(pool):
    _mem["current"] = pool
    return True


async def _fake_load_pool_staging():
    return _mem["staging"]


async def _fake_save_pool_staging(pool):
    _mem["staging"] = pool
    return True


async def _fake_delete_pool_staging():
    _mem["staging"] = None
    return True


async def _fake_collect(*args, **kwargs):
    return pool_mod.sources.SourceBundle(persona_block="人设", used=["persona"])


pool_mod.store.load_pool = _fake_load_pool  # type: ignore[assignment]
pool_mod.store.save_pool = _fake_save_pool  # type: ignore[assignment]
pool_mod.store.load_pool_staging = _fake_load_pool_staging  # type: ignore[assignment]
pool_mod.store.save_pool_staging = _fake_save_pool_staging  # type: ignore[assignment]
pool_mod.store.delete_pool_staging = _fake_delete_pool_staging  # type: ignore[assignment]
pool_mod.sources.collect = _fake_collect  # type: ignore[assignment]

from daily_schedule.persona import PersonaSnapshot  # noqa: E402

fake_snapshot = PersonaSnapshot(nickname="测试角色", identity="学生", fingerprint="newfprint")
fake_profile = types.SimpleNamespace(kind="original_oc", character_name="测试角色", occupation="学生", source_work="")

pool_config = DailyScheduleConfig()
pool_config.pool.archetypes = 2
pool_config.pool.weekend_archetypes = 0
pool_config.pool.retry_times = 0
pool_config.pool.min_archetypes = 2

# 回归：日型系统提示词的正文里带着一份完整的 JSON 输出示例。拼提示词必须用
# 逐字 ``str.replace``，一旦改回 ``str.format``，示例开头的 ``{"name": ...}`` 就会被
# 当成占位符名，抛 ``KeyError('\n  "name"')``——真机表现是刷池 100% 失败，日志里
# 只剩一句「刷池失败: '\n  "name"'」，池子永远编不出来（下面 refresh_pool 的用例
# 把 build_archetype 换成了假的，所以那条路走不到这里，必须单独钉）。
try:
    system_prompt = pool_mod._system_prompt(
        pool_config, kind=ARCHETYPE_WORKDAY, existing=[]
    )
    prompt_error = ""
except Exception as error:  # noqa: BLE001 - 自检里要报 FAIL 而不是崩掉
    system_prompt, prompt_error = "", f"{type(error).__name__}: {error}"

check(
    "日型系统提示词能拼出来（模板里带 JSON 示例也不炸）",
    not prompt_error and "输出格式" in system_prompt and '"name"' in system_prompt,
    prompt_error or system_prompt[:80],
)
check(
    "模板占位符全部被替换，没有残留的 {xxx}",
    not any(
        token in system_prompt
        for token in (
            "{min_entries}",
            "{max_entries}",
            "{want_variants}",
            "{kind_label}",
            "{kind_hint}",
            "{existing}",
        )
    ),
    prompt_error or system_prompt[:120],
)
check(
    "已有日型名会写进「已经编过的日型」那句",
    "训练日、闲散日"
    in pool_mod._system_prompt(
        pool_config, kind=ARCHETYPE_WORKDAY, existing=["训练日", "闲散日"]
    ),
)
check(
    "休息日也能拼（拿得到 weekend 的提示词）",
    "休息日" in pool_mod._system_prompt(
        pool_config, kind=ARCHETYPE_WEEKEND, existing=[]
    ),
)


# 下面这段会把 build_archetype 换成假的（只测池子编排），先留一份真身的引用，
# 给 5b 段的「真实路径」用例用。
_real_build_archetype = pool_mod.build_archetype


async def _failing_build(*args, **kwargs):
    return None, "fake-model", ["时段太少"], []


pool_mod.build_archetype = _failing_build  # type: ignore[assignment]
failed = run(pool_mod.refresh_pool(pool_config, None, now=MONDAY, snapshot=fake_snapshot, profile=fake_profile))
check("全部日型都编不出来 → 不替换旧池子", failed is None and _mem["current"] is pool_ok)

built: list[str] = []


async def _succeeding_build(config, snapshot, profile, bundle, *, kind, existing, now, feedback="", plan_block=""):
    key = f"{kind}-{len(built)}"
    built.append(key)
    return make_archetype(key, kind=kind), "fake-model", [], ["我先把刀放下。"]


pool_mod.build_archetype = _succeeding_build  # type: ignore[assignment]
refreshed = run(pool_mod.refresh_pool(pool_config, None, now=MONDAY, snapshot=fake_snapshot, profile=fake_profile))
check("编够了就替换池子", refreshed is not None and _mem["current"] is refreshed)
check("新池子带上了有效期", refreshed is not None and refreshed.refresh_at > MONDAY.timestamp())
check("新池子带上了心声与指纹", refreshed is not None and refreshed.yield_lines == ["我先把刀放下。"] and refreshed.persona_fingerprint == "newfprint")
check("替换后清掉半成品", _mem["staging"] is None)

# ── 5b. build_archetype 真实路径：提示词里带着 JSON 示例也必须能编出日型 ──────
section("5b. build_archetype 真实路径（只 fake 模型返回，整条链路都得通）")

import json as _json  # noqa: E402

from daily_schedule.llm import LLMCallResult  # noqa: E402
from daily_schedule.models import PersonaProfile  # noqa: E402
from daily_schedule.sources import SourceBundle  # noqa: E402


def _slots_json(count: int = 13, variants: int = 3) -> str:
    """造一份合规的日型 JSON（完整覆盖 00:00-23:59，模拟真实模型的正常输出）。"""
    span = 24 * 60 // count
    slots = []
    for index in range(count):
        begin = index * span
        end = 24 * 60 - 1 if index == count - 1 else (index + 1) * span
        slots.append(
            {
                "start": f"{begin // 60:02d}:{begin % 60:02d}",
                "end": f"{end // 60:02d}:{end % 60:02d}",
                "variants": [
                    {
                        "doing": f"真实路径测试动作{index}-{n}",
                        "busy": n % 3,
                        "hint": "",
                    }
                    for n in range(variants)
                ],
            }
        )
    return _json.dumps(
        {
            "name": "测试日型",
            "mood": "测试基调",
            "tags": ["训练", "外出"],
            "slots": slots,
            "yield_lines": ["我先把刀放下。", "我想先陪你一会儿。"],
        },
        ensure_ascii=False,
    )


# 还原真身：上面为了测池子编排把它换成了假的
pool_mod.build_archetype = _real_build_archetype  # type: ignore[assignment]

_real_pool_llm_call = pool_mod.llm.call
_pool_seen = {"system": "", "user": ""}


async def _fake_pool_llm_call(config, system, user, **kwargs):
    """只拦模型调用，把真实拼出来的提示词留证。"""
    _pool_seen["system"] = system
    _pool_seen["user"] = user
    return LLMCallResult(ok=True, text=_slots_json(), model_tag="fake-model")


pool_mod.llm.call = _fake_pool_llm_call  # type: ignore[assignment]

_real_pool_config = DailyScheduleConfig()
_real_pool_config.schedule.min_entries = 6
_real_pool_config.schedule.max_entries = 14
_real_pool_config.pool.variants_per_slot = 3
# 这里必须用真的 PersonaProfile：上面那个 fake_profile 是 SimpleNamespace，
# 走不到 generator.profile_block（它要 is_roleplay），只能糊弄被 monkeypatch 的路径。
_real_pool_profile = PersonaProfile(character_name="测试角色", occupation="学生")

try:
    _real_archetype, _real_tag, _real_problems, _real_lines = run(
        pool_mod.build_archetype(
            _real_pool_config,
            fake_snapshot,
            _real_pool_profile,
            SourceBundle(persona_block="测试人设", used=["persona"]),
            kind=ARCHETYPE_WORKDAY,
            existing=[],
            now=MONDAY,
        )
    )
    _real_error = ""
except Exception as error:  # noqa: BLE001 - 自检里要报 FAIL 而不是崩掉
    _real_archetype, _real_problems, _real_lines = None, [], []
    _real_error = f"{type(error).__name__}: {error}"

check(
    "真实 build_archetype：能编出日型（提示词拼装不炸；旧写法会抛 KeyError）",
    _real_error == "" and _real_archetype is not None,
    _real_error or str(_real_problems),
)
check(
    "真实 build_archetype：编出的日型通过机械校验",
    _real_error == "" and _real_problems == [],
    _real_error or str(_real_problems),
)
check(
    "真实 build_archetype：模型收到的系统提示词无残留占位符",
    not any(
        token in _pool_seen["system"]
        for token in (
            "{min_entries}",
            "{max_entries}",
            "{want_variants}",
            "{kind_label}",
            "{kind_hint}",
            "{existing}",
        )
    ),
    _pool_seen["system"][:120],
)
check(
    "真实 build_archetype：JSON 输出示例被原样保留在提示词里",
    '"name"' in _pool_seen["system"] and "yield_lines" in _pool_seen["system"],
    _pool_seen["system"][-160:],
)

pool_mod.llm.call = _real_pool_llm_call  # type: ignore[assignment]

# ── 5c. 「结构不完整」也必须留现场（以前的诊断盲区）───────────────────────────
section("5c. 日型结构不完整时留下现场")

from daily_schedule import store as store_mod  # noqa: E402

_saved: list[dict[str, str]] = []


async def _spy_save_raw_failure(text, *, model_tag="", error="", slot=""):
    _saved.append({"text": text, "error": error, "slot": slot})
    return True


_real_save_raw_failure = store_mod.save_raw_failure
store_mod.save_raw_failure = _spy_save_raw_failure  # type: ignore[assignment]

# slots 写成字符串（模型偶尔会这样跑偏）：from_dict 抠不出 slots
_bad_payload = _json.dumps(
    {"name": "休息日型", "mood": "基调", "tags": ["休息"], "slots": "一会儿再说"},
    ensure_ascii=False,
)


async def _fake_bad_llm_call(config, system, user, **kwargs):
    return LLMCallResult(ok=True, text=_bad_payload, model_tag="fake-model")


pool_mod.llm.call = _fake_bad_llm_call  # type: ignore[assignment]

_blank_archetype, _blank_tag, _blank_problems, _blank_lines = run(
    pool_mod.build_archetype(
        _real_pool_config,
        fake_snapshot,
        _real_pool_profile,
        SourceBundle(persona_block="测试人设", used=["persona"]),
        kind=ARCHETYPE_WEEKEND,
        existing=[],
        now=MONDAY,
    )
)

check(
    "slots 不是数组 → 判为结构不完整",
    _blank_archetype is None
    and _blank_problems == ["结构不完整（缺少 slots 或变体）"],
    str(_blank_problems),
)
check(
    "结构不完整时留下了现场（旧代码这里直接 return，日志里只剩一句话）",
    len(_saved) == 1 and _saved[0]["slot"] == "pool",
    str(_saved),
)
check(
    "留档原因里带上顶层键名与 slots 类型（省得下次还要靠猜）",
    bool(_saved)
    and "payload 顶层键=" in _saved[0]["error"]
    and "slots 类型=str" in _saved[0]["error"],
    _saved[0]["error"] if _saved else "（没有留档）",
)

store_mod.save_raw_failure = _real_save_raw_failure  # type: ignore[assignment]

# ── 5d. 输出被截断时也要把日型救回来（旧代码只认 entries，救不回来）──────────
section("5d. 日型被 max_tokens 截断时的补救")

from daily_schedule import llm as llm_mod  # noqa: E402

# 形状照抄真机日志：slots 写到最后一个的中间就断了，yield_lines 还没轮到输出。
# 旧代码里 _recover_truncated 写死 parsed.get("entries")，日型永远配不平，于是
# extract_json 退化成 _iter_balanced_objects 找到的内部 slot 片段 →
# from_dict 抠不到 slots → 报「结构不完整（缺少 slots 或变体）」。
_truncated_archetype = (
    '{"name": "宅家慢活日", "mood": "睡到自然醒。", "tags": ["宅家", "下厨"], "slots": ['
    '{"start": "00:00", "end": "07:00", "variants": ['
    '{"doing": "睡得摊成一片，一只手还搭在床沿外头。", "busy": 0, "hint": ""}]},'
    '{"start": "07:00", "end": "09:00", "variants": ['
    '{"doing": "醒了却不起，躺着看天花板上那道裂纹发呆。", "busy": 0, "hint": ""}]},'
    ' {"doing": "抱着电脑点开论文又合上，最后'
)

_recovered_payload = llm_mod.extract_json(_truncated_archetype)
_recovered_archetype = Archetype.from_dict(
    {**(_recovered_payload or {}), "kind": ARCHETYPE_WEEKEND}
)

check(
    "截断在最后一个 slot 中间 → 救回的是日型对象，不是内部 slot 片段",
    isinstance(_recovered_payload, dict) and "slots" in _recovered_payload,
    str(sorted((_recovered_payload or {}).keys())),
)
check(
    "救回的 slots 保留了截断前的完整段",
    isinstance(_recovered_payload, dict)
    and len(_recovered_payload.get("slots") or []) == 2,
    str(len((_recovered_payload or {}).get("slots") or [])),
)
check(
    "救回的日型能被 from_dict 接住（不再报「结构不完整」）",
    _recovered_archetype is not None,
)
check(
    "但仍被覆盖校验拦下，反馈是「必须到 23:59」这种能指导重试的话",
    _recovered_archetype is not None
    and any(
        "23:59" in item
        for item in validate_archetype(
            _recovered_archetype, min_entries=6, max_entries=14, want_variants=3
        )
    ),
    str(
        validate_archetype(
            _recovered_archetype, min_entries=6, max_entries=14, want_variants=3
        )
        if _recovered_archetype is not None
        else []
    ),
)

# ── 6. 三层规划：周期边界 / 校验 / 型池填充 / 随机评估 / 链条 ──────────────────
section("6. 三层规划：年程 → 月程 → 周程")

from daily_schedule import plan as plan_mod  # noqa: E402
from daily_schedule import progress as progress_mod  # noqa: E402
from daily_schedule.models import (  # noqa: E402
    PLAN_KIND_REST,
    PLAN_LAYER_MONTH,
    PLAN_LAYER_WEEK,
    PLAN_LAYER_YEAR,
    PeriodPlan,
    PlanItem,
    PlanPool,
    period_expiry,
    period_key,
    validate_period_plan,
)

check("周标识按 ISO 周", period_key(PLAN_LAYER_WEEK, datetime(2026, 9, 28)) == "2026-W40", period_key(PLAN_LAYER_WEEK, datetime(2026, 9, 28)))
check("月标识", period_key(PLAN_LAYER_MONTH, datetime(2026, 9, 28)) == "2026-09")
check("年标识", period_key(PLAN_LAYER_YEAR, datetime(2026, 9, 28)) == "2026")
check(
    "周程到期＝下周一零点",
    datetime.fromtimestamp(period_expiry(PLAN_LAYER_WEEK, datetime(2026, 9, 28, 14, 0))).strftime("%Y-%m-%d %H:%M") == "2026-10-05 00:00",
    datetime.fromtimestamp(period_expiry(PLAN_LAYER_WEEK, datetime(2026, 9, 28, 14, 0))).isoformat(),
)
check(
    "月程到期＝下月一号",
    datetime.fromtimestamp(period_expiry(PLAN_LAYER_MONTH, datetime(2026, 12, 20))).strftime("%Y-%m-%d")
    == "2027-01-01",
)
check("年程到期＝元旦", datetime.fromtimestamp(period_expiry(PLAN_LAYER_YEAR, datetime(2026, 5, 5))).strftime("%Y-%m-%d") == "2027-01-01")

week_plan = PeriodPlan(
    layer=PLAN_LAYER_WEEK,
    period="2026-W40",
    title="推进周",
    mood="前半周赶课，后半周松下来",
    kind="busy",
    items=[
        PlanItem(text="把课程论文写完", why="月底要交", kind="work", focus_days=["周三", "周六"]),
        PlanItem(text="去训练场练刀", why="不练会退步", kind="life"),
    ],
    expires_at=period_expiry(PLAN_LAYER_WEEK, datetime(2026, 9, 28)),
)
check("规划通过校验", validate_period_plan(week_plan) == [], str(validate_period_plan(week_plan)))
check("没有 title → 不合格", validate_period_plan(PeriodPlan(layer=PLAN_LAYER_WEEK, period="2026-W41", title="", items=[PlanItem(text="做事")])) != [])
check("没有推进项 → 不合格", validate_period_plan(PeriodPlan(layer=PLAN_LAYER_WEEK, period="2026-W41", title="周", items=[])) != [])
check(
    "推进项出现禁词 → 不合格",
    validate_period_plan(PeriodPlan(layer=PLAN_LAYER_WEEK, period="2026-W41", title="周", items=[PlanItem(text="按日程去做事")])) != [],
)
check(
    "裸字符串推进项也能解析",
    (PlanItem.from_dict("写论文") or PlanItem()).text == "写论文",
)
check(
    "progress 只认白名单",
    PlanItem.from_dict({"text": "x", "progress": "???"}).progress == "",
)

WEDNESDAY = datetime(2026, 9, 30, 14, 0)  # 周三
check("今天命中 focus_days 的推进项", (plan_mod.today_focus({PLAN_LAYER_WEEK: week_plan}, WEDNESDAY) or PlanItem()).text == "把课程论文写完")
check("没标日子时退回第一条", (plan_mod.today_focus({PLAN_LAYER_WEEK: week_plan}, datetime(2026, 9, 29, 10, 0)) or PlanItem()).text == "去训练场练刀")

# 型池：抽一套 + 用上层目标规则化填充（不调模型）
month_plan = PeriodPlan(
    layer=PLAN_LAYER_MONTH,
    period="2026-09",
    title="开学月",
    mood="开学，事情多起来",
    kind="busy",
    items=[PlanItem(text="把选修课的作业补上", why="拖了两周")],
)
week_pool = PlanPool(
    layer=PLAN_LAYER_WEEK,
    pool_id="week-test",
    refresh_at=time.time() + 86400,
    archetypes=[
        make_plan("忙周", kind="busy", items=["赶课", "练刀"]),
        make_plan("闲周", kind=PLAN_KIND_REST, items=["睡到自然醒", "看闲书"]),
    ],
)
filled = plan_mod.compose_from_pool(
    week_pool, period="2026-W40", upper=month_plan, moment=datetime(2026, 9, 28, 8, 0)
)
check("池子抽取填上了上层目标", filled is not None and any("选修课" in item.text for item in filled.items), str([i.text for i in (filled.items if filled else [])]))
check("上层偏忙 → 抽忙碌型", filled is not None and filled.kind == "busy", getattr(filled, "kind", None))
check("填充结果带时期与到期", filled is not None and filled.period == "2026-W40" and filled.expires_at > 0)
check("填充结果标了来源池子", filled is not None and filled.mode == "pool" and filled.pool_id == "week-test")

rest_upper = PeriodPlan(layer=PLAN_LAYER_MONTH, period="2026-07", title="暑假", kind=PLAN_KIND_REST, items=[PlanItem(text="休息")])
rest_filled = plan_mod.compose_from_pool(
    week_pool, period="2026-W27", upper=rest_upper, moment=datetime(2026, 7, 1, 8, 0)
)
check("上层是休息期 → 抽休息型", rest_filled is not None and rest_filled.kind == PLAN_KIND_REST, getattr(rest_filled, "kind", None))

# 随机评估：八成成功、两成失败；只评估未评估过的；结果落盘后不重掷
# 判定只落在日程层：忙碌等级 + 忙时被打断会影响成功率（一天一次掷骰，不调模型）
def busy_day(*levels: int):
    """造一天日程，按时段给定忙碌等级。"""
    from daily_schedule.models import ScheduleEntry

    count = max(1, len(levels))
    span = 24 * 60 // count
    entries = []
    for index, level in enumerate(levels):
        begin = index * span
        end = 24 * 60 - 1 if index == count - 1 else (index + 1) * span
        entries.append(
            ScheduleEntry(
                start=f"{begin // 60:02d}:{begin % 60:02d}",
                end=f"{end // 60:02d}:{end % 60:02d}",
                doing=f"第 {index} 段",
                busy=level,
            )
        )
    return DailySchedule(date="2026-09-29", entries=entries)


idle_schedule = busy_day(0, 0, 0)
busy_schedule = busy_day(0, 2, 1)
check("最忙一档取全天最大值", progress_mod.busy_of(busy_schedule) == 2, str(progress_mod.busy_of(busy_schedule)))

same_roll = random.Random(7)
idle_outcome = progress_mod.judge_day(
    config, day="2026-09-29", schedule=idle_schedule, interrupts=[], rng=same_roll
)
busy_outcome = progress_mod.judge_day(
    config, day="2026-09-29", schedule=busy_schedule, interrupts=[], rng=random.Random(7)
)
check("空闲日有加成 → 成功率高于基准", idle_outcome["rate"] > 0.8, str(idle_outcome["rate"]))
check("忙日没有加成（就等于基准）", abs(busy_outcome["rate"] - 0.8) < 1e-6, str(busy_outcome["rate"]))

interrupted = progress_mod.judge_day(
    config,
    day="2026-09-29",
    schedule=busy_schedule,
    interrupts=[{"busy": 2}, {"busy": 1}],
    rng=random.Random(7),
)
check(
    "在忙的时段被打断会扣成功率（很忙算两档）",
    interrupted["rate"] < busy_outcome["rate"] and interrupted["busy_interrupts"] == 3,
    f"rate={interrupted['rate']} busy_interrupts={interrupted['busy_interrupts']}",
)
idle_interrupt = progress_mod.judge_day(
    config,
    day="2026-09-29",
    schedule=idle_schedule,
    interrupts=[{"busy": 0}, {"busy": 0}],
    rng=random.Random(7),
)
check("空闲时被打断不扣", abs(idle_interrupt["rate"] - idle_outcome["rate"]) < 1e-6, str(idle_interrupt["rate"]))
check(
    "成功率被钳制在合理区间",
    0.0 < progress_mod.judge_day(
        config, day="2026-09-29", schedule=busy_schedule,
        interrupts=[{"busy": 2}] * 20, rng=random.Random(1),
    )["rate"] >= 0.05,
)
check(
    "判定记录写得清来龙去脉",
    all(key in interrupted for key in ("ok", "rate", "roll", "base", "busy_peak", "busy_interrupts")),
    str(sorted(interrupted)),
)
check(
    "判定能说成一句话",
    "成功率" in progress_mod.describe_outcome(interrupted)
    and ("算成了" in progress_mod.describe_outcome(interrupted) or "没算成" in progress_mod.describe_outcome(interrupted)),
    progress_mod.describe_outcome(interrupted),
)

# 500 次采样：基准 0.8、无打断、空闲日加成后，成功比例应落在 0.82~0.88
rng = random.Random(20260928)
ok_count = sum(
    1
    for _ in range(500)
    if progress_mod.judge_day(
        config, day="2026-09-29", schedule=idle_schedule, interrupts=[], rng=rng
    )["ok"]
)
check("空闲日的成功比例贴近 0.85", 0.82 <= ok_count / 500 <= 0.88, f"{ok_count}/500 = {ok_count / 500:.2f}")

rng = random.Random(20260928)
busy_ok = sum(
    1
    for _ in range(500)
    if progress_mod.judge_day(
        config,
        day="2026-09-29",
        schedule=busy_schedule,
        interrupts=[{"busy": 1}],
        rng=rng,
    )["ok"]
)
check("忙时被打断的成功比例明显更低", busy_ok / 500 < 0.7, f"{busy_ok}/500 = {busy_ok / 500:.2f}")

# 完成度：下级 50% 以上上卷（纯计算，零模型调用）
def outcome(ok: bool) -> dict:
    return {"ok": ok, "rate": 0.8, "roll": 0.5, "busy_peak": 1, "interrupts": 0, "busy_interrupts": 0}


week_days = [f"2026-09-{day:02d}" for day in range(28, 31)] + [f"2026-10-{day:02d}" for day in range(1, 5)]
check("周标识从日期推得", plan_mod.period_key("week", datetime(2026, 9, 30)) == "2026-W40")

three_ok = {day: outcome(True) for day in week_days[:4]} | {week_days[4]: outcome(False)}
week_info = progress_mod.rollup(config, PLAN_LAYER_WEEK, "2026-W40", three_ok)
check("4/5 成功 → 这周算完成", week_info["complete"] and week_info["ok"] == 4 and week_info["judged"] == 5, str(week_info))

half = {day: outcome(index % 2 == 0) for index, day in enumerate(week_days[:4])}
half_info = progress_mod.rollup(config, PLAN_LAYER_WEEK, "2026-W40", half)
check("恰好 50% 不算完成（要「50% 以上」）", half_info["complete"] is False, str(half_info))

empty_info = progress_mod.rollup(config, PLAN_LAYER_WEEK, "2026-W40", {})
check("一天都没判定 → 不算完成", empty_info["judged"] == 0 and empty_info["complete"] is False)

# 月 = 该月各周上卷；年 = 该年各月上卷
month_outcomes = {}
for day in range(1, 29):  # 2026-09：前四周全成功
    month_outcomes[f"2026-09-{day:02d}"] = outcome(True)
month_info = progress_mod.rollup(config, PLAN_LAYER_MONTH, "2026-09", month_outcomes)
check("月由各周上卷", month_info["judged"] >= 4 and month_info["complete"], str(month_info))

year_outcomes = {
    f"2026-{month:02d}-{day:02d}": outcome(True)
    for month in range(1, 7)
    for day in range(1, 29)
}
year_info = progress_mod.rollup(config, PLAN_LAYER_YEAR, "2026", year_outcomes)
check("年由各月上卷（上半年满，1-6 月都完成）", year_info["ok"] >= 5 and year_info["complete"], str(year_info))

# 推进项完成度 = 盯过它的那些天里成功日占比 > 50%
item_info = progress_mod.item_completion(
    week_plan,
    {
        "2026-09-30": outcome(True),
        "2026-10-01": outcome(True),
        "2026-10-02": outcome(False),
    },
    {
        "2026-09-30": "把课程论文写完",
        "2026-10-01": "把课程论文写完",
        "2026-10-02": "把课程论文写完",
    },
)
first_item = item_info[0]
check("2/3 成功 → 这条推进项算完成", first_item["complete"] and first_item["ok"] == 2, str(first_item))
check(
    "没人盯过的推进项不算完成",
    item_info[1]["judged"] == 0 and item_info[1]["complete"] is False,
    str(item_info[1]),
)

# 情绪：由最近几天的成功日比例推出
mood_days = {"2026-09-28": outcome(True), "2026-09-29": outcome(True), "2026-09-30": outcome(True)}
level, note = progress_mod.mood_of(config, mood_days, today=datetime(2026, 10, 1).date())
check("连着顺 → good", level == "good", f"{level} {note}")
level_bad, _ = progress_mod.mood_of(
    config,
    {"2026-09-28": outcome(False), "2026-09-29": outcome(False)},
    today=datetime(2026, 9, 30).date(),
)
check("连着不顺 → bad", level_bad == "bad", level_bad)
check("没有判定过的日子 → normal", progress_mod.mood_of(config, {}, today=datetime(2026, 9, 30).date())[0] == "normal")

# 链条：缺哪层补哪层；三层都可生成时用 direct 各调一次；池子可用时不再调模型
_chain_store: dict[tuple[str, str], PeriodPlan] = {}
_pools: dict[str, PlanPool] = {}
_build_calls: list[str] = []


async def _fake_load_plan(layer, period):
    return _chain_store.get((layer, period))


async def _fake_save_plan2(plan):
    _chain_store[(plan.layer, plan.period)] = plan
    return True


async def _fake_load_plan_pool(layer):
    return _pools.get(layer)


async def _fake_load_plan_pool_staging(layer):
    return None


async def _fake_save_plan_pool(pool):
    _pools[pool.layer] = pool
    return True


async def _fake_delete_plan_pool_staging(layer):
    return True


async def _fake_ensure_profile(*args, **kwargs):
    return types.SimpleNamespace(kind="original_oc", character_name="测试角色", occupation="学生", source_work="")


def _make_built(layer):
    async def _fake_build(config, snapshot, profile, plans, layer_arg, **kwargs):
        _build_calls.append(layer_arg)
        return make_plan(f"{layer_arg}计划", kind="busy", items=["要推进的事"], layer=layer_arg), "fake", []

    return _fake_build


plan_mod.store.load_plan = _fake_load_plan  # type: ignore[assignment]
plan_mod.store.save_plan = _fake_save_plan2  # type: ignore[assignment]
plan_mod.store.load_plan_pool = _fake_load_plan_pool  # type: ignore[assignment]
plan_mod.store.load_plan_pool_staging = _fake_load_plan_pool_staging  # type: ignore[assignment]
plan_mod.store.save_plan_pool = _fake_save_plan_pool  # type: ignore[assignment]
plan_mod.store.delete_plan_pool_staging = _fake_delete_plan_pool_staging  # type: ignore[assignment]
plan_mod.generator.ensure_persona_profile = _fake_ensure_profile  # type: ignore[assignment]
plan_mod.read_persona = lambda: PersonaSnapshot(nickname="测试角色", identity="学生", fingerprint="chainfp")  # type: ignore[assignment]
# 真身留一份：第 6b 段要**不经过桩**地验证 build_plan 自己。
# 之前这里直接把它换掉，真实的「抠 JSON → 补字段 → 校验」路径从未被执行，
# 于是「提示词不要求 period、解析却强制要 period」这个必崩 bug 在 207 项
# 全绿的情况下溜了过去。
_real_build_plan = plan_mod.build_plan
plan_mod.build_plan = _make_built("stub")  # type: ignore[assignment]

chain_config = DailyScheduleConfig()
chain_config.plan.year_mode = "direct"
chain_config.plan.month_mode = "direct"
chain_config.plan.week_mode = "direct"
_build_calls.clear()
chain = run(plan_mod.ensure_chain(chain_config, None, now=MONDAY))
check("direct 模式三层各生成一次", _build_calls == ["year", "month", "week"], str(_build_calls))
check("链条三层都拿到了计划", all(chain.get(name) is not None for name in (PLAN_LAYER_YEAR, PLAN_LAYER_MONTH, PLAN_LAYER_WEEK)), str(sorted(k for k, v in chain.items() if v)))
check("计划落盘（可回溯）", len(_chain_store) == 3, str(len(_chain_store)))

_build_calls.clear()
chain_cached = run(plan_mod.ensure_chain(chain_config, None, now=MONDAY))
check("同一天再跑不重复生成", _build_calls == [], str(_build_calls))

chain_config.plan.enabled = False
disabled = run(plan_mod.ensure_chain(chain_config, None, now=MONDAY))
check("关掉规划层后三层都为空", all(item is None for item in disabled.values()))
chain_config.plan.enabled = True

# ── 6b. build_plan 真实路径：时期标识由程序补齐，不依赖模型 ──────────────────
section("6b. build_plan 真实路径（模型不给 period 也必须成功）")

from daily_schedule.llm import LLMCallResult  # noqa: E402
from daily_schedule.models import PersonaProfile  # noqa: E402
from daily_schedule.sources import SourceBundle  # noqa: E402

# 这一份 JSON **刻意不含 period**：三层提示词的输出格式里本来就没有这个字段，
# 真实模型就是这样回的。修好 plan.build_plan 之前，它会被判「结构不完整」。
_NO_PERIOD_JSON = (
    '{"title":"推进周","mood":"前半周赶课，后半周松下来","kind":"busy",'
    '"items":[{"text":"复习高数","why":"期末要考","kind":"work"}],"note":""}'
)
# 模型万一自己编一个 period，也不能让它说了算。
_WRONG_PERIOD_JSON = (
    '{"period":"1999-01","title":"推进周","mood":"前半周赶课","kind":"busy",'
    '"items":[{"text":"复习高数","why":"期末要考","kind":"work"}],"note":""}'
)

_real_llm_call = plan_mod.llm.call
_response_text = {"value": _NO_PERIOD_JSON}


async def _fake_llm_call(config, system, user, **kwargs):
    return LLMCallResult(ok=True, text=_response_text["value"], model_tag="fake-model")


plan_mod.llm.call = _fake_llm_call  # type: ignore[assignment]

_real_bundle = SourceBundle(persona_block="测试人设", used=["persona"])
_real_snapshot = PersonaSnapshot(nickname="测试角色", identity="学生", fingerprint="realfp")
_real_profile = PersonaProfile(character_name="测试角色", occupation="学生")

for _layer, _want in (
    (PLAN_LAYER_YEAR, "2026"),
    (PLAN_LAYER_MONTH, "2026-09"),
    (PLAN_LAYER_WEEK, "2026-W40"),
):
    _built, _tag, _problems = run(
        _real_build_plan(
            chain_config,
            _real_snapshot,
            _real_profile,
            {},
            _layer,
            bundle=_real_bundle,
            now=MONDAY,
        )
    )
    check(
        f"真实 build_plan：{_layer} 模型不给 period 也能成功",
        _built is not None,
        str(_problems),
    )
    check(
        f"真实 build_plan：{_layer} 时期标识补齐 = {_want}",
        getattr(_built, "period", "") == _want,
        repr(getattr(_built, "period", "")),
    )
    check(
        f"真实 build_plan：{_layer} period == period_key()",
        getattr(_built, "period", "") == period_key(_layer, MONDAY),
        repr(getattr(_built, "period", "")),
    )
    check(
        f"真实 build_plan：{_layer} 到期落在自然边界（不再恒为 0）",
        getattr(_built, "expires_at", 0.0) == period_expiry(_layer, MONDAY),
        str(getattr(_built, "expires_at", 0.0)),
    )

_response_text["value"] = _WRONG_PERIOD_JSON
_overridden, _, _ = run(
    _real_build_plan(
        chain_config, _real_snapshot, _real_profile, {}, PLAN_LAYER_WEEK,
        bundle=_real_bundle, now=MONDAY,
    )
)
check(
    "真实 build_plan：模型自编的 period 被程序权威值覆盖",
    getattr(_overridden, "period", "") == "2026-W40",
    repr(getattr(_overridden, "period", "")),
)

plan_mod.llm.call = _real_llm_call  # type: ignore[assignment]

# ── 7. 周程 → 日程：命中标签优先 ─────────────────────────────────────────────
section("7. 周程指导日程：标签匹配优先，零额外调用")

tagged_pool = SchedulePool(
    pool_id="2026-09-30-tagtest",
    created_at=time.time(),
    refresh_at=time.time() + 7 * 86400,
    persona_fingerprint="abcdef01",
    archetypes=[
        make_archetype("workday-0"),  # 无 tags
        Archetype(
            key="workday-out",
            name="外出日",
            kind=ARCHETYPE_WORKDAY,
            mood="在外面跑一天",
            tags=["外出", "办事"],
            slots=slots_of(13, variants=3, doing_prefix="out"),
        ),
    ],
)
plain = pool_mod.compose_day(tagged_pool, moment=MONDAY, recent=[])
focused = pool_mod.compose_day(tagged_pool, moment=MONDAY, recent=[], focus_text="出门办事，把手续办了")
check("没有周程指引时按正常逻辑抽", plain is not None and plain.archetype in {"workday-0", "workday-out"}, getattr(plain, "archetype", None))
check("周程说「外面办事」→ 抽中外向日型", focused is not None and focused.archetype == "workday-out", getattr(focused, "archetype", None))
check("抽取仍然不调模型（只做字符串匹配）", focused is not None and focused.pool_id == tagged_pool.pool_id)

# ── 8. 用量闸门：预算截断 / 统计有界 / 退避 / 注入落点 ────────────────────────
section("8. 用量闸门：提示词预算、失败退避、注入落点安全")

from daily_schedule import budget as budget_mod  # noqa: E402

sections = [
    ("人设", "人" * 200),
    ("人设梳理", "梳" * 200),
    ("关于它的记忆", "记" * 400),
    ("联网查到的参考", "网" * 400),
]
fitted, notes = budget_mod.fit(sections, limit=1000, keep=("人设", "人设梳理"))
check("预算超限会丢可牺牲的材料", "联网查到的参考" not in fitted and notes, str(notes))
check("被保住的人设还在", "人" * 50 in fitted and "梳" * 50 in fitted)
check("截断会留下说明（写日志用）", all("丢弃" in note or "截断" in note for note in notes), str(notes))

small, small_notes = budget_mod.fit(sections, limit=100000, keep=("人设",))
check("没超限就一个字都不动", small_notes == [] and "联网查到的参考" in small)
check("拼装带上段名", "【人设】" in small and "【联网查到的参考】" in small)

hard, hard_notes = budget_mod.fit([("人设", "人" * 5000)], limit=800, keep=("人设",))
check("只剩不可丢的段、又超限时硬截断", "已按预算截断" in hard, str(hard_notes))

check("退避曲线 60→120→240，封顶 1800", [budget_mod.backoff_seconds(n) for n in (1, 2, 3)] == [60, 120, 240])
check("退避有封顶", budget_mod.backoff_seconds(20) == 1800, str(budget_mod.backoff_seconds(20)))
check("没失败就不退避", budget_mod.backoff_seconds(0) == 0 and budget_mod.in_backoff(0, 0.0) == 0.0)
check("刚失败时处在退避窗口里", budget_mod.in_backoff(1, time.time()) > 0)
check("退避窗口过去后可以重试", budget_mod.in_backoff(1, time.time() - 3600) == 0.0)

# 落点安全：只有 user prompt 模板能注入（注进 system 会顶掉前缀缓存）
check(
    "user prompt 模板允许注入",
    SceneInjectorHandler._is_cache_safe_template("default_chatter_user_prompt")
    and SceneInjectorHandler._is_cache_safe_template("neo_default_chatter_user_prompt"),
)
check(
    "system prompt 模板被拒绝",
    not SceneInjectorHandler._is_cache_safe_template("default_chatter_system_prompt")
    and not SceneInjectorHandler._is_cache_safe_template("system_prompt"),
)
check("空模板名被拒绝", not SceneInjectorHandler._is_cache_safe_template(""))

# 注入量统计：进程内计数（用来证明不随对话变长）
budget_mod._inject_counters.update({"turns": 0, "chars": 0, "max_chars": 0, "since_flush": 0})
for size in (100, 300, 200):
    budget_mod.note_injection(size)
snapshot = budget_mod.injection_snapshot()
check(
    "注入计数记轮数/合计/最大/平均",
    snapshot["turns"] == 3 and snapshot["chars"] == 600 and snapshot["max_chars"] == 300 and snapshot["avg_chars"] == 200,
    str(snapshot),
)

# ── 8b. 日程覆盖校验：半截日程不许冒充一整天 ─────────────────────────────────
section("8b. 日程覆盖校验（截断的半截日程不算一天）")

from daily_schedule.generator import _covers_full_day  # noqa: E402


def _spans(*pairs):
    return [ScheduleEntry(start=s, end=e, doing="在做一件事") for s, e in pairs]


check(
    "完整一天（00:00 → 23:59）→ 通过",
    _covers_full_day(_spans(("00:00", "12:00"), ("12:00", "23:59"))),
)
check(
    "末段只到 17:20 → 拒绝（截断后常见的半截形状）",
    not _covers_full_day(_spans(("00:00", "12:00"), ("12:00", "17:20"))),
)
check(
    "只到 13:30 的 6 条 → 拒绝（段数够多，但没排完全天）",
    not _covers_full_day(
        _spans(
            ("00:00", "01:20"),
            ("01:20", "07:20"),
            ("07:20", "08:30"),
            ("08:30", "10:00"),
            ("10:00", "12:10"),
            ("12:10", "13:30"),
        )
    ),
)
check("首段不从 00:00 → 拒绝", not _covers_full_day(_spans(("06:00", "23:59"))))
check("空列表 → 拒绝", not _covers_full_day([]))
check("末段写 24:00 也认", _covers_full_day(_spans(("00:00", "24:00"))))

# ── 9. 汇总 ──────────────────────────────────────────────────────────────────
section("结果")
print(f"断言 {checks} 项，失败 {len(failures)} 项")
if failures:
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("全部通过")
