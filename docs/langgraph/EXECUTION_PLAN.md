# AdaptiveBI-LangGraph 后续实施方案

版本：2026-09-17 / 执行方案 v1

性质：基于已读取代码制定的实施建议，不是已实施或已测试的功能声明。本文中的新增路径、接口、阈值均为建议；必须经过实现和测试才能更新为“完成”。本次未修改 GitHub 仓库、未连接业务数据库、未执行项目测试。

## 0. 基线、目标与范围

核对到的分支：

- `main`: `9aaece19ca36bf2b18425ec22f024c369a8787e9`
- `codex/langgraph-foundation`: `437ff82f26285cdf340f76db3e02a1caf2a74a97`

后续代码工作基于 foundation 分支，或它合并后的等价提交。开始前先检查最新分支与 PR 状态；不要从只有迁移方案的旧 main 重新搭骨架，也不要擅自合并、强推或部署。

当前开发分支已包含：独立 graph-service、LangGraph 六节点图、LangChain Core 模型接口、固定模拟模型、合成 SQLite 工具、20 条固定案例、演示接口与 CI。当前新图尚未接入真实业务。对应来源见文末 S1–S6。

### 本轮要交付的产品行为

一个经过授权的用户，针对一个受支持的数据源，用自然语言查询已发布指标；系统明确口径、时间和分组，执行受限制的只读查询，展示可核对结果；缺少关键条件时澄清，可修复错误有限修复，无法可靠处理时明确停止。原系统继续可用。

第一轮范围固定为：一个真实模型配置、一个隔离测试数据源、单表指标统计、表格及规则生成的基础图表。不同时迁移预测、归因、多数据库方言、嵌入式问答、MCP、自动训练、多智能体或整套前端。

### 四条不可放松的约束

1. 权限由服务端和业务工具执行，不能由模型决定或通过提示词绕过。
2. 新旧后端依赖继续隔离，不把旧 ORM / LLMService 整体导入新服务。
3. 所有实验使用独立配置、合成数据或批准的脱敏快照；不复制旧 `.env`、密钥、客户数据和运行日志。
4. 保留已有许可证和版权声明；设计依据为本仓库、明确业务要求和所用技术的官方资料，不引入原始上游产品材料。

## 1. 架构：两个应用服务，一个内部业务网关

```text
现有页面 / 测试客户端
    ↓ 现有登录与工作空间权限
现有 FastAPI
    ├─ 旧问答引擎：继续运行
    └─ 新引擎入口：功能开关 + 用户/工作空间白名单
           ↓ 服务间身份与短期委托
       graph-service
           ├─ LangGraph：状态、决策路径、修复、暂停恢复
           ├─ GatewayChatModel：保留当前模型注入接口
           └─ HTTP 业务适配器
                    ↓
       现有 FastAPI 内的 graph_gateway 模块
           ├─ 原模型工厂：服务端取配置与密钥
           ├─ 原指标/记忆模块：版本、权限和来源
           ├─ SQL 执行边界：最终校验、只读账号、超时
           └─ 结果与答案：授权结果引用、审计与幂等

另设隔离的运行存储：run 注册表、检查点、事件、工具执行记录。
测试查询数据库与业务元数据库分开；不允许迁移脚本误连原项目数据库。
```

不为模型、指标、记忆、SQL 各部署一个微服务。第一轮新增的是旧后端的一个内部网关模块和现有 graph-service 的适配器。先保持当前验证过的依赖锁文件；新增依赖只修改对应服务并更新锁文件，不执行全局升级。

网关调用必须验证服务身份及短期委托。委托绑定服务端生成的用户、工作空间、run、允许的数据源、用途和有效期；工具侧重新检查当前权限，不能仅信任 `X-User-ID`、`X-Workspace-ID` 等自报字段。模型配置也要检查工作空间可用性。客户端不得提供 API Key、数据库连接串、任意模型 URL 或可信 Principal。

模型调用期间不要长期占用业务 ORM Session；网关与编排器的互调要采用有界并发，测试是否发生线程池饥饿或连接池耗尽。

## 2. 核心业务改动：指标优先，不让模型每次重写公式

当前 `MetricVersion` 已包含 expression、aggregation、time_field、grain、required_tables、dimensions、filters、join_rules、unit 和版本状态。当前检索函数返回提示词和版本引用，并按工作空间、数据源、发布状态及字段可见性筛选。[S7–S9]

建议增加结构化指标接口，复用现有授权与筛选逻辑，不从 XML 风格提示词反向解析规则，也不另建一套不兼容指标库。

### 两条查询路径

**已发布指标路径——本轮用户可用主路径：**

```text
问题 → 获取获授权候选指标 → 模型选择指标与条件
     → 程序验证 QueryPlan → 程序按指标定义编译 SQL
     → 最终权限/SQL 校验 → 只读执行 → 规则化证据与答案
```

**临时自由 SQL 路径——先只允许隔离测试：**

```text
问题 → 模型生成 SQL → 严格校验 → 受限测试库执行
```

没有匹配指标，不等于允许模型在真实业务数据上自由发挥。第一版默认返回“暂不支持/需要明确指标”，自由 SQL 仍受单独开关限制。

### QueryPlan 建议字段

| 字段 | 谁提供 / 规则 |
| --- | --- |
| metric_ref | 模型从授权候选中选择；沿用现有 `id`、`version_id`，服务端重新解析 |
| dimensions | 仅允许选定指标支持且当前用户可见的维度 |
| time_range | 明确 start、end_exclusive、timezone；相对日期依赖受信任 reference_now |
| filters | 受限字段、操作符和类型化值；值参数化，不接受 SQL 片段 |
| order_by / limit | 允许的排序键和有界数量 |
| unresolved_fields | 口径、时间或过滤条件的歧义；不允许默认猜掉关键条件 |
| schema_version / metric_refs | 服务端绑定，不相信模型自行填出的版本 |

模型不生成可信公式、可信权限或可信数据源连接。时区来自业务配置，不从开发机或聊天工具时区推断。

### 单表编译器 v1 的边界

只实现明确支持的单表聚合和维度过滤，不尝试证明任意 SQL 的语义等价。表、字段和表达式由批准指标及允许列表构造，值使用参数绑定。必须先检查现有 expression 与 aggregation 的约定：表达式已经包含聚合时不能再机械套 `SUM()`；不支持的表达式形态明确拒绝。NULL、退款、去重、单位和空结果规则必须来自指标定义，不能由模型补写。

JOIN 暂不开放。后续开放时再验证连接基数、业务粒度、重复放大、连接路径和指标规则，而不只是检查表名存在。

### 最有价值的反例测试

合成 sales 数据的总额是 1500，退款合计 80，净额是 1420。[S5] 对“净销售额”问题，`SUM(gross)` 即使 SQL 合法也必须判为业务错误；治理指标主路径应由批准口径构造正确计算，而不是让另一个模型投票认可。

这能控制“按已选口径计算”的一部分风险，不保证模型始终选对指标、数据源本身无误或业务定义本身正确。因此指标选择和数据质量仍要单独评估。

## 3. 六个交付包

每包独立提交、验证和审阅。PR-4 可按运行存储与中断恢复拆为两个 PR。不要把后面的范围提前塞进前面的任务。

### PR-1：真实模型 + 任意测试问题，保留模拟回归

**目标**：不再只提交 case_id；真实模型回答未预置的问题，但仍只能查询已有的合成 SQLite 数据。

**建议改动**：

- 修改 `graph-service/app/contracts.py`：新增 QueryRequest、对外 QueryResponse、ModelUsage / SafeError。保留旧 demo 契约。
- 修改 `graph-service/app/main.py`：新增受认证的实验入口，保留 `/demo/*` 明确的 synthetic 模式。
- 新增 `graph-service/app/adapters/model_gateway.py`：GatewayChatModel，保留现有 BaseChatModel 注入点。
- 新增 `backend/apps/graph_gateway/` 模块：服务身份和委托校验、模型调用端点；通过现有 `LLMFactory` 读取获授权模型配置。
- 增加模型网关、请求身份、输出过滤测试；补充 `docs/langgraph/IMPLEMENTATION_STATUS.md`。

本包可使用同步实验查询接口，明确不承诺后台执行、重启恢复或断线续传。建议旧 API 实验路径为 `/api/v1/analysis/query`，graph 内部路径为 `/internal/v1/query`；名称需在落地时核对现有路由前缀，不能误称已存在。

**本包的确定行为**：只允许合成数据源；未知字段/危险 SQL 仍拒绝，暂不做业务自动修复。生成失败如实返回失败，不能切换为假的预设答案。模型输出格式受限；代码围栏、多余解释和空输出均有明确处理测试，不能宽松正则截取后盲目执行。

**完成条件**：原图服务测试与20条固定案例不退化；无权限时模型和SQL调用计数为0；同步和流式响应均不暴露完整状态、schema、提示词或密钥；真实模型冒烟报告记录配置标识、实际调用数、耗时和真实用量。缺少测试凭据时，交付适配器与离线测试，真实模型验证标记“未执行”，不得用模拟结果冒充。

### PR-2：测试业务网关 + 指标计划 + 单表 SQL 编译

**目标**：回答已发布指标问题，建立可验证的业务口径。

**建议改动**：

- 扩展 `backend/apps/graph_gateway/`：授权表结构、结构化指标、现有已批准记忆的只读检索、只读测试数据源、结果保存与读取接口。
- 从 `backend/apps/metrics/crud/metric.py` 提取或增加结构化检索函数；保持现有 prompt 函数兼容，不改变旧入口默认行为。
- 新增 `graph-service/app/planning.py`、`metric_compiler.py`、`adapters/business_gateway.py`；按节点代码体量决定是否拆 nodes，不提前制造大量空文件。
- 在 `graph.py` 加入 plan / validate_plan / compile_metric 路径。
- 增加隔离 PostgreSQL 合成数据适配与夹具；保留 SQLite 快速回归，不把二者方言混用。

结构化指标响应要保留来源、版本、单位、粒度、适用时间和可见字段；取不到口径返回澄清或不支持。已批准记忆只作为获授权上下文读取，保留来源和适用范围；记忆不能覆盖发布指标与权限，不在此阶段自动写入新共享规则。先返回结构化澄清请求，真正同一个 run 的 interrupt/resume 在 PR-4 实现；本阶段不能声称可以跨重启继续。

**SQL 安全边界**：最终将执行的 SQL 在所有改写后重新校验；完全限定的表名、字段、函数、语句类型有允许列表；行列权限由确定性服务执行。最小权限账号加只读事务、查询超时、连接池、结果行数和字节限制。PostgreSQL 只读状态和超时是额外防线，不是权限系统的替代。[O4]

**完成条件**：净额/总额反例通过；草稿指标不能进入回答；不可见字段在指标表达式中也不能泄漏；按地区、按时间统计得到人工核对结果；关键歧义不执行；多表与不支持函数明确拒绝；执行前撤销权限会被拦截。业务结果的数值先由确定性格式化生成，再逐步增加模型解释。

### PR-3：错误分类、有限修复、调用预算

**目标**：失败不再全部终止，也绝不无限尝试。

新增 `graph-service/app/errors.py`、`budgets.py`，在图中加入路由与有限修复；网关同步实施超时、请求计数和SQL执行约束。

| 情况 | 处理 |
| --- | --- |
| 权限拒绝、写操作、越界表字段 | 立即终止；不可交给模型尝试绕过 |
| 输出格式错误、上下文中的未知字段、可定位的查询构造错误 | 使用脱敏错误与授权上下文有限修复；然后重新完整校验 |
| 指标编译器代码缺陷 | 记录缺陷并终止；不靠模型掩盖确定性代码错误 |
| 缺少关键业务口径 | 返回澄清，不猜答案 |
| 空结果 | 正常完成并说明范围，不因此自动扩大范围或重试 |
| 提供商短暂错误 | 有界退避；各层重试计入同一总预算 |
| 数据库请求结果不明 | 标记 unknown 并停止自动重放；不能简单当成“未执行” |
| 图表失败 | 降级表格并附警告，不重新跑SQL |

**建议起始配置，需实测校准，不是当前实现或性能承诺**：修复最多2次；底层模型请求总尝试数6次；SQL单次10秒；累计主动运行90秒；单次模型输出上限2048 token；总token上限按模型与上下文预算配置；返回最多1000行且2MiB；LangGraph recursion_limit先设40作为最后一层保险。

修复后的SQL必须经过相同权限与语义验证。provider SDK 自动重试必须关闭或纳入计数，不能图重试2次、HTTP重试3次、SDK再重试3次而无人统计。用量未返回时记录 unknown / null，不记为0；成本需带价格配置版本。

**完成条件**：故障注入可验证一次修复成功、重复错误耗尽预算、拒绝不调用模型、模型超时退出、空结果不修复、图表降级不重复SQL；累计预算跨后续恢复不会清零。

### PR-4：持久运行、澄清、取消与重启恢复

**目标**：任务从“依赖当前请求的一次调用”升级为可管理的 run。

新增 `graph-service/app/persistence/`、`app/api/runs.py`、`app/worker.py`；按已锁定版本引入并测试 PostgreSQL checkpointer。迁移使用独立账号、库或明确隔离schema，先测试再发布。

**关键设计**：

- 一个问题一个 run；一个 run 对应服务端生成的不透明 thread_id；澄清恢复用同一个 run/thread。多轮业务上下文由现有记忆/聊天服务显式获取，不把全部聊天放进检查点。
- thread_id 不是授权机制。读取run、事件、结果、resume、cancel全都检查当前用户和工作空间归属。
- 状态只保存版本、计划、SQL及必要摘要、result_ref、预算和安全错误；SQL本身也按敏感运行数据管理。密钥、连接对象、ORM Session、委托令牌和完整结果不进检查点。
- 创建run先持久落库，再由有租约和心跳的worker认领。小规模可在graph-service运行一个有界worker循环，不强制引入Redis/Celery。只用内存BackgroundTasks并不能形成所需的重启恢复设计。
- 同一会话限制一个活动生成任务；通过数据库唯一约束/乐观版本与worker租约防重，不能仅用单进程锁。
- 恢复重新获取可信身份与短期委托，重检数据源和固定指标版本。原版本被撤销时暂停或终止，不静默换口径。
- 每个run固定engine、graph_version、state_schema_version；新部署不能无条件恢复不兼容状态。

**拟新增公开运行接口**：

| 接口 | 行为 |
| --- | --- |
| POST `/api/v1/analysis/runs` | 验证用户与输入，持久创建，返回202和run_id；Idempotency-Key同主体+同请求只创建一次 |
| GET `/api/v1/analysis/runs/{id}` | 返回过滤后的运行状态和获授权结果引用 |
| GET `/api/v1/analysis/runs/{id}/events` | SSE；支持Last-Event-ID，重连不重新发起任务 |
| POST `/api/v1/analysis/runs/{id}/resume` | 校验interrupt_id、expected_version、答案schema及幂等键；重新鉴权后恢复 |
| POST `/api/v1/analysis/runs/{id}/cancel` | 先cancel_requested；实际调用停止/状态核对后才确认cancelled |

同一幂等键不同请求体返回409。取消晚于完成时返回真实完成状态，不伪造取消成功。断线默认不取消run，显式取消走独立接口。人类等待时间与主动运行预算分开计算；澄清等待可先设置24小时到期，并释放数据库/HTTP资源。

**最小运行存储**：run注册表、过滤后的事件表、tool_execution记录，以及checkpointer自有表。结果仍由获授权的结果存储保存；不要把用户业务记忆迁进checkpointer。

**副作用和未知结果**：答案保存使用 `(run_id, operation)` 唯一幂等键；SQL使用 `(run_id, node, attempt)` 加SQL/参数哈希记录 started/completed/unknown。同键不同内容拒绝。已完成操作可在重新授权后复用结果；连接中断导致状态unknown时停止自动执行并人工核对，避免假装获得跨系统 exactly-once 保证。模型请求亦记录尝试与已知/未知用量。

LangGraph需要checkpointer与thread_id来支持这里的状态恢复；interrupt会在恢复时重入所在节点，副作用必须另行做幂等设计。[O1–O3]

**完成条件**：澄清后重启仍可继续；其他用户拿到run_id不能查看或恢复；权限撤销后不可读取已缓存结果；重复resume只推进一次；worker重启不重复保存答案；SQL结果未知不盲目重查；取消明确反映下游状态；过期结果不会无提示触发新查询；不兼容graph版本拒绝恢复并给出处理路径。

### PR-5：旧页面兼容、小范围接入、回退演练

**目标**：用户从原页面使用新路径，而不是重写整个UI。

修改旧 `backend/apps/chat/` 引擎选择和事件适配；按真实前端事件处理位置修改，先搜索定位，不能凭文件名猜路径。

拟定开关 `CHAT_ENGINE=legacy|langgraph`，默认legacy；服务端限制工作空间/数据源/受支持功能白名单，不能由请求体提升权限。未迁移的分析、预测、重生成、嵌入式和MCP继续旧路径。

新事件统一run_id、sequence、type、node和安全payload，再映射旧的id/question/sql-result/sql-data/finish等事件。旧页面没有澄清交互前，不把interrupt流程分配给它。

SSE持久事件跨重启连续编号，客户端按event_id去重；逻辑终态事件只有一个，但重放允许重复送达，不承诺网络exactly-once。流式API以当前锁定版本的兼容测试为准，不为了追随新文档接口同时升级运行方式。[O5]

**页面最小展示**：当前步骤；采用的指标、版本、时间范围和单位；结果表格；必要澄清；支持取消；错误或截断提示。只显示业务步骤，不暴露完整图状态、提示词、模型内部推理或连接信息。SQL展示也遵循现有权限策略。

**灰度顺序**：开发/测试账号 → 指定内部工作空间 → 经批准的更多工作空间。未完成业务安全和结果验收时，不以随机百分比向所有人开放。

**回退条件**：任何授权泄漏/写操作边界失败、核心口径回归、明显卡死或成本预算失控。关闭开关只影响新run；进行中的run按原版本结束、取消或明确迁移。同一次失败请求不自动转旧引擎重新执行。对照只在合成/快照数据执行；生产影子模式默认只生成与校验，不重复查询客户库。

发布前核对原部署的持久签名密钥、RSA密钥和HTTPS配置；不能因新增worker顺手扩容原API而破坏登录或密钥一致性。这项风险已在仓库迁移方案中列出。[S10]

### PR-6：有审核的反馈学习

**目标**：纠正能在验证后改善后续回答，而不是“点个赞就自动训练”。

复用现有 `learning`、`feedback`、`memory` 模块。现有批准记忆的只读检索在 PR-2 接入；本包只推进经过治理的新增与更新，流程为：反馈 → 候选 → 依赖及权限检查 → 固定评估集回放 → 人工审核 → 发布版本 → 监测 → 撤销。

候选保存来源run、用户/工作空间、适用数据源、指标版本、内容、评估结果、审核人和失效条件。个人偏好与团队计算规则分开。模型不能自行批准自己生成的共享规则，文本记忆不能覆盖权限与指标。

**完成条件**：一条错误纠正不会自动污染其他用户；未批准候选不参与共享检索；被撤销规则下次不再生效；指标或schema变化会触发相关候选重新验证；反馈回放只在隔离数据执行。

## 4. 评估：保留工程测试，新增真正的业务评估

### 三套结果分开报告

| 套件 | 内容 | 能证明什么 |
| --- | --- | --- |
| deterministic | 保留原39项图服务测试与20条模拟固定案例，并扩展故障测试 | 在这些样例下，流程和契约行为符合预期 |
| live-business | 真实模型 + 固定数据 + 人工口径/结果 | 在声明范围内，自然语言到业务答案的表现 |
| security-resilience | 越权、注入、撤权、并发、断流、取消、重启与重复提交 | 在这些安全和故障用例下的隔离与恢复行为 |

39项是基线文档/此前CI记录中的数量，不是本次重新执行的结果。新增后报告实际数目。mock通过率不称作模型准确率。

### 第一版业务集建议：60条

40条应回答（基础聚合、过滤、分组、时间边界、空结果、NULL、单位、总额与净额等）；10条应澄清；10条应拒绝或不支持。按类别分成30条开发集、30条留出集：留出集20条应回答、5条应澄清、5条应拒绝。另设至少20条独立权限/恢复工程用例，不与业务正确率混成一个分数。

问题、参考结果和SQL必须存于评估侧。在线请求只看到允许的问题、结构、指标与上下文，不把标准SQL、结果或case_id提示给真实模型。开发时不反复根据留出答案调提示词；泄漏后替换留出集。

**每例记录**：case_id、类别、question、固定reference_now及timezone、dataset_snapshot、model_config_ref、prompt_version、expected_action、expected_metric_refs、expected_time_range、expected_result、order_sensitive、tolerance、expected_denial_or_clarification。

**对照规则**：新旧引擎使用相同模型配置、获授权上下文、数据快照和相对日期基准；业务样例各重复3次，并记录失败样例。重复运行不是独立新增问题，分别报告按执行次数与按问题的稳定性，不把样本量膨胀成总体可靠性结论。

**结果比较**：不要求SQL字符串相同；无序结果按保留重复次数的多重集比较；排序/Top-K按用例规则比较。NULL和0区分，金额按约定精度处理，浮点容差逐例指定。答案中的指标、单位、时间、过滤和版本也必须匹配。

### 建议的初始内测门槛

- 安全/隔离用例全部通过；这是发布门槛，不是“所有输入绝对安全”的保证。
- 关键金额和已发布口径反例全部通过；留出集应回答样例的结果正确率暂以不低于90%且不低于旧引擎为起始门槛，同时报告分母与逐例失败。
- 同时记录该澄清却猜答、无需澄清却反复追问的情况，不以“总是问用户”刷安全分数。
- P95延迟和均次用量/成本对照旧引擎，沿用迁移方案“先以基线+20%为警戒线”的建议；这是待实测校准的预算标准，不是现有效果或承诺。[S10]
- 没有真实旧引擎基线，就标记“未验证优于旧引擎”，不宣布迁移提升准确率。

## 5. 建议代码组织

先沿现有代码逐步添加；以下都是拟新增或拟修改路径，不代表已经存在：

```text
backend/apps/graph_gateway/          # 内部鉴权、模型与业务工具边界
backend/apps/chat/                   # 引擎路由、实验入口、SSE兼容
backend/apps/metrics/crud/metric.py   # 复用筛选，增加结构化版本结果

graph-service/app/
  graph.py                          # 暂时保留入口；变复杂再拆graphs/
  contracts.py                      # 暂时保留，避免与同名package并存
  planning.py                       # QueryPlan及校验
  metric_compiler.py                # 受支持指标形态的SQL构造
  errors.py
  budgets.py
  adapters/
    model_gateway.py
    business_gateway.py
  api/runs.py                       # PR-4再引入
  persistence/                      # PR-4再引入
  worker.py                         # PR-4再引入

graph-service/tests/                # mock、契约、运行恢复
  test_model_gateway.py
  test_metric_planning.py
  test_metric_compiler.py
  test_budgets_and_repair.py
  test_run_security.py
  test_recovery_and_cancel.py

evaluations/graph_business/          # 拟新增真实业务评估，检查现有runner后复用
  dev_cases.jsonl
  holdout_cases.jsonl
  fixtures/
  evaluate_live.py
  compare_engines.py

docs/langgraph/
  EXECUTION_PLAN.md
  GATEWAY_CONTRACT.md
  IMPLEMENTATION_STATUS.md
```

`contracts.py` 若未来转为 `contracts/` 包，应在单独重构中完成导入迁移，不能同时保留两个同名入口。每个文件由实际功能需要驱动，不为目录结构先创建几十个空壳。

## 6. 每个交付包的交付格式

每个PR说明包含：目标与不做事项；确切基线；改动文件；接口变化；离线测试命令与真实输出；真实模型测试是否执行；已知限制；配置/迁移风险；回退方式。CI绿只表示该CI覆盖内容通过。

现有基础验证命令（在 graph-service 目录）：

```bash
uv sync --locked
uv run --frozen pytest -q
uv run --frozen python evaluate.py
```

后续live评估命令由实现者在新增runner后给出，不能现在把尚不存在的脚本称为可运行。开发过程中只有确认相应文件已创建并测试后，才更新运行说明。

缺少密钥或测试库配置时，不复制现网配置、不索要明文密钥、不伪造成功。可以完成代码与离线契约测试，并精确列出仍缺的验证条件。

## 7. 第一包可直接交给 Codex 的任务

```text
请继续 AdaptiveBI-LangGraph 项目，本次只完成 PR-1“真实模型网关 + 任意测试问题”，不要实现完整迁移。

先读取 AGENTS.md、docs/langgraph/MIGRATION_PLAN.md、IMPLEMENTATION_STATUS.md、graph-service 的代码和测试，以及 backend/apps/ai_model/model_factory.py 与现有认证/路由注册方式。

核对当前分支、未提交改动与远端状态。以 codex/langgraph-foundation 的 437ff82 或其已合并等价版本为基线；不从仅有方案的旧 main 重建。保留用户改动，不擅自合并、强推、部署或修改原项目数据库。

本次目标：登录测试用户能够输入未预置的自然语言问题，通过真实模型生成只读查询，但 SQL 仍只作用于现有合成 SQLite 数据。

实施内容：
1. 保留 /demo/*、FakeListChatModel 回归与20条固定案例，明确 synthetic 模式。
2. 新增 QuestionRequest/安全响应结构。实验问答接受问题，但不接受客户端 Principal、user_id、oid、API Key、模型URL、连接串或任意SQL。客户端选择的数据源仍需服务端校验，本包仅允许 synthetic-sales。
3. 在旧后端新增内部 graph_gateway 模块，复用已授权模型配置及 LLMFactory；模型密钥不返回graph-service。服务间鉴权+短期委托必须绑定服务端身份、工作空间、run、范围、用途及有效期；网关重新校验当前权限。
4. 在新服务增加 GatewayChatModel，保持当前 BaseChatModel 注入方式；不要把旧 LLMService 或旧依赖导入新服务。加入超时、错误映射和实际usage记录。无usage时记unknown/null。
5. 新增受认证、默认关闭的实验查询入口。核对现有路由前缀后注册；保留正常旧问答默认走legacy。本包同步执行即可，不宣传持久恢复或后台任务。
6. 同步及SSE都只返回显式允许字段；不得返回整份graph state、schema、提示词、内部推理、连接信息或密钥。
7. 新增测试：未知/无权限身份在模型调用前拒绝，伪造委托拒绝，外部身份字段拒绝，危险SQL拒绝，模型超时，格式错误，响应字段过滤；原测试不退化。
8. 真实模型测试单独开启，只用新建测试环境和合成数据；缺少授权测试凭据时完成离线实现并报告未执行的检查，禁止静默使用fake后报告真实成功。
9. 更新实施状态和网关契约，区分已实现、已测试、未验证。

本次不做：生产数据库、多租户全量上线、指标编译器、自动修复、持久checkpoint、澄清恢复、前端改版、自动学习、多智能体、同时升级旧后端依赖。仅保留后续所需的清晰接口，不做空壳实现。

完成后报告：改动文件；接口与输入输出；运行方式；执行过的测试及输出；真实模型是否调用；失败或跳过的项目；尚未完成的条件；回退方式。不要仅以“测试通过”代替实际结果。
```

## 8. 资料与核对依据

以下仓库资料以 `437ff82f26285cdf340f76db3e02a1caf2a74a97` 为快照；分支主线另行标明。本文中的架构取舍和阈值是建议，不是这些资料已经实现的能力。

| 编号 | 仓库文件 / 官方资料 | 用途 |
| --- | --- | --- |
| S1 | main 与 codex/langgraph-foundation 分支元数据 | 确认代码基线 |
| S2 | docs/langgraph/IMPLEMENTATION_STATUS.md | 已实现和未实现边界 |
| S3 | graph-service/app/graph.py | 当前六节点、无修复与持久恢复 |
| S4 | graph-service/app/main.py、contracts.py | demo身份、case_id和响应行为 |
| S5 | graph-service/app/synthetic.py | 5条合成数据、SQL边界 |
| S6 | graph-service/tests/test_graph.py、evaluate.py | 测试覆盖与模拟评估性质 |
| S7 | backend/apps/metrics/models/metric.py | 业务指标与版本字段 |
| S8 | backend/apps/metrics/crud/metric.py | 授权筛选、提示词与版本引用 |
| S9 | backend/apps/ai_model/model_factory.py | 模型配置与工厂复用 |
| S10 | docs/langgraph/MIGRATION_PLAN.md | 原迁移目标、回退与发布约束 |
| O1 | LangGraph: Persistence | 检查点与长期数据的区别 |
| O2 | LangGraph: Checkpointers | 持久状态与thread配置 |
| O3 | LangGraph: Interrupts | 中断恢复与节点重入 |
| O4 | PostgreSQL: Client Connection Defaults | 只读事务与statement_timeout |
| O5 | LangGraph: Streaming | 流式接口及兼容边界 |

官方资料地址（2026-09-17读取；实现仍需匹配本项目锁定版本）：

```text
https://docs.langchain.com/oss/python/langgraph/persistence
https://docs.langchain.com/oss/python/langgraph/checkpointers
https://docs.langchain.com/oss/python/langgraph/interrupts
https://docs.langchain.com/oss/python/langgraph/streaming
https://www.postgresql.org/docs/current/runtime-config-client.html
```

最终决策：先通过 PR-1 证明真实模型链路，再通过 PR-2 证明口径与结果。只有这两件事可核对之后，才继续扩大自主修复、持久恢复和用户范围。
