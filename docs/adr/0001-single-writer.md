# 0001：单 Writer、六频道与 SQLite 顺序队列

## 状态

已接受。历史架构保留；成功历史与失败隔离按本轮澄清补充，见 [0004](0004-durable-publication.md)。

## 背景

**历史事实**：应用以 Telegram 用户账号运行，区分 `from`、`oneshot`、`repeat`、`up`、`down`、`man` 六个互异频道；没有 Reader、SOURCE、INFO/VER 或 Web 层。SQLite WAL 保存队列与业务状态，数据目录文件锁阻止共享目录的第二个 Writer。

**推断动机**：把历史分类、人工操作和发布进度串起来，可避免并行业务同时基于旧状态规划，降低单实例恢复复杂度。

## 决策

- 一个 Worker 按持久化任务顺序执行业务；队首 pending 即使等待重试，也不允许后续业务越过。
- FROM 以完整相册作为一条记录，仅按成功归档历史的 Hash 分类，优先级为 **DOWN > UP > REPEAT > ONESHOT**；不使用自身或业务编号命中。
- REPEAT 保存当次新记录，不复制、聚合或删除历史 ONESHOT。人工删除频道消息不触发自动补发。
- **本轮澄清**：未发布计划的暂存业务变更不是成功历史；终态失败不能污染后续分类。归档成功与清理成功分开定义。

## 替代方案

多 Worker、外置队列或独立 Reader 可提高吞吐，但需要额外并发控制和发布协调；目前不引入。也不把 Telegram 频道现状视为可替代 SQLite 的业务数据库。

## 后果与局限

顺序明确但队首重试会阻塞后续业务。WAL 不意味着支持多 Writer 部署；不同数据目录的双实例也无法由同一文件锁保护。尚无大规模容量压测结论。SQLite 与 Telegram 不具备跨系统原子事务。

## 代码相对链接

[应用及锁](../../src/tp4by/app.py)、[持久化队列](../../src/tp4by/storage.py)、[业务引擎](../../src/tp4by/engine.py)、[配置](../../src/tp4by/config.py)。

## 提交证据

[`5047af0`](https://github.com/svlic/tp4by/commit/5047af0) 一次引入应用、数据库、引擎与测试；[`a9605b7`](https://github.com/svlic/tp4by/commit/a9605b7) 简化路由表达与启动分组收集，声明保持行为。本轮“仅成功历史参与分类”是补充约束，不能归功于旧实现已完整处理终态失败。
