# 云端 Helm Chart

Chart 运行一个独立云端管理/模型网关和普通 PostgreSQL，Agent 在桌面执行。无需 pgvector、cron/渠道租约、共享工作区或粘性会话。多网关恢复未验收，`replicaCount` 限定为 1，升级采用 Recreate，期间云端短暂不可用，桌面保留待同步状态。

1. 使用本次 cloud-only 源码构建的镜像，设置准确 tag；不要复用旧全功能同名版本。
2. 按 [secrets.example.yaml](secrets.example.yaml) 创建 Secret：`url` 是云端 DSN，`user/password/database` 用于内置 PostgreSQL。DSN 中密码需 URI 编码。托管库填写 DSN 和就绪检查的 host/port 即可。
3. 配置 HTTPS Ingress 与 TLS Secret。托管数据库设置 `postgresql.enabled=false` 和 `database.host`，无需向量扩展。

```bash
helm lint deploy/helm/octop
helm template octop deploy/helm/octop -f deploy/helm/octop/values.example.yaml
helm upgrade --install octop deploy/helm/octop -n octop --create-namespace -f my-values.yaml
```

bootstrap 只在空库创建首位管理员，不改已有账号。云端状态、加密密钥与同步内容均在 PostgreSQL，Pod home 可丢弃。破坏性维护前备份整个数据库（包含 secrets 表）。健康探针为 `/api/health`，管理 UI 为 `/`。

模板 lint/render 不等于已在真实集群部署；产品边界见[产品说明](../../../docs/product-applications.md)和[架构](../../../docs/architecture.md)。原生桌面安装与真实生命周期按目标平台另行验收。
