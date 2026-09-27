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
            default=4000,
            description=(
                "单次生成的输出上限。日程条目越多、越详细，需要留的余量越大。\n"
                "注意：task_name（默认 actor）若指向**思考模型**，思考链会先吃掉这份预算，\n"
                "给低了会表现为「空返回」或 JSON 被截断（日志里的 empty response / 无法解析 JSON）\n"
                "——那不是模型写得短，而是正文没输出。这种情况下按模型池里最贵的那档配。"
            ),
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

        mode: str = Field(
            default="pool",
            description=(
                "日程从哪来，两种模式：\n"
                "pool（默认）：**日程池**——每隔 pool.refresh_days 天编几套「日型」"
                "（每种日子一整套骨架 + 每段多个变体），每天从中**抽**一套。\n"
                "  变化来自轮换、连续性来自日记与真实过过的日程，而且每日抽取不花模型调用。\n"
                "daily：老模式——每天让模型把今天写一遍（已加「别照抄昨天骨架」的约束，\n"
                "  但连续几天的重样仍比 pool 明显）。\n"
                "pool 不可用（还没编好/编坏了/过期）时会自动回退到 daily 生成一份，"
                "保证今天一定有日程。"
            ),
        )
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

    @config_section("pool")
    class PoolSection(SectionBase):
        """日程池配置（``schedule.mode = "pool"`` 时生效）。

        池子 = 几套「日型」，每套是一整天的骨架，每个时段带若干变体；
        每天从中抽一套（不调模型），每隔 ``refresh_days`` 天重编一次。

        为什么默认这么设：日型数×变体数决定"多久不重样"（3 套 × 3 变体 ≈ 一周内
        看不出循环）；每次只编一个日型（输出小、不容易被截断），编好一个存一个，
        整池达标才替换——**池子一次写坏就是坏一整周，所以这里比单次生成多设了几道闸**。
        """

        refresh_days: int = Field(
            default=7,
            description=(
                "池子有效期（天）。到期后重新编一套。\n"
                "按周（7）最划算：刚好把日型走完一轮；填 30 意味着要准备一个月的量，\n"
                "日型数与变体数都得加大，否则一个月内会明显看出重复。"
            ),
        )
        archetypes: int = Field(
            default=3,
            description=(
                "要编几套**工作日**日型（1-6）。\n"
                "3 套 ≈ 一周里工作日基本不撞同一套（抽取会优先用最久没用的）。\n"
                "每套是一次模型调用，所以这个数字同时决定刷池成本。"
            ),
        )
        weekend_archetypes: int = Field(
            default=2,
            description=(
                "要编几套**休息日**日型（0-4）。\n"
                "0 表示不区分工作日与周末（周末会退回用工作日日型，容易看出来，不推荐）。"
            ),
        )
        variants_per_slot: int = Field(
            default=3,
            description=(
                "每个时段编几个变体（2-4）。\n"
                "变体必须「场景一致、动作不同」——同一个时段同一套日型下，\n"
                "换的是具体在做的事，不是换措辞。变体越多，同一套日型连续用两天也不重样。"
            ),
        )
        retry_times: int = Field(
            default=2,
            description=(
                "单个日型生成不合格时的重试次数（会把问题反馈给模型再要一次）。\n"
                "重试只在**编池子**时发生，不影响对话。"
            ),
        )
        min_archetypes: int = Field(
            default=2,
            description=(
                "工作日日型少到这个数以下，就判定整池不达标、不替换旧池子。\n"
                "默认 2：池子刚装好正在编的时候，宁可继续用旧池子/回退 daily，\n"
                "也不要把只有一套日型的池子换上去（那等于每天都一样）。"
            ),
        )
        avoid_recent_days: int = Field(
            default=7,
            description=(
                "抽取时回顾最近几天的日程，用来避开刚用过的日型、也避开昨天同一时段的同一句话。"
            ),
        )
        max_tokens: int = Field(
            default=4000,
            description=(
                "编**一个日型**时的输出上限。\n"
                "一个日型 ≈ 13 段 × 3 变体，正文约 1200-2000 token；\n"
                "actor 若是思考模型，思考链会先吃掉预算，这种情况按 8000 配。"
            ),
        )
        fallback_to_daily: bool = Field(
            default=True,
            description=(
                "池子不可用（还没编好 / 编坏了 / 过期 / 抽不出来）时，是否回退到 daily 模式\n"
                "现生成一份今天的日程。\n"
                "默认 true——宁可这一次多花一次模型调用，也不能让今天没有日程。"
            ),
        )

    @config_section("plan")
    class PlanSection(SectionBase):
        """三层规划：年程 → 月程 → 周程（日程层在 [schedule] / [pool]）。

        每层围绕上一层生成，**变化程度逐层放大**：年程最稳（一年一个走向）、
        月程分忙碌 / 休息（学期月 vs 寒暑假月）、周程定义日程该干什么、
        日程最活（每天不重样）。

        每层都有两种模式：

        - ``pool``（默认）：编几套**型**（"这类时期通常怎么过"的模板），到期抽一套，
          再用上层目标规则化填充——抽取与填充都不调模型。
        - ``direct``：到期现场生成一份（每次一次调用，更贴合当下）。
        """

        enabled: bool = Field(
            default=True,
            description="是否启用三层规划。关掉后日程层照常工作，只是不再有年月周的依据。",
        )
        year_mode: str = Field(
            default="pool",
            description="年程模式：pool / direct。年层一次调用本来就不贵，想更贴合当下可以改 direct。",
        )
        month_mode: str = Field(
            default="pool",
            description="月程模式：pool / direct。",
        )
        week_mode: str = Field(
            default="pool",
            description=(
                "周程模式：pool / direct。\n"
                "周程是「定义日程该干什么」的那一层，也是被日程层每周期消费的东西，\n"
                "默认走池子（抽模板 + 上层填充，不额外花调用）。"
            ),
        )
        refresh_days_year: int = Field(
            default=180,
            description="年程型池的有效期（天）。",
        )
        refresh_days_month: int = Field(
            default=60,
            description="月程型池的有效期（天）。",
        )
        refresh_days_week: int = Field(
            default=21,
            description="周程型池的有效期（天）。",
        )
        min_items: int = Field(
            default=1,
            description="每层至少要有几条推进项，少于这个数视为生成失败。",
        )
        retry_times: int = Field(
            default=2,
            description="某一层编型不合格时的重试次数（会把问题反馈给模型）。",
        )
        max_tokens: int = Field(
            default=1200,
            description=(
                "生成一层规划的**单次**输出上限。\n"
                "一层规划只有几句话几条事项，1200 富余；\n"
                "actor 若是思考模型就把思考链的余量加进去（按 3000 配）。"
            ),
        )
        feed_schedule: bool = Field(
            default=True,
            description=(
                "是否把这些规划喂给日程层（生成日型、写日记时当材料，抽取时按周程挑日型）。\n"
                "关掉就退回「只看人设与记忆」的老行为。"
            ),
        )
        inject_in_chat: bool = Field(
            default=False,
            description=(
                "是否把周程也注入对话（每轮一段「这周在推进什么」）。\n"
                "**默认关**：规划只在生成侧起作用，不占每轮 token；\n"
                "开启后每轮多几十字，好处是她能直接聊起「这周在忙什么」。"
            ),
        )
        announce_year: bool = Field(
            default=True,
            description=(
                "新的一年（或首次装上）生成年目标时，是否主动分享给主人。\n"
                "分享目标是「最近一次主人开口的会话」；没有已知会话时只记日志，"
                "年目标随时可以用 /目标 查。"
            ),
        )

    @config_section("progress")
    class ProgressSection(SectionBase):
        """完成度与情绪：判定只落在日程层，上层靠「下级 50% 上卷」。

        主人定的口径：「每一级下一部分完成 50% 以上就算上一级完成，省去了判定完成的
        token，只需要判定日程。」所以这里没有模型调用——日程层每天判一次（掷骰，
        含忙碌等级与被打断的影响），周 / 月 / 年 / 推进项的完成度全是**算出来的**。
        """

        rollup_threshold: float = Field(
            default=0.5,
            description=(
                "上卷阈值：下级完成比例**超过**这个值，上一级就算完成（默认 0.5）。\n"
                "例：本周已判定的日子里成功日占比 > 50% → 这周算完成。"
            ),
        )
        interrupt_penalty: float = Field(
            default=0.2,
            description=(
                "忙时被打断（主人到场让位）每档扣多少成功概率。\n"
                "较忙算 1 档、很忙算 2 档；空闲时被打断不扣。"
            ),
        )
        idle_bonus: float = Field(
            default=0.05,
            description="一整天都没忙过（最忙的一档是空闲）时的加成：那天本来就没什么可耽误的。",
        )
        max_days: int = Field(
            default=7,
            description="一次补判定最多往前判几天，防止停机很久后一次性算一大堆。",
        )
        mood_days: int = Field(
            default=3,
            description="情绪看最近几天的成功日比例（顺 / 平常 / 有点背）。",
        )
        inject_mood: bool = Field(
            default=False,
            description=(
                "是否把情绪也注入对话。**默认关**（省 token）：情绪先只用于日记与 ``/目标``。"
            ),
        )

    @config_section("budget")
    class BudgetSection(SectionBase):
        """用量闸门：提示词预算、告警线。

        防的是一个真实事故形状：**一次性的巨大请求（前缀没有可复用的东西）**
        ——API 侧没有缓存命中、程序侧还要跟着重建一遍上下文，两头一起炸。
        本插件的每一次模型调用都是自带材料的小请求，这里给材料配上硬上限：

        - 超过 ``max_prompt_chars``：按优先级丢材料（先丢联网、再丢记忆、最后丢历史），
          人设这种"不能丢"的段用 ``keep`` 标着不动；丢完还超就末尾硬截断；
        - 超过 ``warn_prompt_chars``：日志里 WARNING 一次，方便早发现"悄悄长起来"。

        单位是**字符**（中文字符 ≈ 1 token 量级，当上界够保守），不需要分词器依赖。
        """

        max_prompt_chars: int = Field(
            default=12000,
            description=(
                "单次模型调用的输入硬上限（字符）。\n"
                "本插件的调用（日型池 / 规划 / 日记）正常都在 2k-6k 字符量级，\n"
                "给到 12000 是留足余量；超了会先丢可牺牲的材料，并记一条 WARNING。"
            ),
        )
        warn_prompt_chars: int = Field(
            default=8000,
            description="输入超过这个字符数就告警（不截断），用来早发现异常增长。",
        )
        note_injection: bool = Field(
            default=True,
            description=(
                "是否统计「每轮往对话里注入多少字符」（进程内计数，每 20 轮落盘一次）。\n"
                "统计结果在 /日程 用量 里，用来证明注入量是固定的、不随对话变长。"
            ),
        )
        fail_backoff: bool = Field(
            default=True,
            description=(
                "模型调用连续失败时是否退避（60→120→240…封顶 1800 秒）。\n"
                "默认开：防止「失败就立刻重试」把 token 打爆（另一个真实事故里一晚上烧了几百次）。"
            ),
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
        hint_only_when_busy: bool = Field(
            default=True,
            description=(
                "反应倾向是否**只在忙的时候**附加。\n"
                "默认 true：闲着的时段场景行就只剩「你此刻正在：……」一句，\n"
                "省 token 也少一层噪音（闲时本来就不需要「可以搭话」这种说明）。"
            ),
        )
        channel: str = Field(
            default="reminder_first",
            description=(
                "注入走哪条路：reminder_first（默认）/ both / extra_only。\n"
                "reminder_first：优先写 system reminder；写成功就不再往 user prompt 的\n"
                "  ``extra`` 里追加——**同一段背景在同一个请求里出现两遍**只是把 token 付两遍，\n"
                "  还会让模型更注意到它。写失败（或关掉 reminder_enabled）才退回 extra。\n"
                "both：两条路都写（旧行为）。只有当你的 chatter **不拾取** with_reminder 时\n"
                "  才需要它——DFC / NDFC 都会拾取，所以默认不是 both。\n"
                "extra_only：只走 extra，完全不碰 reminder。"
            ),
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
        yield_stream_scope: bool = Field(
            default=True,
            description=(
                "让位是否**只在触发它的那个会话**生效。\n"
                "开启（默认）时：日程行照旧全局注入，让位心声改为写进该会话的\n"
                "**流私有 reminder**（prompt_api.add_stream_reminder），别的群/私聊不受影响；\n"
                "同时让位期间不再追加 busy_suffix，免得「我手上正忙」和「这些先放一放」\n"
                "两句话同时进上下文。\n"
                "关闭则退回旧行为：让位心声替换场景行、写进全局 bucket\n"
                "（主人在任意一个会话开口，所有会话都会看到这句话）。"
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
            default=False,
            description=(
                "检测到 time_sense（时间感知插件）时，是否自动打开离线生活。\n"
                "**默认 false：日记默认关闭**（主人定的口径）。装好 time_sense 只是让它\n"
                "「可以」记日记，要不要花这份 token 由你决定——想开就把这一项与 enabled 一起设 true。\n"
                "（v1.2.0 之前默认是 true，装上 time_sense 就自动开；从 1.2.0 起改成默认关。）"
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
            default=True,
            description=(
                "是否把最近的日记注入对话提示词。\n"
                "开启后主模型才知道「它不在的时候做了什么」，被问起时不会前后矛盾；\n"
                "不开启的话日记只躺在盘上，问起来照样只能现编——那就白写了。\n"
                "担心 token 就把 inject_max_chars 调小或 inject_days 设 0。"
            ),
        )
        inject_days: int = Field(
            default=1,
            description="注入最近几天的日记（1 表示只给最近一篇）。",
        )
        inject_max_chars: int = Field(
            default=600,
            description=(
                "注入日记的字符上限，超出即截断。\n"
                "设为 0 表示不截断。"
            ),
        )
        inject_turns: int = Field(
            default=3,
            description=(
                "每次记完日记之后，**只在接下来的几轮对话里**注入它（默认 3 轮）。\n"
                "实测量过：日记注入块约 400 字，是「每轮注入」里最大的那一份（占 82%）。\n"
                "每轮都发等于按请求数重复付费，而它要传达的信息（「它不在的时候做了什么」）\n"
                "只需要被模型知道一次——之后历史里已经有了。\n"
                "填 0 表示每轮都注入（旧行为），只在你确实希望它一直挂在上下文里时才这么设。"
            ),
        )
        roll_enabled: bool = Field(
            default=True,
            description=(
                "离线回来时，是否对「这周 / 这月正在推进的事」做一次随机评估。\n"
                "结果会写进日记（这一次是顺利还是卡住了），并成为情绪与后续安排的材料。\n"
                "同一条推进项只评估一次（结果落盘），重启不会重掷，人设不会自相矛盾。"
            ),
        )
        roll_success_rate: float = Field(
            default=0.8,
            description=(
                "随机评估的**成功概率**：默认 0.8 ＝ 八成顺利（它高兴）、两成不顺利（它失落）。\n"
                "取值 0-1，0.5 就一半一半。"
            ),
        )

    plugin: PluginSection = Field(default_factory=PluginSection)
    model: ModelSection = Field(default_factory=ModelSection)
    source: SourceSection = Field(default_factory=SourceSection)
    schedule: ScheduleSection = Field(default_factory=ScheduleSection)
    pool: PoolSection = Field(default_factory=PoolSection)
    plan: PlanSection = Field(default_factory=PlanSection)
    progress: ProgressSection = Field(default_factory=ProgressSection)
    budget: BudgetSection = Field(default_factory=BudgetSection)
    scene: SceneSection = Field(default_factory=SceneSection)
    log: LogSection = Field(default_factory=LogSection)
    offline: OfflineSection = Field(default_factory=OfflineSection)


__all__ = ["DailyScheduleConfig"]
