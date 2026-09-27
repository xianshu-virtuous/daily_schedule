"""场景提示注入处理器。

订阅 ``on_prompt_build``，在目标 user prompt 模板构建时，把「此刻正在做什么」
追加到 ``values["extra"]``；同时把这句背景写进 **system reminder**，让 chatter 在
构建请求时自己拾取。

为什么要两路（模仿 DFC / NDFC 的注入方式）：

- **reminder 路（主）**：chatter 以 ``with_reminder="actor"`` 建请求时会登记
  全局 + 流私有两个 bucket，并把内容包成 ``<system_reminder>…</system_reminder>``。
  带标签的文本在模型眼里是**系统级的元指令**，而不是「一段等着被念出来的旁白」
  ——这正是背景设定不再被背书的关键。
- **extra 路（兜底）**：不是所有 chatter 都会传 ``with_reminder``；此时仍按老办法
  往 user prompt 的 ``extra`` 追加以保证可见。两路内容一致，重复出现也无害。

**两条线分桶**（``scene.yield_stream_scope`` 打开时）：

| 内容 | 写哪儿 | 理由 |
| --- | --- | --- |
| 日程场景行（此刻在做什么） | 全局 bucket | 这是 Bot 自己的状态，对所有对话者一致 |
| 让位行（主人开口，先陪你） | ``stream:{id}:{bucket}`` | 只跟「哪个会话触发」有关，别人不该看到 |

``on_prompt_build`` 的 params 里确实没有 stream_id，但 **``values`` 里有**
（default_chatter 的 prompt_builder 把它当随行元数据塞进去）。老 chatter 不塞时
退回旧行为：让位行合并进全局文本，不做隔离。

与 prompt_injector 保持同样的累加语义（换行拼接），两者可以共存互不覆盖。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import service_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseEventHandler
from src.app.plugin_system.types import EventType
from src.kernel.event import EventDecision

from ..service import SceneTexts, ScheduleService

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

    @staticmethod
    def _stream_id_of(values: Any) -> str:
        """从模板变量里取当前会话 ID。

        ``on_prompt_build`` 的参数里没有 stream_id，但 chatter 会把它塞进
        ``values``（不在模板占位符里，仅作随行元数据）。

        Args:
            values: 模板变量字典。

        Returns:
            会话 ID；拿不到时返回空字符串（此时不做流隔离）。
        """
        if not isinstance(values, dict):
            return ""
        return str(values.get("stream_id") or "").strip()

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

        values = params.get("values")
        stream_id = self._stream_id_of(values)

        try:
            texts = await service.injection_texts(stream_id=stream_id or None)
        except Exception as error:  # noqa: BLE001 - 注入失败不应影响本轮对话
            logger.error(f"[daily_schedule] 生成注入文本失败: {error}")
            return EventDecision.SUCCESS, params

        if not texts.base and not texts.stream:
            return EventDecision.SUCCESS, params

        # 主路：写进 system reminder（下一轮构建请求时由 chatter 自动拾取）
        channel = str(getattr(config.scene, "channel", "reminder_first") or "").strip().lower()
        wrote_reminder = False
        if config.scene.reminder_enabled and channel != "extra_only":
            wrote_reminder = self._sync_reminders(config, texts, stream_id)

        if not isinstance(values, dict):
            return EventDecision.SUCCESS, params

        # reminder_first：写成功就不再往 extra 里追加。同一段背景在一个请求里出现两遍
        # 只是把 token 付两遍，还会让模型更注意到它；写失败（或没开 reminder）才退回 extra。
        skip_extra = channel == "reminder_first" and wrote_reminder
        if not skip_extra:
            injection = texts.full
            existing = str(values.get("extra", ""))
            values["extra"] = (existing + "\n" + injection) if existing else injection

        if config.plugin.debug_log:
            logger.info(
                f"[daily_schedule] 已注入场景（stream={stream_id or '-'}，"
                f"channel={channel}，reminder={wrote_reminder}）：{texts.full!r}"
            )

        return EventDecision.SUCCESS, params

    def _sync_reminders(self, config: Any, texts: SceneTexts, stream_id: str) -> bool:
        """按「全局 + 该流私有」两条线同步 reminder。

        用 ``dynamic`` + 覆盖写（同名即覆盖）：每轮构建请求时重新读取，
        所以拿到的永远是**当下这一刻**的文本，不会残留上一段的场景。

        全局 bucket 而非流私有 bucket 放日程行：日程是 bot 自己的状态，对所有
        对话者一致。让位行反过来——它只对触发它的那个会话有意义，因此写进
        ``stream:{id}:{bucket}``，并且**该流不再让位时主动删掉**，免得那句
        「我更想听你说」永远挂在那个会话上。

        任何失败都只降级为「这轮退回 extra 注入」，不影响对话。

        Args:
            config: 插件配置。
            texts: 装配好的全局段与让位段。
            stream_id: 当前会话 ID，可为空。

        Returns:
            是否至少成功写了一条 reminder（调用方据此决定要不要退回 extra）。
        """
        written = False
        try:
            from src.app.plugin_system.api import prompt_api

            bucket = config.scene.reminder_bucket
            name = config.scene.reminder_name

            if texts.base:
                prompt_api.add_system_reminder(
                    bucket=bucket,
                    name=name,
                    content=texts.base,
                    insert_type="dynamic",
                    consume="forever",
                )
                written = True

            if not stream_id:
                return written

            if texts.stream:
                prompt_api.add_stream_reminder(
                    stream_id=stream_id,
                    bucket=bucket,
                    name=name,
                    content=texts.stream,
                    insert_type="dynamic",
                    consume="forever",
                )
            else:
                # 该流已经不在让位：清掉可能残留的让位行
                remover = getattr(prompt_api, "delete_stream_reminder", None)
                if callable(remover):
                    remover(stream_id=stream_id, bucket=bucket, name=name)
        except Exception as error:  # noqa: BLE001 - 写入失败退回 extra 注入
            logger.warning(f"[daily_schedule] 写 system reminder 失败，改用 extra 注入: {error}")
            return False
        return written
