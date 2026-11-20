# Quark-VOD 协作契约

Quark-VOD 是可独立安装的夸克点播内核。本机另外通过家庭影院适配启用 Jellyfin2、宿主作业和现有状态库。本文依据 3.0.0 源码及隔离验证重写；开源化总体边界见[验收报告](/home/dong/.agent-context/verification/quark-vod-open-20261120/REPORT.md)，当前 RemoteStore 契约、失败语义、发布制品和本机回归/播放证据见[storage-contract 批次报告](/home/dong/.agent-context/verification/quark-vod-storage-contract-87d2228/REPORT.md)。

## 事实来源与交付

- [公共架构](docs/architecture.md)、[安装](docs/quickstart.md)、[插件协议](docs/plugins.md)：独立用户的接口和部署约定。
- [本机行为边界](docs/agent-architecture.md)、[影院契约](../hometheater/CLAUDE.md)、[宿主适配](../hometheater/docs/vod-host-migration.md)：本机路径、调度、保护与回滚。
- app/quark_vod 是共享内核权威源；app/main.py 是 API，包内 static/admin.html 是管理台。make sync 同步 Python 模块到 hometheater/scripts/quark_vod。七个宿主作业以影院 scripts 为准，make sync-jobs 仅维护旧 app/jobs 镜像。
- 公共发行从经检查的 sdist 取得，不推送这个运维仓库的历史。旧 agent 文档、宿主验证工具、app/jobs 和 custom-ffmpeg 二进制不属于公共发行。wheel、源码包与 amd64 Docker 镜像保存在 /home/dong/releases/quark-vod/3.0.0/。

## 不可破坏的行为

1. 环境文件只在显式 VOD_ENV_FILE 时读取；进程环境优先。路径、搜索源、OpenList 数据库继承、模型和媒体库集成都显式配置。公共默认关闭 Jellyfin、破坏性维护、自愈、启动扫描、启动云写探针与 AI。凭据支持文件来源，不输出 Cookie、token、分享或签名媒体地址。
2. 搜索 JSON 的增删、启停和参数修改可热加载；坏配置保留上次有效快照，在途请求不切换配置，缓存按配置隔离。安装或更新 Python provider 代码需要重启；仅显式允许的 entry point 执行。当前网盘保存仍由 Quark/OpenList 实现，不能宣称任意网盘都能换入。
3. 身份先于画质。作品、年份、类型、季集及同一枚举 relative_path 一起核验；禁止模糊包含放行、跨目录补身份或按电影文件大小猜片。强作品目录可帮助英文/数字集名，但显式版本、年份、季集和多集冲突仍拒绝。未知画质和历史 valid_hd 不证明高清。
4. 网络、鉴权、404、超时及不完整枚举保留旧证据。只有完整枚举且身份可靠才能撤销所选集的存在提示，不波及其他集。无搜索配置、搜索不完整和本次限定范围无源分别报告，不能表述为全网无资源。
5. 保存/删除走配置的写挂载，播放走读挂载。本机仍为 /夸克网盘/点播 与 /夸克/点播。同身份只存一个精确文件；提交不明先恢复 save intent/task_id，禁止盲目重提。done 不等于可播，必须检查读取；Jellyfin 模式还必须激活既有 STRM。无 Jellyfin 模式直接返回已验证 URL，不创建或改写片库。
6. /api/admin/*、stats、capability、preheat 统一 Bearer 认证；无 token 禁用管理，管理台令牌只保存在页面内存。GET capability/overview 不创建云探针；只有显式 POST probe 执行连接与写权限验证。播放接口供播放器使用，不构成面向公网的用户权限系统。
7. 维护必须持锁并确认保护库、会话、用户状态、置顶和近期访问；未知即停止。先恢复入口，再删除精确云对象，成功后提交 cleaned。在线 SQLite 用普通 mode=ro 读 WAL，不用 immutable，不直接写 Jellyfin 库；不处理回收站、不整库 prune、不覆盖后续观看状态。
8. 本机保留 256 MiB 限额、只读 app、持久化路径和现有播放包装器。实际加载位置与磁盘同步分别核对。实质变更后重新隔离验证、现场验收并重写相关文档，不能用健康检查代替完整播放；清理本次无用产物，保留明确回滚材料。
