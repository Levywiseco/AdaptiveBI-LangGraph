# 聊天引擎灰度路由

更新日期：2026-09-22

本文描述 `CHAT_ENGINE` 灰度开关：如何让指定工作空间的网页问答走新 LangGraph 指标引擎，其余流量继续走旧引擎，且前端不改动。

## 开关与门控

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `CHAT_ENGINE` | `legacy` | `legacy` 全部走旧引擎；`langgraph` 启用灰度路由 |
| `CHAT_ENGINE_WORKSPACES` | 空 | 逗号分隔的工作空间 ID 白名单；为空或格式非法时按未启用处理 |

一次聊天提问走新引擎需同时满足：

1. `CHAT_ENGINE=langgraph`；
2. 提问者所属工作空间在 `CHAT_ENGINE_WORKSPACES` 白名单内；
3. 会话已绑定数据源，且该数据源在 `GRAPH_METRIC_DATASOURCES` 白名单内；
4. 网关配置完整（`GRAPH_EXPERIMENT_ENABLED` 与三个服务秘密）；
5. 仅交互式网页问答（`in_chat`）；MCP、嵌入式等入口暂不路由。

任一门控不满足即回落旧引擎——这是路由决策，不是失败回退。按迁移方案约定，**进入新引擎的请求失败后不会自动转回旧引擎重跑**，错误以聊天错误事件呈现。白名单或数据源配置写错时一律按"未启用"处理（fail closed）。

## 路由位置与事件兼容层

路由点在旧 `stream_sql` 入口最前端（`apps/chat/api/chat.py`）。命中灰度时由 `apps/graph_gateway/chat_stream.py` 完成：

1. 复用 `save_question` 创建聊天记录（历史列表照常工作）；
2. 调用与 `/api/v1/analysis/metrics/query` 完全相同的 `run_metric_query` 受控执行链路（签名 metric-query 委托、后端重新编译、只读执行、行数上限）；
3. 全部落库（记录 sql/data/chart、重命名会话标题、finish 或 error）后，把结果映射为旧引擎 SSE 事件序列：`id → question → datasource → brief → sql-result → info → sql → sql-data → chart → finish`，失败时为 `id → question → datasource → error`；
4. 图表降级为表格（`{"type": "table", ...}`），列信息来自真实结果并推断数值列。

前端 `ChartAnswer` 等组件按既有 `type` 分发渲染，无需改动。

## 安全边界

- 前端展示的"SQL"是受治理查询摘要（指标、版本、维度、时间范围、行数、截断标记、SQL 指纹），不是 SQL 语句；真实 SQL 仍只在旧后端生成与执行。
- 结果数据沿用 `GRAPH_METRIC_MAX_ROWS` 截断并在摘要中标注。
- 路由决策记录安全日志（`chat_engine_routed`：engine、record_id、user、workspace、datasource）。

## 当前边界（第一阶段）

- 图服务先执行完再一次性映射事件，无 token 级流式输出；`id` 事件会在模型调用完成后才到达（等待期前端显示思考状态）。
- 无结果解释、图表推荐、下载；图表固定为表格。
- 未匹配到已发布指标时返回明确错误提示，不回退旧引擎。
- 每次请求的路由结果不落库（仅日志）；澄清中断、取消、checkpoint 属于后续持久化任务。

## 回退

将 `CHAT_ENGINE` 改回 `legacy`（或清空 `CHAT_ENGINE_WORKSPACES`）并重启即可。新引擎已写入的聊天记录保持旧结构（sql/data/chart 字段），历史页照常渲染；不影响旧引擎数据。
