# 架构决策记录（ADR）

这些 ADR 按长期有效的决策主题组织，不是一提交一篇，也不是对原始需求未决边界的追认。状态“已接受”表示当前采用的约定，不代表真实 Telegram 已验证。

| 编号 | 决策 |
| --- | --- |
| [0001](0001-single-writer.md) | 单 Writer、六频道与 SQLite 顺序队列 |
| [0002](0002-record-number-hash.md) | record 身份、业务编号与媒体 Hash 分离 |
| [0003](0003-manual-seeding.md) | 人工编号一次播种与 Hash 扩散 |
| [0004](0004-durable-publication.md) | 持久化发布计划、成功边界与先发后删 |
| [0005](0005-up-presentation.md) | UP 展示签名、原件与最早可读副本 |
| [0006](0006-ingestion-and-man.md) | 事件去重、启动补录与 MAN |
| [0007](0007-compose-ghcr.md) | Compose 部署与 GHCR 双架构发布 |

## 证据与阅读约定

- **历史事实**：以源码、测试和 Git 差异为依据。`e2d3347` 的 README 只有 `# tp4by` 标题，并无可追溯的架构正文；不能从它恢复完整设计讨论。
- **推断动机**：解释设计可能解决的问题，不冒充作者当时的决策记录。
- **本轮澄清**：2026-11-08 [架构调查与确认](https://ampcode.com/threads/T-01a1b96d-2606-75e6-ac15-6e2149bc8184)中明确的业务约束及据此采用的工程方案，不能倒写为旧提交已经实现。ADR 0004 的失败隔离与旧计划恢复随本记录一同修复，并增加模拟网关回归。

历史主线：[`5047af0`](https://github.com/svlic/tp4by/commit/5047af0)（2026-11-08 11:48:10 +08:00）一次引入主要实现；[`8e9e77d`](https://github.com/svlic/tp4by/commit/8e9e77d)（11:55:33）加入镜像发布；[`a9605b7`](https://github.com/svlic/tp4by/commit/a9605b7)（12:31:45）是以行为保持为目标的消融优化，不是新一轮业务重定义。

代码链接均相对仓库定位到文件，不依赖私有 helper 或易漂移的行号。仓库测试采用模拟网关，不构成真实账号发送、删除或线上 GHCR 拉取成功的证明。部署步骤与未决边界见[根 README](../../README.md)。
