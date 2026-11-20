# Agent 与 skills 资源发布、分配和个人资源（P4）

P4 使用独立云端与桌面应用，不装配旧 `OctopServer` 或 `AgentManager`。云端保存定义、不可变版本、绑定到当前设备的分配和安装回执；桌面运行 Harness。P6 已接入产品管理页面、资源选择和错误展示，并切换默认入口，见 [产品说明](product-applications.md)。启动与认证见 [cloud-identity.md](cloud-identity.md)，模型与计费见 [platform-models.md](platform-models.md)。

云端 schema v4、本地 schema v2，各自提供 SQLite/PostgreSQL 成对迁移。P3 账本和身份数据不因迁移被重建。旧消费上下文不会自动变成正式资源授权。

## 资源、版本与运行实例

资源以稳定 `resource_id` 标识，类型为 `agent` 或 `skill`，归属为 `platform` 或 `personal`。每次保存新增修订；已保存版本的内容、文件摘要和依赖不可变。删除也新增 tombstone，不复活旧 ID，不删除已结算消费记录或本地聊天线程。

平台资源先保存草稿，再发布指定修订，最后分配给用户当前有效的设备和 binding generation。发布不自动改变已有分配，管理员可显式分配新版本或回退到已发布的旧版本。替换设备后需要重新分配；旧设备不能继续拉取、上传或调用模型。

Agent spec 只包含名称、逻辑平台模型、系统提示词和具体 skills 版本。它不能设置供应商地址、Key、工作区、Shell 权限或本机路径。平台发布不放宽本机工具审批或用户模型授权。一个发布版本只能引用平台已发布 skill 的具体修订，不使用“最新版本”浮动依赖。

桌面资源响应分别提供 `resource_id` 和 `agent_id`。共享平台定义为每个账号建立独立的本地运行实例，安装更新保持该实例 ID 和工作区。个人 Agent 使用自己的资源 ID 标识实例。模型消费上下文携带实例、正式定义、thread 和 turn；云端核对所有权/当前分配后登记不可重挂的关联，预占和实际 dispatch 前再次检查授权。同一平台定义分配给不同用户，不会共用消费上下文或工作区。

## 定义和包合同

Agent 示例：

```json
{
  "kind": "agent",
  "name": "工作助手",
  "model_id": "platform/your-model",
  "system_prompt": "按用户授权处理本地文件。",
  "skills": [{"resource_id": "skill-notes", "revision": 1}]
}
```

Skill spec 为 `kind=skill`、名称、`slug` 和 `files`。每个文件包含 `path`、`content_base64`、解码后的 `size` 和 `sha256`。必须有 UTF-8 的根 `SKILL.md`；其他文件可为二进制。当前接口直接提交有界文件清单，不接受远程下载 URL、任意 ZIP 解压、符号链接或文件类型元数据。

复用 `infra/skills/skill_packages.py` 的路径与内容规范化，并补充跨平台校验：拒绝绝对路径、路径穿越、非规范路径、反斜线、控制字符、Windows 保留文件名/非法字符、大小写或 Unicode 规范化重复、文件/目录碰撞、隐藏系统路径。新版本使用内容摘要验证整个 spec，每个文件另外验证长度和摘要。

首期限额：每个 skill 最多 256 个文件、解码后合计 4 MiB，Agent 最多固定引用 32 个 skills；两端资源写入请求在解析 JSON 前有 8 MiB 包络限制。每个用户最多 100 个未删除的个人资源，含自动复制的 skills；云端个人历史版本序列化内容合计最多 64 MiB。版本为审计和重试保留，删除不会回收历史字节额度；删除本身不会因存储额度耗尽而被拒绝。限额是本期实现边界，大附件/产物传输属于 P5。

## 云端接口

所有接口使用 P2 Bearer 会话，具体请求/响应见新云端 `/api/docs` 和 `/api/openapi.json`。

| 接口 | 权限与行为 |
| --- | --- |
| `GET /api/resource-admin/resources` | 管理员账号查看定义元数据，含最新修订、发布修订和删除状态 |
| `PUT /api/resource-admin/resources/{resource_id}` | 管理员账号保存平台草稿或 tombstone |
| `POST /api/resource-admin/resources/{resource_id}/publish` | 管理员账号发布 `{ "revision": 1 }`，不执行 Agent |
| `GET /api/resource-admin/resources/{resource_id}/versions/{revision}` | 管理员账号查看草稿或历史版本 |
| `PUT /api/resource-admin/users/{user_id}/assignments/{resource_id}` | 管理员账号分配 `{ "revision": 1 }`；`null` 撤销当前设备分配 |
| `GET /api/resource-admin/users/{user_id}/reports` | 管理员账号查看各代设备的安装/运行状态与服务端接收时间 |
| `PUT /api/resources/personal/{resource_id}` | 当前设备上传自己的个人资源修订 |
| `GET /api/resources/changes?cursor=0&limit=100` | 当前设备拉取期望修订；分页和持久化游标独立于后续 P5 数据同步 |
| `GET /api/resources/{resource_id}/versions/{revision}` | 当前设备下载本人版本、当前分配版本及其固定依赖 |
| `PUT /api/resources/reports` | 当前设备报告期望修订、实际安装修订、安装错误和观察到的运行状态 |

保存请求为：

```json
{
  "operation_id": "op-stable-identifier",
  "expected_revision": 0,
  "spec": {"kind": "agent", "name": "个人助手", "model_id": "platform/your-model"}
}
```

创建时预期修订为 0；修改使用已知修订；删除传 `spec: null`。相同 operation ID、资源和请求内容重试返回已提交结果，不新增版本；改变内容返回 `RESOURCE_CONFLICT`。个人创建可附 `source: { "resource_id": "platform-source", "revision": 1 }` 记录来源；云端验证创建时可读的同类型平台版本，后续修改不能更改来源或平台源。

设备不能修改平台资源，也不能使用账号管理员权限提升设备会话。未分配用户不能下载平台内容；Agent 的固定依赖仅开放相应版本。复制或个人 Agent 在有权访问时固定引用的依赖快照可继续读取；复制操作另建个人 skills，使后续源更新或撤销不改变个人副本。

资源写入、当前绑定检查、修订比较、数量/存储额度、版本与操作回执在同一数据库事务提交。资源写入锁与用户锁使用固定顺序，避免并发覆盖和重复创建；配置游标按已提交事件推进。旧事件返回当前期望版本，避免漏过更新后重新安装已撤销版本。

## 桌面安装与个人创建

本机接口继续使用独立的本机 API Token，不接受云端 Token 作为本机通用凭证。

| 接口 | 行为 |
| --- | --- |
| `GET /api/resources` | 查看账号内个人草稿、期望/安装修订、同步和安装错误 |
| `PUT /api/resources/personal/{resource_id}` | 本地先保存个人修订与 outbox；可在已打开账号离线编辑 |
| `POST /api/resources/{platform_resource_id}/copy` | 复制缓存的平台版本及依赖，创建新个人 ID |
| `POST /api/resources/sync` | 上传队列、拉取分配、重试安装、报告状态 |
| `POST /api/resources/sync?restore_personal=true` | 用户显式恢复个人 Agent/skills；正常拉取不会自动导入新安装上的个人资源 |

复制请求为 `{ "resource_id": "new-personal-id", "operation_id": "stable-copy-op", "revision": 1 }`，其中 `resource_id` 是新副本 ID。依赖副本使用稳定派生 ID，重复复制请求不会新增一套资源。个人副本的提示词、模型及 skills 可随后单独修改，不随平台源更新。

本地草稿、不可变缓存和上传操作共用 SQLite 事务；上传失败和响应丢失保留 operation ID，重启后重试。云端确认前显示 `pending`。修订或授权冲突显示 `conflict` 并保留原草稿与队列，不自动覆盖、重挂或跳过被拒绝的操作。需要保留为独立资源时，可读取草稿内容并以新的个人资源 ID 创建；P6 页面显示冲突并保留草稿，支持创建个人资源或平台个人副本，不静默解决修订冲突。

后台任务随账号启动，约每 30 秒拉取；本地变更与 turn 结束会唤醒它，也可显式重试。此任务只同步资源，不是用户定时任务，不触发模型推理。账号退出/撤销会立即停止授权，取消资源传输，并等待已开始的持久化/安装转换结束后关闭数据库；不会把旧任务转到新账号。

下载后先验证清单和摘要，固定依赖全部就绪后才安装。Standalone skill 安装表示通过校验的版本已进入账号本地包缓存；Agent 使用它时才 materialize 到该 Agent 的私有版本目录。所有工作区内容写入和回读都经过 `HarnessAgent.workspace`，不直接扫描或写入工作区文件。

Agent 安装创建候选 Harness，skills 写入全新的 `.octop/resource-versions/<id>/skills/` 暂存树，逐文件回读校验。完整成功才在事务内切换安装快照与运行配置；异常保留旧实例、旧目录和安装修订。暂存失败的目录不成为运行配置，且位于后续同步排除的系统路径内。切换复用工作区和本地检查点，不自动重放旧任务。

有运行中或等待工具审批的 turn 时只记录 `pending`，不替换配置；已有审批继续使用原版本。空闲时重试或新一轮开始前生效，删除/撤销同样等本地安全边界停用；云端模型授权在撤销后立即拒绝新消费。回执区分 `desired_revision`、`installed_revision`、安装状态、运行状态和错误码，它是设备报告，不作为平台授权或计费依据。

没有可用模型时仍能保存个人定义，安装会报告 `PLATFORM_MODEL_UNAVAILABLE` 或安装失败，不会启用本地直连。已经选中的账号可离线看和编辑草稿；冷启动/替换设备的身份验证继续遵循 P2，不承诺离线创建新的模型客户端或执行模型调用。

## 验收边界

专项覆盖跨用户权限、发布依赖固定、同源多用户实例、设备替换、个人副本隔离、幂等/删除、离线/响应丢失/重启、损坏下载、部分安装失败保留旧树、运行/审批边界、账号撤销和实际双 HTTP 进程下的已分配 Agent「模型 → 本地工具 → 模型」与结算。

云端-only 隔离环境没有安装 Harness、Memory、Browser、Gateway 或 LangChain，可执行发布、分配、下载和消费上下文授权。P4 不同步聊天/附件/产物/记忆，不上传检查点，不实现 P6 产品页面或 P7 原生安装器验收；这些继续按 `Plan.md` 的阶段执行。

## P5 接入

P5 新增聊天、文件与逻辑记忆同步，资源 outbox 仍单独维护。绑定变化将原绑定未确认的个人编辑保留为冲突；当前绑定没有平台分配时不伪报安装状态。显式数据恢复不会覆盖未同步内容或让撤销的 Agent 自动运行，参见 [同步与恢复](platform-sync.md)。
