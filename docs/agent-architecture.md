# 本机点播行为与部署边界

本文件针对 dong-server 的影院集成，依据 3.0.0 最终实现重写。公共用户先看[公共架构](architecture.md)与[插件协议](plugins.md)；当前本机验收结果见[本轮报告](/home/dong/.agent-context/verification/quark-vod-open-20261120/REPORT.md)，宿主作业见[影院架构](../../hometheater/docs/agent-architecture.md)。

## 内核与适配

CLI `quark-vod` 经 `quark_vod.api:create_app` 启动 FastAPI，单 worker。wheel 自带管理台；公共 Compose 使用 /data 持久卷、非 root 用户、只读根文件系统、256 MiB 内存和默认 loopback 发布。默认不连接媒体库、不扫描宿主目录、不执行云写探针、不启动 AI 或自愈。

本机通过 hometheater/quark-vod.compose.yml 运行同一镜像，使用 host 网络和 UID/GID 1000，保留 app 到 /app 的只读挂载，以 PYTHONPATH=/app 加载与镜像同版的源码。旧 data、logs、cinema2 继续原路径读写；OpenList/Jellyfin 数据库目录、原 .env 和凭据目录只读挂载。不得因权限问题递归 chown 业务数据。

宿主统一经 scripts/run_vod_host.py 读取 .vod-runtime.env，再由 VOD_ENV_FILE 指向原 .env；运行参数和凭据层分开。该文件显式保留状态库、OpenList Cookie 继承、Jellyfin 路径与保护开关。搜索五节点属于本机 profile，放在 ~/.config/quark-vod/search.json；管理 token 同目录另存 0600 文件。配置挂载整个目录，确保原子替换可见。具体入口和命令以[迁移说明](../../hometheater/docs/vod-host-migration.md)为准。

## 搜索与实际文件

每次搜索使用一个不可变配置快照，最多四种查询变体共享该快照；配置指纹进入缓存键。JSON 变更自动加载，坏文件保留旧版并报告错误；新增第三方 Python provider 必须明确列入允许名单并重启。内置 html/json provider 仅承担搜索，分享保存由原 QuarkTransfer 与 OpenList 适配完成。

节点请求、响应体、页数、详情数、线程和整组预算均有界。未配置源是 search_not_configured，部分节点未完成是 search_incomplete；完整限定搜索无候选是 source_unavailable，均不能证明全网无源。搜索候选宣传与实际文件质量分开记录。

文件选择首先核对作品身份、年份、季集及 relative_path；保持电影/剧集、动漫/真人、续作和特别篇冲突拒绝。剧集的数字集名或未知英文名只有同一文件路径中的强作品父目录才能补身份；父目录不能覆盖另作品、不同季、版本或多集冲突。强目录与数字叶子已确定集号时不再让弱命名 AI 覆盖。未知英文混入仍存在语义识别边界。

4K 名称、文件尺寸字段、真实媒体宽高、解码和设备观看是不同证据。unknown 或 valid_hd 不能自动显示已实测高清；体积、HDR、DV、REMUX 也不能独立证明 4K。

## 转存与播放

Work 为分类、带年份标题、季和集；电影 0/0，普通剧集为正季集。旧无年份身份必须有唯一映射证据。同身份共享 Future，同季跨进程锁，后台保存不因单个 HTTP 等待结束而取消。

按顺序复用 done、恢复不明保存意图、尝试精确锁源、再检查有界搜索候选。只保存选中的 fid 到确定性目录，回读名称与大小后提交 done。读取通道未就绪时保留保存状态，不盲目重存；Jellyfin 模式读取通过后激活既有 STRM，映射缺失或激活失败阻止报告成功。独立模式不要求门户预建文件，直接提供已验证 URL，也不改写任何 STRM。

STRM 稳定指向 /play，远端路径从状态库解析，不能信任客户端任意路径。MP4 经 /media 的有界块缓存与连接槽；其它媒体使用读挂载或清洗路由。Range、后缀 Range、416、HEAD 和断连释放分别回归，不采用曾引起 OOM 的 interleaved_read=0。

公开 /clean 的首跳仅接受显式 OpenList 的 /d 下载路径，拒绝任意本机地址。重定向至 CDN 时 DNS 全部地址须为公网地址，连接固定到已检查 IP，HTTPS 仍验证原主机名；不继承环境代理。配置的 OpenList 本身是管理员信任的依赖，不把此限制描述为整个项目所有网络请求的统一防护。

## 管理与副作用

health 和空壳管理页面可匿名访问；管理 API、stats、capability、preheat 统一要求 Bearer。缺 token 时返回 503，错误 token 返回 401，未授权请求不会进入写入函数。token 文件可轮换，页面刷新后需重新输入，不保存到 URL、Cookie 或浏览器持久存储。

GET capability/overview 只读配置和最近显式探针缓存。POST /api/admin/capability/probe 会核验 Cookie，并可能创建、回读、删除一个独立云探针。更新 Cookie 原子写 0600，非法 header 拒绝且不回显；保存失败不继续探测。play、preheat、新增入库、reset、自愈等副作用须按各接口判断，不能把所有 GET 当只读。

播放器接口没有用户登录/配额鉴权，服务应位于可信网络或由部署者提供访问控制。公开包也没有内置 TMDB 门户；本机 8088 门户及七个宿主作业由影院项目维护。

## 审计、维护与回滚

根目录巡检与分集文件审计使用独立持久游标。根目录可读不刷新目标集证据；鉴权/网络/枚举不完整保留旧状态；只有完整枚举和可靠身份能撤销所选集提示。作品中的其它集、有效旧分享和旧画质证据不能连带清空。

破坏性维护需要显式开关、真实 Jellyfin 保护库、有效凭据、可确认会话、独占片库锁及调度锁。收藏、观看进度、已看、置顶和近期访问保留，Episode 归属未知即停止。先恢复 pending 入口，再精确删除云对象，成功才记 cleaned。无 Jellyfin 的独立模式默认不启用自动淘汰；不把缺失保护库当作空闲。

升级前在线 backup 状态库并保留旧镜像、源码、部署参数和受影响 STRM。本轮恢复材料位于 /home/dong/agent-doc-backups/quark-vod-open-20261120/。数据库快照用于排障和精确恢复，不允许整库覆盖升级后新增的观看/转存状态。旧测试脚本已改为只读接口检查，不再强杀进程或盲目触发真实作品保存。

隔离测试、受限镜像启动、浏览器和本机媒体解码分别留证。测试不调用原定时任务模型；常规生产配置是否恢复、真实样本和最终代码哈希统一看本轮报告，不能把前一版本的完整观看结果当作新版本设备验收。
