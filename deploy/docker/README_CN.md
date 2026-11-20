# Octop 云端 Docker 部署

此目录现在只构建独立云端管理/模型网关，不运行 Agent、工具、IM、Cron、Bridge、浏览器或远程桌面。桌面执行使用 Electron 应用。

从仓库根目录运行：

```sh
docker compose -f deploy/docker/docker-compose.yml up --build -d
docker compose -f deploy/docker/docker-compose.yml exec octop octop-cloud-init
```

第二条命令交互创建第一个管理员，不提供网页 setup。之后访问 `http://127.0.0.1:8088` 管理用户、设备、资源、模型与费用。生产环境为云端配置 HTTPS 反向代理。

可设置 `OCTOP_DATA` 指定数据卷、`OCTOP_PORT` 指定端口、`OCTOP_DEFAULT_TIMEZONE` 指定平台时区，或通过 `OCTOP_CLOUD_DATABASE_URL` 使用 PostgreSQL。没有外部数据库时使用卷内 `cloud/cloud.db`。自动引导可通过 Secret 注入 `OCTOP_BOOTSTRAP_PASSWORD` 和 `OCTOP_ADMIN_USERNAME`；只在账号库为空时创建账号，不写明文凭据文件，启动服务前清除密码环境变量。

`docker-compose.cn.yml` 现在是镜像覆盖文件，必须指定从新 Dockerfile 构建的 `OCTOP_CLOUD_IMAGE`，与默认 Compose 组合使用。旧 mobile/cluster Compose 和 full/lite 桌面服务器镜像方案已退役；P7 已调整为 cloud-only 发布矩阵，实际未运行的平台保持待验收。

详见 [产品入口与生命周期](../../docs/product-applications.md)。P7 按用户要求改为仓库内 vendor 源码依赖，不再等待运行库远端发布。已验证 frozen cloud-only 环境与本地镜像启动，平台/安装器覆盖见 Plan.md，不等同于已发布镜像。
