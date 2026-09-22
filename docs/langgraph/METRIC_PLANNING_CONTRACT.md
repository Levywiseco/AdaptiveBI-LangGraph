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
3. `/internal/graph/metrics/model` 只提交候选ID和版本ID，后端再次读取候选并构造模型提示；
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
