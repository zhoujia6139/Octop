# 云端账号与桌面设备授权（P2）

P2 在独立云端和本地应用上提供账号密码、TOTP MFA（含恢复码）、单设备绑定、撤销与账号数据隔离。首期登录方式已与用户确认，不迁入 SSO、LDAP 或 Passkey。产品登录页面与默认入口的切换在 P6，模型网关已在 P3 接入（见 [platform-models.md](platform-models.md)），实际 outbox 和同步传输在 P5。

## 启动与初始化

```bash
uv sync --no-dev --extra cloud
# 交互输入并确认密码，仅能在新云端库中初始化一次。
uv run --no-dev --extra cloud octop-cloud-init --home /path/to/cloud --username admin
uv run --no-dev --extra cloud octop-cloud --home /path/to/cloud --port 8088
```

首次管理员通过本机 CLI 初始化，没有公开的抢注管理员或 setup HTTP 接口。之后由管理员通过 `/api/users` 创建用户。用户名不区分大小写；密码沿用现有 Argon2id、长度、字母数字与常见弱密码限制。云端只初始化自己的身份表，继续支持 SQLite 和 PostgreSQL（`OCTOP_CLOUD_DATABASE_URL`）。

如需 API 文档，在云端目录的 `config.json` 设置 `{"enable_api_docs": true}`，访问 `/api/docs`。云端应用未挂载 Agent 执行、聊天、工具或服务器工作区路由。

桌面本地配置为 `$OCTOP_HOME/local/config.json` 的 `cloud_url`，或环境变量 `OCTOP_CLOUD_URL`。必须是 HTTPS origin；本机开发允许 HTTP loopback，不接受账号密码、路径、查询参数或 fragment。相同 origin 的大小写与默认端口被规范化。

```bash
OCTOP_CLOUD_URL=https://cloud.example uv run --no-dev --extra local-runtime octop-local
# Electron 调试入口（在 desktop/src 下，已有对应开发运行时）：
OCTOP_CLOUD_URL=https://cloud.example pnpm dev -- --local-runtime
```

Electron 为每次 sidecar 启动生成独立本机 API token。云端账号会话、云端设备授权与该本机 token 分别验证，不能互换。

## 授权与接口

| 凭证 | 有效期 | 用途与存储 |
| --- | --- | --- |
| 账号 access token | 15 分钟 | 管理、MFA 设置与设备绑定；Electron 仅在主进程内暂存 |
| MFA challenge | 5 分钟 | 只能提交第二因素，成功后单次消费 |
| 设备 access token | 5 分钟 | 当前用户、device_id、binding_generation 的短期授权；sidecar 只在内存持有 |
| 设备 refresh token | 30 天绝对有效期 | 仅换取设备 access；由 Electron 用 OS safeStorage 加密保存，数据库仅存摘要 |

新应用采用随机不透明 token 与服务端 purpose/session 记录，不沿用旧 JWT 的离线角色信任。每次云端鉴权读取当前账号状态、安全版本与设备绑定；Token 不包含可自行指定的权限。续期不轮换长期凭证，其固定到期、设备撤销、重新绑定、账号禁用、密码和 MFA 变更均由云端执行。过期短期会话在该用户后续登录或续期时清理。

| 方法与路径 | 行为 |
| --- | --- |
| `POST /api/auth/login` | 密码登录；启用 MFA 时只返回 challenge |
| `POST /api/auth/mfa/verify` | 验证 TOTP 或恢复码并取得账号会话 |
| `GET /api/auth/me` | 校验并返回当前账号/设备身份及平台时区 |
| `POST /api/auth/mfa/enroll`、`/confirm` | 最近账号登录后启用 TOTP，一次性返回 10 个恢复码；启用后撤销已有授权 |
| `POST /api/auth/mfa/disable` | 密码和 TOTP/恢复码验证后停用 MFA，同时撤销已有授权 |
| `POST /api/auth/password` | 验证当前密码及已启用的第二因素，修改密码并撤销所有会话/设备授权 |
| `GET/POST /api/auth/devices` | 查看或绑定当前用户的唯一执行设备；已有设备时返回 409，显式 `replace=true` 才替换 |
| `POST /api/auth/device/refresh` | 长期设备凭证换取短期授权，不能换取管理权限 |
| `GET /api/auth/device/me` | 只接受仍有效的当前设备授权 |
| `DELETE /api/auth/devices/{device_id}` | 撤销自己的设备及其所有短期授权 |
| `POST /api/auth/logout` | 账号会话退出；设备会话退出同时撤销设备续期 |
| `GET/POST /api/users`、`PUT /api/users/{user_id}` | 仅管理员账号会话可以管理用户；禁用和角色变更立即撤销该用户授权 |

TOTP 秘钥复用现有 Fernet 密封能力；恢复码复用现有生成、规范化和 Argon2 校验。登录失败与 MFA 失败状态持久化，MFA 同时限制单个 challenge 与账号级尝试，换 challenge 不能绕过锁定。TOTP 时间步及恢复码在用户行锁保护下单次消费。所有身份响应禁止缓存，字段验证错误不回显提交的秘密。

`CloudIdentity.authorized_device(token)` 是 P3/P5 的事务授权入口：在持有用户行锁的同一事务里检查当前 generation，再预占额度或提交同步写入。P2 测试验证当前设备、并发替换、过期与撤销的拒绝行为；真实模型预占和结算已在 P3 使用此入口实现，同步写入仍由 P5 实现，不能只校验客户端上报的 generation。

## 桌面凭证与账号目录

`window.octopDesktop.cloud` 提供 `login(username, password)`、`verifyMfa(code)`、`bind(replace)`、`renew()`、`logout()` 和 `current()`。前两步只返回 `mfa_required` 或 `ready_to_bind`，绑定成功返回公开身份；renderer 不取得账号 token、设备长期凭证或设备 access token。管理 UI 的 MFA 设置和用户管理界面在 P6 接入上述 HTTP 合同。

Electron 主进程的 `cloud-session/desktop-device.json` 使用现有 `DeviceCredentialStore` 和系统凭证存储加密；无可用 keychain 时只存当前进程内存，重启后重新登录。云端 origin 也保存在密文中，修改服务地址不会把旧服务器凭证发送到新服务器。主进程自动在短期授权过期前续期，临时网络失败保留本地数据并重试。主进程退出时停止续期。

sidecar 的 `/api/cloud/session` 接收短期设备授权，并回到配置的云端验证身份；不能从客户端的 user_id 直接选目录。其数据路径为：

```text
$OCTOP_HOME/local/accounts/<sha256(cloud_origin:cloud_id:user_id)>/
  local.db
  workspaces/<agent_id>/...
```

每个账号拥有独立数据库、工作区与后续 outbox 的归属目录；云端地址和云端实例 ID 也参与隔离。未登录的生产 sidecar 不自动打开公共/旧数据目录。可信 Python 测试装配保留 P1 的模型 stub 沙箱，HTTP/config.json 不能开启该模式。

切换或退出会取消当前任务、关闭 Harness 和数据库，保留原账号文件。已有 WS 继续指向已关闭的旧运行实例，不能转向新账号。`LocalAccounts.device_access(expected_identity=...)` 要求后台工作携带原来的账号/绑定身份，并在线复核；旧账号任务不能取得新账号凭证。实际 outbox 表、重试和上传在 P5 按此归属接入。

云端不可达时，已打开的账号数据仍可本地查看。应用冷启动后首次选择账号需要完成在线设备验证，离线冷启动体验在后续数据恢复/产品阶段处理。没有网关授权时不提供生产模型直连。

本机 `/api/cloud/*` 控制面只供主进程使用，renderer 通过受校验的 IPC 调用，普通 HTTP 中继拒绝这些路径（含编码形式）。HTTP/WS 中继只向 sidecar 注入本机 token；凭证不放在 URL 中。

退出优先解除本地账号并清除长期凭证，然后尝试撤销云端设备。云端不可达时返回 `revoked=false`，不得显示远端撤销成功；本机数据不删除。若 sidecar 解除账号失败，主进程停止它后返回失败，避免退出后仍运行原账号任务。

## 验证位置

- `tests/integration/test_cloud_identity.py`：SQLite/PostgreSQL 参数化认证、MFA、锁定、角色、设备撤销/替换、并发绑定、本地隔离与活动 WS 退出。
- `tests/integration/test_independent_applications.py`：新应用导入隔离与 Harness 流式工具回归。
- `tests/unit/test_application_launch.py`：管理员初始化、配置与可信 cloud origin。
- `desktop/src/main/session/cloud-session.test.ts`：加密保存、MFA 交接、重启续期、撤销、离线退出与续期/退出竞争。
- `desktop/src/main/protocol/` 测试：编码控制路径拒绝、WS 上游本机凭证注入。

实际验收结果记录在根目录 Plan.md。P3 网关与账本参见 [platform-models.md](platform-models.md)，P4 及之后阶段仍按计划实施。
