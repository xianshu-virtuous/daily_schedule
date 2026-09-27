"""``/目标`` 命令：一眼看完她现在的规划与此刻在做什么。

用法（英文 / 中文均可）::

    /目标   /goal    — 年目标、本月忙闲、本周主题、当前时段、心情

和 ``/日程`` 的分工：``/日程`` 管"日程本身"（全天时段、重生成、池子、日记），
``/目标`` 只管**看**——年 / 月 / 周 / 此刻，一条线读下来。

命令级权限为 OWNER：这是主人的插件。
"""

from __future__ import annotations

from typing import Any

from src.app.plugin_system.api import service_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from ..service import ScheduleService

logger = get_logger("daily_schedule.goal_command")

#: 日程服务组件签名。
_SERVICE_SIGNATURE = "daily_schedule:service:schedule"

_USAGE = """\
/目标 用法：
  目标 / goal   — 年目标、本月忙闲、本周主题、当前时段与心情"""


class GoalCommand(BaseCommand):
    """查看她的目标链（仅主人可用）。"""

    name: str = "goal"
    description: str = "查看年目标 / 本月忙闲 / 本周主题 / 当前时段（仅主人可用）"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，同时支持 ``goal`` 与 ``目标``。"""
        if not parts:
            return 0
        if parts[0] in ("goal", "目标"):
            return 1
        return 0

    def _get_service(self) -> ScheduleService | None:
        """取日程服务实例。"""
        try:
            service = service_api.get_service(_SERVICE_SIGNATURE)
        except Exception as error:  # noqa: BLE001 - 服务查询失败按不可用处理
            logger.warning(f"[daily_schedule] 获取日程服务失败: {error}")
            return None
        if isinstance(service, ScheduleService):
            return service
        return None

    async def _reply(self, text: str) -> None:
        """向当前聊天流回复文本。"""
        await send_text(text, stream_id=self.stream_id)

    @cmd_route()
    async def handle_goal(self) -> tuple[bool, str]:
        """展示目标链。"""
        service = self._get_service()
        if service is None:
            await self._reply("日程服务未就绪，请确认 daily_schedule 插件已加载。")
            return False, "service unavailable"

        try:
            lines: list[str] = await service.goal_lines()
        except Exception as error:  # noqa: BLE001 - 展示失败不该抛给用户
            logger.error(f"[daily_schedule] 生成 /目标 失败: {error}")
            await self._reply(f"目标暂时读不出来：{error}")
            return False, "error"

        await self._reply("\n".join(lines) if lines else "还没有任何目标。")
        return True, "ok"

    @cmd_route("帮助")
    async def handle_help_cn(self) -> tuple[bool, str]:
        """显示帮助（中文别名）。"""
        await self._reply(_USAGE)
        return True, "help"

    @cmd_route("help")
    async def handle_help(self) -> tuple[bool, str]:
        """显示帮助。"""
        await self._reply(_USAGE)
        return True, "help"


__all__: list[Any] = ["GoalCommand"]
