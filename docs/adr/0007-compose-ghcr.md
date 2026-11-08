# 0007：Compose 部署与 GHCR 双架构发布

## 状态

已接受当前发布配置；工作流存在不等于镜像已发布或真实部署已验证。本轮只补充决策记录，不改变发布或部署配置。

## 背景

**历史事实**：项目最初实现已包含 Dockerfile 与 Compose；后续单独增加 GHCR 发布。运行依赖用户账号 session、SQLite 和媒体原件，必须持久保存。

**推断动机**：预构建同标签多架构镜像可减少服务器构建负担，同时保留本地构建入口，适配 AMD64 与 64 位 ARM 主机。

## 决策

- Compose 默认镜像为 `ghcr.io/svlic/tp4by:latest`，可用 `TP4BY_IMAGE` 固定版本或 SHA 标签，也可源码构建。
- GitHub Actions 使用 Buildx/QEMU 构建 `linux/amd64` 和 `linux/arm64`；镜像名按实际仓库路径生成。
- main 推送发布 `latest`、`main` 和短 SHA 标签；`v*` 标签发布同名版本和 SHA，不覆盖 `latest`；PR 只构建验证，不登录、不推送。允许手动运行，主分支运行可更新 `latest`。
- 使用 GitHub 提供的 `GITHUB_TOKEN` 写 Packages，不内置 Telegram 凭据，也不自动部署服务器。首次发布后包是否公开需要人工设置；Fork 需配置自己的镜像地址。
- 命名 volume 保存 session、数据库与原件；首次交互登录完成后再启动后台实例。备份须停服后整体进行，不能单独清空媒体，也不能执行删除数据卷的升级操作。

## 替代方案

只支持本地构建会增加部署机器负担；单架构镜像不能覆盖目标主机；发布后自动部署涉及凭据与共享基础设施，不纳入此工作流。

## 后果与局限

镜像可构建不证明 Telegram 账号权限有效。包私有时匿名拉取会失败，需要公开包或登录 GHCR。双架构是构建目标声明，不是本文实际运行两种架构的测试结论；部署必须保持单 Writer，不同时启动登录容器与服务。

Tests 与镜像发布是独立工作流，目前没有“测试成功才发布”的门禁，也没有滚动标签的并发发布顺序保护。`latest` 可变，基础镜像和 Python 依赖未完整锁定；未来是否加入门禁或固定 digest 属于交付策略调整，不随本次业务修复擅自更改。

## 代码相对链接

[Compose](../../compose.yaml)、[Dockerfile](../../Dockerfile)、[发布工作流](../../.github/workflows/docker.yml)、[部署说明](../../README.md)。

## 提交证据

[`5047af0`](https://github.com/svlic/tp4by/commit/5047af0) 引入 Dockerfile 和持久卷部署；[`8e9e77d`](https://github.com/svlic/tp4by/commit/8e9e77d)（2026-11-08 11:55:33 +08:00）加入 Compose 镜像部署与多架构发布。二者分别证明配置演进，不能据此宣称某次线上发布已成功。
