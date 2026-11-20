# 桌面数据同步与显式恢复（P5）

新云端仅保存备份，不执行 Agent。桌面使用账号目录内独立的 SQLite、工作区、记忆与持久化 outbox；用户身份、设备和绑定代次仍由 P2 的事务鉴权校验。cloud schema 为 **5**，local schema 为 **3**，SQLite/PostgreSQL 迁移成对提供。

## 依赖与交付

P5 使用 `octop-harness` 的 `BackendWorkspace.for_local_directory/scan_sync_files/read_sync_chunk/write_sync_chunk/commit_sync_file/remove_sync_file`，以及 `octop-memory` 的 `Memory.sync_changes/restore_sync_changes/close`。这些接口分别在两个运行库实现，没有在 Octop 中扫描记忆私有表或复制文件后端实现。

目标发布版本为 **octop-harness 1.0.12、octop-memory 1.0.6**。本轮准备候选包与本地联调，正式发布、Octop 依赖约束及 `uv.lock` 的更新是发布后的步骤；不能把当前仓库的旧锁文件视为已经包含 P5 运行库。缺少接口时，桌面选择账号会返回 `LOCAL_RUNTIME_UPGRADE_REQUIRED`，不会静默跳过同步。

联调可以使用两个候选源码 checkout：先按现有锁文件安装 Octop，再 `uv pip install --no-deps -e <harness-checkout> -e <memory-checkout>`，随后使用 `UV_NO_SYNC=1 make all`。正式部署应安装已发布版本并重新生成锁文件，不依赖相邻 checkout，也不直接修改 site-packages。

## 同步内容

| 数据 | 同步合同 |
| --- | --- |
| 线程 | 稳定线程 ID、所属运行实例、标题、创建/更新时间与状态 |
| 消息 | 稳定消息 ID、线程/turn、顺序、角色、正文、工具调用/结果 ID、附件版本引用和完成/中断状态 |
| 文件 | `inbound/` 附件、`artifacts/` 产物，使用规范化相对路径、SHA-256、大小和修订 |
| 记忆 | 账号内 Agent namespace 的逻辑记录、修改和删除；包括 facts、evidence、entities、pages、episodes、digests、tree、active entities、journal |

普通工作区文件不会自动全部备份。`.octop/`、`.octop-auth`、隐藏路径、数据库、检查点、锁、缓存、临时文件、浏览器 profile、密钥文件与约定根之外的内容均排除。系统凭证本身不放入导出根。符号链接、Windows reparse points、硬链接和特殊文件不作为文件内容读取；父目录也通过 no-follow/固定句柄校验。路径采用 NFC 形式并检测大小写/规范化冲突。

文件通过 workspace 公共接口操作，允许不启动 Agent 或模型即可查看/管理本地内容。用户主动上传的外部文件先复制进 `inbound/`，才进入同步范围。聊天中用户输入和已产生的工具结果仍是聊天记录；文件排除规则不等于对聊天正文进行内容脱敏。

当前内存模型不启用可选向量索引；恢复由 Memory 库重建 SQLite FTS，PostgreSQL 使用生成的索引列。自定义向量索引的逻辑恢复会明确拒绝，需宿主另行重建。新桌面开启逻辑记忆存储、捕获及已有记忆工具；P5 不新增自动提炼策略，辅助提炼开关保持关闭。运行检查点、提炼调度水位、原生浏览器登录态和设备凭证不进入备份。

## 持久化与重试

线程/消息投影与 outbox 在同一个本地事务内保存；流式内容先保存再交给桌面。尚未发送的更新合并成一个最新快照，第一次发送前持久化 claim，之后请求体及 operation ID 保持不变。响应丢失或重启重试不会重复建立消息。后续修订等待前一修订确认；云端按用户、设备、绑定代次及 operation ID 保存幂等回执。

云端在身份事务内校验实例归属、正式资源权限、修订、内容与引用，备份上下文本身不能获得模型调用权限。已建立的用户备份归属在撤销执行权限后保留。跨用户相同摘要不是下载授权；文件块按用户存储和鉴权。

文件先以至多 **1 MiB** 的块保存到本地不可变缓存，完整扫描后才进入 outbox。云端分块确认已接收的 offset，完整大小和 SHA-256 校验通过后才接受文件引用。单文件上限 **256 MiB**；每用户云端内容预留上限 **2 GiB**，历史元数据上限 **128 MiB**，单条逻辑备份记录上限 **1 MiB**。这些是首期实现限制，不是计费套餐。历史版本/内容保留，因此删除仍占用历史空间；额度耗尽不阻止 tombstone。

后台任务只在桌面存活时运行。每次扫描重新核对摘要，补偿工具写文件后崩溃造成的漏报；不完整枚举或变化中的文件不能推进删除扫描水位。删除产生永久 tombstone；同一路径重建使用新 ID，旧 ID 不复活。历史消息的附件绑定内容摘要，即使当前文件修改/删除，旧附件版本仍可查看。

修订冲突保留本地数据与失败操作，不自动覆盖或合并。替换绑定后，原绑定未确认的个人资源及数据操作保留为冲突，不用新代次静默上传。退出/切换账号取消并等待本账号的传输，outbox 保留；授权始终向最初选定账号申请。

## 显式恢复

普通启动只扫描/上传本地数据，不自动用云端备份覆盖本地。`POST /api/sync/restore` 明确触发恢复；正在运行或等待审批的 turn 会阻止恢复。

恢复使用云端固定 watermark，验证修订、摘要、路径、工具和附件关联。存在未同步数据或未扫描的本地变更时跳过相应 Agent，保留原内容。先下载并验证当前文件和历史附件，再由 Memory 公共接口事务恢复逻辑数据，最后发布可见的本地引用。失败/中断留下持久化恢复标记，普通扫描和执行不能越过它；显式重试继续同一 watermark。损坏的缓存块会被丢弃，允许重新下载。

有可用定义时保留原运行实例 ID，重新安装固定 skills，不复用另一实例的绝对路径。未分配、撤销或删除定义的备份也能恢复为禁用归档；恢复数据不会授予模型或工具执行权限。需要执行时仍须安装当前获授权的定义。

恢复的 running/awaiting_approval 记录标为 interrupted，产生对应待同步更新。聊天先作为本地归档展示；用户开始新一轮对话时，通过 Harness 的 `aappend_messages` 补入规范化历史，不调用模型、不恢复旧检查点，也不重放未完成工具。未配对的工具请求保留在归档中，不作为新图的待执行任务。

## API

全部云端接口要求当前有效设备会话；管理员账号会话不代替设备。全部本机接口要求独立 loopback Bearer 凭证及已选定账号。Scalar/OpenAPI 展示各自的类型与鉴权。

| 位置 | 接口 | 用途 |
| --- | --- | --- |
| 云端 | `PUT /api/sync/records` | 提交一个修订及幂等回执 |
| 云端 | `GET /api/sync/changes?cursor=&watermark=&limit=` | 分页读取自己的不可变备份历史 |
| 云端 | `POST /api/sync/blobs` | 预留内容、查询已有块 |
| 云端 | `PUT /api/sync/blobs/{sha256}/chunks/{offset}` | 二进制分片上传/重试 |
| 云端 | `POST /api/sync/blobs/{sha256}/complete` | 完整内容校验 |
| 云端 | `GET /api/sync/blobs/{sha256}/chunks/{offset}` | 下载本人完整内容的一个块 |
| 本机 | `GET /api/sync/status` | 待同步数、冲突数、错误与扫描代次 |
| 本机 | `GET /api/sync/records?agent_id=&kind=` | 查看本地线程、消息、文件与逻辑记忆 |
| 本机 | `POST /api/sync/retry` | 扫描并重试上传 |
| 本机 | `POST /api/sync/restore` | 显式恢复 |
| 本机 | `DELETE /api/sync/records/{item_id}` | 本地删除并排队 tombstone |
| 本机 | `PUT /api/agents/{agent_id}/files/{path}` | 流式复制到支持的工作区路径 |
| 本机 | `GET /api/sync/records/{item_id}/content?offset=` | 当前文件版本的本地缓存块 |
| 本机 | `GET /api/sync/messages/{message_id}/attachments/{file_id}?offset=` | 消息所引用的历史附件内容块 |

聊天 WS 的 start 命令可以附带 `attachment_ids`，只接受本 Agent 的已复制附件；resume/cancel 不接受附件。P6 已接入同步状态页面及 Electron 退出/休眠处理，见 [产品说明](product-applications.md)；完整安装器/实机验证仍属于 P7。

专项验证：`uv run pytest tests/integration/test_platform_sync.py tests/unit/sync -q`；配置专用 `OCTOP_TEST_DATABASE_URL` 后运行同一组用例覆盖 PostgreSQL。Memory 库的专用 PG 用 `TEST_POSTGRES_DSN`，不使用生产数据。
