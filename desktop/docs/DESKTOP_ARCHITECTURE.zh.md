# Electron 与本地 Python 应用

本文记录当前实现。整体边界见[架构](../../docs/architecture.md)与 [ADR 004](../../docs/adr/004-local-agent-cloud-control.md)，安装步骤见[桌面 README](../README.md)。

## 进程与权限

| 进程 | 职责 |
| --- | --- |
| Electron main | 单实例、窗口/托盘、OS 凭据加密、可信云端会话、sidecar 守护、HTTP/WS 代理、休眠/退出、整包更新 |
| 沙箱 renderer | `octop://app/` 产品 UI，通过受限 preload/代理发请求，不持有长期凭据或本机 token |
| Python sidecar | `launch.py run --role desktop`，随机 loopback 端口，验证 main 注入的本机 token；账号隔离目录、本地 Harness/Memory、安装与同步 |
| 独立 cloud | 登录/MFA/设备、发布分配、模型鉴权/额度/结算、数据同步；没有本机执行器 |

开发 UI 同样经过 main 自定义协议，不向 Vite 注入凭据。main 验证 IPC 来源和主 frame，代理拒绝 cloud 配置/会话及 lifecycle 控制路径（含编码路径）。本机 token 与云端账号/设备凭据不能混用。

## 分发与目录

源码库由 Octop 的 `vendor/octop-harness`、`vendor/octop-memory` 维护，uv 使用相对路径；便携包从同一源码构建 wheel。`packages/` 是安装结果，不含 editable 路径。CPython、UI、main/preload 和依赖随同一个版本交付。macOS/Linux 安装器使用展开的 portable 树，Windows NSIS 展开 zip。版本、平台、Python 与 zip SHA256 由打包流水线记录。

`OCTOP_HOME` 是数据根；sidecar 配置在 `local/config.json`，经验证的 cloud/user 身份选择自己的账号目录。安装目录存放运行时，不与用户数据混放。不再认领旧本地管理员、复用旧 JWT 或启动旧 setup wizard。默认包不带 Gateway/cron；云端镜像不带四个执行库。

## 登录与生命周期

main 先配置可信 HTTPS origin（回环开发可 HTTP），执行密码与已启用的 TOTP/恢复码验证，绑定当前设备，再向 sidecar 选择账号。长期刷新凭据通过系统 keychain 加密；没有可靠后端时仅存内存。设备替换/撤销通过 generation fence 阻止旧设备写入。

休眠/退出立即关闭新请求，串行 prepare：中断回合与审批、保存状态、排空已接受记忆任务、限时最终同步，再 detach。超时则停止 sidecar；outbox 未获确认不能显示成功。唤醒等待 prepare 完成，续期并验证身份后再开放入口；不会发送 start/resume。快速多次事件由 lifecycle 的 revision 控制最终开放时机。托盘隐藏保持运行。

更新由壳整体替换，不支持从旧 Web 设置或 `octop update` 单独升级 Python/UI。强制终止下次启动读取持久历史，标为 interrupted，不自动重放工具。实际 OS keychain、原生安装器、真实睡眠及各目标平台的验收见[检查表](QUIT_TASK_CHECK.zh.md)；模拟生命周期不能替代这些结果。
