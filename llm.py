"""daily_schedule 模型调用层。

把「选模型 → 组请求 → 取文本 → 抠 JSON」这套动作收敛到一处，
供人设判定与日程生成共用。

模型选择规则：

1. ``model.model_name`` 非空 → 用 ``llm_api.get_model_set_by_name`` 直接点名；
2. 否则用 ``model.task_name``（默认 ``actor``，即主回复模型）。

调用失败一律返回 ``LLMCallResult(ok=False)``，由调用方决定降级行为，
不向对话主流程抛异常。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

from src.app.plugin_system.api import llm_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.types import LLMPayload, ROLE, Text

from .config import DailyScheduleConfig

logger = get_logger("daily_schedule.llm")

#: 从模型返回中抠 JSON 时使用的代码围栏匹配。
_FENCE_PATTERN = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

#: 全角标点 → 半角（模型偶尔会用中文引号包 JSON）。
_FULLWIDTH_TABLE = str.maketrans({"“": '"', "”": '"', "＂": '"', "：": ":", "，": ","})

#: 注释与多余逗号：模型爱在 JSON 里写 ``// 说明`` 或留尾逗号。
_LINE_COMMENT_PATTERN = re.compile(r"(?<![:/\"'])//[^\n]*")
_BLOCK_COMMENT_PATTERN = re.compile(r"/\*.*?\*/", re.DOTALL)
_TRAILING_COMMA_PATTERN = re.compile(r",\s*([}\]])")


@dataclass
class LLMCallResult:
    """一次模型调用的结果。

    Attributes:
        ok: 是否成功拿到文本。
        text: 返回的纯文本。
        model_tag: 实际使用的模型标识（写日志用）。
        error: 失败原因摘要。
    """

    ok: bool
    text: str = ""
    model_tag: str = ""
    error: str = ""


def _entry_identifier(entry: Any) -> str:
    """读取模型条目里的模型标识（写日志用）。"""
    if isinstance(entry, dict):
        for key in ("model_identifier", "name", "id"):
            value = entry.get(key)
            if value:
                return str(value)
    return ""


def resolve_model_set(
    config: DailyScheduleConfig,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> tuple[Any, str]:
    """按配置解析模型集，并按需覆盖温度与输出上限。

    Args:
        config: 插件配置。
        temperature: 覆盖温度；``None`` 表示用配置值。
        max_tokens: 覆盖输出上限；``None`` 表示用配置值。

    Returns:
        ``(model_set, model_tag)``；解析失败时抛出底层异常由调用方兜住。
    """
    model_name = config.model.model_name.strip()
    if model_name:
        model_set = llm_api.get_model_set_by_name(
            model_name,
            temperature=temperature if temperature is not None else config.model.temperature,
            max_tokens=max_tokens if max_tokens is not None else config.model.max_tokens,
        )
        return model_set, f"by_name:{model_name}"

    task_name = config.model.task_name.strip() or "actor"
    model_set = llm_api.get_model_set_by_task(task_name)

    target_temperature = (
        temperature if temperature is not None else config.model.temperature
    )
    target_max_tokens = max_tokens if max_tokens is not None else config.model.max_tokens

    overridden: list[Any] = []
    identifiers: list[str] = []
    for entry in model_set:
        if isinstance(entry, dict):
            patched = dict(entry)
            # 温度与输出上限以插件配置为准：日程生成需要比日常闲聊更高的温度，
            # 而 max_tokens 要留够条目 + 让位心声 + 小结的输出空间。
            patched["temperature"] = target_temperature
            patched["max_tokens"] = target_max_tokens
            overridden.append(patched)
            identifier = _entry_identifier(entry)
            if identifier:
                identifiers.append(identifier)
        else:
            overridden.append(entry)

    tag = f"task:{task_name}"
    if identifiers:
        tag = f"{tag}:{identifiers[0]}"
    return overridden, tag


def extract_text(response: Any) -> str:
    """从 LLMResponse 中提取纯文本。

    Args:
        response: ``request.send`` 返回的响应对象。

    Returns:
        提取到的文本；无内容时返回空字符串。
    """
    message = getattr(response, "message", None)
    if message is None:
        return ""

    if isinstance(message, str):
        return message.strip()

    if isinstance(message, list):
        chunks: list[str] = []
        for item in message:
            if isinstance(item, str):
                chunks.append(item)
            else:
                text = getattr(item, "text", None) or getattr(item, "content", None)
                if text:
                    chunks.append(str(text))
        return "".join(chunks).strip()

    text = getattr(message, "text", None)
    if text:
        return str(text).strip()
    return str(message).strip()


def excerpt(text: str, *, head: int = 100, tail: int = 40) -> str:
    """把模型返回压成一小段，用于失败日志（主人只看一眼就够）。"""
    flat = " ".join(str(text or "").split())
    if len(flat) <= head + tail:
        return flat
    return f"{flat[:head]} … {flat[-tail:]}"


def _iter_balanced_objects(text: str, *, limit: int = 5) -> list[str]:
    """按顺序取出若干个括号配平完整的 ``{...}``，跳过字符串里的括号。

    模型爱在 JSON 前写一段思考（里面也可能带 ``{``），所以不能只看第一个
    ``{``：某个位置配不平就往后换一个位置再试，最多取 ``limit`` 个。
    """
    found: list[str] = []
    start = text.find("{")
    while start != -1 and len(found) < limit:
        depth = 0
        in_string = False
        escaped = False
        closed_at = -1
        for index in range(start, len(text)):
            char = text[index]
            if in_string:
                if escaped:
                    escaped = False
                elif char == "\\":
                    escaped = True
                elif char == '"':
                    in_string = False
                continue
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    closed_at = index
                    break

        if closed_at == -1:
            start = text.find("{", start + 1)
            continue

        found.append(text[start : closed_at + 1])
        start = text.find("{", closed_at + 1)
    return found


def _close_containers(text: str) -> str | None:
    """给被截断的 JSON 补上收尾括号；字符串没闭合时返回 ``None``。"""
    stack: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "{[":
            stack.append(char)
        elif char in "}]":
            if not stack:
                return None
            stack.pop()

    if in_string:
        return None
    return text + "".join("}" if item == "{" else "]" for item in reversed(stack))


def _loosen(text: str) -> str:
    """把模型写坏的 JSON 往标准 JSON 靠：去注释、去尾逗号、全角转半角。"""
    loosened = _BLOCK_COMMENT_PATTERN.sub("", text)
    loosened = _LINE_COMMENT_PATTERN.sub("", loosened)
    loosened = _TRAILING_COMMA_PATTERN.sub(r"\1", loosened)
    return loosened.translate(_FULLWIDTH_TABLE)


def _recover_truncated(text: str) -> str | None:
    """输出被 max_tokens 截断时的补救：退回最后一个完整位置的括号闭合。

    从尾部往前试若干刀，每次都把括号补齐再解析；只要 ``entries`` 里
    至少有一条可用条目就算救回来了（宁可少几条，也别整天没有日程）。
    """
    start = text.find("{")
    if start == -1:
        return None

    body = text[start:]
    floor = max(len(body) - 600, 1)
    for cut in range(len(body), floor, -1):
        piece = body[:cut].rstrip().rstrip(",:[{ \t\r\n")
        if not piece:
            continue
        closed = _close_containers(piece)
        if closed is None:
            continue
        try:
            parsed = json.loads(_loosen(closed))
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and parsed.get("entries"):
            return closed
    return None


#: 认得出这是「本插件要的 JSON」的键名。
_PAYLOAD_KEYS = ("entries", "yield_lines", "yesterday_summary", "kind", "character_name")


def _looks_like_payload(payload: dict[str, Any]) -> bool:
    """判断这个对象是不是我们要的那份数据（而不是夹在里面的小片段）。"""
    return any(key in payload for key in _PAYLOAD_KEYS)


def extract_json(text: str) -> dict[str, Any] | None:
    """从模型返回中抠出第一个 JSON 对象。

    依次尝试：直接解析 → 代码围栏内解析 → 括号配平截取 → 首尾大括号截取
    → 上面各种形态的「宽松版」（去注释、去尾逗号、全角转半角）
    → 被截断时的补括号补救。

    Args:
        text: 模型返回的原始文本。

    Returns:
        解析出的字典；失败返回 ``None``。
    """
    if not text:
        return None

    stripped = text.strip()
    raw_candidates: list[str] = [stripped]
    for match in _FENCE_PATTERN.finditer(text):
        raw_candidates.append(match.group(1).strip())

    balanced = _iter_balanced_objects(stripped)
    raw_candidates.extend(balanced)

    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end > start:
        raw_candidates.append(stripped[start : end + 1])

    candidates: list[str] = []
    for candidate in raw_candidates:
        if not candidate:
            continue
        candidates.append(candidate)
        loosened = _loosen(candidate)
        if loosened != candidate:
            candidates.append(loosened)
        # 只有单引号、没有双引号的场合，基本可以判定是引号写错了
        if '"' not in candidate and "'" in candidate:
            candidates.append(candidate.replace("'", '"'))

    fallback: dict[str, Any] | None = None

    for candidate in candidates:
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue

        objects: list[dict[str, Any]] = []
        if isinstance(parsed, dict):
            objects.append(parsed)
        elif isinstance(parsed, list):
            # 偶尔模型会返回 [{...}] 形式
            objects.extend(item for item in parsed if isinstance(item, dict))

        for item in objects:
            if _looks_like_payload(item):
                return item
            if fallback is None:
                fallback = item

    # 输出被截断时，退回最后一个完整位置补括号再试
    for candidate in raw_candidates:
        if not candidate:
            continue
        recovered = _recover_truncated(_loosen(candidate))
        if recovered is None:
            continue
        try:
            parsed = json.loads(recovered)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict) and _looks_like_payload(parsed):
            return parsed

    return fallback


async def call(
    config: DailyScheduleConfig,
    system_prompt: str,
    user_prompt: str,
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
    request_name: str = "daily_schedule",
) -> LLMCallResult:
    """执行一次单轮模型调用。

    Args:
        config: 插件配置。
        system_prompt: 系统提示词。
        user_prompt: 用户提示词。
        temperature: 覆盖温度。
        max_tokens: 覆盖输出上限。
        request_name: 请求名（写入 LLM 统计，便于排查）。

    Returns:
        调用结果。
    """
    try:
        model_set, model_tag = resolve_model_set(
            config, temperature=temperature, max_tokens=max_tokens
        )
    except Exception as error:  # noqa: BLE001 - 模型解析失败按调用失败处理
        logger.warning(f"[daily_schedule] 解析模型集失败: {error}")
        return LLMCallResult(ok=False, error=f"resolve model set: {error}")

    try:
        request = llm_api.create_llm_request(model_set, request_name=request_name)
        request.add_payload(LLMPayload(ROLE.SYSTEM, Text(system_prompt)))
        request.add_payload(LLMPayload(ROLE.USER, Text(user_prompt)))
        response = await request.send(stream=False)
        await response
    except Exception as error:  # noqa: BLE001 - 调用失败按失败结果返回
        logger.warning(f"[daily_schedule] 模型调用失败: {error}")
        return LLMCallResult(ok=False, model_tag=model_tag, error=str(error))

    text = extract_text(response)
    if not text:
        return LLMCallResult(ok=False, model_tag=model_tag, error="empty response")
    return LLMCallResult(ok=True, text=text, model_tag=model_tag)


__all__ = [
    "LLMCallResult",
    "call",
    "excerpt",
    "extract_json",
    "extract_text",
    "resolve_model_set",
]
