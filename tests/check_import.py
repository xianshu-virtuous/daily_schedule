# -*- coding: utf-8 -*-
r"""真实导入冒烟：证明「框架能加载这个插件」，而不只是「语法对」。

「语法对」与「框架能加载」是两件事：框架的插件加载器是把入口文件当
``daily_schedule.plugin`` 直接执行的，导入期的一点点循环引用或写错的 API 路径
都会让整个插件加载失败，而 `py_compile` 一点问题都看不出来。所以自检的第一步
永远是真实 import 一遍。

它做四件事：

1. 把框架根与插件父目录插进 sys.path，逐个 import 本插件模块；
2. 检查 manifest.json 合法（根级、必需字段、无 BOM、可选依赖不许写成硬依赖）；
3. 检查 manifest 里声明的组件与入口文件真的存在；
4. 实例化配置模型，钉住几处关键默认值（默认值错了，用户装上是不会有人报错的）。

用法（用实例自带 venv，别用系统 python）：

    $env:NEO_ROOT="<你的 Neo-MoFox 框架根目录>"
    & "<框架根>\.venv\Scripts\python.exe" <插件目录>\tests\check_import.py

不设 `NEO_ROOT` 时会自动从「插件装在 `<框架根>\plugins\<插件>`」这个位置倒推两级。
"""

from __future__ import annotations

import importlib
import json
import os
import sys
from pathlib import Path

#: 控制台可能是 GBK，而脚本结尾会打 ✓ ——直接 print 会抛 UnicodeEncodeError
#: 把自检结果吞掉（断言全过却返回 1）。统一强制 UTF-8。
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
except Exception:  # noqa: BLE001 - 老解释器没有 reconfigure 就算了
    pass

PLUGIN_DIR = Path(__file__).resolve().parents[1]
#: 框架根：优先用环境变量；装进实例时（``<根>/plugins/<插件>``）可以倒推两级。
_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

#: 本插件的全部模块（漏一个就等于漏一处加载期错误）
MODULES = [
    "daily_schedule",
    "daily_schedule.config",
    "daily_schedule.models",
    "daily_schedule.persona",
    "daily_schedule.table",
    "daily_schedule.store",
    "daily_schedule.llm",
    "daily_schedule.sources",
    "daily_schedule.scene",
    "daily_schedule.pool",
    "daily_schedule.plan",
    "daily_schedule.progress",
    "daily_schedule.budget",
    "daily_schedule.diary",
    "daily_schedule.generator",
    "daily_schedule.service",
    "daily_schedule.handlers.owner_presence",
    "daily_schedule.handlers.scene_injector",
    "daily_schedule.commands.schedule_command",
    "daily_schedule.commands.goal_command",
    "daily_schedule.plugin",
]

failures: list[str] = []
checks = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    """记录一条断言结果。"""
    global checks
    checks += 1
    if condition:
        print(f"  OK    {label}")
    else:
        print(f"  FAIL  {label}  {detail}")
        failures.append(label)


def section(title: str) -> None:
    """打印一节的分隔标题。"""
    print()
    print("=" * 66)
    print(title)
    print("=" * 66)


# ── 1. 导入 ────────────────────────────────────────────────────────────────
section("1. 真实导入（框架根 + 插件父目录已入 sys.path）")
print(f"框架根  : {NEO_ROOT}  存在={NEO_ROOT.exists()}")
print(f"插件目录: {PLUGIN_DIR}")
sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

imported: dict[str, object] = {}
for name in MODULES:
    try:
        imported[name] = importlib.import_module(name)
    except Exception as error:  # noqa: BLE001 - 导入期任何异常都要暴露
        check(f"import {name}", False, f"{type(error).__name__}: {error}")

check(f"{len(MODULES)} 个模块全部导入成功", len(imported) == len(MODULES), f"成功 {len(imported)}")

# ── 2. manifest ────────────────────────────────────────────────────────────
section("2. manifest：合法性与依赖声明")

manifest_path = PLUGIN_DIR / "manifest.json"
raw_bytes = manifest_path.read_bytes()
manifest = json.loads(raw_bytes.decode("utf-8"))

check("manifest.json 在归档根级", manifest_path.is_file())
check("manifest 无 BOM", not raw_bytes.startswith(b"\xef\xbb\xbf"), raw_bytes[:3].hex())
for field in ("name", "version", "description", "author", "dependencies", "entry_point"):
    check(f"必需字段 {field}", bool(manifest.get(field)) or field == "dependencies")

check("plugin_id 为 daily_schedule", manifest.get("name") == "daily_schedule")
check(
    "version 形如 X.Y.Z",
    bool(__import__("re").match(r"^\d+\.\d+\.\d+", str(manifest.get("version", "")))),
    str(manifest.get("version")),
)
check(
    "dependencies.plugins 为空（time_sense 是运行时探测的可选依赖）",
    manifest.get("dependencies", {}).get("plugins") == [],
    str(manifest.get("dependencies")),
)
check("dependencies_required 为 false", manifest.get("dependencies_required") is False)

entry = PLUGIN_DIR / str(manifest.get("entry_point", ""))
check("entry_point 文件存在", entry.is_file(), str(entry))

include = manifest.get("include")
check("include 是列表", isinstance(include, list), type(include).__name__)
if isinstance(include, list):
    for item in include:
        if not isinstance(item, dict):
            check("include 条目是对象", False, repr(item))
            continue
        kind = str(item.get("component_type", ""))
        name = str(item.get("component_name", ""))
        expected = {
            "service": PLUGIN_DIR / "service.py",
            "event_handler": PLUGIN_DIR / "handlers" / f"{name}.py",
            "command": PLUGIN_DIR / "commands" / f"{name}_command.py",
        }.get(kind)
        if expected is None:
            check(f"include 组件类型可识别: {kind}", False, kind)
            continue
        check(f"include 声明有对应文件: {kind}/{name}", expected.is_file(), str(expected))

for api in ("command_api", "config_api", "llm_api", "prompt_api", "service_api", "storage_api"):
    check(f"api_version 声明了 {api}", api in (manifest.get("api_version") or {}))

# ── 3. 配置模型 ────────────────────────────────────────────────────────────
section("3. 配置模型：能实例化，且关键默认值正确")

from daily_schedule.config import DailyScheduleConfig  # noqa: E402

config = DailyScheduleConfig()
check("配置可实例化", isinstance(config, DailyScheduleConfig))
check("plugin.enabled 默认开", bool(config.plugin.enabled) is True)
check("schedule.mode 默认 pool", str(config.schedule.mode) == "pool", str(config.schedule.mode))
check("model.max_tokens 默认 4000（思考模型的教训）", int(config.model.max_tokens) == 4000)
check("scene.channel 默认 reminder_first（省一半注入）", str(config.scene.channel) == "reminder_first")
check("scene.yield_stream_scope 默认开（让位不串群）", bool(config.scene.yield_stream_scope) is True)
check("scene.hint_only_when_busy 默认开", bool(config.scene.hint_only_when_busy) is True)
check("offline.inject_enabled 默认开", bool(config.offline.inject_enabled) is True)
check("offline.inject_turns 默认 3（日记限次注入）", int(config.offline.inject_turns) == 3)
check(
    "offline.auto_enable_with_time_sense 默认关（日记默认关闭）",
    bool(config.offline.auto_enable_with_time_sense) is False,
)
check("offline.roll_enabled 默认开", bool(config.offline.roll_enabled) is True)
check("offline.roll_success_rate 默认 0.8", abs(float(config.offline.roll_success_rate) - 0.8) < 1e-6)
check("progress.rollup_threshold 默认 0.5（下级 50% 上卷）", abs(float(config.progress.rollup_threshold) - 0.5) < 1e-6)
check("progress.interrupt_penalty 默认 0.2", abs(float(config.progress.interrupt_penalty) - 0.2) < 1e-6)
check("progress.inject_mood 默认关", bool(config.progress.inject_mood) is False)
check("plan.enabled 默认开", bool(config.plan.enabled) is True)
check("plan.announce_year 默认开（元旦分享年目标）", bool(config.plan.announce_year) is True)
check("budget.max_prompt_chars 默认 12000（单次输入硬上限）", int(config.budget.max_prompt_chars) == 12000)
check("budget.warn_prompt_chars 默认 8000", int(config.budget.warn_prompt_chars) == 8000)
check("budget.fail_backoff 默认开（不许失败就重试）", bool(config.budget.fail_backoff) is True)
check("budget.note_injection 默认开（统计每轮注入量）", bool(config.budget.note_injection) is True)
for layer in ("year", "month", "week"):
    check(f"plan.{layer}_mode 默认 pool", str(getattr(config.plan, f"{layer}_mode")) == "pool")
check("plan.inject_in_chat 默认关（规划不占每轮 token）", bool(config.plan.inject_in_chat) is False)

# ── 4. 结果 ────────────────────────────────────────────────────────────────
section("结果")
print(f"断言 {checks} 项，失败 {len(failures)} 项")
if failures:
    for item in failures:
        print(f"  - {item}")
    print("\n导入冒烟失败：框架很可能加载不了这个插件，先修上面的问题再打包。")
    sys.exit(1)
print("导入冒烟通过 ✓")
