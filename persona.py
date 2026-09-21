"""daily_schedule 人设层。

第一层素材：直接读取框架的 Bot 人格配置（``config/core.toml`` 的
``[personality]``），并把关键字段拼成给模型看的文本块。

同时提供人设指纹：人设文本一变，指纹就变，用于让人设类型判定结果自动失效。
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

from src.app.plugin_system.api.log_api import get_logger

logger = get_logger("daily_schedule.persona")

#: 背景故事进入提示词时的最大字符数。
_BACKGROUND_LIMIT = 1200
#: 表达风格进入提示词时的最大字符数。
_STYLE_LIMIT = 800


def _get_personality_config() -> Any:
    """获取核心配置中的人格节点，失败时返回 ``None``。"""
    try:
        from src.core.config import get_core_config

        return get_core_config().personality
    except Exception as error:  # noqa: BLE001 - 人设读取失败由调用方降级处理
        logger.warning(f"[daily_schedule] 读取人格配置失败: {error}")
        return None


def _read_text(source: Any, field_name: str, default: str = "") -> str:
    """读取某个字段的文本值，缺失或类型不符时返回默认值。"""
    value = getattr(source, field_name, default) if source is not None else default
    if value is None:
        return default
    return str(value).strip()


@dataclass
class PersonaSnapshot:
    """当前 Bot 人设的快照。

    Attributes:
        nickname: Bot 昵称。
        alias_names: 别名列表。
        identity: 身份设定。
        personality_core: 核心人格。
        personality_side: 人格侧面。
        background_story: 世界观背景。
        reply_style: 表达风格。
        fingerprint: 上述内容的内容指纹。
    """

    nickname: str = ""
    alias_names: list[str] = field(default_factory=list)
    identity: str = ""
    personality_core: str = ""
    personality_side: str = ""
    background_story: str = ""
    reply_style: str = ""
    fingerprint: str = ""

    @property
    def is_usable(self) -> bool:
        """人设是否足以支撑日程生成（至少有昵称或身份）。"""
        return bool(self.nickname or self.identity or self.personality_core)

    def to_prompt_block(self) -> str:
        """拼成交给模型的人设文本块。

        Returns:
            多行人设描述；无可用内容时返回空字符串。
        """
        lines: list[str] = []
        if self.nickname:
            alias = f"（别名：{'、'.join(self.alias_names)}）" if self.alias_names else ""
            lines.append(f"昵称：{self.nickname}{alias}")
        if self.identity:
            lines.append(f"身份：{self.identity}")
        if self.personality_core:
            lines.append(f"核心人格：{self.personality_core}")
        if self.personality_side:
            lines.append(f"人格侧面：{self.personality_side}")
        if self.background_story:
            lines.append(f"背景设定：{self.background_story[:_BACKGROUND_LIMIT]}")
        if self.reply_style:
            lines.append(f"表达风格（仅参考语气，不必照搬）：{self.reply_style[:_STYLE_LIMIT]}")
        return "\n".join(lines)


def read_persona() -> PersonaSnapshot:
    """读取当前人设快照并计算指纹。

    Returns:
        人设快照；框架配置不可用时返回空快照（``is_usable`` 为假）。
    """
    personality = _get_personality_config()

    aliases_raw = getattr(personality, "alias_names", []) if personality is not None else []
    alias_names: list[str] = []
    if isinstance(aliases_raw, (list, tuple)):
        alias_names = [str(item).strip() for item in aliases_raw if str(item).strip()]

    snapshot = PersonaSnapshot(
        nickname=_read_text(personality, "nickname"),
        alias_names=alias_names,
        identity=_read_text(personality, "identity"),
        personality_core=_read_text(personality, "personality_core"),
        personality_side=_read_text(personality, "personality_side"),
        background_story=_read_text(personality, "background_story"),
        reply_style=_read_text(personality, "reply_style"),
    )
    snapshot.fingerprint = compute_fingerprint(snapshot)
    return snapshot


def compute_fingerprint(snapshot: PersonaSnapshot) -> str:
    """计算人设指纹。

    只取与人设判别强相关的字段，避免无关字段变动导致缓存失效。

    Args:
        snapshot: 人设快照。

    Returns:
        16 位十六进制指纹。
    """
    payload = "\u0001".join(
        [
            snapshot.nickname,
            "、".join(snapshot.alias_names),
            snapshot.identity,
            snapshot.personality_core,
            snapshot.personality_side,
            snapshot.background_story,
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


__all__ = ["PersonaSnapshot", "compute_fingerprint", "read_persona"]
