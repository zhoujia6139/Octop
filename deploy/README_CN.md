# 云端部署

云端独立运行管理与模型网关，Agent 在 Octop Desktop 执行。使用 [Docker](docker/README_CN.md) 或 [Helm](helm/octop/README_CN.md) 示例；新镜像仅包含 cloud 依赖，通过显式初始化创建首位管理员，没有 setup wizard。

持久化使用 SQLite 卷或 PostgreSQL，保存账号/设备、资源、账本与同步内容。无需 Agent 工作区、浏览器/终端控制、IM、定时任务、向量数据库或 full 执行镜像。桌面连接公开 HTTPS origin，代理 URL 不携带后台或本机凭据。

源码依赖与 wheel 集合见[产品说明](../docs/product-applications.md)。旧 cluster/mobile Compose 和 FnOS 全功能包已退役。当前 Chart 只运行一个网关，不能据此宣称多副本恢复已验收。
