# AdaptiveBI-LangGraph：下一步开发任务

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
