# 平台模型网关、额度与结算（P3）

P3 在独立云端应用中增加模型目录、授权、供应商凭证、原生协议转发和事务账本。本地 Harness 通过 sidecar 的账号专属 loopback 中继调用平台；没有供应商 Key 或自带 Key 的桌面配置。云端不运行 Harness 或工具。

## 配置与启用

启动、管理员初始化和桌面设备登录沿用 [cloud-identity.md](cloud-identity.md)。P3 的 schema 为云端 v3、本地 v1；P4 已升级为云端 v4、本地 v2，SQLite/PostgreSQL 使用对应迁移，详见 [platform-resources.md](platform-resources.md)。

默认没有供应商、模型、模型授权或预算，不能直接消费真实模型。管理员取得账号 access token 后依次操作：

1. `PUT /api/model-admin/providers`：提交 `provider_id`、`base_url`、`api_key`，以及可选 `free_rejection_statuses`。地址为供应商 API base，例如包含 `/v1` 的 OpenAI/Anthropic 地址。必须 HTTPS；开发时仅 loopback 可以 HTTP。Key 使用既有 Fernet 能力密封，任何查询都不返回 Key。
2. `PUT /api/model-admin/models`：提交逻辑 `model_id=platform/<name>`、协议、能力、输入/输出上限、费率版本和有序 `routes`。每个 route 指定 `provider_id` 与供应商模型名。模型停用同样通过此接口设置 `enabled=false`。
3. `PUT /api/model-admin/users/{user_id}/grants/{name}`：用 `{"allowed":true}` 显式授权逻辑模型；`false` 撤销。管理员身份不自动获得模型消费授权。
4. `PUT /api/model-admin/users/{user_id}/budget`：提交 `currency` 与 `limit_nanos`，设置该币种的绝对消费上限；不能降到已结算加已预占金额以下。这是预算设置，不是支付、充值或账本改写接口。

模型定义合同示意如下。所有数字仅演示测试费率，**不是生产价格或模型默认参数**；实际供应商、币种、费率、原生模型限制和无消费错误合同需要由部署方填写。

```json
{
  "model_id": "platform/test",
  "name": "Test model",
  "protocol": "chat_completions",
  "input_modalities": ["text", "image"],
  "tools": true,
  "input_token_ceiling": 128000,
  "output_token_ceiling": 4096,
  "rate": {
    "revision": "test-rate-001",
    "currency": "USD",
    "effective_at": 0,
    "uncached_input": 3,
    "cache_read": 1,
    "cache_write": 4,
    "output": 8
  },
  "routes": [{"provider_id": "supplier", "upstream_model": "native-model-name"}],
  "enabled": true
}
```

`GET /api/model-admin/providers` 和 `/api/model-admin/models` 为管理员提供配置查询。费率 revision 不可修改，变更价格必须使用新 revision；相同 revision 重复提交相同内容是幂等操作。费率尚未到 `effective_at` 的模型不在可用目录中。更新协议或降低输出上限后，已有本地实例可能需要重启来刷新模型客户端；服务端会拒绝不匹配当前能力或限制的请求，进行中请求仍保留原费率快照。P4 的资源安装会在安全 turn 边界切换配置；平台模型目录自身的协议/上限变化没有资源版本事件，已有 factory 仍可能需要通过资源新版本安装或重启刷新。

`GET /api/models` 仅返回当前用户已授权的逻辑模型、原生协议、文本/图像/工具能力、上限及费率，不返回 routes、供应商地址、供应商模型名或凭证。只有管理员账号会话可以变更供应商、路由、授权和预算；设备授权不能代替管理权限。

## 协议与执行上下文

| 协议值 | 供应商路径 | 本期验证 |
| --- | --- | --- |
| `chat_completions` | `/chat/completions` | JSON/SSE、文本、图像请求、函数工具、usage、取消；真实 Harness 工具闭环 |
| `responses` | `/responses` | JSON/SSE、文本、图像请求、函数参数增量、usage、取消；真实 Harness 工具闭环 |
| `messages` | `/messages` | JSON/SSE、文本、图像请求、工具参数增量、缓存与输出 usage、取消；真实 Harness 工具闭环 |
| `embeddings` | `/embeddings` | 独立非流式文本向量入口、输入 usage、同一授权与账本 |

所有验证使用可计数供应商 stub；没有调用真实收费供应商。实际供应商接入时应核实协议、模型能力、usage 口径和限制符合所发布目录。

首期仅允许桌面执行的函数工具，不开放供应商托管搜索、代码执行或其他额外计价工具。Responses 使用 `store=false`，不接受远端 conversation/previous_response_id；上下文由本地维护。只声明文本与图像输入，不声明音频、视频、远端文件或生成图像等能力。Anthropic 缓存本期只接受默认/5 分钟 TTL，不将 1 小时缓存按同一费率计价。Bedrock 未进入本期目录。

云端调用使用 `POST /api/models/requests`，包含稳定 `request_id`、`agent_id/thread_id/turn_id`、逻辑 `model_id`、`purpose` 和原生 `payload`。purpose 支持 chat/subagent/summary/title/memory/embedding。设备身份与 binding generation 从服务端会话解析，不能从请求中指定。JSON 信封上限为 8 MiB；超过返回本地化的 413 错误。

当前设备先通过 `POST /api/model-contexts` 注册 Agent 运行实例、正式 `resource_id`、线程及 turn 的关联；个人 Agent 的资源 ID 可默认等于实例 ID，共享平台定义必须使用独立实例 ID。公共实例 ID 一经归属不能被其他用户认领，也不能重挂到其他父资源。P4 已接入正式资源所有权/当前设备分配校验，在登记、预占和 dispatch 前执行；消费上下文表自身不授予资源或模型权限。

首次调用返回原生 JSON 或 SSE。同一用户、设备代次和内容的 request_id 重放返回 **202 + RequestView**，包含现有状态与 attempt，不重新调用上游；内容不同返回 409，其他用户的请求返回 404。首期没有流重放缓存，调用方应查询请求状态并显式决定下一步。

## 预占、结算与核对

金额全部以整数 nano 表示，1 nano = 10^-9 对应币种单位。费率四项表示每个 token 的 nano 单价，不用浮点数累计余额。

- 输入分为互不重叠的 uncached_input/cache_read/cache_write。OpenAI 的 input/prompt total 包含缓存子项，需要扣除缓存后得到未缓存输入；Anthropic 的 input_tokens 已排除缓存，直接使用。口径参见 [OpenAI usage 定义](https://github.com/openai/openai-python/blob/main/src/openai/types/responses/response_usage.py) 与 [Anthropic prompt caching](https://platform.claude.com/docs/en/build-with-claude/prompt-caching)。
- output 包含供应商计入的 reasoning tokens，不能再次单独收费。
- 预占使用整个已验证输入上限乘最高输入分项单价，加输出上限乘输出单价。模型定义的输入上限必须覆盖供应商实际强制的最大输入，包括工具与图像；不得填成客户端估计值。输出上限同时约束本地 SDK 和网关请求。
- 这种预占方式有意保守：短提示词也可能需要足以覆盖整个模型输入上限的可用预算。本期不通过猜测文本、工具和图像的 token 数缩小预占。
- 预占、设备权限及 generation 检查处于同一用户行锁事务中，并发请求共享相同预算边界。云端再次检查授权后才将 attempt 标记为 running 并发送上游。
- 请求保存模型定义和费率快照；后台改价不改变进行中请求的结算价格。每次实际供应商尝试都有独立 attempt_id。
- 获得确认的最终 usage 后，在同一事务内唯一写入 attempt 结算、增加已消费金额并释放预占余量。usage 超出已配置模型上限时暂停自动结算并进入核对。
- `free_rejection_statuses` 默认空。只有部署方确认某个错误状态保证不计费，才能配置为可释放预算、尝试下一 route。未知 HTTP 错误、断流、超时、取消或缺少最终 usage 都保留预占，进入 `reconciliation_required`，不自动切换到另一个供应商再次消费。
- 转发连接与账本任务分别管理。HTTP 断开或桌面退出会取消供应商连接，清理仍会等待已开始的数据库事务完成。原生错误内容不会直接回传供应商诊断信息。
- running 请求有 180 秒 lease，每 30 秒续期。启动及后台扫描到失效 lease 后，未派发的 reserved attempt 释放预占；可能已派发的 running attempt 转入待核对。进程重启不重放调用。

`POST /api/model-admin/attempts/{attempt_id}/reconcile` 只允许管理员处理待核对 attempt，必须提供 `evidence`（供应商回执/查询依据和原因）及权威 `usage`。只有确认无消耗时才用 `usage:null` 释放预算。核对人、依据、用量和费用原子保存；同一依据及用量重放不重复结算，冲突返回 409。设备或客户端报送的 token 数不能用于扣费。真实消耗核对后如超过预算，账本仍记录真实费用，剩余额度为负并阻止后续预占。

## 桌面与费用查询

已验证账号登录后，sidecar 基于 uvicorn 提供的本机监听地址建立账号专属模型入口。Harness 继续使用公开 ChatModelFactory、ProviderConfig.base_url/headers/session_header；Octop 只组合平台目录、输出上限和 SDK 零重试策略，不复制 Harness 的模型协议实现。

Harness 每次 HTTP 模型调用由本机中继分配独立 request_id，关联活动 Agent、线程和 turn，并在发送前取得该账号的最新短期设备授权。内部摘要等使用同一 factory 的调用同样走网关；自动 Harness 调用统一标记为 chat。显式辅助入口支持更细的 purpose 标签。本机模型入口另有账号专属随机凭证，不能使用 renderer 的普通本机 API token 调用，也不会将云端 access token 放进 SDK 配置。退出或切换账号会关闭旧中继和模型客户端，旧账号任务不能获取新账号凭证。

本地新增：

- `GET /api/models`：当前账号的平台目录。
- `POST /api/models/auxiliary`：在已存在且归属当前本地 Agent 的 thread/turn 上调用辅助模型；Embedding 使用单独能力。提供传输入口不等于启用完整记忆、子 Agent 产品或标题生成功能。
- `GET /api/usage/budgets`、`/api/usage/requests`、`/api/usage/summary`：仅查询当前账号，费用来自云端，不使用本地计数推算账单。

云端同名 `/api/usage/*` 查询接受账号或设备授权，默认只看本人。管理员使用账号会话显式指定 `user_id` 才能查询他人。requests 可见失败、释放和待核对状态；summary 只聚合已结算的 attempt，按逻辑模型和币种分组，支持 `since`（含）与 `until`（不含）的 Unix 时间范围。

P3 不改默认产品入口。Electron 调试仍使用 `--local-runtime`；登录、模型管理与费用的产品页面在 P6 接入。P4 已接入资源发布和安装，见 [platform-resources.md](platform-resources.md)；聊天/文件/记忆同步仍属于 P5。

## 验证

- `uv run pytest tests/integration/test_platform_models.py tests/unit/models -q`：原生协议、工具增量、多模态转发、幂等、并发预算、failover、费率快照、未知用量核对与真实 HTTP 取消。
- 设置 `OCTOP_TEST_DATABASE_URL` 后运行上述集成测试与 `tests/unit/db/test_application_migrations.py`：SQLite/PostgreSQL 同一账本行为。
- 同一集成套件通过实际两个 uvicorn 监听端口验证 sidecar → 云端 → 供应商 stub；三种生成协议都用真实 Harness 执行「模型 → 本地 read_file → 模型」。
- `tests/integration/test_independent_applications.py`：新应用依赖隔离与既有本地审批/取消回归。独立仅安装 cloud extra 的环境中，也验证了模型消费、幂等及费用结算。

具体验收结果记录在根目录 Plan.md。

## P5 接入

P5 恢复保留原账号内运行实例 ID 和聊天关联；数据备份所有权不会授予模型使用权限。缺少当前定义/分配时恢复成禁用归档，有效模型、预算和正式资源权限仍由网关逐次检查。参见 [同步与恢复](platform-sync.md)。
