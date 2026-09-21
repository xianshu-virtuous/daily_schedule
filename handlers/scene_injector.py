"""场景提示注入处理器。

订阅 ``on_prompt_build``，在目标 user prompt 模板构建时，把「此刻正在做什么」
追加到 ``values["extra"]``。

与 prompt_injector 保持同样的累加语义（换行拼接），两者可以共存互不覆盖。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import service_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from ..service import ScheduleService

logger = get_logger("daily_schedule.scene_injector")

#: 日程服务组件签名。
_SERVICE_SIGNATURE = "daily_schedule:service:schedule"


class SceneInjectorHandler(BaseEventHandler):
    """把当前时段的场景提示注入 chat prompt。

    只注入一句旁白式描述，不强制场景、不补充展开；是否注入由配置
    （``plugin.enabled`` / ``scene.enabled`` / ``plugin.target_prompts``）决定。
    """

    name: str = "scene_injector"
    description: str = "向 chatter user prompt 注入「此刻在做什么」的场景旁白"
    weight: int = 12
    intercept_message: bool = False
    init_subscribe: list[EventType | str] = [EventType.ON_PROMPT_BUILD]

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
        """处理 ``on_prompt_build`` 事件。

        Args:
            event_name: 事件名。
            params: 事件参数，含 ``name``（模板名）与 ``values``（模板变量）。

        Returns:
            ``(EventDecision.SUCCESS, params)``，必要时已改写 ``values["extra"]``。
        """
        config = self.plugin.config
        if not config.plugin.enabled or not config.scene.enabled:
            return EventDecision.SUCCESS, params

        template_name = str(params.get("name", ""))
        targets = [str(item) for item in config.plugin.target_prompts]
        if template_name not in targets:
            return EventDecision.SUCCESS, params

        service = self._get_service()
        if service is None:
            return EventDecision.SUCCESS, params

        try:
            injection = await service.get_injection()
        except Exception as error:  # noqa: BLE001 - 注入失败不应影响本轮对话
            logger.error(f"[daily_schedule] 生成注入文本失败: {error}")
            return EventDecision.SUCCESS, params

        if not injection:
            return EventDecision.SUCCESS, params

        values = params.get("values")
        if not isinstance(values, dict):
            return EventDecision.SUCCESS, params

        existing = str(values.get("extra", ""))
        values["extra"] = (existing + "\n" + injection) if existing else injection

        if config.plugin.debug_log:
            logger.info(f"[daily_schedule] 已注入场景：{injection!r}")

        return EventDecision.SUCCESS, params


__all__ = ["SceneInjectorHandler"]
