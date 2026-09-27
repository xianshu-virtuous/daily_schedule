"""daily_schedule 插件入口。

日程系统：让 Bot 有自己的「一天」。

- 素材顺序：人设 → 记忆 → 互联网（有则用，无则跳过）；
- 三层规划（``plan.enabled``，默认开）：年程 → 月程 → 周程，每层围绕上一层生成，
  每层可 ``pool``（编型 + 抽取 + 上层填充，不花调用）或 ``direct``（到期现生成），
  见 :mod:`plan`；缺哪层补哪层，断链不影响日程；
- 日程来源两种模式（``schedule.mode``）：``pool``（默认）每隔几天编几套「日型」、
  每天抽一套（变化靠轮换、不花调用），``daily`` 每天让模型写一份；池子不可用时回退 daily，
  见 :mod:`pool`；周程会给今天点名重点，命中日型标签时优先用它；
- 离线判定（``offline.roll_enabled``）：**只判日程层**——每天一次掷骰，忙碌等级与
  「忙时被打断」都参与；周 / 月 / 年 / 推进项的完成度靠**下级 50% 上卷**算出来，
  全程零模型调用，见 :mod:`progress`；情绪也由最近几天的成功日比例推出；
- 离线日记（``offline.enabled``，**默认关闭**）：启动时把「它不在线的这段时间」结合当天日程
  与记忆缝成第一人称日记，按天存进 ``data``，见 :mod:`diary`；判定不依赖它。
- 生成结果落盘成日志，注入时只提供一句「你此刻正在做什么」的旁白，
  不强制场景、不强调、不追加规则；
- 主人出现时（权限系统判定）把日程时间让出来，措辞取自角色自己的心声池；
  这份让位只写进触发它的那个会话（流私有 reminder），不会串到别的群；
- 时间一律以 time_sense 为准，它缺席时退回系统时间（见 ``service.current_time``）；
  time_sense 是**可选**的运行时探测（manifest 里不写依赖，否则缺它会被框架静默剔除）。
"""

from __future__ import annotations

import asyncio
from typing import Any

from src.app.plugin_system.api.log_api import get_logger
from src.app.plugin_system.base import BasePlugin, register_plugin
from src.kernel.concurrency import get_task_manager
from src.kernel.scheduler import TriggerType, get_unified_scheduler

from .commands import GoalCommand, ScheduleCommand
from .config import DailyScheduleConfig
from .handlers import OwnerPresenceHandler, SceneInjectorHandler
from .service import TIME_SENSE_SIGNATURE, ScheduleService

logger = get_logger("daily_schedule")

#: 预生成巡检的调度器任务名。
_PREWARM_TASK_NAME = "daily_schedule_prewarm"

#: 预生成巡检间隔的下限（秒）。
_MIN_INTERVAL_SECONDS = 30

#: 启动报告等日程服务的重试次数（每次 0.5s）。
_STARTUP_RETRY_TIMES = 60

#: 等「启动画面收尾」的最长时间（秒）。
#:
#: Bot 初始化阶段用 Rich Live 画启动进度条，这期间往控制台打的日志会被进度画面
#: 盖掉（文件日志不受影响）。调度器是在 ``Bot.start()`` 里启动的，那时启动画面
#: 已经退出，所以以「调度器已启动」作为可以往控制台说话的信号。
_BOOT_UI_WAIT_SECONDS = 180


def _scheduler_running(scheduler: Any) -> bool:
    """调度器是否已经启动。

    Args:
        scheduler: 统一调度器实例。

    Returns:
        已启动返回 True；启动前拿不到运行状态时返回 False。
    """
    try:
        statistics = scheduler.get_statistics()
    except Exception:  # noqa: BLE001 - 启动前统计不可用是正常的
        return bool(getattr(scheduler, "_running", False))
    if isinstance(statistics, dict) and "is_running" in statistics:
        return bool(statistics["is_running"])
    return bool(getattr(scheduler, "_running", False))


def _check_interval_seconds(config: Any) -> int:
    """读取巡检间隔。

    Args:
        config: 插件配置。

    Returns:
        巡检间隔秒数；配置缺失或非法时回退到 1800，且不低于 30。
    """
    raw: Any = 1800
    schedule_section = getattr(config, "schedule", None)
    if schedule_section is not None:
        raw = getattr(schedule_section, "check_interval_seconds", raw)
    try:
        seconds = int(raw)
    except (TypeError, ValueError):
        seconds = 1800
    return max(_MIN_INTERVAL_SECONDS, seconds)


@register_plugin
class DailySchedulePlugin(BasePlugin):
    """日程系统插件。"""

    plugin_name: str = "daily_schedule"
    plugin_description: str = (
        "让 Bot 拥有自己的时间：三层规划（年程→月程→周程）定方向、日程池每日轮换抽取、"
        "主人来时只在触发会话让位；判定只落在日程层，完成度按下级 50% 上卷"
    )
    plugin_version: str = "1.2.0"
    configs: list[type] = [DailyScheduleConfig]

    def __init__(self, config: Any = None) -> None:
        """初始化插件。

        Args:
            config: 插件配置实例。
        """
        super().__init__(config)
        self._schedule_ids: list[str] = []
        self._register_task_id: str | None = None
        self._report_task_id: str | None = None
        self._settle_task_id: str | None = None

    def get_components(self) -> list[type]:
        """返回本插件提供的组件。

        插件在配置里被关掉时不注册任何组件，等于整体下线。

        Returns:
            组件类列表。
        """
        if isinstance(self.config, DailyScheduleConfig) and not self.config.plugin.enabled:
            return []
        return [
            ScheduleService,
            SceneInjectorHandler,
            OwnerPresenceHandler,
            ScheduleCommand,
            GoalCommand,
        ]

    # ── 生命周期 ──────────────────────────────────────────────────────────────

    async def on_plugin_loaded(self) -> None:
        """加载后：等调度器就绪注册巡检任务，并把当前日程打到日志里。"""
        if isinstance(self.config, DailyScheduleConfig) and not self.config.plugin.enabled:
            return

        try:
            task_info = get_task_manager().create_task(
                self._startup_report(),
                name="daily_schedule_startup_report",
                daemon=True,
            )
            self._report_task_id = task_info.task_id
        except Exception as error:  # noqa: BLE001 - 报告失败只影响日志
            logger.warning(f"[daily_schedule] 排队启动日程报告失败: {error}")

        if isinstance(self.config, DailyScheduleConfig):
            self._autodetect_offline()

        if isinstance(self.config, DailyScheduleConfig) and self.config.offline.enabled:
            try:
                task_info = get_task_manager().create_task(
                    self._settle_offline_when_ready(),
                    name="daily_schedule_offline_settlement",
                    daemon=True,
                )
                self._settle_task_id = task_info.task_id
            except Exception as error:  # noqa: BLE001 - 失败只影响这一篇日记
                logger.warning(f"[daily_schedule] 排队离线结算失败: {error}")

        if isinstance(self.config, DailyScheduleConfig) and not self.config.schedule.prewarm_time.strip():
            logger.info("[daily_schedule] 未配置 prewarm_time，跳过预生成任务注册")
            return

        try:
            task_info = get_task_manager().create_task(
                self._register_schedule_when_ready(),
                name="daily_schedule_register_schedule",
                daemon=True,
            )
            self._register_task_id = task_info.task_id
        except Exception as error:  # noqa: BLE001 - 注册失败只影响预生成
            logger.warning(f"[daily_schedule] 排队注册预生成任务失败: {error}")

    def _autodetect_offline(self) -> None:
        """检测机制：装了 time_sense 就自动打开离线生活。

        ``offline.enabled`` 默认是 ``false``，理由是保守——没装时间插件的机器上开着它
        只会白跑一遍启动结算。但「明明装了时间插件、还得自己去翻配置打开」又太别扭，
        所以这里补一次检测：**time_sense 在位就自动打开**。

        规则（顺序就是优先级）：

        1. ``offline.enabled`` 已经是 ``true`` → 不动（用户自己开的）；
        2. ``offline.auto_enable_with_time_sense`` 是 ``false`` → 不自动开
           （要让离线生活**始终关闭**，把这两项都设 false）；
        3. time_sense 不在位 → 不自动开（没有时间事实可缝，开了也只剩系统时间）。

        只改**运行时**的配置对象，不回写 ``config.toml``：这是本进程的行为决策，
        不该偷偷改用户的配置文件。代价是「自动开」这件事在配置里看不出来，
        所以打开时会打一条 INFO，说清是谁开的、怎么关。
        """
        if not isinstance(self.config, DailyScheduleConfig):
            return

        offline = self.config.offline
        if offline.enabled:
            return

        if not getattr(offline, "auto_enable_with_time_sense", True):
            logger.debug("[daily_schedule] 离线生活未开启，且自动检测已关")
            return

        from src.app.plugin_system.api import service_api

        try:
            sense = service_api.get_service(TIME_SENSE_SIGNATURE)
        except Exception as error:  # noqa: BLE001 - 探测失败按「不在位」处理
            logger.debug(f"[daily_schedule] 探测 time_sense 失败: {error}")
            sense = None

        if sense is None or not callable(getattr(sense, "now_snapshot", None)):
            logger.info(
                "[daily_schedule] 未检测到 time_sense，离线生活保持关闭"
                "（装上它之后会自动开启）"
            )
            return

        offline.enabled = True
        logger.info(
            "[daily_schedule] 检测到 time_sense，已自动开启离线生活"
            "（想始终关闭：offline.enabled 与 offline.auto_enable_with_time_sense 都设 false）"
        )

    async def on_plugin_unloaded(self) -> None:
        """卸载前：移除巡检任务。

        卸载钩子不该让框架看到异常：调度器可能根本没启动（比如刚加载就卸载），
        这里每一步都各自兜底，失败只记日志。
        """
        schedule_ids = list(self._schedule_ids)
        self._schedule_ids.clear()

        try:
            scheduler = get_unified_scheduler()
        except Exception as error:  # noqa: BLE001 - 调度器不可用时无需清理
            logger.debug(f"[daily_schedule] 调度器不可用，跳过巡检任务清理: {error}")
            scheduler = None

        if scheduler is not None:
            try:
                found = await scheduler.find_schedule_by_name(_PREWARM_TASK_NAME)
            except Exception:  # noqa: BLE001 - 查询失败只影响兜底删除
                found = None
            if found and found not in schedule_ids:
                schedule_ids.append(found)

            for schedule_id in schedule_ids:
                try:
                    await scheduler.remove_schedule(schedule_id)
                except Exception as error:  # noqa: BLE001 - 卸载阶段尽力清理
                    logger.debug(f"[daily_schedule] 移除巡检任务失败: {error}")

        if self._register_task_id:
            try:
                get_task_manager().cancel_task(self._register_task_id)
            except Exception:  # noqa: BLE001 - 任务可能已结束
                pass
            self._register_task_id = None

        if self._report_task_id:
            try:
                get_task_manager().cancel_task(self._report_task_id)
            except Exception:  # noqa: BLE001 - 任务可能已结束
                pass
            self._report_task_id = None

        if self._settle_task_id:
            try:
                get_task_manager().cancel_task(self._settle_task_id)
            except Exception:  # noqa: BLE001 - 任务可能已结束
                pass
            self._settle_task_id = None

    async def _startup_report(self) -> None:
        """加载后把当前日程打到日志里。

        这里先等启动画面收尾，再打报告——否则表格会打在 Rich 启动进度条上被盖掉
        （文件里有、控制台看不到）。随后取服务重试一小段窗口，避免加载顺序或
        启动较慢时报告悄悄消失。
        """
        from src.app.plugin_system.api import service_api

        await self._wait_boot_ui_done()

        for _ in range(_STARTUP_RETRY_TIMES):
            try:
                service = service_api.get_service("daily_schedule:service:schedule")
            except Exception as error:  # noqa: BLE001 - 拿不到就退避重试
                logger.debug(f"[daily_schedule] 启动日程报告取服务失败: {error}")
                service = None

            if isinstance(service, ScheduleService):
                try:
                    await service.startup_report()
                except Exception as error:  # noqa: BLE001 - 报告失败不影响运行
                    logger.warning(f"[daily_schedule] 启动日程报告失败: {error}")
                return

            await asyncio.sleep(0.5)

        waited = _STARTUP_RETRY_TIMES * 0.5
        logger.warning(f"[daily_schedule] 等 {waited:.0f}s 仍没拿到日程服务，跳过启动日程报告")

    async def _wait_boot_ui_done(self) -> bool:
        """等启动进度画面收尾，再往控制台打东西。

        调度器在 ``Bot.start()`` 里启动，那时启动进度条（Rich Live）已经退出；
        启动前打的日志会被进度画面覆盖，所以这里轮询等待。

        Returns:
            等到了返回 True；超时返回 False（此时照样报备，只是可能被启动画面盖住）。
        """
        try:
            scheduler = get_unified_scheduler()
        except Exception as error:  # noqa: BLE001 - 拿不到调度器就别干等
            logger.debug(f"[daily_schedule] 拿不到调度器，直接报备: {error}")
            return False

        for _ in range(_BOOT_UI_WAIT_SECONDS * 2):
            if _scheduler_running(scheduler):
                return True
            await asyncio.sleep(0.5)

        logger.debug(f"[daily_schedule] 等 {_BOOT_UI_WAIT_SECONDS}s 启动画面仍未收尾，直接报备")
        return False

    async def _register_schedule_when_ready(self) -> None:
        """等待调度器就绪后注册周期巡检任务。

        ``scheduler.start()`` 发生在 Bot 运行阶段，插件加载时调度器尚未启动，
        因此这里轮询等待，拿到 ``RuntimeError`` 就退避重试。
        """
        interval = _check_interval_seconds(self.config)
        try:
            scheduler = get_unified_scheduler()
        except Exception as error:  # noqa: BLE001 - 拿不到调度器就别注册
            logger.warning(f"[daily_schedule] 调度器不可用，预生成巡检未注册: {error}")
            return

        for _ in range(600):
            try:
                schedule_id = await scheduler.create_schedule(
                    callback=self._prewarm_job,
                    trigger_type=TriggerType.TIME,
                    trigger_config={"interval_seconds": interval},
                    is_recurring=True,
                    task_name=_PREWARM_TASK_NAME,
                    force_overwrite=True,
                )
            except RuntimeError:
                await asyncio.sleep(0.5)
                continue
            except Exception as error:  # noqa: BLE001 - 反复失败则放弃注册
                logger.warning(f"[daily_schedule] 注册预生成巡检任务失败: {error}")
                await asyncio.sleep(2.0)
                continue

            self._schedule_ids = [schedule_id]
            logger.info(f"[daily_schedule] 巡检已注册（每 {interval}s）")
            return

        logger.warning("[daily_schedule] 等待调度器就绪超时，预生成巡检未注册")

    async def _prewarm_job(self) -> None:
        """调度器回调：巡检并预生成当天日程。"""
        from src.app.plugin_system.api import service_api

        service = service_api.get_service("daily_schedule:service:schedule")
        if not isinstance(service, ScheduleService):
            logger.warning("[daily_schedule] 日程服务不可用，跳过本次预生成巡检")
            return

        try:
            await service.prewarm_tick()
        except Exception as error:  # noqa: BLE001 - 巡检失败不打断调度
            logger.error(f"[daily_schedule] 预生成巡检异常: {error}")

    async def _settle_offline_when_ready(self) -> None:
        """离线生活的启动结算：把「它不在线的时候」记成日记。

        时机放在启动画面收尾之后：结算要读日程、检索记忆、可能调一次模型，
        这期间往控制台打的日志会被启动进度条盖掉。

        这里不负责等 time_sense——它的启动结算同样在异步跑，先后不确定，
        真正的等待在 ``service.offline_settlement()`` 里（靠 time_sense 报出的
        ``boot_at`` 判断本次结算是否已完成）。
        """
        from src.app.plugin_system.api import service_api

        await self._wait_boot_ui_done()

        service: Any = None
        for _ in range(_STARTUP_RETRY_TIMES):
            try:
                service = service_api.get_service("daily_schedule:service:schedule")
            except Exception as error:  # noqa: BLE001 - 拿不到就退避重试
                logger.debug(f"[daily_schedule] 离线结算取服务失败: {error}")
                service = None

            if isinstance(service, ScheduleService):
                break
            service = None
            await asyncio.sleep(0.5)

        if not isinstance(service, ScheduleService):
            logger.warning("[daily_schedule] 等不到日程服务，跳过离线结算")
            return

        try:
            result = await service.offline_settlement()
        except Exception as error:  # noqa: BLE001 - 结算失败不影响启动
            logger.warning(f"[daily_schedule] 离线结算异常: {error}")
            return

        if result.get("settled"):
            entry = result.get("entry") or {}
            anchors = entry.get("anchors") or []
            logger.info(
                f"[daily_schedule] 离线结算已写入"
                f"（{len(anchors)} 项锚点，来源 {entry.get('source') or '-'}）"
            )
        else:
            logger.info(
                f"[daily_schedule] 离线结算未写入：{result.get('reason') or '未达条件'}"
            )


__all__ = ["DailySchedulePlugin"]
