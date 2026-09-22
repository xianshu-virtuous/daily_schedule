"""daily_schedule 素材层。

按「人设 → 记忆 → 互联网」顺序收集日程素材：

- **人设**：由 :mod:`persona` 提供，始终可用（配置可关）；
- **记忆**：调用记忆服务（默认 ``booku_memory``）检索与「平常做什么」相关的记忆，
  服务不存在或检索失败一律静默跳过；
- **互联网**：运行时探测可用的 MCP 搜索工具，有则主动调用一次，无则跳过。

三层互不依赖，任何一层失败都不影响其余层，也不抛异常给上层。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api import service_api
from src.app.plugin_system.api.log_api import get_logger

from .config import DailyScheduleConfig

logger = get_logger("daily_schedule.sources")

#: MCP 工具签名的前缀。
_MCP_PREFIX = "mcp_provider:tool:"

#: 自动探测搜索工具时的关键词（按优先级）。
_SEARCH_KEYWORDS = ("search", "ddg", "duckduckgo", "web", "fetch")


def _tool_registry() -> Any | None:
    """取组件注册表。

    框架没有插件侧的 ``tool_api``：工具能力的真身在
    :mod:`src.core.components.registry`（枚举签名 / 取组件类）。这里惰性导入，
    框架内部路径变化时只让互联网层降级，而不是整个插件加载失败。

    Returns:
        全局注册表；不可用时返回 ``None``。
    """
    try:
        from src.core.components.registry import get_global_registry

        return get_global_registry()
    except Exception as error:  # noqa: BLE001 - 拿不到就当作没有工具
        logger.debug(f"[daily_schedule] 组件注册表不可用: {error}")
        return None


def _tool_use() -> Any | None:
    """取工具执行器（惰性导入）。

    Returns:
        ``ToolUse`` 单例；不可用时返回 ``None``。
    """
    try:
        from src.core.managers.tool_manager import get_tool_use

        return get_tool_use()
    except Exception as error:  # noqa: BLE001 - 拿不到就当作不能调用
        logger.debug(f"[daily_schedule] 工具执行器不可用: {error}")
        return None


@dataclass
class SourceBundle:
    """收集到的素材包。

    Attributes:
        persona_block: 人设文本块。
        memories: 记忆层摘录（每条一句话）。
        internet_notes: 互联网层摘录（每条一段，已截断）。
        used: 实际用上的素材层名（``persona`` / ``memory`` / ``internet``）。
        error: 收集过程中的异常摘要（排查用，不影响生成）。
    """

    persona_block: str = ""
    memories: list[str] = field(default_factory=list)
    internet_notes: list[str] = field(default_factory=list)
    used: list[str] = field(default_factory=list)
    error: str = ""

    def memory_block(self) -> str:
        """把记忆层渲染成提示词片段。

        Returns:
            多行文本；无素材时返回空字符串。
        """
        if not self.memories:
            return ""
        return "\n".join(f"- {line}" for line in self.memories)

    def internet_block(self) -> str:
        """把互联网层渲染成提示词片段。

        Returns:
            多行文本；无素材时返回空字符串。
        """
        if not self.internet_notes:
            return ""
        return "\n".join(f"- {line}" for line in self.internet_notes)


def _truncate(text: str, limit: int) -> str:
    """按字符数截断文本并补省略号。"""
    text = " ".join(str(text).split())
    if len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


def _stringify(value: Any, limit: int) -> str:
    """把任意结构压成一段可读文本。

    Args:
        value: 待转换对象。
        limit: 最大字符数。

    Returns:
        压平后的文本；无法提取时返回空字符串。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return _truncate(value, limit)
    if isinstance(value, (int, float, bool)):
        return _truncate(str(value), limit)
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            text = _stringify(item, limit)
            if text:
                parts.append(f"{key}: {text}")
            if sum(len(part) for part in parts) > limit:
                break
        return _truncate(" | ".join(parts), limit)
    if isinstance(value, (list, tuple)):
        parts = []
        remaining = limit
        for item in value:
            text = _stringify(item, remaining)
            if not text:
                continue
            parts.append(text)
            remaining -= len(text)
            if remaining <= 0:
                break
        return _truncate(" / ".join(parts), limit)

    try:
        return _truncate(json.dumps(value, ensure_ascii=False), limit)
    except (TypeError, ValueError):
        return _truncate(str(value), limit)


async def collect_memory_notes(config: DailyScheduleConfig, query: str) -> list[str]:
    """通过记忆服务检索素材。

    Args:
        config: 插件配置。
        query: 检索关键词。

    Returns:
        记忆摘录列表；服务不可用时返回空列表。
    """
    signature = config.source.memory_service_signature.strip()
    if not signature:
        return []

    try:
        service = service_api.get_service(signature)
    except Exception as error:  # noqa: BLE001 - 服务查询失败按不可用处理
        logger.warning(f"[daily_schedule] 查询记忆服务失败: {error}")
        return []

    if service is None:
        logger.debug(f"[daily_schedule] 记忆服务 {signature} 不存在，跳过记忆层")
        return []

    retrieve = getattr(service, "retrieve_memories", None)
    if not callable(retrieve):
        logger.debug(f"[daily_schedule] 记忆服务 {signature} 无 retrieve_memories，跳过")
        return []

    try:
        result = await retrieve(query, top_k=max(1, config.source.memory_top_k))
    except Exception as error:  # noqa: BLE001 - 检索失败按无素材处理
        logger.warning(f"[daily_schedule] 记忆检索失败，跳过记忆层: {error}")
        return []

    if not isinstance(result, dict):
        return []

    raw_items = result.get("results")
    if not isinstance(raw_items, list):
        return []

    notes: list[str] = []
    for item in raw_items:
        if not isinstance(item, dict):
            continue
        title = _stringify(item.get("title"), 40)
        body = _stringify(
            item.get("content_snippet") or item.get("content") or item.get("summary"), 160
        )
        if not body:
            continue
        notes.append(f"{title}：{body}" if title else body)
        if len(notes) >= config.source.memory_top_k:
            break
    return notes


def discover_search_tool(config: DailyScheduleConfig) -> str | None:
    """探测可用的搜索工具签名。

    优先使用配置显式指定的签名；未指定时在已注册工具里挑选名字带
    ``search`` / ``ddg`` 等关键词的 MCP 工具。

    Args:
        config: 插件配置。

    Returns:
        工具签名；没有可用工具时返回 ``None``。
    """
    registry = _tool_registry()
    if registry is None:
        return None

    try:
        signatures = registry.list_all()
    except Exception as error:  # noqa: BLE001 - 枚举失败按不可用处理
        logger.warning(f"[daily_schedule] 枚举工具失败: {error}")
        return None

    if not isinstance(signatures, (list, tuple)) or not signatures:
        return None

    configured = config.source.search_tool_signature.strip()
    if configured:
        if configured in signatures:
            return configured
        logger.warning(
            f"[daily_schedule] 配置的搜索工具 {configured} 未注册，回退到自动探测"
        )

    candidates: list[tuple[int, str]] = []
    for signature in signatures:
        if not signature.startswith(_MCP_PREFIX):
            continue
        lowered = signature.lower()
        for rank, keyword in enumerate(_SEARCH_KEYWORDS):
            if keyword in lowered:
                candidates.append((rank, signature))
                break

    if not candidates:
        logger.debug("[daily_schedule] 未找到可用搜索工具，跳过互联网层")
        return None

    candidates.sort()
    return candidates[0][1]


def _search_params(signature: str, query: str) -> dict[str, Any]:
    """根据工具 schema 组装调用参数。

    在 ``properties`` 中寻找第一个必填的字符串字段（通常是 ``query``），
    找不到必填字段时退而使用第一个字符串字段。

    Args:
        signature: 工具签名。
        query: 检索关键词。

    Returns:
        传给工具的参数字典。
    """
    registry = _tool_registry()
    if registry is None:
        return {"query": query}

    try:
        tool_cls = registry.get(signature)
        schema = tool_cls.to_schema() if tool_cls is not None else None
    except Exception as error:  # noqa: BLE001 - schema 获取失败时退回默认参数名
        logger.warning(f"[daily_schedule] 获取 {signature} schema 失败: {error}")
        return {"query": query}

    if not isinstance(schema, dict):
        return {"query": query}

    # BaseTool.to_schema() 给的是 {"name", "description", "parameters"}
    # （parameters 才是 JSON Schema）；这里同时兼容 {"function": {...}} 的写法。
    node = schema.get("function") if isinstance(schema.get("function"), dict) else schema
    parameters = node.get("parameters") if isinstance(node, dict) else None
    container = parameters if isinstance(parameters, dict) else node
    properties = container.get("properties") if isinstance(container, dict) else None
    required = container.get("required") if isinstance(container, dict) else None

    if not isinstance(properties, dict) or not properties:
        return {"query": query}

    required_names = [name for name in required or [] if name in properties]
    for name in required_names:
        field = properties.get(name)
        if isinstance(field, dict) and field.get("type") in (None, "string"):
            return {name: query}

    for name, field in properties.items():
        if isinstance(field, dict) and field.get("type") == "string":
            return {name: query}

    return {"query": query}


def _build_query(config: DailyScheduleConfig, character: str, occupation: str, work: str) -> str:
    """按模板拼检索关键词。

    使用简单字符串替换而非 ``str.format``，模板里出现未知占位符时不会报错。

    Args:
        config: 插件配置。
        character: 角色名。
        occupation: 身份职业。
        work: 出处作品。

    Returns:
        检索关键词。
    """
    template = config.source.search_query or "{character} 日常"
    query = (
        template.replace("{character}", character or "")
        .replace("{occupation}", occupation or "")
        .replace("{work}", work or "")
    )
    return " ".join(query.split()).strip()


async def collect_internet_notes(
    config: DailyScheduleConfig,
    plugin: Any,
    character: str,
    occupation: str,
    work: str,
) -> list[str]:
    """通过 MCP 搜索工具补充互联网素材。

    Args:
        config: 插件配置。
        plugin: 插件实例（工具执行需要）。
        character: 角色名。
        occupation: 身份职业。
        work: 出处作品。

    Returns:
        搜索结果摘录；无可用工具或调用失败时返回空列表。
    """
    signature = discover_search_tool(config)
    if not signature:
        return []

    query = _build_query(config, character, occupation, work)
    if not query:
        return []

    message = _synthetic_message(query)
    params = _search_params(signature, query)

    tool_use = _tool_use()
    if tool_use is None:
        return []

    try:
        success, result = await tool_use.execute_tool(
            signature, plugin, message, **params
        )
    except Exception as error:  # noqa: BLE001 - 工具异常按无素材处理
        logger.warning(f"[daily_schedule] 调用搜索工具 {signature} 失败: {error}")
        return []

    if not success:
        logger.warning(f"[daily_schedule] 搜索工具 {signature} 返回失败: {result}")
        return []

    text = _stringify(result, config.source.search_max_chars)
    if not text:
        return []
    return [_truncate(text, config.source.search_max_chars)]


def _synthetic_message(query: str) -> Any:
    """构造一条用于工具调用的合成消息。

    部分工具实现会读取触发消息的上下文（平台、聊天流等），
    因此即使本次调用与真实对话无关，也传一条结构完整的消息，
    避免工具因缺少上下文而报错。

    Args:
        query: 检索关键词。

    Returns:
        ``Message`` 实例；构造失败时返回 ``None``。
    """
    try:
        from datetime import datetime

        from src.app.plugin_system.types import ChatType, Message, MessageType

        return Message(
            message_id=f"daily_schedule-{int(datetime.now().timestamp())}",
            time=datetime.now(),
            reply_to=None,
            content=query,
            processed_plain_text=query,
            message_type=MessageType.TEXT,
            sender_id="daily_schedule",
            sender_name="daily_schedule",
            sender_cardname=None,
            sender_role="system",
            platform="internal",
            chat_type=ChatType.PRIVATE,
        )
    except Exception as error:  # noqa: BLE001 - 构造失败时退化为 None
        logger.warning(f"[daily_schedule] 构造合成消息失败: {error}")
        return None


async def collect(
    config: DailyScheduleConfig,
    plugin: Any,
    persona_block: str,
    character: str,
    occupation: str,
    work: str,
) -> SourceBundle:
    """按 人设 → 记忆 → 互联网 顺序收集素材。

    Args:
        config: 插件配置。
        plugin: 插件实例。
        persona_block: 人设文本块。
        character: 角色名。
        occupation: 身份职业。
        work: 出处作品。

    Returns:
        素材包。
    """
    bundle = SourceBundle(persona_block=persona_block)

    if config.source.use_persona and persona_block:
        bundle.used.append("persona")

    if config.source.use_memory:
        bundle.memories = await collect_memory_notes(config, config.source.memory_query)
        if bundle.memories:
            bundle.used.append("memory")

    if config.source.use_internet:
        bundle.internet_notes = await collect_internet_notes(
            config, plugin, character, occupation, work
        )
        if bundle.internet_notes:
            bundle.used.append("internet")

    return bundle


__all__ = [
    "SourceBundle",
    "collect",
    "collect_internet_notes",
    "collect_memory_notes",
    "discover_search_tool",
]
