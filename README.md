# TP4BY — 单 Writer Telegram 媒体归档

参考 [svlic/T4BY](https://github.com/svlic/T4BY)（[参考提交 d4b512e](https://github.com/svlic/T4BY/commit/d4b512e30656f2803781567f872bf056d34ae551)），按《T4BY 简化版业务逻辑说明书 v2.6》重新实现。
Python 3.11+ / Telethon 用户账号 / SQLite WAL。不包含 Reader、SOURCE、INFO/VER、Web 界面或旧版 blacklist 逻辑。

架构背景、替代方案及提交证据见 [ADR 索引](docs/adr/README.md)。ADR 区分历史事实、推断动机与本轮澄清，不将未决边界写成最终业务定义。

## 业务行为

- 六个独立频道：`from`、`oneshot`、`repeat`、`up`、`down`、`man`。
- FROM 收齐相册后，以整组作为一条记录。业务编号取合并文本中第一个匹配的 6/7 位数字，不是主键。
- 图片（包括 document 发送、MIME 标注错误但能解码的图片）使用 **64 位 pHash 精确相等**；视频使用文件内容 SHA-256。
  Hash 键包含类型和算法，不使用图片相似度阈值或新增二次校验；纯色图片排除明确延期。
- FROM 只按成功归档历史 Hash 判断：**DOWN > UP > REPEAT > ONESHOT**，不使用自身或编号命中。
- REPEAT 只存当次新记录，不复制、聚合或删除历史 ONESHOT。
- 人工向 UP/DOWN **转发**单条或相册：编号只查当时成功归档历史的同号记录一次，然后按全部媒体 Hash 递归扩散，不在递归中重复编号搜索。
  人工 DOWN 允许任意来源的带编号转发；即使媒体不同，也先找成功历史同号记录作为种子。
  编号关系显式持久化供 UP 后续重建使用，不成为未来 FROM 的路由规则。
- UP 合并连通集合、媒体去重、编号去重，按 Writer 首次记录顺序展示；优先使用最早可读副本。
  每批最多 10 个图片/视频，首项 caption 带完整编号。编号超 1024 字符时改为独立文本，超过 4096 时按编号边界分段。
  展示未变只更新成员关系；两组被新记录桥接时合为一组。
- DOWN 对新增 Hash 做唯一插入和队列追溯，不做聚合。人工 DOWN 原消息不动，其他记录独立归档。
- 先发布所有新批次并持久化映射，再清理 ONESHOT、旧 UP 版本和已替代的人工 UP 输入。
  不清理旧 REPEAT。人工删除频道消息没有任何事件处理或自动补发。
- 解析错误直接转 MAN；技术错误指数退避重试，耗尽后转 MAN。MAN 接收错误说明及原始输入的转发，不提供恢复指令。

## Docker 部署

需要 Docker Engine 和 Docker Compose v2+。默认使用 `ghcr.io/svlic/tp4by:latest`，
同一镜像标签包含 `linux/amd64` 和 `linux/arm64`（64 位 ARM），Docker 自动选择主机架构。

需要一个 Telegram **用户账号**（不是 Bot Token），账号必须能够读取三个输入频道、在归档频道发媒体，并删除 ONESHOT/UP 的旧消息。
六个频道必须互不相同。请先在测试频道验证权限和清理行为。

### 直接拉取镜像部署（推荐，无需克隆源码）

只下载 Compose 配置和环境变量模板，不需要 Git、Python 环境或本地构建：

```bash
mkdir -p tp4by && cd tp4by
curl -fL https://raw.githubusercontent.com/svlic/tp4by/main/compose.yaml -o compose.yaml
curl -fL https://raw.githubusercontent.com/svlic/tp4by/main/.env.example -o .env
# 编辑 .env，填写 Writer API ID / API Hash 和六个频道 ID（也支持公开用户名）。
docker compose pull
docker compose run --rm --no-deps --pull never tp4by
```

下载 `.env` 模板仅用于首次部署，已有配置时不要重新下载覆盖。
`docker compose pull` 直接拉取预构建镜像，不执行 Compose 文件中的 `build`；
下方启动命令使用 `--no-build`，无需下载源码，也不会回退到本地构建。

首次在交互终端输入手机号、验证码和可能的两步验证密码。看到 `T4BY started` 后按 Ctrl+C，再后台启动：

```bash
docker compose up -d --no-build --pull never
docker compose logs -f tp4by
```

当前首次启动仅从当前频道末尾建立基线，不导入启动前历史；是否另行支持首次历史导入仍待确认。以后启动按持久化游标回看，恢复离线消息；相同相册不会重复建档。
在线消息在启动历史补录后入队，历史按 Telegram 时间排序（同秒跨频道没有严格全局顺序）。
游标在启动补录后推进，长期运行后的首次重启可能扫描较多已处理消息，但事件唯一键会跳过重复。

升级镜像（登录状态和数据保留）：

```bash
docker compose pull
docker compose up -d --no-build --pull never
```

可在 `.env` 中添加 `TP4BY_IMAGE=ghcr.io/svlic/tp4by:v0.1.0` 固定到已发布的版本标签
（示例标签，实际以仓库 Packages 为准），或使用 `sha-<短提交号>` 固定提交。

### 从源码构建（可选）

若镜像尚未发布，或需要运行本地修改，可从源码构建，无需访问 GHCR：

```bash
git clone https://github.com/svlic/tp4by.git
cd tp4by
cp .env.example .env
# 编辑 .env，填写账号和频道配置。
docker compose build
docker compose run --rm --no-deps --pull never tp4by
# 完成首次登录并按 Ctrl+C 后：
docker compose up -d --pull never
```

本地构建后的升级使用 `git pull`、`docker compose build` 和 `docker compose up -d --pull never`。
不要同时运行登录容器和后台服务；重新登录前先执行 `docker compose stop tp4by`。

`tp4by-data` volume 保存登录 session、数据库和媒体原件。
**不要执行 `docker compose down -v`，也不要单独清空媒体缓存。** 停服后整体备份 volume，数据库和原件须一起恢复。
单实例运行；文件锁阻止共享同一目录的第二个进程。没有从旧 T4BY 数据库迁移的逻辑。

### GitHub Actions 镜像发布

`.github/workflows/docker.yml` 使用 Buildx/QEMU 构建 AMD64 和 ARM64 多架构镜像并推送到
`ghcr.io/<仓库所有者>/tp4by`（Fork 时按实际仓库名生成）：

- 推送到 `main`：发布 `latest`、`main` 和 `sha-<短提交号>`。
- 推送 `v*` 标签：发布同名版本标签和 SHA 标签，不覆盖 `latest`。
- Pull Request：仅验证双架构构建，不登录、不推送镜像。
- 支持通过 Actions 页面手动运行；主分支运行时也会更新 `latest`。

发布使用 GitHub 自动提供的 `GITHUB_TOKEN`，无需配置额外的 Registry 密钥；仓库需允许 Actions 写入 Packages。
首次发布后请在 GitHub Packages 设置中将镜像包设为 **Public**，才能匿名拉取；
私有包需先使用具有 `read:packages` 权限的凭据执行 `docker login ghcr.io`。
Fork 部署时请在 `.env` 中设置 `TP4BY_IMAGE` 为自己的镜像地址。构建工作流只发布镜像，不会自动部署服务器。

## 本地开发

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
set -a
. ./.env
set +a
export T4BY_DATA_DIR=./data
tp4by
```

`.env` 不会被应用自动读取，Docker Compose 使用 `env_file`，本地运行需如上导入环境变量。
默认技术失败上限为 5 次（`T4BY_RETRIES`），退避 2、4、8…秒，上限 300 秒；Telegram FloodWait 遵守服务器等待时间，不消耗普通重试次数。
MAN 自身发送失败持久保留待投递状态，60 秒后再试；FloodWait 使用服务器等待时间。

```bash
pytest
ruff check .
ruff format --check .
```

测试使用临时 SQLite、真实 Pillow 图像和模拟 Telegram 网关，不使用真实账号、不写外部频道。
覆盖：首次/重复分类、人工两阶段扩散、同号未来输入、UP 合并/无变化跳过、10+2 相册及长编号、DOWN 传递、
ONESHOT 安全清理、重启续发、发送结果不确定、异常投递、图片 document 和视频摘要。

## 工程边界与待确认项

这些选择不是对文档未决业务的最终定义，后续可按确认结果调整：

| 边界 | 当前实现 |
| --- | --- |
| 多个文本字段 | 按消息 ID 合并，重复出现同一编号有效；没有编号或存在多个不同编号转 MAN |
| 输入附件 | 无媒体、非图片/视频附件整条转 MAN，不静默丢弃组内附件；FROM 单条消息转 MAN |
| UP 编号顺序 | 按首次记录顺序，不按编号大小排序 |
| REPEAT 后续升类 | 保留其 REPEAT 物理归档，只清理 ONESHOT |
| DOWN 命中人工编号连接的 UP 一支 | 只沿 Hash 扩散，不沿编号关系扩大；剩余 UP 媒体安全重建 |
| 已聚合 UP 相册再人工转 DOWN | 不提供专门的“聚合转移”指令；多编号会进入 MAN |
| 媒体存储 | 原件长期保存在本地，自动清理不删除原件；需要监测磁盘容量 |
| 图片输出 | 保留原文件及 photo/document 形式，不主动转码或缩图；文件相册与图片/视频相册分开，不改变媒体顺序 |

SQLite 持久化接收事件、记录/Hash 索引、人工种子关系、归档位置、当前 UP 版本及每一步发送/清理进度。
单 Worker 串行执行业务，等待重试期间不让后续业务越过未完成计划；适合简化版单实例，尚未做大规模容量压测。

### 成功归档与失败隔离

以下约定随 ADR 整理修复并由模拟网关回归覆盖，不能追认为旧提交已完整实现，详见 [ADR 0004](docs/adr/0004-durable-publication.md)。

- 规划前状态作为 undo 持久化。pending 期间不允许后续业务越过；全部新批次发送并在事务中发布映射前，业务变更仍为暂存状态，不算成功历史。
- 尚未发布就终态失败：撤回本任务新增 record/Hash/人工种子以及历史 route/DOWN 变更，避免后续任务隐式补发。已发送的部分批次留在失败计划供 MAN 人工检查，不自动清理或续发；旧有效版本保留。
- 全部新批次发送成功且映射事务发布后即为归档成功。之后清理终态失败不撤销归档，只报 MAN 并保留失败清理计划；成功历史仍可用于后续分类。
- 旧格式失败计划尝试依据已发布的 `archives` / `up_groups` 恢复状态，不承诺从任意旧中间状态无损还原。恢复 pending 计划与自动续发终态失败是两回事。
- 若升级时同时存在未隔离的旧失败历史和未发布的待续发计划，后者停止续发并转 MAN，避免继续使用污染历史；已被后续旧计划成功发布的结果保留，不自动重新分类。升级前请停服整体备份数据卷。

Telegram 和 SQLite 无法组成分布式事务。发送请求使用持久计划生成的固定 `random_id`，进程重启后复用，
已确认的批次不会再次发送；结果不确定时不提前清理旧版本。Telegram 的去重窗口有限，不能保证任意延迟重试下的绝对 exactly-once。
重试耗尽后的部分新批次不等于成功归档；已发布后失败的旧版清理也不等于归档失败。MAN 转发可能因保护内容或原消息已删除而失败，此时错误通知先保留，原件转发任务持续等待。

本仓库通过 GitHub Actions 发布镜像，不自动部署、不内置 Telegram 凭据。真实 Telegram 发送/删除需要配置账号后另行联调。
