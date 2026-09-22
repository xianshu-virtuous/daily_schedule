"""``/日程`` 命令：查看与手动干预 Bot 自己的日程。

用法（英文 / 中文均可）::

    /日程                     — 状态摘要（此刻在做什么）
    /日程 查看   /status      — 状态摘要
    /日程 全天   /today       — 列出今天的全部时段
    /日程 重生成 /regenerate  — 强制重新生成今天的日程（后台执行）
    /日程 人设   /persona     — 查看人设类型判定结果
    /日程 让位   /yield       — 手动让位（主人到场验证用）
    /日程 收回   /unyield     — 结束让位，恢复日程
    /日程 日志   /log         — 最近几天的生成记录
    /日程 日记   /diary       — 离线生活的日记（它不在线的时候在做什么）
    /日程 帮助   /help        — 帮助

命令级权限为 OWNER：这是主人的插件。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from src.app.plugin_system.api import service_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.api.send_api import send_text
from src.app.plugin_system.base import BaseCommand, cmd_route
from src.app.plugin_system.types import PermissionLevel

from .. import store
from ..service import ScheduleService

logger = get_logger("daily_schedule.command")

#: 日程服务组件签名。
_SERVICE_SIGNATURE = "daily_schedule:service:schedule"

_USAGE = """\
/日程 用法（英文 / 中文均可）：
  查看 / status        — 此刻在做什么（默认）
  全天 / today         — 列出今天全部时段
  重生成 / regenerate  — 强制重新生成今天的日程
  人设 / persona       — 查看人设类型判定
  让位 / yield         — 手动让位（验证用）
  收回 / unyield       — 结束让位
  日志 / log           — 最近生成记录
  日记 / diary         — 离线时在做什么（离线生活）
  帮助 / help          — 本帮助"""


class ScheduleCommand(BaseCommand):
    """Bot 日程管理命令（仅主人可用）。"""

    name: str = "schedule"
    description: str = "查看与干预 Bot 自己的日程（仅主人可用）"
    permission_level: PermissionLevel = PermissionLevel.OWNER

    @classmethod
    def match(cls, parts: list[str]) -> int:
        """匹配命令名，同时支持 ``schedule`` 与 ``日程``。

        Args:
            parts: 命令片段列表。

        Returns:
            匹配长度，不匹配返回 0。
        """
        if not parts:
            return 0
        if parts[0] in ("schedule", "日程"):
            return 1
        return 0

    # ── 内部工具 ──────────────────────────────────────────────────────────────

    async def _reply(self, text: str) -> None:
        """向当前聊天流回复文本。

        Args:
            text: 回复内容。
        """
        await send_text(text, stream_id=self.stream_id)

    def _get_service(self) -> ScheduleService | None:
        """取日程服务实例。

        Returns:
            服务实例；未注册时返回 ``None``。
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
    def _format_time(ts: float) -> str:
        """把时间戳格式化成 ``HH:MM``。

        Args:
            ts: Unix 时间戳。

        Returns:
            格式化文本；``ts`` 非正数时返回 ``-``。
        """
        if ts <= 0:
            return "-"
        return datetime.fromtimestamp(ts).strftime("%H:%M")

    async def _require_service(self) -> ScheduleService | None:
        """取服务并在缺失时直接回复错误。

        Returns:
            服务实例；缺失时返回 ``None``。
        """
        service = self._get_service()
        if service is None:
            await self._reply("日程服务未就绪，请确认 daily_schedule 插件已加载。")
            return None
        return service

    # ── 路由 ──────────────────────────────────────────────────────────────────

    @cmd_route()
    async def handle_root(self) -> tuple[bool, str]:
        """无子命令时显示状态摘要。"""
        return await self.handle_status()

    @cmd_route("status")
    async def handle_status(self) -> tuple[bool, str]:
        """显示当前状态摘要。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        status: dict[str, Any] = await service.status()
        lines = [f"【日程 {status['date']}】"]

        if status["has_schedule"]:
            lines.append(
                f"  条目：{status['entries']} 条"
                f"（生成于 {self._format_time(float(status['generated_at']))}）"
            )
            if status["persona_name"]:
                kind = (
                    "扮演角色"
                    if status["persona_kind"] == "roleplay"
                    else "原创角色"
                )
                lines.append(f"  人设：{status['persona_name']}（{kind}）")
            if status["sources_used"]:
                lines.append(f"  素材：{'、'.join(status['sources_used'])}")
            doing = status["doing"] or "（当前时段没有安排）"
            lines.append(f"  此刻：{doing}")
            if status["next"]:
                lines.append(f"  接下来：{status['next']}")
        else:
            lines.append("  今天还没有日程。")

        if status["yielding"]:
            until = float(status["yield_until"])
            until_text = self._format_time(until) if 0 < until < 4102444800 else "今天结束"
            lines.append(
                f"  让位中：{status['yield_line'] or '（无心声）'}"
                f"（至 {until_text}）"
            )

        if status["generating"]:
            lines.append("  正在后台生成……")
        if status["last_error"]:
            lines.append(f"  上次生成失败：{status['last_error']}")

        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("today")
    async def handle_today(self) -> tuple[bool, str]:
        """列出今天的全部时段。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        schedule = await service.get_schedule()
        if schedule is None or schedule.is_empty:
            await self._reply("今天还没有日程，可用 /日程 重生成 生成一份。")
            return True, "empty"

        # 时间以 time_sense 为准（并顺手在那里立一个时间戳）：
        # 命令显示的「现在」必须和日程系统判定的「现在」是同一刻。
        moment = await service.current_time(touch=True, reason="查看日程")
        now = moment.strftime("%H:%M")
        lines = [f"【{schedule.date} 日程】"]
        for entry in schedule.entries:
            mark = "▶" if entry.start <= now < (entry.end or "23:59") else " "
            busy = "※" if entry.busy else " "
            span = f"{entry.start}-{entry.end or '??:??'}"
            lines.append(f"{mark}{busy} {span} {entry.doing}")
        if schedule.yield_lines:
            lines.append("心声：" + " / ".join(schedule.yield_lines[:3]))
        lines.append("（▶ 当前 ※ 忙碌）")
        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("regenerate")
    async def handle_regenerate(self) -> tuple[bool, str]:
        """强制重新生成今天的日程（后台执行）。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        if not service.request_generation(force=True):
            await self._reply("已有一次生成在进行中，稍等一下。")
            return True, "busy"

        await self._reply("好，我去重新安排一下今天……生成完就能用。")
        return True, "ok"

    @cmd_route("persona")
    async def handle_persona(self) -> tuple[bool, str]:
        """查看人设类型判定结果（缓存）。"""
        profile = await store.load_persona_profile()
        if profile is None:
            await self._reply(
                "还没有做过人设判定。生成过一次日程后就有了（/日程 重生成）。"
            )
            return True, "empty"

        kind = "扮演已有角色" if profile.is_roleplay else "原创角色（原创 OC）"
        lines = [
            "【人设判定】",
            f"  类型：{kind}",
            f"  角色：{profile.character_name or '-'}",
        ]
        if profile.source_work:
            lines.append(f"  出处：{profile.source_work}")
        if profile.world:
            lines.append(f"  世界观：{profile.world}")
        if profile.occupation:
            lines.append(f"  平日身份：{profile.occupation}")
        if profile.anchors:
            lines.append("  日常锚点：" + "、".join(profile.anchors))
        lines.append(f"  判定时间：{self._format_time(profile.checked_at)}")
        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("yield")
    async def handle_yield(self) -> tuple[bool, str]:
        """手动进入让位状态（验证文案用）。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        changed = await service.mark_yield(
            level=PermissionLevel.OWNER,
            chat_type=None,
            master_name="主人",
            stream_id=self.stream_id,
        )
        if not changed:
            await self._reply("让位未生效（配置里可能关掉了让位，或范围不含当前会话）。")
            return False, "not applied"

        status = await service.status()
        await self._reply(f"（{status['yield_line'] or '好，先陪你。'}）")
        return True, "ok"

    @cmd_route("unyield")
    async def handle_unyield(self) -> tuple[bool, str]:
        """结束让位状态。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        await service.clear_yield()
        await self._reply("嗯，我回去接着做我的事了。")
        return True, "ok"

    @cmd_route("log")
    async def handle_log(self) -> tuple[bool, str]:
        """列出最近几天的生成记录。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        days = await service.recent_days(5)
        if not days:
            await self._reply("还没有任何生成记录。")
            return True, "empty"

        lines = ["【最近记录】"]
        for day in days:
            payload = await store.load_log(day) or {}
            entries = payload.get("entries")
            count = len(entries) if isinstance(entries, list) else 0
            model_tag = str(payload.get("model_tag") or "-")
            summary = str(payload.get("yesterday_summary") or "").strip()
            events = payload.get("events")
            event_count = len(events) if isinstance(events, list) else 0
            lines.append(f"  {day}：{count} 条 · {model_tag} · 事件 {event_count}")
            if summary:
                lines.append(f"    小结：{summary}")
        await self._reply("\n".join(lines))
        return True, "ok"

    @cmd_route("diary")
    async def handle_diary(self) -> tuple[bool, str]:
        """查看离线生活的日记（它不在线的时候在做什么）。"""
        service = await self._require_service()
        if service is None:
            return False, "service unavailable"

        if not getattr(service.config.offline, "enabled", False):
            await self._reply(
                "离线生活还没开启。\n"
                "在 config/plugins/daily_schedule/config.toml 的 [offline] 里把 enabled "
                "设为 true，重启之后它就会把不在线的那段时间记下来。\n"
                "（需要 time_sense 插件配合）"
            )
            return True, "disabled"

        days = await service.diary_days(3)
        blocks = [day.text_block(with_anchors=True) for day in days if not day.is_empty]
        if not blocks:
            await self._reply(
                "还没有日记。\n"
                "下次它离线超过设定的时长（默认 30 分钟）再启动，就会记上一笔。"
            )
            return True, "empty"

        await self._reply("【离线生活】\n\n" + "\n\n".join(blocks))
        return True, "ok"

    @cmd_route("help")
    async def handle_help(self) -> tuple[bool, str]:
        """显示帮助信息。"""
        await self._reply(_USAGE)
        return True, "help"

    # ── 中文别名路由 ──────────────────────────────────────────────────────────

    @cmd_route("查看")
    async def handle_status_cn(self) -> tuple[bool, str]:
        """查看状态（中文别名）。"""
        return await self.handle_status()

    @cmd_route("全天")
    async def handle_today_cn(self) -> tuple[bool, str]:
        """列出全天时段（中文别名）。"""
        return await self.handle_today()

    @cmd_route("重生成")
    async def handle_regenerate_cn(self) -> tuple[bool, str]:
        """重新生成（中文别名）。"""
        return await self.handle_regenerate()

    @cmd_route("重新生成")
    async def handle_regenerate_cn2(self) -> tuple[bool, str]:
        """重新生成（中文别名二）。"""
        return await self.handle_regenerate()

    @cmd_route("人设")
    async def handle_persona_cn(self) -> tuple[bool, str]:
        """查看人设判定（中文别名）。"""
        return await self.handle_persona()

    @cmd_route("让位")
    async def handle_yield_cn(self) -> tuple[bool, str]:
        """手动让位（中文别名）。"""
        return await self.handle_yield()

    @cmd_route("收回")
    async def handle_unyield_cn(self) -> tuple[bool, str]:
        """结束让位（中文别名）。"""
        return await self.handle_unyield()

    @cmd_route("日志")
    async def handle_log_cn(self) -> tuple[bool, str]:
        """查看日志（中文别名）。"""
        return await self.handle_log()

    @cmd_route("日记")
    async def handle_diary_cn(self) -> tuple[bool, str]:
        """查看日记（中文别名）。"""
        return await self.handle_diary()

    @cmd_route("帮助")
    async def handle_help_cn(self) -> tuple[bool, str]:
        """显示帮助（中文别名）。"""
        return await self.handle_help()


__all__ = ["ScheduleCommand"]
