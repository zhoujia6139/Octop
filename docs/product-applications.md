# P6 产品入口与桌面生命周期

本产品由独立云端应用、Electron 桌面壳和本机 Python sidecar 组成。云端只管理账号、设备、资源、模型、费用和备份；Agent 与工具在桌面运行。P6 目前以 P5 候选运行库验收，正式运行库发布和 Octop 最低版本锁定仍是交付前置项。

## 启动和首次配置

云端安装 `octop[cloud]`，先运行交互式 `octop-cloud-init` 创建第一个管理员，然后运行 `octop-cloud`。`octop run` 默认也是云端；可通过 `--home` 指定云端自己的目录，或使用 `OCTOP_HOME` 下的 `cloud/`。

```sh
uv sync --no-dev --extra cloud
uv run --no-sync octop-cloud-init
uv run --no-sync octop-cloud --host 127.0.0.1 --port 8088
```

`cloud/config.json` 支持 `default_timezone`、`enable_api_docs` 和数据库配置；PostgreSQL 使用 `OCTOP_CLOUD_DATABASE_URL`。设置 `enable_api_docs: true` 后，`/api/docs` 提供 Scalar 文档。`octop health --role cloud --url http://127.0.0.1:8088` 校验角色与就绪状态。管理员初始化仅使用账号领域服务，不会额外启动模型网关或运行请求恢复任务。

Docker 默认镜像只安装 cloud extra，不包含 Harness、Memory、Browser、Gateway、Node 工具执行环境或虚拟桌面。使用 `deploy/docker/docker-compose.yml` 构建和启动；可在容器启动后交互执行 `octop-cloud-init`，或者通过 Secret 注入 `OCTOP_BOOTSTRAP_PASSWORD`，仅在账号库为空时初始化。密码不会写入凭据文件或命令行，服务启动前清除该环境变量。容器不会开启旧网页 setup，也不会生成默认密码。HTTP 开发监听与生产反向代理/TLS 的边界与 [身份说明](cloud-identity.md) 一致。

桌面壳默认启动 `run --role desktop`，随机生成本机 API 凭证。用户首次打开桌面应用时，在登录页配置 HTTPS 云端地址（本机开发可用 loopback HTTP），然后使用账号密码、TOTP 或恢复码登录并绑定设备。替换已有设备需要在界面中明确确认；不会自动顶替旧设备。

也可提前运行 `octop local-config --cloud-url https://cloud.example.com`。配置写入 `local/config.json`，账号已选择时禁止更改云端地址。桌面长期设备凭证仍只由 Electron main 和系统凭据存储持有，renderer 只得到身份与操作结果。本机 API 凭证不传入 renderer。

`octop-local` 和便携包 `start.sh` / `start.bat` 供本机运行诊断，不是网页聊天入口；手工调用本机 API 需要显式提供足够强的 `OCTOP_LOCAL_API_TOKEN`。正常产品使用 Electron 的受控 HTTP 代理与 WebSocket relay。开发 UI 同样经过主进程自定义协议，不把本机凭证注入 Vite 环境或 JS 包。

## 产品界面

| 角色 | 页面 |
| --- | --- |
| 桌面 | 聊天、Agent、Skills、工作区文件、同步、用量与费用 |
| 云端管理员 | 用户、设备与安装回执、Agent/skills 发布和分配、供应商与平台模型、额度费用与核对、账号安全 |
| 云端普通账号 | 自己的设备、用量费用、密码与 MFA |

云端静态页面与 API 都使用独立路由集合。旧 `/chat`、setup、终端、浏览器控制、IM、Cron、Bridge 等入口不挂载；未知 API 返回 404，不回退为 SPA。云端普通账号不能通过修改前端 URL 获得管理权限。

桌面资源支持创建个人 Agent/skill、编辑个人版本、为平台资源创建个人副本，以及同步与重试安装。当前版本、期望版本、已安装版本和同步/安装错误分开显示。Agent 引用明确的 skill 版本；平台发布不会覆盖个人副本。云端可分别发布 Agent 与 skill，再分配到用户当前绑定的设备。设备页展示实际安装回执。

聊天读取 P5 保存的消息投影，展示消息、工具调用、附件和运行状态。用户发起聊天后，工具默认直接执行，写文件、编辑文件和 Shell 命令不再逐次请求确认；界面仍保留显式审批请求的展示与处理能力。打开会话、切换语言、重新连接和读取历史都不会发送 start/resume。断线后必须显式重新连接；已有的待审批操作仍需用户明确批准或拒绝。只读归档可以查看历史，但不能执行新任务。文件页只列出支持同步的工作区附件和产物，历史附件按消息内的固定内容引用下载。

同步页只根据持久化 outbox 与云端确认展示待同步、冲突和错误，提供重试与显式恢复。恢复会跳过有本地未同步修改的 Agent，不恢复运行检查点。资源安装问题在 Agent/Skills 页面处理；数据同步问题在同步页处理。产品界面不展示协议游标。

用量读取服务端预算、预占、已结算与待核对状态。JS 解析前保留账本整数金额的字符串形式，避免大额纳单位被 Number 舍入；设置额度时使用十进制到整数的精确转换。供应商 Key 仅存在于云端管理员表单和云端加密存储，桌面只选择平台逻辑模型。

`admin` 角色不限用量，无需设置预算；云端和桌面的用量页显示“无限制”，仍记录实际费用、预占与待核对金额。管理员仍需获得模型授权。普通用户继续按币种执行预算限制；角色变更以云端当前状态为准。

CLI 文案遵循终端 `LC_ALL` / `LC_MESSAGES` / `LANG`，不读取旧管理数据库；语言不是应用 config.json 的字段。界面支持 zh/en，语言选择保存到当前浏览器/桌面偏好，并在 API 请求中发送 `Accept-Language`。桌面日期统一使用已验证云端身份的 `timezone`，云端页面使用同一平台时区，不取操作系统时区作为业务显示规则。

## 退出、休眠和唤醒

1. Electron 立即关闭新请求入口，通过仅主进程可访问的 `/api/lifecycle/prepare` 通知 sidecar。renderer 代理拒绝 cloud 配置/会话及 lifecycle 控制路径，包括编码路径。
2. sidecar 拒绝新 turn，取消运行任务，保存 interrupted 状态；待审批任务也变为 interrupted。关闭并等待记忆写入器后，限时 2 秒尝试最终资源和数据同步。
3. 未收到确认的内容继续保留在 outbox。准备操作超时或失败时，Electron 停止 sidecar；不等待全部大文件传完，也不把超时当成同步成功。
4. 唤醒先等待保存/停机完成，再重新获取并验证云端设备授权。必要时重启 sidecar，然后重新选择同一账号的数据目录。不会发送聊天、恢复审批或重放工具命令。
5. 快速连续休眠/唤醒按顺序处理，只有最后一次唤醒可以重新开放请求入口。退出状态禁止后来的唤醒重新打开入口。
6. 强制终止无法执行最后同步；下次启动根据本地持久状态恢复为 interrupted，不自动执行旧任务。

窗口隐藏到托盘时应用仍存活；真正退出通过系统 Quit/退出菜单或已有“关闭即退出”偏好触发。云端不接管任何中断的任务。账号退出会关闭本机账号，同时尽力撤销远端设备；未确认远端撤销时界面会提示在云端设备页处理。

## 旧入口退役与保留代码

默认 CLI 注册表仅保留 run、cloud-init、local-config、health、version 和 completion。旧启动器不再装配 `OctopServer`、旧应用容器、AgentManager、Gateway、Cron 或 Bridge。仅供旧模块回归使用的依赖移到开发 dependency group `legacy-tests`；产品没有 `legacy-runtime` 安装模式。

旧 API、CLI 辅助模块和旧页面中尚被回归测试或公共能力引用的源码暂不批量删除；新应用入口和构建图不引用旧产品编排。已删除仅验证退役 CLI、旧 setup 容器、虚拟桌面镜像和 FnOS 旧启动器的测试，并以本阶段的角色边界、首次初始化及中断恢复用例替代；保留共享模块回归测试，但按本轮用户要求不运行无关用例。

旧 mobile/cluster Compose 已移除，旧全功能 FnOS 启动器明确拒绝启动。P7 取消旧 FnOS/full/lite 发布链，当前部署为 cloud-only 镜像或单实例 Helm（普通 PostgreSQL，无 pgvector 前提）。实际平台验收及未覆盖项目见 [Plan.md](../Plan.md)；不能把配置好的 CI 当作已执行结果。

## 本地源码依赖与产品 wheel 集合

按用户 P7 的最新要求，两库直接维护在仓库内 `vendor/octop-harness` 和 `vendor/octop-memory`，各自保留代码、许可证、测试和来源记录。`pyproject.toml` 的 `tool.uv.sources` 与 `uv.lock` 使用相对路径；普通 `uv sync --frozen` 加载本地 1.0.12/1.0.6 源码。不发布运行库，不依赖相邻 `*-p5` worktree，也不需要候选包覆盖或 `UV_NO_SYNC`。

```bash
uv sync --frozen
make build                   # 前端 + dist/product
make test-product            # 实际产品及直接依赖专项
```

`dist/product/` 包含 Octop wheel/sdist、两库 wheel 和 cloud/local-runtime 的锁定 requirements。单个 Octop wheel 不包含 uv 的源码映射；安装集合时先按角色装锁定依赖，再装 Octop：

```bash
# Python 3.12；示例路径为 POSIX，Windows 的解释器在 env/Scripts/python.exe
uv venv env
uv pip install --python env/bin/python --find-links dist/product -r dist/product/requirements-cloud.txt
uv pip install --python env/bin/python --no-deps dist/product/octop-*.whl
env/bin/python scripts/verify_product_install.py --role cloud
```

本地角色将 requirements 换为 `requirements-local-runtime.txt`；在安装 Octop 前，显式安装本次构建的运行库文件，验证参数为 `--role local`：

```bash
uv pip install --python env/bin/python --no-deps dist/product/octop_harness-*.whl dist/product/octop_memory-*.whl
```

角色 requirements 仅包含锁定的第三方包，两库以明确的本地文件安装，避免同版本远端包参与选择；不携带 editable 路径。云端依赖集合不会安装两库、Browser、Gateway、LangChain 或 cron。需要完全离线交付时，先把所选角色 requirements 中的第三方 wheel 下载进 wheelhouse，再用 `--no-index --find-links` 安装；默认 wheel 集合只内置产品与两个本地库。

云端 Docker 构建只复制两库 pyproject 元数据以读取 frozen 路径记录，不复制执行库源码，也不安装执行组件。Electron 便携包使用相同本地 wheel，并在压缩前检查同步接口与真实 CLI 启动。`uv run` 不再安装远程 Harness/Memory。分发方式见 [vendor/README.md](../vendor/README.md)。
