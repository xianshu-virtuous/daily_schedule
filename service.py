"""daily_schedule 服务组件。

对外的唯一入口，承担：

- 读取（必要时后台生成）当天日程；
- 装配注入文本供事件处理器使用（拆成全局段与本流让位段）；
- 处理「主人到场」的让位状态；
- 供 ``/日程`` 命令查询与手动重生成。

生成一律走后台任务（``task_manager``）：注入发生在 prompt 构建链路里，
绝不能在那一轮等待模型生成，否则会拖慢回复。当轮先不注入，下一轮生效。

时间只认 time_sense：本插件所有「现在几点」都走
:meth:`ScheduleService.current_time`，它读 time_sense 的时间事实。
日程侧与时间侧各自 ``datetime.now()`` 迟早会错开（跨天判定、注入的当前时段
都会跟着歪），所以这里不留第二套时钟；time_sense 缺席时退回系统时间，
功能不中断。

离线生活（``offline.enabled``，默认关闭）：启动时拿 time_sense 结算出的
离线跨度，结合当天日程与记忆服务缝成第一人称的日记，按天合并存盘，
见 :mod:`diary`。

日程来源有两种模式（``schedule.mode``）：``pool`` 从日程池抽取（默认，
见 :mod:`pool`），``daily`` 每天让模型写一份。池子不可用时回退到 daily。

注意：框架的 ``service_api.get_service()`` 每次调用都会 **新建** 一个服务实例
（非单例）。所以跨调用者共享的东西只能放两处——要么落在类属性上（进程内共享），
要么落盘到 ``store``（跨进程、跨重启共享）。日程、日志、让位状态都在盘上；
「正在生成」这个并发闸门则必须是类属性，放实例上等于没有。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.app.plugin_system.api import service_api
from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BaseService
from src.app.plugin_system.types import PermissionLevel
from src.kernel.concurrency import get_task_manager

from . import diary, generator, plan as plan_module, pool as pool_module, scene, store
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

#: 日程来源模式：日程池（池子抽取，默认）。
_MODE_POOL = "pool"

#: 日程来源模式：每天让模型生成一份（老模式，已加防重复约束）。
_MODE_DAILY = "daily"

#: time_sense 服务签名。日程的时间一律以它为准——它是本进程里「真实时间」的
#: 唯一权威，还负责在启动时结算离线跨度。两处各算一次时间，迟早会对不上。
_TIME_SENSE_SIGNATURE = "time_sense:service:time_sense"

#: 同一签名的公开别名——别的模块（比如 plugin 里的自动检测）不必再抄一遍字符串。
TIME_SENSE_SIGNATURE = _TIME_SENSE_SIGNATURE

#: 本进程的时间基准（本模块被导入的时刻）。用来判断 time_sense 报出的
#: ``boot_at`` 是不是「本次进程启动之后」的，见 ``wait_time_sense_settled``。
_PROCESS_START_TS = time.time()

#: 等 time_sense 完成本次启动结算的最长秒数。超时后按当前值结算，
#: 由 diary 的按时刻去重兜住重复。
_OFFLINE_BOOT_WAIT_SECONDS = 120.0

#: 日志表格各列的最大显示宽度（时段 / 忙 / 在做什么）。
#: 总宽约 91 列，宽一点的终端里不会折行，窄终端里也只折一次。
_TABLE_TIME_WIDTH = 15
_TABLE_BUSY_WIDTH = 6
_TABLE_DOING_WIDTH = 60


@dataclass
class SceneTexts:
    """一次注入装配的结果：全局段 + 本流让位段。

    Attributes:
        base: 写全局 reminder / 兜底 extra 的文本（不含让位心声）。
        stream: 只写给触发让位那个会话的让位行；不需要时为空。
    """

    base: str = ""
    stream: str = ""

    @property
    def full(self) -> str:
        """合并形态（兼容只认一份文本的调用方）。"""
        if self.base and self.stream:
            return f"{self.base}\n{self.stream}"
        return self.base or self.stream


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
    - ``injection_texts``：同上，但拆成「全局段 + 本流让位段」，供按流注入使用
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

    #: 刷池中的闸门（同样必须是类属性）。刷池要跑好几次模型调用，
    #: 比生成一份日程慢得多，所以它与生成闸门分开：抽取（快、不调模型）
    #: 不该被刷池挡住。
    _refreshing_pool: bool = False
    _refreshing_since: float = 0.0

    #: 规划层的闸门：年 / 月 / 周程的生成也要走后台（可能连着几次调用）。
    _planning: bool = False
    _planning_since: float = 0.0

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

    # ── 时间源（time_sense） ─────────────────────────────────────────────────

    def _sense(self) -> Any | None:
        """取 time_sense 服务。

        Returns:
            服务实例；未安装、未启用或查询失败时返回 ``None``。
        """
        try:
            service = service_api.get_service(_TIME_SENSE_SIGNATURE)
        except Exception as error:  # noqa: BLE001 - 服务查询失败按不可用处理
            logger.debug(f"[daily_schedule] 查询 time_sense 失败: {error}")
            return None
        return service if service is not None else None

    def time_sense_available(self) -> bool:
        """time_sense 是否在位可用（检测机制用）。

        与 :meth:`current_time` 的区别：这里只回答「在不在」，不读时间、不落盘。

        判据是服务实例存在，**且**具备 ``now_snapshot`` 与 ``offline_span`` 两个方法
        ——按能力认，不按牌子认：万一有别的东西占了同一个签名，能力不对也不认。

        Returns:
            可用返回 True。
        """
        service = self._sense()
        if service is None:
            return False
        return callable(getattr(service, "now_snapshot", None)) and callable(
            getattr(service, "offline_span", None)
        )

    async def current_time(
        self, *, touch: bool = False, reason: str = ""
    ) -> datetime:
        """取当前时间，优先以 time_sense 为准。

        这是本插件唯一的时间入口。time_sense 是时间事实的权威，日程侧不再
        自己 ``datetime.now()``——两套时钟在跨天那一刻必然分岔。

        形态是异步的：调用点全在 ``async`` 方法里，而 ``touch`` 需要 await，
        同步签名会逼每个调用方自己拆成两段。

        Args:
            touch: 是否顺手建立一次「当前时间戳」。用户主动询问日程时用，
                这样 time_sense 记下的时刻与这次询问严格对应。
            reason: 建立时间戳的原因（写进 time_sense 的状态文件，便于排查）。

        Returns:
            当前时间；拿不到 time_sense 时退回系统时间。
        """
        service = self._sense()
        if service is None:
            return datetime.now()

        if touch:
            touch_method = getattr(service, "touch", None)
            if callable(touch_method):
                try:
                    await touch_method(reason=reason or "询问日程", force=True)
                except Exception as error:  # noqa: BLE001 - 打点失败不影响读时间
                    logger.debug(f"[daily_schedule] 建立时间戳失败: {error}")

        snapshot_method = getattr(service, "now_snapshot", None)
        if not callable(snapshot_method):
            return datetime.now()

        try:
            snapshot = snapshot_method()
        except Exception as error:  # noqa: BLE001 - 读时间失败退回系统时间
            logger.debug(f"[daily_schedule] 读取 time_sense 时间失败: {error}")
            return datetime.now()

        raw = snapshot.get("timestamp") if isinstance(snapshot, dict) else getattr(
            snapshot, "timestamp", None
        )
        try:
            value = float(raw)
        except (TypeError, ValueError):
            return datetime.now()
        if value <= 0:
            return datetime.now()
        return datetime.fromtimestamp(value)

    async def wait_time_sense_settled(self, timeout: float | None = None) -> bool:
        """等 time_sense 完成本次启动的离线结算。

        time_sense 的启动结算与本次结算都是异步任务，谁先谁后不确定。若在它
        结算前读 ``offline_span()``，读到的是**上一轮**启动的时间事实——那段
        离线会被重复记一笔，或者记成一段早就发生过的跨度。

        同进程内可以直接判断：time_sense 结算后会把自己的启动时刻写进
        ``boot_at``，而早于本进程启动的 ``boot_at`` 必定来自上一轮。

        Args:
            timeout: 最长等待秒数，默认 ``_OFFLINE_BOOT_WAIT_SECONDS``。

        Returns:
            本次启动的结算已完成返回 True；服务不可用或超时返回 False。
        """
        service = self._sense()
        if service is None:
            return False

        span_method = getattr(service, "offline_span", None)
        if not callable(span_method):
            return False

        budget = _OFFLINE_BOOT_WAIT_SECONDS if timeout is None else max(0.0, timeout)
        deadline = time.time() + budget

        while True:
            try:
                span = await span_method()
            except Exception as error:  # noqa: BLE001 - 读失败退避重试
                logger.debug(f"[daily_schedule] 等 time_sense 结算时读取失败: {error}")
                span = None

            if isinstance(span, dict):
                try:
                    boot_ts = float(span.get("boot_at") or 0.0)
                except (TypeError, ValueError):
                    boot_ts = 0.0
                if boot_ts >= _PROCESS_START_TS:
                    return True

            if time.time() >= deadline:
                return False
            await asyncio.sleep(0.5)

    async def offline_settlement(self) -> dict[str, Any]:
        """启动结算：把「它不在线的这段时间」记成日记。

        由插件在启动流程里作为后台任务调用。这里只做编排——读跨度、
        交给 :mod:`diary` 决定记不记、记成什么——判断逻辑都在 diary 里。

        Returns:
            结算摘要（日志与排查用）：``settled`` 是否写入，
            ``reason`` 未写入的原因，``span`` 时间锚，``entry`` 写入的条目。
        """
        config = self.config
        result: dict[str, Any] = {"settled": False, "reason": ""}

        if not getattr(config.offline, "enabled", False):
            result["reason"] = "offline.enabled 未开启"
            return result

        service = self._sense()
        if service is None:
            result["reason"] = "time_sense 不可用"
            logger.warning(
                "[daily_schedule] 离线生活已开启，但 time_sense 不可用，跳过本次结算"
            )
            return result

        # 先等它把本次启动的离线跨度结算出来，否则读到的是上一轮的时间事实
        if not await self.wait_time_sense_settled():
            logger.warning(
                f"[daily_schedule] 等 time_sense 启动结算超时"
                f"（{_OFFLINE_BOOT_WAIT_SECONDS:.0f}s），按当前值结算"
            )

        span_method = getattr(service, "offline_span", None)
        if not callable(span_method):
            result["reason"] = "time_sense 缺少 offline_span"
            logger.warning(
                "[daily_schedule] time_sense 版本不匹配（无 offline_span），跳过离线结算"
            )
            return result

        try:
            span = await span_method()
        except Exception as error:  # noqa: BLE001 - 结算失败不该影响启动
            result["reason"] = f"读取离线跨度失败: {error}"
            logger.warning(f"[daily_schedule] 读取离线跨度失败: {error}")
            return result

        result["span"] = span

        # 随机评估：离线回来时，对「这周 / 这月正在推进的事」掷一次骰
        # （八成顺利、两成卡住）。结果落盘进计划文件，所以同一条不会重掷。
        moment_now = await self.current_time()
        plan_text = await self._plan_block(moment_now)
        outcome_lines = ""
        outcome_items: list[str] = []
        try:
            state = await store.load_state()
            today = moment_now.date().isoformat()
            if getattr(state, "last_roll_date", "") == today:
                logger.debug("[daily_schedule] 今天已经评估过了，跳过随机评估")
            else:
                plans = await plan_module.load_current_chain(moment_now)
                outcomes = plan_module.roll_progress(
                    self.config, plans, moment=moment_now
                )
                if outcomes:
                    await plan_module.advance_progress(plans, outcomes)
                    outcome_lines = plan_module.outcome_block(outcomes)
                    outcome_items = [
                        f"{item.text}：{'顺利' if progress == 'ok' else '卡住了'}"
                        for item, progress in outcomes
                    ]
                    state.last_roll_date = today
                    await store.save_state(state)
                    logger.info("[daily_schedule] 随机评估：" + "；".join(outcome_items))
        except Exception as error:  # noqa: BLE001 - 评估失败不该影响日记
            logger.warning(f"[daily_schedule] 随机评估失败: {error}")

        try:
            entry = await diary.record_offline(
                config,
                span,
                plan_block=plan_text,
                outcome_lines=outcome_lines,
                outcomes=outcome_items,
            )
        except Exception as error:  # noqa: BLE001 - 日记失败不该影响启动
            result["reason"] = f"记录日记失败: {error}"
            logger.warning(f"[daily_schedule] 记录离线日记失败: {error}")
            return result

        if entry is None:
            result["reason"] = "未达记录条件"
            return result

        result["settled"] = True
        result["entry"] = entry.to_dict()
        return result

    async def diary_days(self, limit: int = 3) -> list[Any]:
        """取最近几天的日记（命令展示用）。

        Args:
            limit: 天数上限。

        Returns:
            日记列表（倒序）；没有记录时返回空列表。
        """
        return await diary.recent_days(limit)

    async def diary_injection(self) -> str:
        """取要注入对话的日记文本（**限次**，不是每轮都发）。

        只有 ``offline.enabled`` 与 ``offline.inject_enabled`` 同时开启时才有内容：
        主模型得先知道「它不在的时候做了什么」，被问起时才不会前后矛盾。

        但实测这块约 400 字，是每轮注入里最大的那一份（占 82%）。每轮都发等于按请求数
        重复付费，而它要传达的信息只需要被模型知道一次——之后对话历史里已经有了。
        所以按 ``offline.inject_turns`` 限次：每次记完日记，计数器置为该值，
        每注入一次减一，减到 0 就不再发（填 0 表示退回"每轮都发"的旧行为）。

        Returns:
            注入文本；不需要注入时返回空字符串。
        """
        config = self.config
        if not getattr(config.offline, "enabled", False):
            return ""
        if not getattr(config.offline, "inject_enabled", False):
            return ""

        turns = max(0, int(getattr(config.offline, "inject_turns", 3)))
        state = None
        if turns > 0:
            state = await store.load_state()
            if state.diary_inject_left <= 0:
                return ""

        days = await diary.recent_days(max(1, int(config.offline.inject_days)))
        blocks = [day.text_block() for day in days if not day.is_empty]
        if not blocks:
            return ""
        block = "【它不在线的时候（真实发生过的，可自然提起）】\n" + "\n\n".join(blocks)

        limit = max(0, int(getattr(config.offline, "inject_max_chars", 600)))
        if limit and len(block) > limit:
            block = block[:limit].rstrip() + "…"

        if state is not None:
            # 这一次算用掉一轮：写回计数器，用完就不再打扰上下文
            state.diary_inject_left = max(0, state.diary_inject_left - 1)
            await store.save_state(state)

        return block

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

    @classmethod
    def is_refreshing_pool(cls) -> bool:
        """当前是否有一次刷池正在进行（进程内）。"""
        if not cls._refreshing_pool:
            return False
        if time.time() - cls._refreshing_since >= _GENERATION_STALE_SECONDS:
            return False
        return True

    @classmethod
    def _claim_pool_refresh(cls) -> bool:
        """抢占刷池闸门。"""
        if cls.is_refreshing_pool():
            return False
        cls._refreshing_pool = True
        cls._refreshing_since = time.time()
        return True

    @classmethod
    def _release_pool_refresh(cls) -> None:
        """释放刷池闸门。"""
        cls._refreshing_pool = False
        cls._refreshing_since = 0.0

    @classmethod
    def is_planning(cls) -> bool:
        """当前是否在生成规划（年 / 月 / 周程）。"""
        if not cls._planning:
            return False
        if time.time() - cls._planning_since >= _GENERATION_STALE_SECONDS:
            return False
        return True

    @classmethod
    def _claim_planning(cls) -> bool:
        """抢占规划闸门。"""
        if cls.is_planning():
            return False
        cls._planning = True
        cls._planning_since = time.time()
        return True

    @classmethod
    def _release_planning(cls) -> None:
        """释放规划闸门。"""
        cls._planning = False
        cls._planning_since = 0.0

    # ── 三层规划（年 / 月 / 周程） ────────────────────────────────────────────

    async def current_plans(
        self, moment: datetime | None = None
    ) -> dict[str, Any]:
        """只读地取当前三层规划（不触发生成）。"""
        return await plan_module.load_current_chain(moment)

    async def _plan_block(self, moment: datetime) -> str:
        """取喂给日程层 / 日记层的规划文本（配置关掉时为空）。"""
        if not bool(getattr(self.config.plan, "feed_schedule", True)):
            return ""
        return plan_module.plan_block(await plan_module.load_current_chain(moment))

    def request_planning(self, *, force: bool = False) -> bool:
        """把「补齐三层规划」排进后台。

        规划最长要跑三次调用（年 / 月 / 周各一次），绝不能卡在对话链路或启动报告上；
        池子模式下如果型池已就绪，多数时候是零调用的。

        Args:
            force: 是否强制重生成三层（``/日程 规划 重生成`` 用）。

        Returns:
            是否成功排队（已有一次在跑时返回 ``False``）。
        """
        if not bool(getattr(self.config.plan, "enabled", True)):
            return False
        if not self._claim_planning():
            return False

        async def _job() -> None:
            try:
                moment = await self.current_time()
                await plan_module.ensure_chain(
                    self.config, self.plugin, now=moment, force=force
                )
                # 规划一变，今天的日程也该跟着重排一次（抽取不花钱）
                if self.mode() == _MODE_POOL:
                    existing = await store.load_schedule(moment.date().isoformat())
                    if existing is not None and not existing.is_empty:
                        await self._compose_from_pool(moment, force=True)
            except Exception as error:  # noqa: BLE001 - 规划失败不影响对话
                logger.warning(f"[daily_schedule] 生成规划失败: {error}")
            finally:
                self._release_planning()

        try:
            get_task_manager().create_task(
                _job(), name="daily_schedule.planning", group_name="daily_schedule"
            )
        except Exception as error:  # noqa: BLE001 - 排队失败按未排队处理
            logger.warning(f"[daily_schedule] 排队规划任务失败: {error}")
            self._release_planning()
            return False
        return True

    async def _ensure_plans(self, moment: datetime) -> bool:
        """三层规划有缺失或过期就排一次后台补齐。

        Returns:
            三层都齐（有效期内）返回 True。
        """
        if not bool(getattr(self.config.plan, "enabled", True)):
            return True
        plans = await plan_module.load_current_chain(moment)
        stale = [
            name
            for name, item in plans.items()
            if item is None or item.is_expired(moment.timestamp())
        ]
        if not stale:
            return True
        logger.info(
            "[daily_schedule] 规划需要补齐："
            + "、".join(
                plan_module.PLAN_LAYER_LABELS.get(name, name) for name in stale
            )
        )
        self.request_planning()
        return False

    # ── 模式 ──────────────────────────────────────────────────────────────────

    def mode(self) -> str:
        """当前日程来源模式（``pool`` / ``daily``）。

        配置写错时按 ``pool`` 处理，并在日志里说一次——池子有 daily 回退，
        所以往池子这边靠是更安全的一侧。

        Returns:
            模式字符串。
        """
        raw = str(getattr(self.config.schedule, "mode", _MODE_POOL) or "").strip().lower()
        if raw not in (_MODE_POOL, _MODE_DAILY):
            logger.warning(
                f"[daily_schedule] schedule.mode 配置非法: {raw!r}，按 {_MODE_POOL} 处理"
            )
            return _MODE_POOL
        return raw

    async def _compose_from_pool(
        self, moment: datetime, *, force: bool = False
    ) -> DailySchedule | None:
        """从池子里抽一套今天的日程（不调用模型）。

        Args:
            moment: 参考时间。
            force: 是否换一套（``/日程 重生成`` 用：避开今天已经抽过的日型）。

        Returns:
            抽取出的日程并已落盘；池子不可用时返回 ``None``。
        """
        pool = await store.load_pool()
        snapshot = read_persona()
        fingerprint = snapshot.fingerprint if snapshot is not None else ""

        if not pool_module.pool_is_usable(
            pool, fingerprint=fingerprint, now_ts=moment.timestamp()
        ):
            if pool is not None:
                if pool.is_expired(moment.timestamp()):
                    logger.info("[daily_schedule] 池子已过期，需要重刷")
                elif fingerprint and pool.persona_fingerprint != fingerprint:
                    logger.info("[daily_schedule] 人设变了，池子需要重刷")
            return None

        assert pool is not None  # pool_is_usable 已保证
        recent = await store.recent_schedules(
            max(1, int(getattr(self.config.pool, "avoid_recent_days", 7)))
        )

        exclude: list[str] = []
        if force:
            for item in recent:
                if item.date == moment.date().isoformat() and item.archetype:
                    exclude.append(item.archetype)

        # 周程给今天的重点：命中日型标签就优先用它（字符串匹配，不花调用）
        focus_text = ""
        if bool(getattr(self.config.plan, "feed_schedule", True)):
            plans = await plan_module.load_current_chain(moment)
            focus = plan_module.today_focus(plans, moment)
            if focus is not None:
                focus_text = focus.text
                logger.debug(f"[daily_schedule] 周程今天偏重：{focus.text}")

        schedule = pool_module.compose_day(
            pool,
            moment=moment,
            recent=recent,
            exclude_keys=exclude,
            focus_text=focus_text,
        )
        if schedule is None:
            logger.warning("[daily_schedule] 池子里没有可用日型，抽不出今天的日程")
            return None

        await store.save_schedule(schedule)
        await generator.record_schedule_log(
            schedule, log_enabled=bool(self.config.log.enabled)
        )
        logger.info(
            f"[daily_schedule] 已从池子抽取 {schedule.date} 日程："
            f"日型 {schedule.archetype}（{len(schedule.entries)} 段）"
        )
        return schedule

    def request_pool_refresh(self, *, force: bool = False) -> bool:
        """把一次刷池排进后台，立刻返回。

        刷池要跑好几分钟（每个日型一次模型调用），绝不能卡在对话链路上；
        编好一个日型就落盘一次，所以中途重启不会白编。

        Args:
            force: 是否无视现有池子直接重编（``/日程 刷池`` 用）。

        Returns:
            是否成功排队（已有刷池在跑时返回 ``False``）。
        """
        if self.mode() != _MODE_POOL:
            return False
        if not self._claim_pool_refresh():
            logger.info("[daily_schedule] 已有一次刷池在进行，跳过这次请求")
            return False

        async def _job() -> None:
            try:
                moment = await self.current_time()
                plan_text = await self._plan_block(moment)
                pool = await pool_module.refresh_pool(
                    self.config,
                    self.plugin,
                    now=moment,
                    force=force,
                    plan_block=plan_text,
                )
                if pool is None:
                    return
                # 刷完顺手看一眼今天有没有日程：刚装好的那天正好补上
                existing = await store.load_schedule(moment.date().isoformat())
                if existing is None or existing.is_empty:
                    await self.ensure_today(now=moment)
            except Exception as error:  # noqa: BLE001 - 刷池失败不影响对话
                logger.warning(f"[daily_schedule] 刷池失败: {error}")
            finally:
                self._release_pool_refresh()

        try:
            get_task_manager().create_task(
                _job(), name="daily_schedule.refresh_pool", group_name="daily_schedule"
            )
        except Exception as error:  # noqa: BLE001 - 排队失败按未排队处理
            logger.warning(f"[daily_schedule] 排队刷池任务失败: {error}")
            self._release_pool_refresh()
            return False
        return True

    async def _ensure_pool_fresh(self, moment: datetime) -> bool:
        """池子失效就排一次刷池（巡检与启动都会调）。

        Args:
            moment: 参考时间。

        Returns:
            池子当前可用返回 True；需要刷池（已排队）返回 False。
        """
        if self.mode() != _MODE_POOL:
            return True

        pool = await store.load_pool()
        snapshot = read_persona()
        fingerprint = snapshot.fingerprint if snapshot is not None else ""
        if pool_module.pool_is_usable(
            pool, fingerprint=fingerprint, now_ts=moment.timestamp()
        ):
            return True

        if pool is None:
            reason = "还没有池子"
        elif pool.is_expired(moment.timestamp()):
            reason = "池子已过期"
        elif fingerprint and pool.persona_fingerprint != fingerprint:
            reason = "人设变了"
        else:
            reason = "池子不可用"
        logger.info(f"[daily_schedule] {reason}，排队刷池")
        self.request_pool_refresh()
        return False

    async def _generate(
        self, moment: datetime, *, force: bool = False
    ) -> DailySchedule | None:
        """按模式产出当天日程（调用方需已持有生成闸门）。

        - ``pool``：先从池子抽（不调模型）；抽不出来就排一次刷池，并按配置回退 daily；
        - ``daily``：直接让模型写一份（回喂里的「已用过」措辞已改成要求避开）。

        Args:
            moment: 参考时间。
            force: 是否强制重来（池子模式下表示换一套日型）。

        Returns:
            生成的日程；失败返回 ``None``。
        """
        if self.mode() == _MODE_POOL:
            schedule = await self._compose_from_pool(moment, force=force)
            if schedule is not None:
                await self.log_today(now=moment, prefix="抽取完成 · ")
                return schedule

            self.request_pool_refresh()
            if not bool(getattr(self.config.pool, "fallback_to_daily", True)):
                logger.warning(
                    "[daily_schedule] 池子不可用，且 pool.fallback_to_daily 已关闭，"
                    "今天没有日程（等池子编好即可）"
                )
                return None
            logger.info("[daily_schedule] 池子不可用，回退到 daily 模式生成今天")

        return await self._generate_daily(moment)

    async def _generate_daily(self, moment: datetime) -> DailySchedule | None:
        """每天让模型写一份日程（老模式，也是池子不可用时的兜底）。

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
                self.config,
                self.plugin,
                snapshot,
                profile,
                now=moment,
                plan_block=await self._plan_block(moment),
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
        moment = now or await self.current_time()
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

        moment = now or await self.current_time()
        day = moment.date().isoformat()

        # 池子模式：启动时顺手看一眼池子在不在（首次安装就在这一刻开始编）
        await self._ensure_pool_fresh(moment)
        # 规划层：年 / 月 / 周程缺哪层补哪层（后台）
        await self._ensure_plans(moment)

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
        moment = now or await self.current_time()
        day = moment.date().isoformat()

        if not force:
            existing = await store.load_schedule(day)
            if not self._is_stale(existing, moment):
                return existing

        if not self._claim_generation():
            logger.info("[daily_schedule] 已有一次生成在进行，跳过这次请求")
            return await store.load_schedule(day)

        try:
            return await self._generate(moment, force=force)
        finally:
            self._release_generation()

    def request_generation(self, *, force: bool = False) -> bool:
        """把一次生成排进后台，立刻返回。

        Args:
            force: 是否强制重新生成（池子模式下表示换一套日型）。

        Returns:
            是否成功排队（已有生成在跑时返回 ``False``）。
        """
        if not self._claim_generation():
            return False

        async def _job() -> None:
            moment = await self.current_time()
            try:
                await self._generate(moment, force=force)
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
        moment = now or await self.current_time()
        if day and day != moment.date().isoformat():
            # 只支持重生成今天：日程的「此刻状态」语义只对当天成立
            logger.warning("[daily_schedule] 仅支持重新生成当天日程")
            return None
        return await self.ensure_today(now=moment, force=True)

    async def prewarm_tick(self) -> None:
        """预生成巡检：到点且当天还没有日程时生成一次。

        池子模式下多一步：先看池子在不在有效期内，不在就排一次刷池；
        今天还没日程就顺手抽一套（抽取不调模型，很快）。

        由统一调度器按固定间隔调用，因此进程重启后同样能自愈。
        """
        config = self.config
        if not config.plugin.enabled:
            return

        moment = await self.current_time()
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

        await self._ensure_pool_fresh(moment)
        await self._ensure_plans(moment)

        existing = await store.load_schedule(day)
        if not self._is_stale(existing, moment):
            logger.info(f"[daily_schedule] 巡检 · {day} 日程正常（{len(existing.entries)} 段）")
            return

        logger.info(f"[daily_schedule] 巡检 · {day} 日程缺失或过期，开始生成")
        schedule = await self.ensure_today(now=moment)
        if schedule is None and not self.is_generating():
            logger.warning(f"[daily_schedule] 巡检 · {day} 生成未成功，下次巡检再试")

    # ── 注入装配 ──────────────────────────────────────────────────────────────

    async def get_injection(
        self, *, now: datetime | None = None, stream_id: str | None = None
    ) -> str:
        """取当前应注入的场景文本（合并形态，兼容旧调用方）。

        当天日程缺失或过期时，只在允许按需生成的情况下排一次后台生成，
        本轮不注入（避免阻塞），下一轮自然生效。

        Args:
            now: 参考时间。
            stream_id: 当前构建的这个会话；传了才能按流隔离让位。

        Returns:
            注入文本；无需注入时返回空字符串。
        """
        texts = await self.injection_texts(now=now, stream_id=stream_id)
        return texts.full

    async def injection_texts(
        self, *, now: datetime | None = None, stream_id: str | None = None
    ) -> "SceneTexts":
        """取当前应注入的场景文本，拆成「全局」与「本流让位」两段。

        为什么要拆：日程是 Bot 自己的状态（所有会话一致，写全局 bucket），
        而让位是「主人在**这个**会话开口」引起的——旧版把它也写进全局 bucket，
        于是主人在 A 群说句话，B 群的场景行也会变成「我更想听你说」。

        拆分规则（``scene.yield_stream_scope`` 打开时）：

        - ``base``：不含让位心声的场景行 + 说明，写全局 bucket；
        - ``stream``：让位行（不含说明），只写给 ``state.yield_stream_id`` 那个流。

        让位期间还会顺手去掉 ``busy_suffix``（「手上正忙」与「这些先放一放」
        不能同时进上下文）。关掉 ``yield_stream_scope`` 即退回旧行为：
        让位行合并进 ``base``、写全局 bucket。

        不知道 stream_id 的调用方（老 chatter）、盘上没记下触发流（旧版状态文件）、
        或没开隔离时，拿到的仍是旧的合并形态——所以 ``get_injection()`` 的语义没有变化。

        Args:
            now: 参考时间。
            stream_id: 当前构建的会话 ID，可为空。

        Returns:
            :class:`SceneTexts`；无需注入时两段都为空。
        """
        config = self.config
        if not config.plugin.enabled or not config.scene.enabled:
            return SceneTexts()

        moment = now or await self.current_time()
        schedule = await store.load_schedule(moment.date().isoformat())
        state = await store.load_state()

        if schedule is not None and schedule.date != moment.date().isoformat():
            # 跨天残留：昨天的日程不能拿来描述今天
            schedule = None

        if self._is_stale(schedule, moment) and config.schedule.generate_on_demand:
            # 只在后台补生成，本轮继续用现有日程（过期但仍是今天的，说了也比空着好）
            if self.request_generation():
                logger.info("[daily_schedule] 当天日程缺失或过期，已排队后台生成")

        scoped = bool(getattr(config.scene, "yield_stream_scope", True))
        yielding = state.is_yielding(moment.timestamp())
        stream_known = bool(str(stream_id or "").strip())
        # 让位是谁引起的：老版本落盘的状态里没有这个字段，消息也可能不带 stream_id。
        # 认不出「是哪个流」时不做隔离——宁可照旧全局注入，也别让它哪都不出现。
        stored_stream = (state.yield_stream_id or "").strip()

        # 让位心声进 base 的三种情况：没开流隔离（旧行为）、这次构建不知道是哪个流
        # （老 chatter 不往 values 里塞 stream_id）、或盘上没记下触发流。
        base_allows_yield = yielding and (not scoped or not stream_known or not stored_stream)

        base = scene.build_injection(
            config,
            schedule,
            state,
            moment,
            allow_yield=base_allows_yield,
            busy_suffix=not (scoped and yielding),
        )

        stream_text = ""
        if yielding and scoped and stream_known and stored_stream == str(stream_id).strip():
            stream_text = scene.build_yield_line(config, schedule, state, moment)

        # 日记接在场景行之后：场景说明「此刻」，日记补上「它不在的时候」，
        # 两者都是真实材料，主模型被问起时才接得上话。
        diary_block = await self.diary_injection()
        if diary_block:
            base = f"{base}\n{diary_block}" if base else diary_block

        # 可选：把周程也注入（默认关——规划只在生成侧起作用，不占每轮 token）
        if bool(getattr(config.plan, "inject_in_chat", False)):
            plans = await plan_module.load_current_chain(moment)
            week = plans.get("week")
            if week is not None and week.items:
                focus = plan_module.today_focus(plans, moment)
                head = f"【这周：{week.label}】"
                if focus is not None:
                    head += f"今天偏重：{focus.text}。"
                base = f"{base}\n{head}" if base else head

        if config.plugin.debug_log:
            logger.debug(
                f"[daily_schedule] 注入内容（stream={stream_id or '-'}）：\n"
                f"{base}\n{stream_text}"
            )
        return SceneTexts(base=base, stream=stream_text)

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

        moment = now or await self.current_time()
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
        # 用户主动询问日程：顺手在 time_sense 立一个时间戳，让「它何时被问起」
        # 与「它回答时以为的现在」严格是同一刻，也顺带确认它读到的确实是这个时刻。
        moment = now or await self.current_time(touch=True, reason="查看日程")
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

        pool = await store.load_pool() if self.mode() == _MODE_POOL else None

        return {
            "date": day,
            "mode": self.mode(),
            "has_schedule": schedule is not None and not schedule.is_empty,
            "entries": len(schedule.entries) if schedule else 0,
            "generated_at": schedule.generated_at if schedule else 0.0,
            "model_tag": schedule.model_tag if schedule else "",
            "persona_kind": schedule.persona_kind if schedule else "",
            "persona_name": schedule.persona_name if schedule else "",
            "sources_used": schedule.sources_used if schedule else [],
            "archetype": schedule.archetype if schedule else "",
            "doing": doing,
            "busy": busy,
            "next": next_entry,
            "yielding": state.is_yielding(moment.timestamp()),
            "yield_line": state.yield_line,
            "yield_master": state.yield_master,
            "yield_until": state.yield_until,
            "generating": self.is_generating(),
            "refreshing_pool": self.is_refreshing_pool(),
            "planning": self.is_planning(),
            "last_error": state.last_error,
            "offline_enabled": bool(getattr(self.config.offline, "enabled", False)),
            "diary_dates": await store.recent_diary_dates(7),
            "pool_id": pool.pool_id if pool else "",
            "pool_expires_at": pool.refresh_at if pool else 0.0,
        }

    async def pool_lines(self) -> list[str]:
        """取池子状态的多行展示文本（命令用）。

        Returns:
            文本行列表。
        """
        if self.mode() != _MODE_POOL:
            return ["  当前模式是 daily（每天生成一份），没有池子。"]
        return pool_module.summary_lines(await store.load_pool())

    async def plan_lines(self, moment: datetime | None = None) -> list[str]:
        """取三层规划的多行展示文本（命令用）。

        Args:
            moment: 参考时间。

        Returns:
            文本行列表。
        """
        config = self.config
        if not bool(getattr(config.plan, "enabled", True)):
            return ["  规划层已关闭（plan.enabled = false）"]

        now = moment or await self.current_time()
        plans = await plan_module.load_current_chain(now)
        pool_lines: dict[str, list[str]] = {}
        for layer in plan_module.PLAN_LAYERS:
            mapping = getattr(config.plan, f"{layer}_mode", None)
            mode = str(mapping if mapping is not None else "pool")
            pool = await store.load_plan_pool(layer)
            if pool is None:
                pool_lines[layer] = [f"型池：无（现在按 {mode} 模式直生成）"]
                continue
            expire = (
                datetime.fromtimestamp(pool.refresh_at).strftime("%m-%d")
                if pool.refresh_at
                else "-"
            )
            pool_lines[layer] = [
                f"型池：{len(pool.archetypes)} 套，到期 {expire}"
                f"（模式 {mode}）"
            ]
        return plan_module.summary_lines(plans, pool_lines=pool_lines)

    async def recent_days(self, limit: int = 3) -> list[str]:
        """列出最近有日志的日期。

        Args:
            limit: 条数上限。

        Returns:
            日期字符串列表（倒序）。
        """
        return await store.recent_log_dates(limit)

    def _quick_now(self) -> datetime:
        """同步取当前时间：优先 time_sense 的快照，取不到就用系统时间。

        ``current_time`` 是异步接口（要求 touch 时得落盘），但个别调用点本身
        是**同步**方法（如 ``next_day_switch``），不能 await。这里退而求其次：
        只读一次快照，不做任何写盘。快照本身是同步方法，所以安全。

        Returns:
            当前时间。
        """
        sense = self._sense()
        snapshot_method = getattr(sense, "now_snapshot", None)
        if callable(snapshot_method):
            try:
                snapshot = snapshot_method()
                stamp = float(snapshot["timestamp"])
                if stamp > 0:
                    return datetime.fromtimestamp(stamp)
            except Exception:  # noqa: BLE001 - 快照不可用则回落系统时间
                pass
        return datetime.now()

    def next_day_switch(self, now: datetime | None = None) -> datetime:
        """取次日零点时间（命令展示跨天倒计时用）。

        Args:
            now: 参考时间。

        Returns:
            次日零点。
        """
        moment = now or self._quick_now()
        return datetime.combine(
            moment.date() + timedelta(days=1), datetime.min.time()
        )


__all__ = ["SceneTexts", "ScheduleService"]
