"""daily_schedule 的用量闸门：提示词预算、调用统计、失败退避。

为什么要有这个模块
------------------
有个真实的事故形状值得防：**每天两次十几万 token 的「无缓存输入」**——一次性的巨大请求
（前缀里没有可复用的东西），加上它把主对话流的前缀缓存一起顶掉，于是「API 缓存」和
「程序侧缓存」一起炸。本模块就是把这个形状在三个位置上按住：

1. **预算**（:func:`fit`）：本插件每一次模型调用都是**自带材料的小请求**，材料有上限；
   拼出来超过 ``budget.max_prompt_chars`` 就按优先级丢材料（先丢联网、再丢记忆、最后丢历史），
   并在超过 ``warn_prompt_chars`` 时告警。**任何一次调用的输入都不许随对话变长**。
2. **统计**（:func:`record_call` / :func:`summary_lines`）：每次调用记一笔
   「用途 / 输入字符 / 输出字符 / 耗时」，有界落盘；``/日程 用量`` 直接看。
   会不会爆不该靠猜，应该看得见。
3. **退避**（:func:`backoff_seconds`）：调用失败后按 60→120→240…→1800 秒退避，
   不许「每 tick 重试一次」把 token 打爆（这是另一个真实事故：679 次失败全是重试烧出来的）。

注意这里的单位是**字符**而不是 token：中文字符 ≈ 1 token 量级，用字符当上界足够保守，
而且不需要引入分词器依赖。
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

from . import store
from .config import DailyScheduleConfig

logger = get_logger("daily_schedule.budget")

#: 用量统计的存储键。
STATS_KEY = "llm-stats"

#: 统计里最多保留最近多少笔调用明细。
_STATS_KEEP = 50

#: 进程内累计的注入量：攒够这么多轮就落盘一次（每轮都写盘是没必要的 IO）。
_FLUSH_EVERY_TURNS = 20


def _chars(text: str) -> int:
    """字符数（当作 token 的保守上界）。"""
    return len(str(text or ""))


# ── 1. 预算：把材料拼成不超限的提示词 ────────────────────────────────────────


def fit(
    sections: list[tuple[str, str]],
    *,
    limit: int,
    keep: tuple[str, ...] = (),
) -> tuple[str, list[str]]:
    """把若干段材料拼成提示词；超限时按顺序丢可牺牲的段。

    ``sections`` 里靠前的段越重要：``keep`` 列出的段永不丢弃（人设、人设梳理之类），
    其余的从**最后一段往前**丢；都丢光了还超，就在末尾硬截断并标出来。

    Args:
        sections: ``[(段名, 文本)]``，靠前＝越重要。
        limit: 字符上限（<=0 表示不限制）。
        keep: 必须完整的段名。

    Returns:
        ``(提示词, 说明列表)``；说明列表里是「丢了谁 / 截断到多少」，写进日志用。
    """
    notes: list[str] = []
    items = [(name, str(text or "")) for name, text in sections if str(text or "").strip()]
    if limit and limit > 0:
        total = sum(_chars(text) for _, text in items) + 2 * max(0, len(items) - 1)
        if total > limit:
            # 先丢可牺牲的段：从最后一段往前（联网 → 记忆 → 历史 → …）
            while total > limit:
                victim_index: int | None = None
                for index in range(len(items) - 1, -1, -1):
                    if items[index][0] in keep:
                        continue
                    victim_index = index
                    break
                if victim_index is None:
                    break
                name, text = items.pop(victim_index)
                notes.append(f"预算超限，丢弃材料「{name}」（{_chars(text)} 字符）")
                total = sum(_chars(body) for _, body in items) + 2 * max(0, len(items) - 1)

            if total > limit:
                # 丢到只剩 keep 还超：硬截断最后一段
                name, text = items[-1]
                allowed = max(200, limit - sum(_chars(t) for n, t in items[:-1]) - 2 * (len(items) - 1))
                items[-1] = (name, text[:allowed].rstrip() + "…（已按预算截断）")
                notes.append(f"预算超限，把「{name}」截到 {allowed} 字符")

    text = "\n\n".join(f"【{name}】\n{body}" if name else body for name, body in items)
    return text, notes


def check_prompt(
    config: DailyScheduleConfig,
    *,
    request_name: str,
    prompt: str,
) -> None:
    """对拼好的提示词做一次体检：超告警线就出声，超硬上限就说明被截了。

    调用方应当已经用 :func:`fit` 保证不超硬上限；这里是第二道保险，
    免得哪个调用点忘了走 fit——那正是"悄悄长起来"的典型路径。

    Args:
        config: 插件配置。
        request_name: 用途（写进日志）。
        prompt: 拼好的提示词。
    """
    size = _chars(prompt)
    warn_at = int(getattr(config.budget, "warn_prompt_chars", 8000) or 0)
    hard = int(getattr(config.budget, "max_prompt_chars", 12000) or 0)
    if hard and size > hard:
        logger.warning(
            f"[daily_schedule] {request_name} 提示词 {size} 字符，超过硬上限 {hard}"
            "（调用点漏了 budget.fit？）"
        )
    elif warn_at and size > warn_at:
        logger.warning(f"[daily_schedule] {request_name} 提示词偏大：{size} 字符（告警线 {warn_at}）")


# ── 2. 用量统计 ───────────────────────────────────────────────────────────────


async def record_call(
    *,
    request_name: str,
    prompt_chars: int,
    output_chars: int,
    elapsed_ms: int,
    ok: bool,
) -> None:
    """记一笔模型调用（有界落盘）。

    Args:
        request_name: 用途（``daily_schedule_pool`` 之类）。
        prompt_chars: 输入字符数。
        output_chars: 输出字符数。
        elapsed_ms: 耗时（毫秒）。
        ok: 是否成功。
    """
    try:
        payload = await store.load_json_raw(STATS_KEY) or {}
    except Exception:  # noqa: BLE001 - 统计失败绝不能影响生成
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    today = datetime.now().strftime("%Y-%m-%d")
    day = payload.get("day")
    if not isinstance(day, dict) or day.get("date") != today:
        day = {"date": today, "calls": 0, "prompt_chars": 0, "output_chars": 0, "max_prompt_chars": 0, "failed": 0}
    day["calls"] = int(day.get("calls") or 0) + 1
    day["prompt_chars"] = int(day.get("prompt_chars") or 0) + max(0, prompt_chars)
    day["output_chars"] = int(day.get("output_chars") or 0) + max(0, output_chars)
    day["max_prompt_chars"] = max(int(day.get("max_prompt_chars") or 0), max(0, prompt_chars))
    if not ok:
        day["failed"] = int(day.get("failed") or 0) + 1

    by_name = payload.get("by_name")
    if not isinstance(by_name, dict):
        by_name = {}
    entry = by_name.get(request_name)
    if not isinstance(entry, dict):
        entry = {"calls": 0, "prompt_chars": 0}
    entry["calls"] = int(entry.get("calls") or 0) + 1
    entry["prompt_chars"] = int(entry.get("prompt_chars") or 0) + max(0, prompt_chars)
    by_name[request_name] = entry

    recent = payload.get("recent")
    if not isinstance(recent, list):
        recent = []
    recent.append(
        {
            "at": time.strftime("%m-%d %H:%M"),
            "name": request_name,
            "in": max(0, prompt_chars),
            "out": max(0, output_chars),
            "ms": max(0, elapsed_ms),
            "ok": bool(ok),
        }
    )

    payload["day"] = day
    payload["by_name"] = by_name
    payload["recent"] = recent[-_STATS_KEEP:]
    payload["updated_at"] = time.time()

    try:
        await store.save_json_raw(STATS_KEY, payload)
    except Exception as error:  # noqa: BLE001 - 同上
        logger.debug(f"[daily_schedule] 记录用量失败: {error}")


#: 进程内累计的注入量（每轮 prompt 构建都会 +1，攒够 _FLUSH_EVERY_TURNS 落盘一次）。
_inject_counters: dict[str, int] = {"turns": 0, "chars": 0, "max_chars": 0, "since_flush": 0}


def note_injection(chars: int) -> None:
    """记一次注入（进程内计数，不写盘）。

    Args:
        chars: 这次注入的字符数。
    """
    _inject_counters["turns"] += 1
    _inject_counters["chars"] += max(0, chars)
    _inject_counters["max_chars"] = max(_inject_counters["max_chars"], max(0, chars))
    _inject_counters["since_flush"] += 1


def injection_snapshot() -> dict[str, int]:
    """取进程内注入计数（命令展示用）。"""
    return {
        "turns": _inject_counters["turns"],
        "chars": _inject_counters["chars"],
        "max_chars": _inject_counters["max_chars"],
        "avg_chars": int(_inject_counters["chars"] / _inject_counters["turns"])
        if _inject_counters["turns"]
        else 0,
    }


async def flush_injection() -> None:
    """把进程内累计的注入量并进统计文件（攒够轮数或命令查询时调）。"""
    if _inject_counters["since_flush"] <= 0:
        return
    try:
        payload = await store.load_json_raw(STATS_KEY) or {}
        if not isinstance(payload, dict):
            payload = {}
        inject = payload.get("inject")
        if not isinstance(inject, dict):
            inject = {"turns": 0, "chars": 0, "max_chars": 0}
        inject["turns"] = int(inject.get("turns") or 0) + _inject_counters["turns"]
        inject["chars"] = int(inject.get("chars") or 0) + _inject_counters["chars"]
        inject["max_chars"] = max(int(inject.get("max_chars") or 0), _inject_counters["max_chars"])
        payload["inject"] = inject
        await store.save_json_raw(STATS_KEY, payload)
        _inject_counters.update({"turns": 0, "chars": 0, "max_chars": 0, "since_flush": 0})
    except Exception as error:  # noqa: BLE001 - 统计失败不影响运行
        logger.debug(f"[daily_schedule] 落盘注入用量失败: {error}")


async def maybe_flush_injection() -> None:
    """攒够轮数就落盘一次（调用方在每轮注入后顺手调）。"""
    if _inject_counters["since_flush"] >= _FLUSH_EVERY_TURNS:
        await flush_injection()


async def usage_lines(config: DailyScheduleConfig) -> list[str]:
    """把用量统计渲染成命令展示的文本。

    Args:
        config: 插件配置。

    Returns:
        文本行列表。
    """
    await flush_injection()
    payload = await store.load_json_raw(STATS_KEY)
    if not isinstance(payload, dict):
        payload = {}

    lines: list[str] = []
    hard = int(getattr(config.budget, "max_prompt_chars", 12000) or 0)
    warn_at = int(getattr(config.budget, "warn_prompt_chars", 8000) or 0)
    lines.append(f"  闸门：单次输入上限 {hard} 字符（告警线 {warn_at}），超限先丢联网/记忆/历史材料")

    day = payload.get("day") if isinstance(payload.get("day"), dict) else {}
    if day:
        lines.append(
            f"  今日：{day.get('calls', 0)} 次调用"
            f" ｜ 输入合计 {day.get('prompt_chars', 0)} 字符"
            f" ｜ 最大单次 {day.get('max_prompt_chars', 0)} 字符"
            f" ｜ 失败 {day.get('failed', 0)} 次"
        )
    else:
        lines.append("  今日：还没有调用记录")

    by_name = payload.get("by_name") if isinstance(payload.get("by_name"), dict) else {}
    if by_name:
        parts = [
            f"{name.replace('daily_schedule_', '')} {int(item.get('calls') or 0)} 次/"
            f"{int(item.get('prompt_chars') or 0)} 字符"
            for name, item in sorted(
                by_name.items(), key=lambda kv: -int((kv[1] or {}).get("calls") or 0)
            )
        ]
        lines.append("  按用途：" + "；".join(parts))

    inject = payload.get("inject") if isinstance(payload.get("inject"), dict) else {}
    live = injection_snapshot()
    turns = int(inject.get("turns") or 0) + live["turns"]
    chars = int(inject.get("chars") or 0) + live["chars"]
    max_chars = max(int(inject.get("max_chars") or 0), live["max_chars"])
    if turns:
        lines.append(
            f"  注入：{turns} 轮，平均 {int(chars / turns)} 字符/轮，最大 {max_chars} 字符"
            "（每轮固定就这么多，不随对话变长）"
        )

    recent = payload.get("recent") if isinstance(payload.get("recent"), list) else []
    if recent:
        lines.append("  最近几次：")
        for item in recent[-5:]:
            if not isinstance(item, dict):
                continue
            flag = "" if item.get("ok") else "（失败）"
            lines.append(
                f"    {item.get('at')} {str(item.get('name')).replace('daily_schedule_', '')}"
                f" 入{item.get('in')}/出{item.get('out')} 字符 {item.get('ms')}ms{flag}"
            )
    return lines


# ── 3. 失败退避 ───────────────────────────────────────────────────────────────


def backoff_seconds(streak: int, *, base: int = 60, cap: int = 1800) -> int:
    """按连续失败次数算下一次允许重试的等待秒数。

    60 → 120 → 240 → … 封顶 1800。这条来自另一个真实事故：失败**不推进水位线**
    意味着「立刻会重试」，于是同一次抽风每 15 秒烧一次调用，一晚上几百次。

    Args:
        streak: 连续失败次数（1 表示第一次失败）。
        base: 起步秒数。
        cap: 封顶秒数。

    Returns:
        等待秒数。
    """
    if streak <= 0:
        return 0
    seconds = base * (2 ** (streak - 1))
    return int(min(cap, max(base, seconds)))


def in_backoff(streak: int, last_fail_at: float, *, cap: int = 1800) -> float:
    """还要等多久才允许再试（0 表示现在就可以）。

    Args:
        streak: 连续失败次数。
        last_fail_at: 最近一次失败的时间戳。
        cap: 封顶秒数。

    Returns:
        剩余等待秒数。
    """
    if streak <= 0 or last_fail_at <= 0:
        return 0.0
    wait = backoff_seconds(streak, cap=cap) - (time.time() - last_fail_at)
    return max(0.0, wait)


__all__ = [
    "STATS_KEY",
    "backoff_seconds",
    "check_prompt",
    "fit",
    "flush_injection",
    "in_backoff",
    "injection_snapshot",
    "maybe_flush_injection",
    "note_injection",
    "record_call",
    "usage_lines",
]
