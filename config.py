"""daily_schedule 插件配置。

配置文件默认路径：``config/plugins/daily_schedule/config.toml``。

设计约定：

- 素材按「人设 → 记忆 → 互联网」顺序获取，任意一层不可用都只跳过该层，不报错；
- 模型调用默认使用主回复模型（``model.task_name = "actor"``），也可用
  ``model.model_name`` 直接点名某个模型；
- 注入内容只说明「此刻在做什么」，措辞由 ``scene.note`` 约束，不强制场景、不强调。
"""

from __future__ import annotations

from typing import ClassVar

from src.app.plugin_system.base import BaseConfig, Field, SectionBase, config_section


class DailyScheduleConfig(BaseConfig):
    """日程系统插件配置模型。"""

    name: ClassVar[str] = "config"
    description: ClassVar[str] = "日程系统插件配置"

    @config_section("plugin")
    class PluginSection(SectionBase):
        """插件基础配置。"""

        enabled: bool = Field(
            default=True,
            description="是否启用日程系统插件（false 时注入与自动生成全部停止）",
        )
        debug_log: bool = Field(
            default=False,
            description=(
                "是否输出调试日志。\n"
                "开启后会记录素材收集摘要、模型原始返回、注入到 extra 的最终文本。"
            ),
        )
        target_prompts: list[str] = Field(
            default_factory=lambda: [
                "default_chatter_user_prompt",
                "neo_default_chatter_user_prompt",
            ],
            description=(
                "允许注入场景提示的 user prompt 模板名列表。\n"
                "默认同时兼容 default_chatter 与 neo_default_chatter，"
                "只有当前实际启用的 chatter 会真正命中。\n"
                "示例：target_prompts = [\"default_chatter_user_prompt\"]"
            ),
        )

    @config_section("model")
    class ModelSection(SectionBase):
        """生成日程所用的模型配置。"""

        task_name: str = Field(
            default="actor",
            description=(
                "模型任务名（config/model.toml 的 [model_tasks.*]）。\n"
                "默认 actor，即主回复模型。"
            ),
        )
        model_name: str = Field(
            default="",
            description=(
                "直接指定模型名（config/model.toml 中 [[models]].name）。\n"
                "非空时优先于 task_name；留空则使用 task_name 对应的模型任务。"
            ),
        )
        temperature: float = Field(
            default=0.85,
            description="生成日程的温度。判断人设类型时内部会另行压低温度。",
        )
        max_tokens: int = Field(
            default=2000,
            description="单次生成的输出上限。日程条目越多、越详细，需要留的余量越大。",
        )

    @config_section("source")
    class SourceSection(SectionBase):
        """素材来源配置（按 人设 → 记忆 → 互联网 顺序获取）。"""

        use_persona: bool = Field(
            default=True,
            description=(
                "是否使用人设（config/core.toml 的 [personality]）作为第一层素材。\n"
                "关闭后日程将失去角色依据，通常不建议关闭。"
            ),
        )
        use_memory: bool = Field(
            default=True,
            description=(
                "是否使用记忆系统作为第二层素材。\n"
                "未安装记忆插件或检索失败时自动跳过，不影响生成。"
            ),
        )
        memory_top_k: int = Field(
            default=4,
            description="记忆检索返回条数。",
        )
        memory_service_signature: str = Field(
            default="booku_memory:service:booku_memory",
            description=(
                "记忆服务组件签名。\n"
                "默认对接 booku_memory；填错或未安装时该层自动跳过。"
            ),
        )
        memory_query: str = Field(
            default="日常 作息 习惯 喜欢做的事",
            description="记忆检索关键词，用于捞取与「这个角色平常做什么」相关的记忆。",
        )
        use_internet: bool = Field(
            default=True,
            description=(
                "是否使用互联网作为第三层素材。\n"
                "开启后会主动探测可用的 MCP 搜索工具，探测不到则跳过该层。"
            ),
        )
        search_tool_signature: str = Field(
            default="",
            description=(
                "搜索工具组件签名，例如 mcp_provider:tool:mcp-ddgg-search。\n"
                "留空表示自动探测（优先匹配名字中带 search 的 mcp_provider 工具）。"
            ),
        )
        search_query: str = Field(
            default="{character} {occupation} 日常 作息 一天",
            description=(
                "搜索关键词模板，可用占位符：{character} 角色名、{occupation} 身份职业、"
                "{work} 出处作品。"
            ),
        )
        search_max_chars: int = Field(
            default=600,
            description="搜索结果进入提示词前截断的最大字符数，避免吃掉过多上下文。",
        )

    @config_section("schedule")
    class ScheduleSection(SectionBase):
        """日程生成策略。"""

        prewarm_time: str = Field(
            default="00:05",
            description=(
                "每天预生成当天日程的时刻，格式 HH:MM（24 小时制，本地时间）。\n"
                "留空字符串表示关闭定时预生成，仅在对话需要时惰性生成。"
            ),
        )
        check_interval_seconds: int = Field(
            default=1800,
            description=(
                "预生成巡检的间隔（秒），最小 30。\n"
                "巡检只是兜底：启动时已经会立刻查一次并写日志，对话中缺日程也会按需补，\n"
                "所以这里保持低频即可；做测试想快点看到巡检日志可填 60。"
            ),
        )
        generate_on_demand: bool = Field(
            default=True,
            description=(
                "当天日程缺失时，是否在对话需要时后台补生成。\n"
                "补生成不阻塞当轮注入（当轮先不注入，下一轮生效）。"
            ),
        )
        lookback_days: int = Field(
            default=2,
            description="生成时回喂前几天的日程与日志，用于保持生活连续性。",
        )
        refresh_if_older_hours: float = Field(
            default=20.0,
            description=(
                "已生成日程超过该小时数即视为过期并重新生成。\n"
                "用于兜底长时间运行的跨天场景；设为 0 表示不按年龄刷新。"
            ),
        )
        min_entries: int = Field(
            default=6,
            description="要求模型给出的日程条目数下限，低于该值视为生成失败。",
        )
        max_entries: int = Field(
            default=14,
            description="日程条目数上限，超出部分会被截断。",
        )

    @config_section("scene")
    class SceneSection(SectionBase):
        """场景提示与让位配置。

        让位（yield_* 字段）与场景提示同属「注入给主模型的此刻状态」，
        因此放在同一节：主人开口时，把场景行换成让位心声。
        """

        enabled: bool = Field(
            default=True,
            description="是否向对话注入「此刻在做什么」的场景提示。",
        )
        template: str = Field(
            default="（你此刻正在：{doing}）",
            description=(
                "场景行模板，可用占位符：{doing} 此刻在做的事、{busy} 忙碌等级数字、"
                "{busy_label} 忙碌等级文字、{hint} 忙碌时的反应倾向。"
            ),
        )
        busy_suffix: str = Field(
            default="，手上正忙，被搭话也可以说等会儿",
            description=(
                "忙碌等级达到 busy_min_level 时追加到场景行末尾的文本。\n"
                "留空表示不追加。这只是「可以推脱」的许可，不是强制拒绝。"
            ),
        )
        busy_min_level: int = Field(
            default=1,
            description="从哪个忙碌等级开始追加 busy_suffix（0=闲，1=有点忙，2=很忙）。",
        )
        hint_template: str = Field(
            default="（{hint}）",
            description="日程条目自带反应倾向时的附加模板，留空表示不附加。",
        )
        note: str = Field(
            default=(
                "（这只是你此刻的状态背景。被问到时自然说起就行，"
                "不必主动提起，也不必展开描写。）"
            ),
            description=(
                "场景行下方的说明，用来约束主模型不要把场景当成必须执行的剧本。\n"
                "留空表示不加说明。"
            ),
        )
        fallback: str = Field(
            default="",
            description=(
                "当天没有可用日程时的兜底注入文本。\n"
                "留空表示宁可不注入，也不硬编一个场景。"
            ),
        )
        yield_enabled: bool = Field(
            default=True,
            description="主人开口时，是否把日程时间让出来。",
        )
        yield_min_level: str = Field(
            default="owner",
            description=(
                "视为「主人」的最低权限等级：owner / operator / user / guest。\n"
                "由权限系统（permission_api）判定，判定失败时按「不是主人」处理。"
            ),
        )
        yield_scope: str = Field(
            default="all",
            description=(
                "让位生效范围：all 表示私聊与群聊都认；private 表示只在私聊让位。"
            ),
        )
        yield_idle_minutes: int = Field(
            default=30,
            description=(
                "主人静默多久后恢复原本的日程。\n"
                "设为 0 表示让位状态一直持续到当天日程结束。"
            ),
        )
        yield_template: str = Field(
            default="（{line}）",
            description=(
                "让位时的场景行模板，可用占位符：{line} 让位心声、{doing} 原本在做的事、"
                "{master} 主人称呼。\n"
                "让位心声由生成日程时的模型一并产出，语气是「为了你而放下」，"
                "而不是「规则要求我让出时间」。"
            ),
        )

    @config_section("log")
    class LogSection(SectionBase):
        """日志配置。"""

        enabled: bool = Field(
            default=True,
            description=(
                "是否把每次生成的日程写入日志（data/json_storage/daily_schedule/）。\n"
                "日志同时作为下次生成的回喂素材。"
            ),
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    model: ModelSection = Field(default_factory=ModelSection)
    source: SourceSection = Field(default_factory=SourceSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    scene: SceneSection = Field(default_factory=SceneSection)
    log: LogSection = Field(default_factory=LogSection)


__all__ = ["DailyScheduleConfig"]
