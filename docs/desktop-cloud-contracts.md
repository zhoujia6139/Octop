# 桌面执行与云端管理合同（P0）

日期：2026-11-19。依据：根目录 [Plan.md](../Plan.md)。

本合同固定新版 P0–P7 的边界。采用新应用层和选择性复用路线，不再扩展旧 OctopServer。生产模型和费率由运营配置，不在此假定。实现进度以 Plan.md 的验收记录为准。

## 1. 既有探索性改动的审查

- 旧 `OctopServer(role="cloud")`、旧 `api/app.py` 的云端分支和旧定义 CRUD 是上一轮探索，不是新版应用工厂，不计入新版 P1/P4 验收。
- 旧定义服务仍依赖 SharedServices 和旧 Agent 表，不能迁入新云端作为管理核心。只复用其经验证的输入边界与所有权规则，P4 依新资源 schema 实现。
- settings 的惰性导出、SessionRow 的按需 Gateway 类型加载、公共头像验证与图像签名函数已经切断不必要的传递依赖，可保留为明确的共享基础。
- 旧云端测试保留为探索性代码的回归验证；新版增加独立应用测试，必须阻止导入旧 server、AppRuntime、AgentManager、GlobalProcessor、SharedServices，而不只是禁用外部执行库。
- P1 使用独立命令与显式 role 接入新工厂；P6 完成 UI 后切换默认入口。P1 不提前把缺少认证和业务能力的新应用作为现有 Electron UI 的默认后端。

旧探索性 API 清单（不约束新应用 URL 或 schema）：

| 路由 | 行为 |
| --- | --- |
| `GET /api/health` | 返回 cloud 身份、数据库就绪状态、已加载用户数和启动时间，不将定义数当作运行中 Agent 数 |
| `GET /api/agents` | 当前用户的个人定义列表 |
| `POST /api/agents` | 创建个人定义；201；接受 name、description、system_prompt、default_model |
| `GET /api/agents/{agent_id}` | 所有者或管理员读取 |
| `PUT /api/agents/{agent_id}` | 所有者或管理员完整替换上述定义字段；省略可选字段表示清空 |
| `DELETE /api/agents/{agent_id}` | 所有者或管理员删除；204 |

创建请求拒绝未知字段，包括 user_id、workspace_dir、backend 和 api_key；身份从认证会话取得。首批定义接口使用现有 Agent 表，版本字段、发布与同步写入在 P4 引入后扩展，不把秒级 updated_at 当作同步修订号。

## 2. 启动与 HTTP 依赖审计

| 耦合点 | 处理要求 |
| --- | --- |
| `infra/server.py` 顶层导入 AgentManager/Gateway/插件等 | 云端不导入这些执行组件；本地运行时按需加载 |
| `AppRuntime` 强制持有 Agent、Gateway、Cron、关怀调度 | 仅桌面过渡链使用；云端用户管理独立于 AppRuntime |
| `api/app.py` 无条件导入并挂载全部路由 | 云端显式挂载管理路由；不以在 handler 中拒绝代替路由隔离 |
| `api/routers/agents.py` 列表、创建、修改、头像均依赖 registry/workspace | 云端使用独立定义领域服务，桌面运行路由继续逐步迁移 |
| `api/routers/skill_packages.py` 修改后热刷新 Agent | 发布内容与本地安装分开；云端发布不得隐式启动或 reload Agent |
| `api/routers/providers.py` 删除/更新依赖 registry 或热重载 | P3 从云端模型目录与引用关系验证；本地通过配置版本切换 |
| `api/routers/usage.py` 某些查询通过 registry 获取 Agent 行 | 改为控制面定义服务校验权限，不要求用户 Agent 在云端运行 |
| `api/routers/setup.py` finish 自动创建默认可运行 Agent | 云端初始化只建账号与管理配置；桌面安装分配资源后才实例化 |
| 数据迁移导入 `agents.settings.profile`，包初始化加载 ACP | settings 导出按需加载，纯 profile 操作不导入执行库 |
| SessionRepo 顶层依赖 Gateway ChannelSubject | 仅转换成 IM subject 时加载，控制面初始化不依赖 Gateway |
| 用户头像验证经 Agent 头像模块导入 Harness/IM | 图像签名和公共头像验证独立于 Agent 工作区 |
| `launch.py` 无条件加载内存控制、bubblewrap、Bridge 常量 | 云端启动不装配本地运行后台任务 |

新版 P1 云端只验收最小启动、配置、数据库和健康检查；认证在 P2，资源管理在 P4。独立进程同时阻止旧编排模块和四个执行库导入。新版本地阻止旧编排、Gateway、Cron 和 Bridge，通过真实 Harness 配合模型 stub 验证工具闭环与取消。不能以旧 CRUD 测试替代新版阶段验收。

### 2.1 能力迁入清单

| 能力 | 决定 | 来源与边界 | 验证 |
| --- | --- | --- | --- |
| Agent 循环、工具、审批 | 直接复用 | octop-harness 公开 HarnessAgent、ChatRequest、cancel、resume_hitl、aclose；不包装旧 AgentManager | 新本地模型 stub 工具闭环、审批与取消集成测试 |
| 本地 workspace | 直接复用 | HarnessAgent.workspace；Harness 管理检查点 | 工作区内容和越界测试，P5 补同步 |
| 密码和 Token | P2 迁入调整 | infra/users/password.py、infra/auth/tokens.py 的纯逻辑；新身份 repo | 密码策略、Token purpose/撤销/禁用/MFA 回归 |
| 登录方式与权限 | P2 按需迁入 | 现有 MFA、用户权限规则作为行为与测试来源，不迁入完整 UserManager/旧认证路由 | 明确保留方式后逐项验证，不能静默丢失保护 |
| skills 校验 | P4 直接复用 | infra/skills/skill_packages.py 的规范化与路径校验，不能使用依赖旧管理 schema 的 Store | 新资源版本安装、非法路径和摘要校验 |
| 历史/事件 | P1 新适配、P5 迁入序列化 | Harness 原生事件；审查 infra/history/projection.py 的可独立函数，不导入 Gateway processor | 流式事件、tool_call/result 关联和消息修订 |
| 用量 | P3 迁入口径、重建账本 | gateway/process/usage_record.py 仅作字段口径参考；不复用 turn 级扣费或旧 repo | 多次模型调用、幂等及结算故障场景 |
| 数据库 | 直接复用连接，重建容器/schema | infra/db/pool.py；不导入 db/services.py 或调用旧 run_migrations | 新库表集合、水位、幂等、事务与 PostgreSQL |
| i18n/时区 | 直接复用工具，按需增加文案 | octop.i18n 与 infra/utils/locale；配置默认时区与 UI formatter | zh/en 键一致与服务端时区 |
| Electron/UI | P1 接入独立入口，P6 迁入页面 | desktop 的进程/凭证/中继和 dashboard 组件，不维持旧 API 合同 | 壳测试、客户端适配与完整用户流程 |
| IM/Cron/Bridge/旧编排 | 排除 | 不进入新应用依赖图；P6 审查退役旧入口 | 导入防火墙与路由集合测试 |

### 2.2 固定代码边界

- `octop/application_config.py`：两端最小配置；仅 stdlib/pydantic。云端可配置 PostgreSQL URL，默认 SQLite；本地固定 SQLite，不继承旧完整 config.json。
- `infra/applications/contracts.py`：版本化健康、实例、turn 和事件数据类型，无应用实例、DB 或运行库依赖。
- `infra/applications/cloud.py`：新云端容器与生命周期，仅装配当前阶段的管理服务。
- `infra/applications/local.py`：新本地容器，直接持有 Harness 实例和活动 turn，不持有旧管理器或 Gateway。
- `api/applications/cloud.py`、`api/applications/local.py`：各自 FastAPI 工厂、路由和 lifespan；不导入旧 api/app.py 或 api/deps.py。
- `infra/db/application_migrations.py`：新迁移发现与执行；`infra/db/migrations/cloud/`、`infra/db/migrations/local/` 是独立集合。旧集合和水位不改写。
- 新 repo 继续位于 `infra/db/repos/`，只负责 SQL，由两端最小容器分别装配；不为方便复用旧 RepoBundle。
- `launch.py` 选择新工厂并管理 ASGI 生命周期；独立命令 `octop-cloud` 与 `octop-local`。P6 已将 CLI/Electron 的默认入口切换到对应新工厂，不提供第三种产品模式。

### 2.3 最小 schema 与后续演进

- 两端默认使用 OCTOP_HOME 下各自的 cloud/local 子目录，配置、数据库和运行数据互不覆盖；显式传入 home 时使用该应用自己的目录。
- 云端 v1 仅建 `cloud_settings` 与 `cloud_schema_version`，用于平台设置与初始化水位。P2 增加身份/设备/MFA/撤销表，P3 增加供应商/授权/费用账本，P4 增加资源/发布/分配，P5 增加同步数据；不预建无调用方的旧全量表。
- 本地 v1 建 `local_agents`、`local_threads`、`local_turns` 与 `local_schema_version`。持久化实例配置引用、线程归属和 turn 状态；检查点属于 Harness；消息投影/outbox 在 P5 增加。
- 本地资源表采用 integer surrogate PK + public string UNIQUE，子表引用 public string。云端后续资源同样遵守此约定；身份保留 integer 用户主键。
- 新目录中的每个版本包含 SQLite/PG SQL 对；本地只运行 SQLite，PG 文件用于保持 DDL 合同可校验。云端支持两者。编号只在各自集合内递增，不使用旧 `_schema_version`。
- 每次迁移在事务内执行并更新唯一水位，失败回滚，重复启动不重复应用；拒绝高于当前程序支持的 schema 版本。没有旧数据库导入或自动接管流程。

## 3. 身份、定义、安装与个人副本

- `user_id` 由服务器验证的账号会话确定；公共资源使用字符串 ID，不接受客户端指定归属。
- 设备凭证与本机访问凭证不同。设备请求身份至少包含用户、device_id、binding_generation；授权需对照服务器当前有效绑定。
- 首期每用户一个有效执行设备。重新绑定提升 generation，并拒绝旧绑定的模型调用和同步写入；不在旧设备上自动启动远程停止命令。
- 设备绑定为 P2 的独立授权流程，不能将当前仅限 loopback 的 `/auth/device/token` 开放到公网来实现。
- Agent/skill 资源来源分为 platform 与 personal，拥有独立资源 ID；platform 的正式版本不可变，personal 仅所属用户写入，管理员按管理权限操作。
- 每个发布版本有单调 revision，Agent 固定引用 skill 的 ID 与 revision。运行实例另记 device_id、desired_revision、installed_revision、状态和错误。
- 个人副本分配新 ID，保存 source_id/source_revision；平台后续更新不修改该副本。
- 配置安装状态为 pending → downloading → installing → installed，失败进入 failed。只有完整校验和切换成功后才能报告 installed；运行中 turn 使用原配置至安全边界。
- 文件路径、本地 MCP 凭据、浏览器登录态及设备能力不放进可移植的云端 Agent 定义。

## 4. 同步消息、修订与恢复

目标写入信封的最少字段：

```json
{
  "protocol_version": 1,
  "operation_id": "stable-operation-id",
  "resource_kind": "message",
  "resource_id": "public-resource-id",
  "agent_id": "public-agent-id",
  "base_revision": 0,
  "operation": "upsert",
  "payload": {}
}
```

- `resource_kind` 仅允许合同支持的 thread/message/file/memory/personal_agent/personal_skill。鉴权与设备代次来自会话，不信任 payload 内的重复身份。
- 云端接收同一用户、设备、operation_id 的重复请求，返回相同结果；operation_id 对应的内容不同则冲突。首次写入 base_revision=0，更新和删除必须匹配已确认修订。
- 确认结果包含 operation_id、resource_id、服务端 revision 和同步水位。确认前持久化数据；分批提交时逐项返回结果，不能一个失败导致其他成功项的水位丢失。
- thread 同步标题和归档状态；message 使用稳定 ID，关联 thread、turn、tool_call_id 和附件。消息从流式中间态到最终态仍是同一资源修订。
- file 使用工作区相对路径、摘要、大小与内容引用。内容上传和清单发布分两步；清单仅引用已验证的内容。读取内容始终验证所有者，不能仅凭摘要下载。
- memory 同步库提供的 namespace 内逻辑数据及修订，处理删除。内存索引和 checkpoint 不等于记忆备份，不通过复制活跃数据库替代逻辑同步。
- 删除必须显式 tombstone；断网、扫描中断、未下载或失败读取不等于删除。保留幂等记录，避免旧 upsert 在删除后复活数据。
- 同设备为单一用户数据写入源；配置发布另由云端写入。两路有独立水位，不做多设备合并。
- 冲突保留本地待同步内容并反馈，不能自动 last-write-wins。重新绑定的设备需显式恢复，不以旧 outbox 抢写云端新状态。
- 恢复聊天用于显示与新 turn 的上下文构建，不还原已中断工具的执行位置，也不自动重放其副作用。

目标错误语义：未认证 401；所有权、设备撤销、模型授权或额度拒绝 403；未找到 404；幂等内容不一致或修订冲突 409；字段非法 422；请求过大 413；临时不可用 503。具体 ErrorCode 在对应实现阶段加入双语后端与 dashboard 对应文案。

## 5. 模型请求与结算状态机

应用内的所有模型使用都经网关；客户端仅获取逻辑模型目录、能力、协议和平台授权。云端供应商地址及 Key 不随 Agent 定义下发。

- 逻辑请求携带稳定 request_id 与 agent_id/thread_id/turn_id/purpose；服务端验证这些关联的归属和分配，并计算规范化请求摘要。
- 逻辑请求状态：accepted → reserved → running → settled；尚未调用上游时失败可进入 rejected/released；结果或用量不明进入 reconciliation_required，不能宣称零费用。
- 每次实际供应商调用有 attempt_id，与逻辑请求关联。客户端重放同 request_id 不重复执行；确需新调用或服务端 failover 时生成新 attempt，真实消耗分别入账。
- 结算记录与 attempt 唯一关联，在同一数据库事务中更新费用、预占和余额。客户端报送的 token 数不用于扣费。
- 流式连接和请求账本独立；客户端断开后传播取消，并完成或标记待核对，不因 HTTP handler 结束丢弃计费状态。
- 无流重放缓存的首期实现，重复请求可返回当前状态或明确的已完成结果引用，不能为了重放流而再次调用供应商。

费率合同：逻辑 model_id、不可变 rate_revision、currency、计价单位、有效时间，以及不重叠的 uncached_input/cache_read/cache_write/output 单价。reasoning 费用是否独立按供应商口径配置，已包含在 output 中时不得重复计算。金额使用定点最小单位或 Decimal；额度预占需覆盖该模型可计算的输入和输出上限。

币种、实际单价、额度单位、用量不明的核对策略在真实计费启用前配置；测试使用明确的测试费率。支付、充值、订阅和发票不属于本合同。

## 6. 已安装运行库能力与前置缺口

本次通过 `uv run python` 核实安装版本及 wheel 中的实现，未把相邻 checkout 当作实际依赖。

| 库 | 已核实接口 | 后续处理 |
| --- | --- | --- |
| octop-harness 1.0.11 | `config.ProviderConfig` 支持 base_url/api_key/headers/session_header，协议为 openai、anthropic、bedrock；`llm.factory` 有 OpenAI/Anthropic/Bedrock 构造与流式处理 | 可复用平台入口；P3 验证短期 token 更新、请求 ID 与每次模型调用的关联，缺少扩展点时在该库补齐 |
| octop-harness 1.0.11 | `BackendWorkspace` 有 als/aglob/adownload_bytes/aupload_bytes/adelete 等异步接口 | 内容 I/O 继续走此接口；P5 必须验证链接不越界及可重试传输能力，现有接口未被证明提供大文件分片/流式读取或 no-follow 枚举合同，必要时在 Harness 扩展 |
| octop-memory 1.0.5 | `operations.migration.export.export_namespace(memory, out_path, tables=...)`、`import_.import_namespace(..., expect_namespace=...)`；逻辑导出不包含 LangGraph checkpoints | 可用于受控 namespace 快照；现有 export 表集合仅覆盖 raw_events/candidates/entities/atoms/aliases/entity_pages/thread_active_entities/journal，不能宣称覆盖全部记忆类型 |
| octop-memory 1.0.5 | `Memory` 提供逻辑记录读写，未发现持久化的公共增量游标与删除追踪合同 | P5 前在 octop-memory 提供覆盖所有所需记录的快照/恢复与持久化修订、删除合同，含 episodes/digests 等适用数据；不得用 Octop 直接读取私有表替代 |
| octop-browser 1.0.0 | 已被桌面便携包依赖，用于本机浏览器 | 首期继续本地使用；P1 无需修改 CDP/profile 逻辑 |
| octop-gateway 1.0.4 | 现有 dashboard 消息处理与 IM 共用处理链 | P1/P6 保留必要的本地聊天编排并剥离 IM 装配；不扩展 IM 协议 |

P3 的协议覆盖矩阵：OpenAI Chat Completions、OpenAI Responses、Anthropic Messages 的文本/工具/多模态/usage/取消都需独立验证；Embedding 用独立能力入口。Bedrock 若进入首期模型目录，其签名和凭证仅在服务端处理，不向桌面泄露 AWS 凭证。未通过某项能力验证的模型不得在目录中宣称支持该能力。

## 7. P1 独立程序的实际入口

```bash
# 两端分别安装；可使用不同的虚拟环境。
uv sync --no-dev --extra cloud
uv run --no-dev --extra cloud octop-cloud --home /path/to/cloud --port 8088

uv sync --no-dev --extra local-runtime
uv run --no-dev --extra local-runtime octop-local --home /path/to/local --port 8089
```

未指定 home 时，使用 `$OCTOP_HOME/cloud` 与 `$OCTOP_HOME/local`（根目录默认 `~/.octop`）。各自读取自己的 config.json，云端数据库为 cloud.db，本地为 local.db。云端可通过 `OCTOP_CLOUD_DATABASE_URL` 使用 PostgreSQL；本地拒绝 PostgreSQL URL 和非 loopback 监听地址。

- 显式 `octop run --role cloud` / `--role desktop` 同样选择新云端/本地工厂；P6 起不指定 role 默认启动云端管理应用。
- P6 起 Electron 默认启动新 sidecar，主进程每次生成独立本机 API token；新产品 UI 使用独立 API 客户端，无需 `--local-runtime`。
- 独立本地 API 通过 Bearer token 授权。由调用方使用 `OCTOP_LOCAL_API_TOKEN` 提供至少 32 字符的本机访问凭证，或由可信应用装配获得本次启动随机生成的 token；它不是云端登录凭证，不下发供应商 Key。
- 两端 `GET /api/health` 返回协议版本、role、就绪状态和 schema_version；云端 P1 仅提供健康和可选 API 文档，没有认证或资源业务路由。
- 本地提供实例列表/创建、线程创建，以及 `/api/agents/{agent_id}/threads/{thread_id}/ws`。WS 接受 start（text）、resume（approve/reject decisions）、cancel；事件有稳定 turn_id、thread_id 和递增 sequence。
- WS 握手使用 Authorization Bearer 请求头，不接受 URL 查询参数中的凭证，避免本机 token 出现在访问日志中；桌面 relay 的产品接入在 P6 完成。
- 本地工具流直接由 Harness 提供，用户发起聊天后，写文件、编辑文件和 Shell 执行默认直接执行，不逐次请求确认。保留显式审批的协议与生命周期处理：取消未批准的写入后，新 turn 不应执行之前的操作；重启将未完成 turn 标记为 interrupted，不自动重放。
- 平台模型适配只允许由可信 Python 装配注入。P1 用真实 Harness 和内存模型 stub 验收；生产默认未连接 P3 网关时返回 PLATFORM_MODEL_UNAVAILABLE，HTTP/config.json 不支持供应商地址、Key 或测试模式。
- P6 已移除 `legacy-runtime` extra；旧模块回归依赖仅存在于 `legacy-tests` 开发 dependency group，不是第三种后端。便携包新依赖导出使用 local-runtime，不包含旧服务器依赖。

## 8. 阶段门槛与尚未完成项

P0 交付物是本合同、能力审计和实现前置条件。P1 验收的是新应用与 Harness 最小运行链，旧定义 CRUD 不计入新资源管理成果。新版认证、资源发布、计费、同步和产品 UI 分别属于后续阶段。

P2 已获用户授权，实施了密码 + TOTP MFA（含恢复码）、云端单设备授权、Electron 安全凭证交接与本地账号隔离，详见 [cloud-identity.md](cloud-identity.md)。新云端身份 schema 为 v2；本地沿用 v1，由已验证的账号身份选择各自的数据库与工作区。P3 已接入平台模型网关、原生协议、额度和结算，见 [platform-models.md](platform-models.md)。P4 已完成资源发布/分配、资源 outbox、安装回执和个人副本，见 [platform-resources.md](platform-resources.md)；P5 增加逻辑备份与文件分片，当前云端 schema v5、本地 v3；P6 已完成产品界面和生命周期的候选运行库验收，见 [product-applications.md](product-applications.md)。P7 已开始，依赖改为仓库内 vendor 源码，普通 frozen 安装生效；最终验收记录见 Plan.md。

P2 审查后直接复用了 Argon2 密码策略、TOTP 原语、恢复码生成/规范化和 Fernet 能力；MFA 包的旧服务入口改为按需导出以切断 SharedServices 依赖。新 Token 采用独立 purpose 的随机不透明凭证、服务端摘要和实时身份校验，未迁入旧 JWT 的离线角色信任。所选登录方式已由用户明确为密码 + TOTP，不迁入 SSO、LDAP 或 Passkey。Electron 复用 DeviceCredentialStore/safeStorage，但云端长期凭证与旧 loopback 凭证存储隔离。

旧探索性定义接口的进程内锁不作为新资源写入的事务保证，P4 需使用新 schema 的数据库事务与约束。

## P5 接入

P5 候选补齐 `BackendWorkspace` 的无需模型/no-follow/分片传输接口及 `Memory.sync_changes/restore_sync_changes/close`，分别由运行库自身实现。Harness 的可选 `aclose(wait_for_memory=True)` 提供恢复前的记忆写入排空边界。P7 将这两库纳入 `vendor/`，本地版本为 Harness 1.0.12、Memory 1.0.6；uv 使用本地源码路径，打包构建相应 wheel，不再以远端发布为前置条件。云端 v5、本地 v3 的实际合同及 API 见 [同步与恢复](platform-sync.md)。
