# PR-1：真实模型网关与任意合成数据问题

日期：2026-09-18。实现基线：437ff82f26285cdf340f76db3e02a1caf2a74a97。
开始时工作区干净，foundation 的 GitHub PR #1 已合并；远端 main `0c3c0ba5570753814c3eb843435b414687aa3635` 与该基线文件无差异。
本交付包称 PR-1（执行计划编号），不等于 GitHub 拉取请求编号。

## 请求路线

登录测试用户 → 旧后端 /analysis/query → graph-service /internal/v1/query
→ 旧后端 /internal/graph/authorize 与 /internal/graph/model
→ LLMFactory 的获授权模型 → 新图校验 → 临时 SQLite → 安全响应。

旧后端路由注册在 apps/api.py 的 api_router 下，实际前缀为 settings.API_V1_STR，默认 /api/v1；有 CONTEXT_PATH 时也要加到 GRAPH_GATEWAY_URL 和测试客户端地址中。
旧 /chat/question 等正常入口保持原逻辑。没有前端改版或引擎自动切换。

## 接口与契约

| 接口 | 身份 | 输入 | 输出 |
| --- | --- | --- | --- |
| POST /api/v1/analysis/query | 现有 X-SQLBOT-TOKEN: Bearer 登录令牌 | question（1–2000字符，非空白）、datasource_id（只能 synthetic-sales，默认此值） | SafeResponse；旧响应中间件包装为 code/data/msg |
| POST /internal/v1/query（图服务） | 服务凭据 + 签名委托 | 上述字段 + 服务端 run_id | 未包装的 SafeResponse |
| POST /api/v1/internal/graph/authorize | 图服务凭据 + 签名委托 | 内部问题请求 | authorized=true；否则拒绝 |
| POST /api/v1/internal/graph/model | 同上，且重新校验模型可用性 | 内部问题请求 | 仅 content、usage、model_calls、耗时或安全错误码 |
| POST /api/v1/analysis/metrics/plan | 现有登录令牌 | question、整数 datasource_id | 指标版本、维度、时间范围、单位、SQL指纹和模型用量；不返回SQL |
| POST /internal/v1/metrics/plan（图服务） | 服务凭据 + 指标签名委托 | 上述字段 + 服务端 run_id | 未包装的 MetricPlanResponse |
| POST /api/v1/internal/graph/metrics/{authorize,candidates,model,compile} | 图服务凭据 + 指标签名委托 | 绑定同一问题、数据源和run；model只加候选ID，compile只加严格计划 | 当前权限检查、脱敏候选、模型JSON或脱敏编译元数据 |
| /demo/* | 保持仅用于本地演示 | 原来的 case_id | synthetic 模式，模拟模型；同步响应也已过滤整份状态 |

公共请求例子：

```json
{"question":"八月东部扣除退款后的销售额是多少？","datasource_id":"synthetic-sales"}
```

SafeResponse 字段：mode=synthetic-live、run_id、model_config_id、status、columns、rows、truncated、answer、error、usage、model_calls、elapsed_ms。
不返回 SQL、schema、问题提示词、内部推理、API Key、连接串或委托令牌。结果说明由确定性代码生成。
外部额外字段（Principal/user_id/oid/API Key/模型地址/SQL等）返回422，不会进入模型或查询。

本包实验接口只实现同步 HTTP，不提供 live SSE、后台任务、断线续传或持久恢复。
已有 demo SSE 只投影允许字段，仍只用于模拟回归。不要把没有实现的 live SSE 当作可用接口。

## 服务身份和委托

- BACKEND_TO_GRAPH_TOKEN 与 GRAPH_TO_GATEWAY_TOKEN 是两个方向的独立服务凭据。
- GRAPH_DELEGATION_SECRET 用于 HS256 签名，三个值必须不同且各至少32字符，独立于登录 SECRET_KEY。
- 委托由旧后端生成，绑定 issuer、audience、用户、工作空间、run_id、model_id、synthetic-sales、scope、purpose、问题哈希、iat/exp，最长120秒。
- 图服务验证请求绑定后，在节点执行前向旧后端复核；模型网关重新读取用户状态、当前工作空间、成员关系和可用模型，不依赖旧登录缓存。
- 服务信任模型：两个进程属于同一受控服务域，共享委托验证密钥；不是针对图进程被攻陷的零信任隔离。服务密钥不得交给浏览器。跨机器使用 TLS 和受控内部网络。
- 旧 TokenMiddleware 仅对两个精确内部路径交给独立服务认证处理，不添加通配白名单。用户接口仍走登录认证。
- 助手相关导入改成在助手验证方法内加载，避免内部服务认证测试被原有 xpack 循环导入阻塞；未修改助手验证规则。
- 本包无持久 run 注册表或防重放账本，同一委托有效期内重复提交可能重复调用模型。不能宣称 exactly-once；客户端失败后不自动重试。

## 模型和执行边界

- 服务端 GRAPH_MODEL_ID 固定选一个已启用的 OpenAI-compatible 配置，复用 get_ai_model_list_by_workspace、get_default_config、LLMFactory；不允许自报模型和URL，也不自动回退其他模型。
- get_default_config 的实际返回 model_id 必须匹配委托，防止配置删除后隐式回退默认模型。
- 网关自行构造合成数据 prompt，只发送允许的 schema 和问题，不接收任意角色消息；模型密钥不离开旧后端。
- 原后端依赖未升级。新服务仅新增其运行所需的 httpx、PyJWT，并更新独立锁文件。
- 并发模型调用每个旧后端进程最多4个；排队0.2秒后拒绝。SDK自动重试关闭，SDK超时25秒，外层调用上限30秒，输出上限2048 token。
- 图服务授权检查HTTP超时3秒，模型HTTP超时35秒，旧入口HTTP超时45秒。超时不保证供应商未计费或远端瞬间停止，因此未返回用量时记录 null。
- 用量优先读取 LangChain usage_metadata，再读取已有 response_metadata.token_usage；缺失项为 null，不估算成0。网关以 run_id、model_config_id、次数、用量和耗时记录结构化日志，不记录秘密、原始提示词或完整响应。
- 模型调用期间不持有业务 ORM Session。没有自动纠错和跨层重试。
- 空输出、代码围栏、说明文字等明确失败；危险 SQL 被拒绝。保持合成 SQLite 的单表、只读、字段及函数限制；本轮不拓宽支持的SQL方言和查询形态。

## 独立测试环境的运行方式

先准备本仓库的独立测试后端、测试元数据库、登录测试账号和模型配置。禁止复制旧 .env 或连接原项目数据库。原 backend/main.py 的正常启动会运行已有迁移，因此必须先核对它指向独立测试数据库；本次没有启动它或执行迁移。

后端独立配置需要：GRAPH_EXPERIMENT_ENABLED=true、GRAPH_TEST_USERS（逗号分隔用户ID，不加空格）、GRAPH_TEST_WORKSPACES（同样格式）、GRAPH_METRIC_DATASOURCES（允许用于指标实验的逗号分隔整数ID）、GRAPH_MODEL_ID、GRAPH_SERVICE_URL，以及三个独立服务秘密。账号还必须当前启用并属于该工作空间，模型也必须可用。

图服务配置与后端匹配三个服务秘密及 GRAPH_METRIC_DATASOURCES，设置 GRAPH_EXPERIMENT_ENABLED=true 和 GRAPH_GATEWAY_URL（包含实际API前缀）。新服务不加载后端 .env；可以使用自己的 graph-service/.env。

```powershell
# 在 graph-service 目录，使用新服务自己的配置；测试实例可改端口以免影响已有演示。
uv sync --locked
uv run --frozen --env-file .env uvicorn app.main:app --host 127.0.0.1 --port 8031
```

如果使用8031，后端 GRAPH_SERVICE_URL 也设置为 http://127.0.0.1:8031。
通过旧登录接口取得测试用户令牌，再对公共实验入口提交问题。不要把令牌、模型密钥写进仓库或测试报告。

## 验证命令与性质

```powershell
# graph-service
uv sync --locked
uv run --frozen pytest -q
uv run --frozen python evaluate.py

# 仓库根目录，使用现有旧后端环境的 Python，不升级旧依赖
$env:PYTHONPATH = "$PWD/backend"
$env:HF_HUB_OFFLINE = '1'
<backend-python> -m pytest tests -q
```

离线测试包含临时元数据库、真实JWT签名/验证、TokenMiddleware、工作空间/模型授权、权限撤销、工厂调用边界、格式错误、超时、未知用量、响应过滤。HTTP供应商和模型输出使用测试替身，不能当成真实模型结果。没有启动完整旧后端生命周期。

真实冒烟需在当前 shell 中设置 ADAPTIVE_LIVE_TEST=1、ADAPTIVE_TEST_ENV_CONFIRMED=1、ADAPTIVE_TEST_API_URL（包含/api/v1）、ADAPTIVE_TEST_LOGIN_TOKEN（测试登录token，不带Bearer），然后执行：

```powershell
uv run --frozen python live_smoke.py
```

脚本只请求合成数据，预期东部八月净额770；报告实际模型配置ID、调用次数、用量、耗时和是否匹配。缺少条件打印 not_executed，退出码2，不调用模拟模型冒充成功。

## 回退

将两个进程的 GRAPH_EXPERIMENT_ENABLED 设为 false 并重启对应测试服务；新入口返回404，旧问答继续原有路径。本包没有数据库迁移。若回退代码，按本PR提交整体 revert，同时恢复图服务锁文件；无需修改客户数据库。

## 后续一步：跨进程 HTTP 联调（2026-09-18）

新增 `tests/test_graph_gateway.py::test_real_http_roundtrip_with_stub_provider`，启动临时旧后端测试应用和独立依赖环境中的图服务，通过真实本机 HTTP 完成公共入口、签名委托、授权回调、模型网关及 SQLite 执行的闭环。只替换供应商模型和登录缓存读取；登录 JWT、TokenMiddleware、响应包装和数据库权限检查使用实际实现。

两项联调通过：东部八月净额返回770；DELETE被拒绝。两项均同时检查未登录及缺少服务身份的请求不会调用模型、安全响应不泄露内部字段、未知用量保持null。测试使用内存元数据库、随机服务秘密和临时端口，退出时关闭两个测试服务，不加载生产启动生命周期、不迁移数据库、不调用真实供应商。

运行方式：在仓库根目录设置 `PYTHONPATH` 为本仓库 backend，使用旧后端依赖环境执行 `python -m pytest tests/test_graph_gateway.py -k real_http -q`；需要先创建 graph-service 的独立虚拟环境。缺少该环境时显式跳过，不视作联调通过。

这补齐了两个运行时之间的 HTTP 验证，不能替代真实模型冒烟；PR-2 业务扩展前仍需配置独立测试模型并通过 live_smoke.py。

## PR-2 指标计划闭环（2026-09-20）

新增独立的指标委托用途和 scope，形成登录入口、图服务状态机、指标候选读取、模型计划、严格解析及旧后端固定编译器之间的 HTTP 往返。候选在模型调用前重新读取；模型只收到安全候选，不接触公式和连接信息；编译阶段再次读取发布版本并复查权限。图服务与公共后端边界都使用响应白名单。

跨进程测试启动两套独立依赖环境和临时后端，通过真实本机HTTP验证：匿名调用和缺少服务身份的调用在模型前被拒绝；已登录调用完成候选、模型和编译链路，返回精确指标版本、单位、SQL指纹和真实测试用量字段。测试中的指标目录、编译结果和供应商模型均为替身，不连接客户数据库，不执行生成的SQL。
