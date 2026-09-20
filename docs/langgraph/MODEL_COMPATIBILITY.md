# 模型兼容范围

更新日期：2026-09-20

图服务不直接保存供应商密钥。旧后端继续从 `ai_model` 读取当前工作空间获授权的模型配置，通过 `LLMFactory` 调用模型。实验链路由服务端固定 `GRAPH_MODEL_ID`，浏览器不能提交模型地址、API Key 或任意模型 ID。

## 当前支持状态

| 类型 | 接口要求 | 网关行为 | 验证状态 |
| --- | --- | --- | --- |
| Kimi | OpenAI-compatible Chat Completions | 非流式、无SDK自动重试、25秒SDK超时 | `kimi-k3` 已完成一次隔离真实查询验证 |
| 阿里云 Model Studio Qwen | `dashscope` OpenAI-compatible endpoint，模型名以 `qwen` 开头 | 在公共限制上增加 `extra_body.enable_thinking=false` | 策略和实际 `ChatOpenAI` 构造已离线验证；尚无Qwen测试密钥，未做真实调用 |
| 其他 OpenAI-compatible 模型 | `protocol=1` 且响应兼容 Chat Completions | 使用公共限制，不附加供应商专用参数 | 通用代码和测试替身已验证；各供应商仍需单独冒烟 |

公共限制为：`streaming=false`、`max_retries=0`、`timeout=25`、`max_tokens=2048`，外层调用上限30秒。网关不会继承模型记录中的任意请求参数，避免已保存配置覆盖这些边界。用量优先读取标准 usage metadata；供应商不返回时保留为 `null`。

Qwen 的思考模式会产生供应商专用输出和流式约束。本链路只需要最终 SQL，因此对阿里云 Model Studio 的 Qwen 明确关闭思考模式。判断同时绑定模型名和 `dashscope` 主机，名称相同但来自其他兼容服务的模型按通用路径处理，避免向未知服务注入专用参数。

## 配置 Qwen

在原有模型管理中增加或编辑模型：

- `protocol` 选择 OpenAI-compatible（数据库值为1）。
- `base_model` 使用账号实际开通的 Qwen 模型 ID，例如 `qwen-plus`。
- `api_domain` 使用所在地域的 Model Studio OpenAI-compatible 基础地址，并在后端加密保存。
- 模型必须启用，并映射到试验用户所在工作空间。
- 将后端 `GRAPH_MODEL_ID` 设置为该模型配置 ID，重启独立测试后端后执行合成数据冒烟。

当前每个测试部署固定一个 `GRAPH_MODEL_ID`。尚未提供每次提问选择模型、自动故障切换或多模型投票；这些行为会改变权限、成本和可重复性，应作为独立功能实施。

Qwen 真实冒烟应使用独立测试账号和合成数据，记录模型配置 ID、调用数、用量、耗时及结果，不把 API Key、endpoint 密文或完整响应写入报告。
