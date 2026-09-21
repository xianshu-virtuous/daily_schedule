"""主人到场处理器。

订阅 ``on_message_received``：当发信人是主人（权限系统判定的 OWNER 级及以上）时，
把日程时间让出来。

这里只做「识别 + 记状态」，措辞由 :mod:`daily_schedule.scene` 从角色自己
的心声池里挑——让位要听起来是「我想先陪你」，而不是「规则要求我让出时间」。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import permission_api, service_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from ..service import ScheduleService

logger = get_logger("daily_schedule.owner_presence")

#: 日程服务组件签名。
_SERVICE_SIGNATURE = "daily_schedule:service:schedule"


class OwnerPresenceHandler(BaseEventHandler):
    """识别主人开口，并触发让位状态。"""

    name: str = "owner_presence"
    description: str = "主人发消息时让出日程时间（权限系统校验身份）"
    weight: int = 5
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_MESSAGE_RECEIVED]

    def _get_service(self) -> ScheduleService | None:
        """取日程服务实例。

        Returns:
            服务实例；服务尚未注册时返回 ``None``。
        """
        try:
            service = service_api.get_service(_SERVICE_SIGNATURE)
        except Exception as error:  # noqa: BLE001 - 服务查询失败按不可用处理
            logger.warning(f"[daily_schedule] 获取日程服务失败: {error}")
            return None
        if isinstance(service, ScheduleService):
            return service
        return None

    async def execute(
        self,
        event_name: str,
        params: dict[str, Any],
    ) -> tuple[EventDecision, dict[str, Any]]:
        """处理 ``on_message_received`` 事件。

        Args:
            event_name: 事件名。
            params: 事件参数，含 ``message``。

        Returns:
            ``(EventDecision.SUCCESS, params)``；本处理器从不拦截消息。
        """
        config = self.plugin.config
        if not config.plugin.enabled or not config.scene.yield_enabled:
            return EventDecision.SUCCESS, params

        message = params.get("message")
        if message is None:
            return EventDecision.SUCCESS, params

        # 跳过 bot 自己发出的消息与系统消息，避免自触发
        if str(getattr(message, "sender_role", "") or "").lower() in ("bot", "system"):
            return EventDecision.SUCCESS, params

        platform = str(getattr(message, "platform", "") or "").strip()
        sender_id = str(getattr(message, "sender_id", "") or "").strip()
        if not platform or not sender_id:
            return EventDecision.SUCCESS, params

        person_id = permission_api.generate_person_id(platform, sender_id)
        try:
            level = await permission_api.get_user_permission_level(person_id)
        except Exception as error:  # noqa: BLE001 - 权限查询失败按非主人处理
            logger.warning(f"[daily_schedule] 查询权限等级失败: {error}")
            return EventDecision.SUCCESS, params

        service = self._get_service()
        if service is None:
            return EventDecision.SUCCESS, params

        master_name = (
            str(getattr(message, "sender_cardname", "") or "").strip()
            or str(getattr(message, "sender_name", "") or "").strip()
        )

        try:
            await service.mark_yield(
                level=level,
                chat_type=getattr(message, "chat_type", None),
                master_name=master_name,
                stream_id=str(getattr(message, "stream_id", "") or ""),
            )
        except Exception as error:  # noqa: BLE001 - 让位失败不应影响本轮对话
            logger.error(f"[daily_schedule] 处理让位失败: {error}")

        return EventDecision.SUCCESS, params


__all__ = ["OwnerPresenceHandler"]
