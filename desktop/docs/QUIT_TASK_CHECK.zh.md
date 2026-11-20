# 桌面生命周期与安装器验收

自动化与实机结果分开记录，执行结果见 [Plan.md P7](../../Plan.md)。这里只列验收步骤，不代表已经在所有平台通过。

## 自动化入口

- `make test-product`：生产 CLI 跨进程全流程，断网/额度/撤销、持久 outbox、实际强杀恢复及不重放工具。
- `desktop/src/main/session/cloud-session.test.ts`、`main/supervisor/lifecycle.test.ts`：登录/续期、并发 prepare/唤醒与超时停止；`main/protocol/` 对应测试验证代理隔离。
- `scripts/verify_product_install.py`：安装环境依赖隔离、必需的运行库接口与真实 CLI 就绪；便携打包在压缩前执行。

macOS arm64 可在构建 Electron 与 portable 后运行 `uv run python scripts/verify_desktop.py`：实际窗口、keychain、发布/安装、UI WS 工具闭环、结算/同步与退出重启；临时云端使用确定性供应商，无付费请求。该脚本不触发真实 OS 休眠，不代替其他平台检查。

## 实机检查（每个目标 OS/架构）

1. 安装原生产物，从干净 OCTOP_HOME 启动，核对平台、版本和 payload 摘要。首启配置 HTTPS 云端并登录，确认产品页和本机随机端口。
2. 启用系统 keychain，记住登录后退出/重启验证续期；检查磁盘无明文刷新凭据。禁用可用 keychain 时不得落盘，重启需登录。
3. 运行会写文件的受审批工具。未批准时退出、断开 WS 或强杀进程；重新打开只显示 interrupted，文件未被自动写入。
4. 在实际 OS 上休眠/唤醒（含快速连续事件），验证没有后台新 turn/重复工具；唤醒先验证设备。云端撤销设备后不得恢复访问。
5. 云端断网时编辑/上传文件并退出。重新上线读取真实 pending 后重试，不得显示虚假的已同步；历史附件引用不受当前文件修改影响。
6. 验证托盘隐藏继续运行、Quit 停止本次 sidecar 进程树；整包升级后数据仍在且没有单独旧服务残留。
7. Windows 验证 NSIS 展开/DLL、卸载保留用户数据；Linux 验证 deb/rpm 与 keychain/basic_text 区别；macOS 验证 DMG/ZIP、Gatekeeper 与 keychain。

不能通过单机 macOS 结果推断 Windows/Linux 或其他 CPU；不能以调用 lifecycle mock 当作真实睡眠事件。没有运行的项目保持待验收，不伪造 CI 通过或签名/公证状态。
