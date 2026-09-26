# 结构化指标计划契约

更新日期：2026-09-20（新增受控执行）

本契约用于 PR-2 的指标主路径。模型只负责从当前用户获授权的候选中选择指标、维度、过滤条件和时间范围；指标公式与 SQL 由旧后端按已发布版本编译。

## 候选快照

旧后端先按工作空间、数据源、发布状态、有效期和字段权限筛选指标，再返回最多20个结构化候选。候选包含：

- `metric_id`、`metric_version_id` 和版本号；
- code、名称、别名和说明；
- 允许的维度、时间字段、粒度与单位；
- 来源表名称和只用于排序的匹配分数。

候选不包含表达式、固定过滤器、连接规则、数据库连接信息或密钥。公式仍由业务网关根据固定版本解析，不能由模型改写。

## 模型计划

模型必须返回一个无代码围栏的 JSON 对象：

```json
{
  "metric_id": 9,
  "metric_version_id": 27,
  "dimensions": ["region"],
  "filters": [{"field": "region", "operator": "=", "value": "east"}],
  "time_range": {
    "start": "2026-08-01T00:00:00",
    "end": "2026-09-01T00:00:00"
  },
  "limit": 100
}
```

未知字段、额外JSON键、未授权指标或版本、未声明维度、非维度过滤字段、无时间字段却提供时间范围，以及无效范围都会被拒绝。时间范围采用 start inclusive、end exclusive 语义。

## 编译边界

通过计划校验后，由现有 `metric-plan-v1` 编译器重新读取指定指标版本并生成单表聚合 SQL。编译器负责：

- 使用发布版本的 expression、aggregation 和固定过滤器；
- 引用实际可见表和字段并正确引用标识符；
- 转义过滤值，限制过滤操作符、列表大小和limit；
- 拒绝多表指标、未声明字段、聚合重复包裹和不支持的表达式；
- 生成版本引用、应用过滤器、SQL fingerprint 与编译器版本。

## 跨服务闭环

公共入口为 `POST /api/v1/analysis/metrics/plan`。它复用现有登录身份，只接收问题和整数数据源ID。后端为每次请求生成 `run_id` 和最长120秒的签名委托，绑定用户、工作空间、固定模型、数据源、问题哈希、用途和三个精确 scope。

图服务按以下顺序调用旧后端：

1. `/internal/graph/metrics/authorize` 复核当前账号、成员关系和模型权限；
2. `/internal/graph/metrics/candidates` 读取当前时刻仍获授权的发布指标候选；
3. `/internal/graph/metrics/model` 只提交候选ID和版本ID，后端再次读取候选并构造模型提示（唯一副本在 `backend/apps/graph_gateway/prompts.py`，图服务不构造提示词）；
4. 图服务严格解析模型JSON，拒绝候选之外的版本、维度和过滤字段；
5. `/internal/graph/metrics/compile` 再次复核权限并由后端固定编译器生成计划元数据。

四个内部入口都要求图服务凭据和同一份签名委托。模型调用前后及编译前后均复查当前权限。公共响应使用字段白名单，只包含指标与版本标识、维度、时间范围、单位、SQL指纹、编译器版本、模型用量和安全错误码；不包含SQL、公式、提示词、候选列表、连接信息或密钥。

后端与图服务都必须显式配置相同的 `GRAPH_METRIC_DATASOURCES` 逗号分隔白名单。未配置时指标入口拒绝请求。`GRAPH_EXPERIMENT_ENABLED=false` 会同时关闭实验路径。

当前已完成候选读取、真实模型网关复用、严格计划校验和单表编译的跨服务HTTP闭环，并已增加下述受控执行闭环。接口仍默认关闭，尚未接入产品页面。

## 受控执行（PR-3，2026-09-20）

公共入口为 `POST /api/v1/analysis/metrics/query`，请求与 plan 入口一致（问题 + 整数数据源ID）。它签发 `purpose=metric-query` 的委托，scope 为 `metrics:read`、`model:invoke`、`metric:compile`、`metric:execute` 四项。共享的授权、候选、模型和编译内部入口同时接受 `metric-plan` 与 `metric-query` 两种委托；新的执行入口 `/internal/graph/metrics/execute` 只接受 `metric-query`，plan-only 委托无法触发查询。

图服务在 `/internal/v1/metrics/query` 上运行七节点流程：授权 → 候选 → 模型计划 → 严格校验 → 编译 → **执行** → 回答。执行节点仍只上传统一验证过的计划，SQL 继续留在旧后端。

后端执行边界：

- 执行前按发布版本重新编译（与编译入口同一套校验），不信任图服务传回的任何 SQL 或中间状态；
- 编译后、执行前重新校验账号、成员关系与模型权限；数据源必须属于当前工作空间；
- 复用现有 `exec_sql` 只读防线（首关键字白名单与危险模式检查），非只读语句直接失败；
- 执行在线程池中进行，受 `GRAPH_METRIC_EXECUTION_TIMEOUT`（默认 15 秒）限制；超时返回 `metric_execution_timeout`，不可取消的底层任务记录为后台状态未知；
- 返回行数受 `GRAPH_METRIC_MAX_ROWS`（默认 200）截断，超出时 `truncated=true`；
- 并发执行受独立信号量限制（4），排队超时返回 `gateway_busy`。

执行结果契约为 `columns`、`rows`（字典行）、`row_count`、`truncated`、`elapsed_ms` 与 `sql_fingerprint`；不含 SQL、公式、连接信息或密钥。图服务侧再次验证 metric 绑定、行数一致性与单元格标量类型，契约违规按 `gateway_unavailable` 失败。公共响应沿用字段白名单，追加执行字段与 `metric_execution_failed`、`metric_execution_timeout` 两个错误码。

新执行入口已加入用户中间件的精确路径豁免清单，豁免仅跳过登录令牌检查，入口自身仍强制服务身份与签名委托。

当前边界：测试中的真实查询跑在隔离 SQLite 文件上（连接层替换、只读检查与结果转换为真实代码）；尚未在真实客户数据源上执行过查询，未接入结果解释、图表推荐、下载或前端。下一步是在隔离测试库发布真实指标，用已验证的 Kimi 配置做真实问题 → 计划 → 执行冒烟，再接前端灰度入口。

## 安全与正确性修复（A包，2026-09-25）

### 行级权限在编译期注入

- `preview_metric_query_plan` 现在对非管理员调用旧引擎同一个 `get_row_permission_filters`，只查询指标所需的目录表；管理员（id=1）按旧规则豁免。
- 每条规则片段用数据源对应的 sqlglot 方言解析，列一律绑定到指标表（`"orders"."region"`），再与版本固定过滤和运行时过滤一起 AND 进 WHERE。SQL 指纹因此覆盖权限边界：不同行权限的用户得到不同指纹。
- 规则无法解析、含子查询、引用其他表或库名限定，或规则指向指标外的表，都返回 403 并拒绝执行（fail closed）。
- 预览结果新增 `row_permission_applied`。没有当前用户的调用直接 403，不再按"无限制"处理。
- 规则内容本身沿用旧引擎渲染逻辑，不做语义修改：旧逻辑中"用户缺少变量值时整条规则被跳过"的行为保持不变，属于既有行为，需单独评估。

### 规划提示词

- 提示词包含当前日期、星期与 `GRAPH_PLANNING_TIMEZONE`（默认 Asia/Shanghai），用于解析"上个月""近7天"等相对时间。
- 明确 `time_range` 为左闭右开，并给出"8月 → 2026-08-01 至 2026-09-01"等示例，与编译器的 `start <= time_field < end` 一致；要求输出不带时区偏移的本地时间。
- 图服务中未被使用、且与后端不一致的 `planning_prompt` 已删除。

### 统一截止时间

- 两种指标委托新增签名声明 `deadline`（绝对时间戳，= 签发时刻 + `GRAPH_REQUEST_TIMEOUT` − 2 秒，预算限制在 5–110 秒，且必须位于 `iat` 与 `exp` 之间）。后端与图服务都要求并校验该声明。
- 后端公共入口的外层超时改为同一预算；图服务每次回调超时取 `min(步骤上限, 剩余时间)`；后端模型调用取 `min(30 秒, 剩余时间)`，执行取 `min(GRAPH_METRIC_EXECUTION_TIMEOUT, 剩余时间)`。
- 剩余时间不足 0.5 秒时不再发起模型调用或查询，返回 `graph_deadline_exceeded`；这样外层超时不会先于内部步骤触发，也不会在调用方放弃后才开始跑 SQL。
- 两端时钟需同步（与 JWT 的 `iat`/`exp` 要求相同）。

## 准确率：召回、维度取值与评测（B包，2026-09-26）

### 候选召回

- 图服务使用的候选召回（`get_metric_candidates`）在原有"名称/别名包含"匹配之外，新增中文字符二元组重叠：别名的二元组有一半以上出现在问题中即可召回，比如"下单客户数"对应"下单的客户有多少"。
- `EMBEDDING_ENABLED` 且本地向量模型可用时，还会比较问题与指标名称、别名、描述、单位的语义相似度；相似度不低于 `GRAPH_METRIC_VECTOR_MIN_SIMILARITY`（默认 0.5）即可召回。
- 分数刻度：精确或包含匹配 500 分以上，语义相似度 ×400，字符重叠低于 100。明确的别名命中始终排在前面。
- 指标文本向量按版本和文本哈希缓存在进程内。模型加载失败时回退到纯字面召回，并暂停语义召回 10 分钟，避免每次提问都重试加载。
- 旧引擎的 SQL 提示词（`get_metric_prompt`）召回行为不变。

### 维度已知取值

- 新表 `metric_dimension_value`（迁移 075），按"版本 + 维度"存一份取值列表 `[{"value", "label"}]`，状态为 `ok`、`high_cardinality` 或 `failed`，来源为 `sampled` 或 `manual`。已发布版本本身保持不可变。
- 抽样 SQL 由编译器生成：`SELECT 维度 ... WHERE 维度非空 AND 版本固定过滤 GROUP BY 维度 ORDER BY COUNT(*) DESC LIMIT 上限+1`，并经过只读检查。去重值超过 `GRAPH_DIMENSION_VALUE_LIMIT`（默认 50）时记为高基数，不列出；超过 64 字符的值会被丢弃。
- 抽样的触发时机：
  - 发布成功并提交后，在后台执行；
  - 管理员调用 refresh 接口时同步执行；
  - 某个版本第一次被规划、库里还没有取值时，在后台补上，每个版本每小时最多一次。
- 自动抽样（前两种以外的）只在 `GRAPH_EXPERIMENT_ENABLED` 与 `GRAPH_DIMENSION_SAMPLING_ENABLED` 同时开启时运行；手工 refresh 不受此限制。
- 管理员可以用 PUT 接口手工维护取值和业务名称，例如 `online` → 线上；手工列表不会被抽样覆盖，DELETE 后恢复抽样。
- 抽样是系统级的，不套用行权限。因此当提问者在该指标表上有行权限规则时，提示词里不附带取值。
- 取值只进入后端构造的规划提示词（候选的 `dimension_values` 字段），不进入图服务的候选契约，也不返回给用户。提示词要求：已列出取值的维度，过滤值必须从列表中原样复制，问题中出现的业务名称要换成对应的 value。

### 评测

`evaluations/metric_suite` 是虚构销售指标库（9 个指标、43 道题）。标准答案由独立的 Python 计算器根据标准计划算出，`evaluations/metric_eval.py` 按生产链路逐题执行并比较结果行。

脚本模式（模型直接返回标准计划、不开语义召回）的当前结果：

| 召回方式 | 正确指标进入候选 | 通过 |
| --- | --- | --- |
| 旧的精确匹配 | 88% | 38/43 |
| 新的混合召回 | 93% | 40/43 |

剩下 3 道都是只能靠语义召回找到的改写问法。所有召回成功的题，编译后的 SQL 结果都与标准答案一致。真实模型的准确率需要用 live 模式配置模型后测量。

## 规划循环：有界修复、澄清与多轮追问（C包，2026-09-26）

### 有界修复

- 计划校验失败（`metric_plan_invalid`、`metric_not_authorized`、`metric_dimension_not_allowed`、`metric_filter_not_allowed`、`metric_time_range_not_allowed`）或编译返回 `metric_compile_failed` 时，图会带着被拒的回复和错误码重新调用模型，最多 `GRAPH_MAX_PLAN_REPAIRS` 次（图服务环境变量，默认 2，上限 3）。
- 权限、网关和超时类错误不会重试。所有重试仍受 A 包的统一截止时间约束。
- `/internal/graph/metrics/model` 新增 `repairs: [{previous, error}]`（最多 3 条，错误码限定在上面的白名单内）。后端把它们还原成"模型上一轮回复 + 拒绝原因说明"两条消息，说明文字固定在 `REPAIR_HINTS`，不透传任意文本。
- 响应新增 `repairs`（重试次数）。`model_calls` 与 `usage` 在多次调用间累加；只要有一次用量未知，总量就记为 null。

### 澄清与拒答

- 模型可以不给计划，而是返回 `{"clarification": "<一句话追问>", "reason": "ambiguous" | "unsupported"}`：
  - `ambiguous`：两个以上候选同样合适；
  - `unsupported`：没有候选能回答。
- 提示词要求：有合理默认值时不要追问（没说时间就不限时间，没说分组就不分组）。
- 响应状态为 `needs_clarification`，并附带 `clarification` 和 `clarification_reason`；不编译、不执行。格式不合法的澄清回复按 `metric_plan_invalid` 进入修复。
- 聊天里以普通文字显示，前缀为"需要确认："或"暂时无法回答："，存入记录的 error 字段（旧前端把纯文本 error 渲染为正文）。用户的下一句话作为追问进入多轮上下文。
- 暂不使用 LangGraph interrupt/checkpoint 恢复：当前每次提问都是一次同步运行，"恢复"就是下一次带上下文的提问。

### 多轮追问

- 问题请求新增 `context`：同一会话里更早的最多 3 个问题（按时间正序，每条不超过 2000 字）。聊天入口会排除分析、预测和数据源占位记录。
- `context` 纳入签名的 `request_hash`：`sha256(datasource_id + "\n" + question [+ "\n" + JSON(context)])`。没有上下文时哈希与之前完全相同。上下文被篡改、增删都会返回 401。
- 召回先按当前问题找候选，不足 10 个时再用上下文补充；因此"那7月呢？"也能找回上一问的指标。
- 提示词列出之前的问题，要求沿用之前的指标、维度、过滤和时间，除非本次问题明确改变了它们。
