"""daily_schedule 服务组件。

对外的唯一入口，承担：

- 读取（必要时后台生成）当天日程；
- 装配注入文本供事件处理器使用；
- 处理「主人到场」的让位状态；
- 供 ``/日程`` 命令查询与手动重生成。

生成一律走后台任务（``task_manager``）：注入发生在 prompt 构建链路里，
绝不能在那一轮等待模型生成，否则会拖慢回复。当轮先不注入，下一轮生效。

注意：框架的 ``service_api.get_service()`` 每次调用都会 **新建** 一个服务实例
（非单例）。所以跨调用者共享的东西只能放两处——要么落在类属性上（进程内共享），
要么落盘到 ``store``（跨进程、跨重启共享）。日程、日志、让位状态都在盘上；
「正在生成」这个并发闸门则必须是类属性，放实例上等于没有。
"""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseService
from src.app.plugin_system.types import PermissionLevel
from src.kernel.concurrency import get_task_manager

from . import generator, scene, store
from .config import DailyScheduleConfig
from .models import BUSY_LABELS, DailySchedule, RuntimeState
from .persona import read_persona
from .table import render_box

logger = get_logger("daily_schedule.service")

#: 预生成巡检任务的调度器任务名。
_SCHEDULER_TASK_NAME = "daily_schedule_prewarm"

#: 预生成巡检间隔（秒）。用「定期巡检 + 当天是否已生成」实现每日预生成，
#: 这样进程重启后也能自愈，不依赖精确到分钟的触发时刻。
_PREWARM_TICK_SECONDS = 1800

#: 「正在生成」标记的有效期（秒）。超过这个时长仍为生成中，视作上一次任务
#: 异常中断留下的残标，允许重新抢占，避免生成能力被永久锁死。
_GENERATION_STALE_SECONDS = 900

#: 日志表格各列的最大显示宽度（时段 / 忙 / 在做什么）。
#: 总宽约 91 列，宽一点的终端里不会折行，窄终端里也只折一次。
_TABLE_TIME_WIDTH = 15
_TABLE_BUSY_WIDTH = 6
_TABLE_DOING_WIDTH = 60


def _required_level(text: str) -> PermissionLevel:
    """解析配置里的权限等级字符串。

    Args:
        text: 等级字符串（owner / operator / user / guest）。

    Returns:
        权限等级；配置非法时退回 ``OWNER``（宁可不让位，也不误认主人）。
    """
    try:
        return PermissionLevel.from_string(text)
    except ValueError:
        logger.warning(
            f"[daily_schedule] scene.yield_min_level 配置非法: {text!r}，退回 owner"
        )
        return PermissionLevel.OWNER


class ScheduleService(BaseService):
    """日程服务。

    对外方法：

    - ``get_schedule``：取某天日程（不触发生成）
    - ``get_injection``：取当前应注入的场景文本（可能顺手排队一次后台生成）
    - ``regenerate``：强制重新生成
    - ``mark_yield``：主人到场，把日程时间让出来
    - ``status``：状态摘要（命令与排查用）
    """

    name: str = "schedule"
    description: str = "日程生成、场景提示装配与主人让位状态服务"

    #: 生成中的进程内闸门。框架每次 get_service() 都新建实例，
    #: 所以这里的互斥必须挂在类上，实例属性起不到任何作用。
    _generating: bool = False
    _generating_since: float = 0.0

    def __init__(self, plugin: Any) -> None:
        """初始化服务。

        Args:
            plugin: 所属插件实例。
        """
        super().__init__(plugin)

    # ── 基础访问 ──────────────────────────────────────────────────────────────

    @property
    def config(self) -> DailyScheduleConfig:
        """插件配置。"""
        return self.plugin.config

    def _today(self, now: datetime | None = None) -> str:
        """取日期字符串。

        Args:
            now: 参考时间，默认当前时间。

        Returns:
            ``YYYY-MM-DD``。
        """
        return (now or datetime.now()).date().isoformat()

    async def get_schedule(
        self, day: str | None = None, *, now: datetime | None = None
    ) -> DailySchedule | None:
        """读取指定日期的日程（不触发生成）。

        Args:
            day: 日期字符串，默认今天。
            now: 参考时间。

        Returns:
            日程实例；不存在时返回 ``None``。
        """
        return await store.load_schedule(day or self._today(now))

    def _is_stale(self, schedule: DailySchedule | None, now: datetime) -> bool:
        """判断日程是否需要重新生成。

        Args:
            schedule: 现有日程。
            now: 当前时间。

        Returns:
            是否需要重新生成。
        """
        if schedule is None or schedule.is_empty:
            return True
        if schedule.date != now.date().isoformat():
            return True

        hours = self.config.schedule.refresh_if_older_hours
        if hours > 0 and schedule.generated_at > 0:
            age_hours = (now.timestamp() - schedule.generated_at) / 3600.0
            if age_hours >= hours:
                return True
        return False

    # ── 生成调度 ──────────────────────────────────────────────────────────────

    @classmethod
    def is_generating(cls) -> bool:
        """当前是否有一次生成正在进行（进程内）。"""
        if not cls._generating:
            return False
        if time.time() - cls._generating_since >= _GENERATION_STALE_SECONDS:
            # 残标：上一次任务没能正常收尾，当作没有在生成
            return False
        return True

    @classmethod
    def _claim_generation(cls) -> bool:
        """抢占生成闸门。

        Returns:
            抢到返回 ``True``；已有生成在跑返回 ``False``。
        """
        if cls.is_generating():
            return False
        cls._generating = True
        cls._generating_since = time.time()
        return True

    @classmethod
    def _release_generation(cls) -> None:
        """释放生成闸门。"""
        cls._generating = False
        cls._generating_since = 0.0

    async def _generate(self, moment: datetime) -> DailySchedule | None:
        """真正去生成当天日程（调用方需已持有生成闸门）。

        Args:
            moment: 参考时间。

        Returns:
            生成的日程；失败返回 ``None``。
        """
        try:
            snapshot = read_persona()
            if not snapshot.is_usable:
                logger.warning("[daily_schedule] 人设不可用，跳过日程生成")
                return None

            profile = await generator.ensure_persona_profile(self.config, snapshot)
            schedule = await generator.generate_daily_schedule(
                self.config, self.plugin, snapshot, profile, now=moment
            )
        except Exception as error:  # noqa: BLE001 - 生成失败不应中断对话
            logger.error(f"[daily_schedule] 日程生成异常: {error}")
            return None

        if schedule is not None:
            # 生成完就把「今天干什么」摊开写进日志，主人一眼能看到 bot 的一天
            await self.log_today(now=moment, prefix="生成完成 · ")
        return schedule

    # ── 日程概览（日志用） ────────────────────────────────────────────────────

    @staticmethod
    def _hhmm(timestamp: float) -> str:
        """把时间戳格式化成 ``HH:MM``。

        Args:
            timestamp: Unix 时间戳。

        Returns:
            文本；时间戳非正数时返回 ``未知``。
        """
        if timestamp <= 0:
            return "未知"
        return datetime.fromtimestamp(timestamp).strftime("%H:%M")

    def render_day(self, schedule: DailySchedule, moment: datetime) -> list[str]:
        """把当天日程渲染成可读的多行文本。

        Args:
            schedule: 当天日程。
            moment: 参考时间（用于标出当前时段）。

        Returns:
            行列表：首行为概要，其后每行一个时段，当前时段带 ``▶``。
        """
        lines = [
            f"{schedule.date} 日程：{len(schedule.entries)} 段"
            f" ｜ 生成 {self._hhmm(schedule.generated_at)}"
            f" ｜ 素材 {','.join(schedule.sources_used) or '无'}"
            f" ｜ 模型 {schedule.model_tag or '未知'}"
        ]
        current = moment.strftime("%H:%M")
        for entry in schedule.entries:
            end = entry.end or "??:??"
            mark = "▶" if entry.start <= current < end else " "
            busy = f"（{BUSY_LABELS.get(entry.busy, '')}）" if entry.busy else ""
            lines.append(f"{mark} {entry.start}-{end} {entry.doing}{busy}")
        return lines

    def render_table(self, schedule: DailySchedule, moment: datetime) -> str:
        """把当天日程渲染成一张框线表格（写日志用）。

        当前时段整行加粗，控制台里一眼能看到「它此刻在做什么」。

        Args:
            schedule: 当天日程。
            moment: 参考时间。

        Returns:
            多行表格文本。
        """
        current = moment.strftime("%H:%M")
        rows: list[list[str]] = []
        styles: list[str | None] = []
        for entry in schedule.entries:
            end = entry.end or "??:??"
            is_now = entry.start <= current < end
            rows.append(
                [
                    f"{'▶' if is_now else ' '} {entry.start}-{end}",
                    BUSY_LABELS.get(entry.busy, ""),
                    entry.doing,
                ]
            )
            styles.append("bold" if is_now else None)
        return render_box(
            ["时段", "忙", "在做什么"],
            rows,
            caps=[_TABLE_TIME_WIDTH, _TABLE_BUSY_WIDTH, _TABLE_DOING_WIDTH],
            row_styles=styles,
        )

    async def log_today(
        self, *, now: datetime | None = None, prefix: str = ""
    ) -> list[str]:
        """把当天日程写进日志。

        Args:
            now: 参考时间。
            prefix: 加在概要行前的说明（如「启动加载 ·」）。

        Returns:
            打印过的行；当天没有日程时返回空列表。
        """
        moment = now or datetime.now()
        schedule = await self.get_schedule(now=moment)
        if schedule is None or schedule.is_empty or schedule.date != moment.date().isoformat():
            return []

        lines = self.render_day(schedule, moment)
        logger.info(f"[daily_schedule] {prefix}{lines[0]}")
        logger.info(self.render_table(schedule, moment))

        state = await store.load_state()
        if state.is_yielding(moment.timestamp()):
            logger.info(
                f"[daily_schedule]   让位中：{state.yield_line or '（无心声）'}"
                f"（至 {self._hhmm(state.yield_until) if state.yield_until < 4102444800 else '今天结束'}）"
            )
        return lines

    async def startup_report(self, *, now: datetime | None = None) -> str:
        """启动时把「当前日程」打到日志里，没有则排一次生成。

        Args:
            now: 参考时间。

        Returns:
            概要行；当天没有日程时返回空字符串。
        """
        config = self.config
        if not config.plugin.enabled:
            return ""

        moment = now or datetime.now()
        day = moment.date().isoformat()
        schedule = await self.get_schedule(now=moment)
        if schedule is not None and schedule.date != day:
            schedule = None

        if schedule is None or schedule.is_empty:
            prewarm = config.schedule.prewarm_time.strip()
            hint = (
                f"将在 {prewarm} 之后自动生成"
                if prewarm
                else "需要手动执行 /日程 重生成"
            )
            logger.info(f"[daily_schedule] 今天（{day}）还没有日程，{hint}")
            if config.schedule.generate_on_demand:
                if self.request_generation():
                    logger.info("[daily_schedule] 已排队生成今天的日程，生成完会打印完整安排")
                else:
                    logger.info("[daily_schedule] 今天的日程正在生成，生成完会打印完整安排")
            return ""

        await self.log_today(now=moment, prefix="启动加载 · ")

        if self._is_stale(schedule, moment) and config.schedule.generate_on_demand:
            if self.request_generation():
                logger.info("[daily_schedule] 今天的日程已过期，已排队重新生成")
        return self.render_day(schedule, moment)[0]

    async def ensure_today(
        self, *, now: datetime | None = None, force: bool = False
    ) -> DailySchedule | None:
        """确保当天有可用日程（必要时同步生成）。

        这是「真的去生成」的入口，耗时较长，调用方应放在后台任务里执行。

        Args:
            now: 参考时间。
            force: 是否无视现有日程强制重新生成。

        Returns:
            生成或已存在的日程；失败返回 ``None``。已有生成在跑时，
            直接返回现有日程（不排队、不等待）。
        """
        moment = now or datetime.now()
        day = moment.date().isoformat()

        if not force:
            existing = await store.load_schedule(day)
            if not self._is_stale(existing, moment):
                return existing

        if not self._claim_generation():
            logger.info("[daily_schedule] 已有一次生成在进行，跳过这次请求")
            return await store.load_schedule(day)

        try:
            return await self._generate(moment)
        finally:
            self._release_generation()

    def request_generation(self, *, force: bool = False) -> bool:
        """把一次生成排进后台，立刻返回。

        Args:
            force: 是否强制重新生成。

        Returns:
            是否成功排队（已有生成在跑时返回 ``False``）。
        """
        if not self._claim_generation():
            return False

        async def _job() -> None:
            try:
                await self._generate(datetime.now())
            finally:
                self._release_generation()

        try:
            get_task_manager().create_task(
                _job(), name="daily_schedule.generate", group_name="daily_schedule"
            )
        except Exception as error:  # noqa: BLE001 - 排队失败按未排队处理
            logger.warning(f"[daily_schedule] 排队生成任务失败: {error}")
            self._release_generation()
            return False
        return True

    async def regenerate(
        self, *, day: str | None = None, now: datetime | None = None
    ) -> DailySchedule | None:
        """强制重新生成某天的日程。

        Args:
            day: 目标日期，默认今天。
            now: 参考时间。

        Returns:
            新生成的日程；失败返回 ``None``。
        """
        moment = now or datetime.now()
        if day and day != moment.date().isoformat():
            # 只支持重生成今天：日程的「此刻状态」语义只对当天成立
            logger.warning("[daily_schedule] 仅支持重新生成当天日程")
            return None
        return await self.ensure_today(now=moment, force=True)

    async def prewarm_tick(self) -> None:
        """预生成巡检：到点且当天还没有日程时生成一次。

        由统一调度器按固定间隔调用，因此进程重启后同样能自愈。
        """
        config = self.config
        if not config.plugin.enabled:
            return

        moment = datetime.now()
        day = moment.date().isoformat()

        prewarm = config.schedule.prewarm_time.strip()
        if not prewarm:
            logger.info(f"[daily_schedule] 巡检 · {day} 未配置预生成时刻，跳过")
            return

        try:
            hour_str, minute_str = prewarm.split(":", 1)
            target = moment.replace(
                hour=int(hour_str), minute=int(minute_str), second=0, microsecond=0
            )
        except (ValueError, TypeError):
            logger.warning(f"[daily_schedule] prewarm_time 格式非法: {prewarm!r}")
            return

        if moment < target:
            logger.info(
                f"[daily_schedule] 巡检 · {moment.strftime('%H:%M')} 未到预生成时刻（{prewarm}）"
            )
            return

        existing = await store.load_schedule(day)
        if not self._is_stale(existing, moment):
            logger.info(f"[daily_schedule] 巡检 · {day} 日程正常（{len(existing.entries)} 段）")
            return

        logger.info(f"[daily_schedule] 巡检 · {day} 日程缺失或过期，开始生成")
        schedule = await self.ensure_today(now=moment)
        if schedule is None and not self.is_generating():
            logger.warning(f"[daily_schedule] 巡检 · {day} 生成未成功，下次巡检再试")

    # ── 注入装配 ──────────────────────────────────────────────────────────────

    async def get_injection(self, *, now: datetime | None = None) -> str:
        """取当前应注入的场景文本。

        当天日程缺失或过期时，只在允许按需生成的情况下排一次后台生成，
        本轮不注入（避免阻塞），下一轮自然生效。

        Args:
            now: 参考时间。

        Returns:
            注入文本；无需注入时返回空字符串。
        """
        config = self.config
        if not config.plugin.enabled or not config.scene.enabled:
            return ""

        moment = now or datetime.now()
        schedule = await store.load_schedule(moment.date().isoformat())
        state = await store.load_state()

        if schedule is not None and schedule.date != moment.date().isoformat():
            # 跨天残留：昨天的日程不能拿来描述今天
            schedule = None

        if self._is_stale(schedule, moment) and config.schedule.generate_on_demand:
            # 只在后台补生成，本轮继续用现有日程（过期但仍是今天的，说了也比空着好）
            if self.request_generation():
                logger.info("[daily_schedule] 当天日程缺失或过期，已排队后台生成")

        injection = scene.build_injection(config, schedule, state, moment)
        if config.plugin.debug_log:
            logger.debug(f"[daily_schedule] 注入内容：\n{injection}")
        return injection

    # ── 让位 ──────────────────────────────────────────────────────────────────

    async def mark_yield(
        self,
        *,
        level: PermissionLevel | None,
        chat_type: Any = None,
        master_name: str = "",
        stream_id: str = "",
        now: datetime | None = None,
    ) -> bool:
        """记录「主人到场」，把日程时间让出来。

        让位不是规则弹窗，而是角色自己愿意的表现：这里只负责记状态，
        具体措辞由 :mod:`scene` 从日程自带的心声池里取。

        Args:
            level: 发信人的权限等级。
            chat_type: 聊天类型（私聊 / 群聊）。
            master_name: 主人的显示名。
            stream_id: 聊天流 ID。
            now: 参考时间。

        Returns:
            是否真的进入了让位状态。
        """
        config = self.config
        if not config.plugin.enabled or not config.scene.yield_enabled:
            return False
        if level is None:
            return False

        required = _required_level(config.scene.yield_min_level)
        if level < required:
            return False

        if config.scene.yield_scope.strip().lower() == "private":
            chat_type_text = str(getattr(chat_type, "value", chat_type) or "").lower()
            if "private" not in chat_type_text:
                return False

        moment = now or datetime.now()
        state = await store.load_state()

        idle_minutes = max(0, int(config.scene.yield_idle_minutes))
        schedule = await store.load_schedule(moment.date().isoformat())
        doing, _ = scene.describe_now(schedule, moment)

        was_yielding = state.is_yielding(moment.timestamp())
        if idle_minutes > 0:
            state.yield_until = moment.timestamp() + idle_minutes * 60
        else:
            # 0 表示让位持续到当天结束（次日零点）
            state.yield_until = self.next_day_switch(moment).timestamp()
        state.yield_doing = doing
        state.yield_stream_id = stream_id or state.yield_stream_id
        state.yield_master = master_name or state.yield_master

        if not was_yielding:
            # 新一段让位：重新抽一条心声，让每段让位听起来都不一样
            state.yield_line = scene.pick_yield_line(config, schedule, RuntimeState())
            detail = f"让位：{state.yield_master or '主人'} 在 {moment.strftime('%H:%M')} 开口"
            if doing:
                detail += f"，当时在{doing}"
            await generator.append_log_event(moment.date().isoformat(), detail)
            logger.info(f"[daily_schedule] {detail}")

        await store.save_state(state)
        return True

    async def clear_yield(self) -> None:
        """清掉让位状态（命令用）。"""
        state = await store.load_state()
        state.yield_until = 0.0
        state.yield_line = ""
        await store.save_state(state)

    # ── 状态摘要 ──────────────────────────────────────────────────────────────

    async def status(self, *, now: datetime | None = None) -> dict[str, Any]:
        """汇总当前状态，供命令展示与排查。

        Args:
            now: 参考时间。

        Returns:
            状态字典。
        """
        moment = now or datetime.now()
        day = moment.date().isoformat()
        schedule = await store.load_schedule(day)
        state = await store.load_state()
        doing, busy = scene.describe_now(schedule, moment)

        next_entry: str = ""
        if schedule is not None and schedule.entries:
            current = moment.strftime("%H:%M")
            for entry in schedule.entries:
                if entry.start > current:
                    next_entry = f"{entry.start} {entry.doing}"
                    break

        return {
            "date": day,
            "has_schedule": schedule is not None and not schedule.is_empty,
            "entries": len(schedule.entries) if schedule else 0,
            "generated_at": schedule.generated_at if schedule else 0.0,
            "model_tag": schedule.model_tag if schedule else "",
            "persona_kind": schedule.persona_kind if schedule else "",
            "persona_name": schedule.persona_name if schedule else "",
            "sources_used": schedule.sources_used if schedule else [],
            "doing": doing,
            "busy": busy,
            "next": next_entry,
            "yielding": state.is_yielding(moment.timestamp()),
            "yield_line": state.yield_line,
            "yield_master": state.yield_master,
            "yield_until": state.yield_until,
            "generating": self.is_generating(),
            "last_error": state.last_error,
        }

    async def recent_days(self, limit: int = 3) -> list[str]:
        """列出最近有日志的日期。

        Args:
            limit: 条数上限。

        Returns:
            日期字符串列表（倒序）。
        """
        return await store.recent_log_dates(limit)

    def next_day_switch(self, now: datetime | None = None) -> datetime:
        """取次日零点时间（命令展示跨天倒计时用）。

        Args:
            now: 参考时间。

        Returns:
            次日零点。
        """
        moment = now or datetime.now()
        return datetime.combine(
            moment.date() + timedelta(days=1), datetime.min.time()
        )


__all__ = ["ScheduleService"]
