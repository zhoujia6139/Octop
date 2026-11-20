# Deployment

> P6：默认部署已切换为独立云端应用。当前可执行入口与首次配置见 [产品说明](../docs/product-applications.md)。下文的旧 full/lite、网页 setup、IM/桌面服务器及 FPK 发布说明属于待 P7 更新的历史记录，不能用于新产品部署。

[中文](README_CN.md)

| Directory | Contents |
|-----------|----------|
| [docker/](docker/README.md) | Image build, Compose, and the container entrypoint |
| [helm/octop/](helm/octop/README.md) | Helm chart: pgvector, multiple Octop replicas, one Ingress |

`fnos/docker/` stays where it is. That tree is the FnOS package, not this image build.
