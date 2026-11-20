# Octop

[English](README.md) · [产品启动](docs/product-applications.md) · [架构](docs/architecture.md) · [实施计划](Plan.md)

Octop 在桌面应用中运行 Agent，独立云端负责账号、Agent/skills 发布分配、模型鉴权与费用、数据备份同步。

桌面采用 Electron + Python sidecar，工具、工作区和记忆在本地执行。所有模型请求经过云端网关，供应商 Key 留在服务端。聊天记录、附件、产物和逻辑记忆按约定同步。退出或休眠中断任务，唤醒和重启不会自动重放工具。

## 源码启动

开发需要 Python 3.12+、uv 和 Node 24。Harness 与 Memory 源码维护在本仓库 `vendor/`，无需额外相邻仓库或运行库远端发布。

```bash
uv sync --frozen
make build-frontend
uv run octop cloud-init --username admin
uv run octop run --host 127.0.0.1 --port 8088
```

在云端管理页配置供应商、平台模型及费率、用户模型授权与预算，并发布、分配 Agent/skills。正式部署使用 HTTPS，然后打开 Octop Desktop，用密码与已启用的 TOTP MFA/恢复码登录。详见[桌面说明](desktop/README.md)。

部署示例：[Docker](deploy/docker/README_CN.md)、[Helm](deploy/helm/octop/README_CN.md)。云端仅安装 `octop[cloud]`，不以本地执行库为运行前提。首期不提供网页聊天、IM、定时任务、多设备接续或跨用户 Agent 协作。

## 构建与验证

```bash
make lint typecheck
make test-product
make build                # UI + dist/product wheel 集合与角色锁定 requirements
```

交付时要一起使用 wheel 集合；单独 Octop wheel 不包含 uv 的本地源码映射。[安装命令](docs/product-applications.md)说明了两种角色的安装方式。桌面便携包也从相同 vendor 源码构建运行库，不发布这些本地版本到 PyPI。

[API](docs/api.md) · [CLI](docs/cli.md) · [贡献说明](AGENTS.md) · [许可证](LICENSE)
