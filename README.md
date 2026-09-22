# AdaptiveBI LangGraph

以我们当前 Adaptive 项目为基线的独立 LangGraph 重构实验仓库。

**当前状态：独立图服务、指标计划与受控只读执行闭环已打通，聊天灰度路由已实现（默认关闭）；原网页默认仍走旧引擎。**

- [完整迁移方案与架构图](docs/langgraph/MIGRATION_PLAN.md)
- 基线提交：`55df5be1fb8bef1b2d912e545fecf4f484565d38`
- 基线包含界面、指标库、记忆、反馈治理、数据源权限、操作审计及密码管理。
- [第一阶段启动与验证](graph-service/README.md)
- 已有模型适配包含 Kimi、MiniMax 等供应商；新增默认关闭的真实模型网关适配，真实供应商端到端尚未验证。
- [PR-1 网关契约、独立测试配置与验证边界](docs/langgraph/GATEWAY_CONTRACT.md)
- [PR-2/PR-3 指标计划与受控执行契约](docs/langgraph/METRIC_PLANNING_CONTRACT.md)
- [PR-4 聊天引擎灰度路由（CHAT_ENGINE）](docs/langgraph/CHAT_ENGINE_ROUTING.md)
- /demo/* 继续使用模拟模型；实验问答只查询合成数据与隔离测试库，尚未接入旧页面。

本仓库按自己的业务需求推进，不以 SQLBot 原始仓库或官方材料作为后续实现参考。
已有代码的许可证与版权声明保留，见 [LICENSE](LICENSE)。

仓库不包含本机运行配置、数据库或 API 密钥。初始化环境应使用新的配置和独立测试数据库。
运行中的原项目不会因本仓库的设计或提交自动切换引擎。
