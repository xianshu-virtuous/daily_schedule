"""场景提示注入处理器。

订阅 ``on_prompt_build``，在目标 user prompt 模板构建时，把「此刻正在做什么」
追加到 ``values["extra"]``；同时把这句背景写进 **system reminder** 的 bucket，
让 chatter 在构建请求时自己拾取。

为什么要两路（模仿 DFC / NDFC 的注入方式）：

- **reminder 路（主）**：chatter 以 ``with_reminder="actor"`` 建请求时会登记
  全局 + 流私有两个 bucket，并把内容包成 ``<system_reminder>…</system_reminder>``。
  带标签的文本在模型眼里是**系统级的元指令**，而不是「一段等着被念出来的旁白」
  ——这正是背景设定不再被背书的关键。
- **extra 路（兜底）**：不是所有 chatter 都会传 ``with_reminder``；此时仍按老办法
  往 user prompt 的 ``extra`` 追加以保证可见。两路内容一致，重复出现也无害。

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

        # 主路：写进 system reminder（下一轮构建请求时由 chatter 自动拾取）
        if config.scene.reminder_enabled:
            self._write_reminder(config, injection)

        values = params.get("values")
        if not isinstance(values, dict):
            return EventDecision.SUCCESS, params

        existing = str(values.get("extra", ""))
        values["extra"] = (existing + "\n" + injection) if existing else injection

        if config.plugin.debug_log:
            logger.info(f"[daily_schedule] 已注入场景：{injection!r}")

        return EventDecision.SUCCESS, params

    def _write_reminder(self, config: Any, injection: str) -> None:
        """把场景文本写进 system reminder 的 bucket。

        用 ``dynamic`` + 覆盖写（同名即覆盖）：每轮构建请求时重新读取，
        所以拿到的永远是**当下这一刻**的文本，不会残留上一段的场景。

        全局 bucket 而非流私有 bucket：日程是 bot 自己的状态，对所有对话者
        一致；而 ``on_prompt_build`` 的参数里也没有 stream_id 可用。

        任何失败都只降级为「这轮退回 extra 注入」，不影响对话。

        Args:
            config: 插件配置。
            injection: 已装配好的注入文本。
        """
        try:
            from src.app.plugin_system.api import prompt_api

            prompt_api.add_system_reminder(
                bucket=config.scene.reminder_bucket,
                name=config.scene.reminder_name,
                content=injection,
                insert_type="dynamic",
                consume="forever",
            )
        except Exception as error:  # noqa: BLE001 - 写入失败退回 extra 注入
            logger.warning(f"[daily_schedule] 写 system reminder 失败，改用 extra 注入: {error}")
