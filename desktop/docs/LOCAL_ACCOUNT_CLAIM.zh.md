# 旧本地账户认领方案已退役

当前未部署产品没有旧数据兼容要求。桌面使用云端密码与 TOTP MFA/恢复码登录，经当前设备身份验证后选择隔离的数据目录，不认领旧本地管理员，不注册旧 desktop_device 凭据，也不启动 setup wizard。

入口与权限见[当前桌面架构](DESKTOP_ARCHITECTURE.zh.md)和[身份合同](../../docs/cloud-identity.md)。不要按旧账户认领草案增加接口或绕过云端登录。
