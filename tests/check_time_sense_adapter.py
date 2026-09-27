"""daily_schedule × time_sense 2.0 适配自检。

用法（用 neo 实例自己的 venv python 跑）::

    $env:NEO_ROOT="F:\\Neo-MoFox-Aemeath"
    & "F:\\Neo-MoFox-Aemeath\\.venv\\Scripts\\python.exe" F:\\mofox插件\\plugin\\repo\\daily_schedule\\tests\\check_time_sense_adapter.py

只验**与 time_sense 的接口面**，不读盘、不调模型、不启动 Bot：

1. 老版本（1.x）的 ``offline_span`` 返回值仍被完整支持——不认识的字段就当作没有，
   文案与从前逐字一致；
2. 2.0 新增的 ``seconds_min`` / ``seconds_max`` / ``uncertainty_seconds`` /
   ``clock_ok`` 会被翻译成「可信区间 / 时钟异常」的说明，写进日记锚点与提示词；
3. ``capabilities()`` 在位时能读出对面版本与能力；不在位（1.x）时安静降级。
"""

from __future__ import annotations

import asyncio
import os
import sys
import types
from datetime import datetime
from pathlib import Path

_ENV_ROOT = os.environ.get("NEO_ROOT", "").strip()
PLUGIN_DIR = Path(__file__).resolve().parent.parent
NEO_ROOT = Path(_ENV_ROOT) if _ENV_ROOT else PLUGIN_DIR.parent.parent

sys.path.insert(0, str(NEO_ROOT))
sys.path.insert(0, str(PLUGIN_DIR.parent))

_failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    """打印一条检查结果。

    Args:
        label: 检查项名称。
        condition: 是否通过。
        detail: 附加说明。
    """
    mark = "PASS" if condition else "FAIL"
    print(f"[{mark}] {label}" + (f"  — {detail}" if detail else ""))
    if not condition:
        _failures.append(label)


def section(title: str) -> None:
    """打印分节标题。

    Args:
        title: 标题。
    """
    print("-" * 72)
    print(title)


async def main() -> int:
    """入口。

    Returns:
        退出码（0 = 全部通过）。
    """
    from daily_schedule import diary as diary_mod
    from daily_schedule import service as service_mod
    from daily_schedule.config import DailyScheduleConfig

    print(f"框架根  : {NEO_ROOT}  存在={NEO_ROOT.exists()}")
    print(f"插件目录: {PLUGIN_DIR}")
    print("-" * 72)

    config = DailyScheduleConfig()

    # ── 1. 老版本 span：必须逐字保持旧文案 ──────────────────────────────────
    section("1. time_sense 1.x 的 offline_span 仍原样工作")

    legacy_span = {
        "has_baseline": True,
        "seconds": 2 * 3600 + 600,
        "days": 0,
        "text": "2 小时 10 分钟",
        "is_long_absence": False,
        "last_seen": datetime(2026, 9, 28, 3, 20).timestamp(),
        "boot_at": datetime(2026, 9, 28, 3, 20).timestamp(),
        "boot_count": 3,
    }
    legacy_precision = diary_mod.describe_span_precision(legacy_span)
    check("1.x：不产生可信度说明", legacy_precision == "", repr(legacy_precision))
    legacy_anchor = diary_mod.describe_span_anchor(legacy_span)
    check(
        "1.x：锚点文案与旧版一致",
        legacy_anchor == "time_sense 结算离线 2 小时 10 分钟（跨 0 个自然日）",
        legacy_anchor,
    )
    check(
        "1.x：空 span 不炸",
        diary_mod.describe_span_precision({}) == ""
        and diary_mod.describe_span_anchor({}).startswith("time_sense 结算离线"),
        diary_mod.describe_span_anchor({}),
    )
    check("1.x：非 dict 入参不炸", diary_mod.describe_span_precision(None) == "")

    # ── 2. 2.0 的区间与时钟健康 ────────────────────────────────────────────
    section("2. time_sense 2.0 的可信区间 / 时钟异常")

    spanned = dict(legacy_span)
    spanned.update(
        {
            "seconds_min": 2 * 3600 + 300,
            "seconds_max": 2 * 3600 + 600,
            "uncertainty_seconds": 300,
            "clock_ok": True,
            "clock_offset_seconds": 0.0,
        }
    )
    precision = diary_mod.describe_span_precision(spanned)
    check(
        "2.0：给出 ± 与区间",
        precision.startswith("（±5 分钟") and "2 小时 5 分钟" in precision and "2 小时 10 分钟" in precision,
        precision,
    )
    anchor = diary_mod.describe_span_anchor(spanned)
    check("2.0：锚点带上可信度", anchor.endswith(precision) and precision in anchor, anchor)

    quiet_span = dict(spanned)
    quiet_span.update(
        {
            "seconds": 3 * 3600,
            "seconds_min": 3 * 3600,
            "seconds_max": 3 * 3600,
            "uncertainty_seconds": 0,
        }
    )
    check(
        "2.0：区间收敛为一个点时不多嘴（< 60s 误差）",
        diary_mod.describe_span_precision(quiet_span) == "",
        repr(diary_mod.describe_span_precision(quiet_span)),
    )

    skewed = dict(spanned)
    skewed.update({"clock_ok": False, "clock_offset_seconds": -3600.0})
    skew_text = diary_mod.describe_span_precision(skewed)
    check(
        "2.0：时钟被改动时直说不可信",
        "系统时钟被改动过" in skew_text and "2 小时 5 分钟" in skew_text,
        skew_text,
    )

    prompt_span = dict(spanned)
    prompt_text = diary_mod._build_prompt(
        config,
        persona_block="",
        from_ts=datetime(2026, 9, 28, 1, 0).timestamp(),
        to_ts=datetime(2026, 9, 28, 3, 20).timestamp(),
        span=prompt_span,
        schedule_lines=[],
        memory_notes=[],
        history="",
        now=datetime(2026, 9, 28, 3, 20),
    )
    check(
        "2.0：日记提示词带上可信度说明",
        "±5 分钟" in prompt_text,
        [line for line in prompt_text.splitlines() if "±" in line][:1],
    )

    skewed_text = diary_mod._build_prompt(
        config,
        persona_block="",
        from_ts=datetime(2026, 9, 28, 1, 0).timestamp(),
        to_ts=datetime(2026, 9, 28, 3, 20).timestamp(),
        span=dict(skewed),
        schedule_lines=[],
        memory_notes=[],
        history="",
        now=datetime(2026, 9, 28, 3, 20),
    )
    check(
        "2.0：时钟异常也会写进提示词",
        "系统时钟被改动过" in skewed_text,
        [line for line in skewed_text.splitlines() if "系统时钟" in line][:1],
    )

    legacy_text = diary_mod._build_prompt(
        config,
        persona_block="",
        from_ts=datetime(2026, 9, 28, 1, 0).timestamp(),
        to_ts=datetime(2026, 9, 28, 3, 20).timestamp(),
        span=dict(legacy_span),
        schedule_lines=[],
        memory_notes=[],
        history="",
        now=datetime(2026, 9, 28, 3, 20),
    )
    check(
        "1.x：提示词里不出现可信度说明（逐字保持旧形态）",
        "±" not in legacy_text and "系统时钟" not in legacy_text,
        [line for line in legacy_text.splitlines() if "中间隔了" in line][:1],
    )

    # ── 3. capabilities() 探测 ────────────────────────────────────────────
    section("3. service.time_sense_info()：按能力认，不按牌子认")

    fake_plugin = types.SimpleNamespace(config=config)
    svc = service_mod.ScheduleService(fake_plugin)

    class _V2Sense:
        """time_sense 2.0 风格的服务替身。"""

        def now_snapshot(self):
            return {"timestamp": datetime(2026, 9, 28, 3, 20).timestamp()}

        async def offline_span(self):
            return spanned

        def capabilities(self):
            return {
                "plugin": "time_sense",
                "version": "2.0.0",
                "storage_namespace": "time_sense",
                "features": {
                    "per_stream_clock": True,
                    "gap_semantics": True,
                    "timeline": True,
                    "events_enabled": True,
                    "clock_health": True,
                },
                "events": {"long_absence": "time_sense:long_absence"},
            }

    class _V1Sense:
        """time_sense 1.x 风格的服务替身（没有 capabilities）。"""

        def now_snapshot(self):
            return {"timestamp": datetime(2026, 9, 28, 3, 20).timestamp()}

        async def offline_span(self):
            return legacy_span

    original = service_mod.service_api.get_service

    service_mod.service_api.get_service = lambda signature: _V2Sense()  # type: ignore[assignment]
    info = svc.time_sense_info()
    check(
        "2.0：识别版本与能力",
        info["available"]
        and info["version"] == "2.0.0"
        and info["gap_precision"]
        and info["features"].get("per_stream_clock") is True
        and info["events"].get("long_absence") == "time_sense:long_absence",
        str(info),
    )

    service_mod.service_api.get_service = lambda signature: _V1Sense()  # type: ignore[assignment]
    info = svc.time_sense_info()
    check(
        "1.x：可用但无能力清单（安静降级）",
        info["available"]
        and info["version"] == ""
        and info["features"] == {}
        and info["gap_precision"] is False,
        str(info),
    )

    def _boom(signature):
        raise RuntimeError("service api down")

    service_mod.service_api.get_service = _boom  # type: ignore[assignment]
    info = svc.time_sense_info()
    check("服务查询异常：按不可用处理", info["available"] is False, str(info))

    service_mod.service_api.get_service = lambda signature: None  # type: ignore[assignment]
    info = svc.time_sense_info()
    check("没有 time_sense：按不可用处理", info["available"] is False, str(info))

    # capabilities 存在但抛异常 / 返回非 dict 时也不能把日程拖崩
    class _BrokenCaps(_V2Sense):
        def capabilities(self):
            raise ValueError("ops")

    class _WeirdCaps(_V2Sense):
        def capabilities(self):
            return "not-a-dict"

    for label, cls in (("capabilities 抛异常", _BrokenCaps), ("capabilities 返回非 dict", _WeirdCaps)):
        service_mod.service_api.get_service = lambda signature, _cls=cls: _cls()  # type: ignore[assignment]
        info = svc.time_sense_info()
        check(f"{label}：降级为无能力清单", info["version"] == "" and info["gap_precision"] is False, str(info))

    service_mod.service_api.get_service = original  # type: ignore[assignment]

    # ── 4. 真机链路（装了 time_sense 才有结果） ──────────────────────────────
    section("4. 真实 time_sense 服务（若已注册则可读到能力清单）")
    if PLUGIN_DIR.parent.joinpath("time_sense").is_dir():
        check("time_sense 插件源码就在同一工作区（便于联动核查）", True, str(PLUGIN_DIR.parent / "time_sense"))
    check(
        "gap_precision 只认 clock_health 特性",
        svc.time_sense_info()["gap_precision"] is False,
        "无服务时按 False 处理",
    )

    print("-" * 72)
    if _failures:
        print(f"结果：{len(_failures)} 项失败 -> {_failures}")
        return 1
    print("结果：全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
