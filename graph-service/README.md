# 第一阶段：独立图服务与测试基线

这是可运行的开发骨架，不是已完成迁移的产品后端。
PR-1 已增加默认关闭的模型网关与同步实验入口，配置见 [网关契约](../docs/langgraph/GATEWAY_CONTRACT.md)。以下 /demo/* 仍为模拟回归。
真实使用 LangGraph StateGraph 编排、LangChain Core 的模型接口和消息格式；当前模型为 FakeListChatModel，返回案例预设 SQL，不具备真实自然语言理解能力，不产生 API 费用。

## 启动

在本目录使用 Python 3.11 或 3.12 和 uv：

```powershell
uv sync --locked
uv run --frozen pytest -q
uv run --frozen python evaluate.py
uv run --frozen uvicorn app.main:app --host 127.0.0.1 --port 8030
```

打开 http://127.0.0.1:8030/docs，选择 POST /demo/run，点击 Try it out，提交：

```json
{"case_id":"net"}
```

结果应为净销售额 1420。GET /demo/cases 列出全部 20 个固定案例。
POST /demo/stream 接收相同参数，按节点返回 SSE 进度，最后发送 completed。
演示接口只接受预定义 case_id，不接受用户身份、任意 SQL、连接信息或任意自然语言问题。

## 当前执行路径

```text
authorize → retrieve → generate → validate → execute → answer
任一步拒绝或失败 → 结束
```

- 数据：每次调用创建并销毁内存 SQLite，只有 5 条合成订单。
- 口径：net = gross - refund，金额为示例整数。
- 查询：仅单表 SELECT；明确拒绝多语句、写操作、跨表和复杂查询形态。执行层还有 SQLite 授权回调、只读模式、行数限制及计算超时。
- 身份：服务器内固定的 demo Principal，不代表已集成产品登录和租户权限。
- 事件：带 run_id 和递增 sequence，只投影允许公开的字段，不转发整份图状态。
- 依赖：独立虚拟环境和锁文件，不能将它的依赖安装到旧 backend 环境。

## 测试解释

20 条固定问题覆盖总额、净额、退款、区域/月分组、均值、排序、空结果等。
问题与 SQL 都是人工预置，评估的是流程、查询结果和接口契约；通过率不是模型正确率。
另外覆盖危险 SQL、未知字段、无权限、执行前权限撤销、模型异常、并发结果隔离及 SSE 终止。

P1 仍需补充真实旧引擎的结果、耗时、用量对照；当前 evaluate.py 只记录模拟运行的耗时，不能拿来推断生产延迟或成本。

## 明确未实现

真实模型供应商端到端验证、指标/记忆适配、旧 SSE 映射、前端切换、图表生成、重试修复、澄清中断、持久 checkpoint、取消和生产部署。模型网关与短期身份委托已在 PR-1 实现并进行离线测试。
图状态暂时包含小型合成结果；生产适配必须改用授权结果引用和保留期。
本演示只绑定 localhost，不用于公网服务，不连接客户数据库、不加载旧 .env、不执行业务迁移。

## 下一阶段

定义并实现经过鉴权的模型/数据源网关；保持图节点独立，不能把原 LLMService 整体塞进一个节点。先用测试账号、测试数据库验证，再接入现有前端。
