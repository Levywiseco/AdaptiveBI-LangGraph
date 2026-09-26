# Adaptive API 与状态约定

所有路径都位于 `/api/v1` 下，并继续使用 SQLBot 原有登录令牌、工作空间和权限依赖。

## 指标库

| 方法与路径 | 作用 | 权限 |
|---|---|---|
| `GET /system/metrics/page/{page}/{size}` | 分页查询指标 | 工作空间管理员 |
| `POST /system/metrics` | 创建指标定义和 v1 草稿 | 工作空间管理员 |
| `GET /system/metrics/{id}` | 查看定义与全部版本 | 工作空间管理员 |
| `PUT /system/metrics/{id}` | 修改定义元数据 | 工作空间管理员 |
| `POST /system/metrics/{id}/versions` | 从新内容创建下一草稿版本 | 工作空间管理员 |
| `POST /system/metrics/{id}/versions/{version_id}/publish` | 校验并发布指定版本 | 工作空间管理员 |
| `POST /system/metrics/{id}/query-plan/preview` | 按精确版本编译受约束的单表只读 SQL（非管理员套用行权限，结果含 `row_permission_applied`） | 工作空间管理员 |
| `GET /system/metrics/{id}/versions/{version_id}/dimension-values` | 查看版本各维度的已知取值、状态与来源 | 工作空间管理员 |
| `POST /system/metrics/{id}/versions/{version_id}/dimension-values/refresh` | 立即从数据源抽样取值（保留手工维护的维度） | 工作空间管理员 |
| `PUT /system/metrics/{id}/versions/{version_id}/dimension-values/{dimension}` | 手工维护取值与业务名称，如 `{"values": [{"value": "online", "label": "线上"}]}` | 工作空间管理员 |
| `DELETE /system/metrics/{id}/versions/{version_id}/dimension-values/{dimension}` | 删除手工取值，恢复抽样 | 工作空间管理员 |
| `DELETE /system/metrics/{id}` | 归档指标 | 工作空间管理员 |
| `POST /system/metrics/{id}/restore` | 恢复归档指标 | 工作空间管理员 |

已发布 `metric_version` 不允许就地修改。发布新版本后，旧发布版成为 `superseded`，历史 `retrieval_trace` 仍能说明旧答案采用了哪个版本。

创建示例：

```json
{
  "code": "net_sales",
  "name": "净销售额",
  "aliases": ["实收销售额", "净收入"],
  "description": "已支付订单金额减优惠金额",
  "datasource_id": 1,
  "expression": "amount - discount_amount",
  "aggregation": "SUM",
  "time_field": "paid_at",
  "required_tables": ["adaptive_demo_sales"],
  "dimensions": ["region"],
  "filters": [{"field": "status", "operator": "=", "value": "paid"}],
  "unit": "元"
}
```

查询计划示例：

```json
{
  "version_id": 1,
  "dimensions": ["region"],
  "filters": [{"field": "region", "operator": "in", "value": ["华东", "华南"]}],
  "time_range": {
    "start": "2026-08-01T00:00:00",
    "end": "2026-09-01T00:00:00"
  },
  "limit": 1000
}
```

响应包含 `metric_version_id`、规范化后的 `applied_filters`、左闭右开的时间语义、`sql`、SHA-256 `sql_fingerprint` 和编译器版本。`metric-plan-v1` 只支持恰好一张必需表；未声明的维度、未声明的运行时筛选字段、歧义字段、非标量值和多表指标都会返回 422，不会降级为自由拼接 SQL。

## 记忆

| 方法与路径 | 作用 |
|---|---|
| `GET /system/memories/page/{page}/{size}` | 按 `scope/status/datasource_id/keyword` 查询 |
| `POST /system/memories` | 新建个人或共享记忆 |
| `GET /system/memories/{id}` | 查看记忆 |
| `PUT /system/memories/{id}` | 编辑并递增版本 |
| `POST /system/memories/{id}/status` | 在 `active` 与 `paused` 间切换 |
| `DELETE /system/memories/{id}` | 软删除并停止召回 |

普通用户只能管理自己的 `personal` 记忆。`workspace` 记忆需要管理员权限。共享 SQL 案例必须声明表和字段依赖；每次召回时依赖都会按当前用户的数据权限重新检查。疑似密码、API key、访问令牌或 secret 值会返回 422。

## 反馈

| 方法与路径 | 作用 |
|---|---|
| `POST /feedback` | 为本人拥有的答案提交反馈 |
| `GET /feedback/page/{page}/{size}` | 查看本人反馈历史 |

`feedback_type` 可为：

- `correct`：生成待审核的确认答案候选。
- `result_wrong`：必须提供修正说明，生成业务规则候选。
- `metric_wrong`：必须提供口径说明，审批后仍需人工发布指标新版本。
- `remember_preference`：用户明确填写的个人偏好经规则检查后立即生效。
- `save_example`：必须提供说明，生成待审核共享 SQL 案例。

调用方必须提供 8–128 字符的 `idempotency_key`。相同工作空间、用户和幂等键的重复请求返回原事件，并设置 `duplicate: true`。

## 学习中心

| 方法与路径 | 作用 | 权限 |
|---|---|---|
| `GET /system/learning/page/{page}/{size}` | 查询候选 | 工作空间管理员 |
| `GET /system/learning/stats` | 查询状态统计 | 工作空间管理员 |
| `GET /system/learning/{id}` | 查看候选与答案快照 | 工作空间管理员 |
| `POST /system/learning/{id}/approve` | 审批并激活允许的经验 | 工作空间管理员 |
| `POST /system/learning/{id}/reject` | 拒绝候选 | 工作空间管理员 |
| `POST /system/learning/{id}/revoke` | 撤销已激活/已批准候选 | 工作空间管理员 |

审批和拒绝都要求至少 3 个字符的 `review_note`。共享 SQL 必须通过单条只读查询检查；指标纠错的审批状态为 `requires_metric_version`，不会直接生成可召回的标准口径。

## 运行开关

| 环境变量 | 关闭后的行为 |
|---|---|
| `ADAPTIVE_METRICS_IN_ANSWERS` | 停止在新回答中召回指标，管理和历史数据保留 |
| `ADAPTIVE_MEMORY_IN_ANSWERS` | 停止在新回答中召回记忆，管理和历史数据保留 |
| `ADAPTIVE_FEEDBACK_WRITES_ENABLED` | 新反馈返回 503，已有反馈仍可查看 |
| `ADAPTIVE_SHARED_LEARNING_ACTIVATION_ENABLED` | 新审批返回 503，仍可查看、拒绝或撤销 |

修改环境变量后重启后端。开关用于试点和止损，不能代替数据库备份或版本回滚。
