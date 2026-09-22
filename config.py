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
        reminder_enabled: bool = Field(
            default=True,
            description=(
                "是否额外把这句背景写进 system reminder 的 bucket。\n"
                "DFC / NDFC 会用 with_reminder=\"actor\" 建请求，自动拾取全局与流私有\n"
                "bucket 并包成 <system_reminder> 标签——模型会把它当系统级元指令，\n"
                "而不是一段等着被念出来的旁白（这是背景设定不被背书的关键）。\n"
                "关闭后只走 user prompt 的 extra 注入。"
            ),
        )
        reminder_bucket: str = Field(
            default="actor",
            description=(
                "写入的 reminder bucket 名，需与 chatter 的 with_reminder 一致。\n"
                "DFC 主会话用 actor，决策子代理用 sub_actor。"
            ),
        )
        reminder_name: str = Field(
            default="daily_schedule_scene",
            description="reminder 名称：同名即覆盖写，因此每轮读到的都是当下的场景。",
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
                "（以上只是你此刻的状态背景，不是要你念出来的台词："
                "不要复述这句话本身、不要照搬里面的措辞、"
                "不要主动提起或展开描写；被问到时用你自己的话说个大概就行。）"
            ),
            description=(
                "场景行下方的说明，用来约束主模型把背景当背景——"
                "既不要当成必须执行的剧本，也不要**背书**（照搬到回复里）。\n"
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

    @config_section("offline")
    class OfflineSection(SectionBase):
        """离线生活配置（装了 time_sense 就自动开启）。

        开启后，Bot 每次启动都会拿 time_sense 结算出的离线跨度，
        结合当天日程与记忆服务里的真事，缝出一段「我那时在做什么」，
        按天合并成日记存到 ``data/json_storage/daily_schedule/diary-*.json``。

        ``enabled`` 本身默认 false，但插件加载时会做一次**检测**：
        只要 time_sense 在位，就自动把它打开（见 ``auto_enable_with_time_sense``）。
        于是「装了时间插件」等于「离线生活可用」，不用翻配置；
        反过来，没装 time_sense 时它保持关闭，一次额外的模型调用都不会有。
        """

        enabled: bool = Field(
            default=False,
            description=(
                "是否开启离线生活（把「它不在线的那段时间」记成日记）。\n"
                "默认 false，但检测到 time_sense 时会自动置为 true（见下一项）。\n"
                "想**强制开启**（哪怕没装 time_sense，只记时间事实）：直接设 true。\n"
                "想**强制关闭**：本项设 false，同时把 auto_enable_with_time_sense 也设 false。"
            ),
        )
        auto_enable_with_time_sense: bool = Field(
            default=True,
            description=(
                "检测到 time_sense（时间感知插件）时，是否自动打开离线生活。\n"
                "默认开启——装了时间插件就自动记离线日记，省得用户去翻配置。\n"
                "自动开启只改**运行时**的配置值，不会回写这份 config.toml。\n"
                "要让离线生活始终关闭，就把本项与 enabled 一起设为 false。"
            ),
        )
        min_seconds: int = Field(
            default=1800,
            description=(
                "离线时长达到多少秒才值得记一笔。\n"
                "默认 1800（半小时）：重启几秒钟不该写成「我睡了一觉」。"
            ),
        )
        generate_text: bool = Field(
            default=True,
            description=(
                "是否调用模型把材料缝成第一人称的正文。\n"
                "关闭后只记录「哪段时间不在线」这个事实，不花模型调用。"
            ),
        )
        use_schedule_context: bool = Field(
            default=True,
            description="是否把离开期间原本排的日程段作为材料（让生活节奏连贯）。",
        )
        max_schedule_lines: int = Field(
            default=6,
            description="最多引用几段日程。跨天离线时会取「离开那天」与「回来那天」各一部分。",
        )
        use_memory: bool = Field(
            default=True,
            description="是否检索记忆服务里的真事作为材料（允许日记提到主人与过去的事）。",
        )
        memory_top_k: int = Field(
            default=5,
            description="记忆检索最多取几条。",
        )
        memory_query: str = Field(
            default="",
            description=(
                "记忆检索关键词，可留空使用默认值。\n"
                "可用占位符：{day} 日期、{period} 时刻、{doing} 当时原本在做的事。"
            ),
        )
        history_days: int = Field(
            default=2,
            description=(
                "回喂前几天的日记作为延续材料。\n"
                "这是「延续」的关键：写下来的过去会成为下一次生成的输入。"
            ),
        )
        keep_days: int = Field(
            default=30,
            description="日记保留天数，超出即回收；设为 0 表示永久保留。",
        )
        inject_enabled: bool = Field(
            default=False,
            description=(
                "是否把最近的日记注入对话提示词。\n"
                "开启后主模型才知道「它不在的时候做了什么」，被问起时不会前后矛盾。"
            ),
        )
        inject_days: int = Field(
            default=1,
            description="注入最近几天的日记（1 表示只给最近一篇）。",
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    model: ModelSection = Field(default_factory=ModelSection)
    source: SourceSection = Field(default_factory=SourceSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    scene: SceneSection = Field(default_factory=SceneSection)
    log: LogSection = Field(default_factory=LogSection)
    offline: OfflineSection = Field(default_factory=OfflineSection)


__all__ = ["DailyScheduleConfig"]
