# Quark-VOD 项目约定

负责 8099 点播 API、搜索、单文件保存、播放和自愈。2026-11-19 按源码、生产播放和审计回归重写。当前部署及未验收项见[影院续验](/home/dong/.agent-context/verification/cinema-continuation-2026-11-19.md)。

## 按需读取

- [架构与行为](docs/agent-architecture.md)：身份、资源边界、播放和审计。
- [验证流程](../hometheater/docs/agent-verification.md)：自测、部署、副作用和恢复。
- [影院约定](../hometheater/CLAUDE.md)：门户、Jellyfin、OpenList 和宿主作业。

app/quark_vod 是共享业务权威源，make sync 更新 hometheater/scripts/quark_vod；七个普通作业以 hometheater/scripts 为准，make sync-jobs 更新 app/jobs。生产入口 app/main.py。不得反向覆盖。

## 维护约束

1. 保留 256 MiB 限额、只读 app 挂载和持久数据。凭证、候选 token、分享及签名媒体 URL 不进入报告。
2. 身份先于画质。搜索命中、目标文件存在、流分辨率、解码和设备观看分别验收；4K 名称始终只是提示。partial 空候选不能报全网无资源。
3. 超时、404、鉴权、限流和不完整枚举保留原证据；明确失效才废弃分享。完整枚举缺少所选集仅撤销该集提示，不连带撤销其他集。根目录可读不刷新分集证据。
4. 保存和删除走 /夸克网盘/点播，读取走 /夸克/点播。同身份只保存一个目标文件；提交结果不明时先恢复原意图与 task_id，禁止盲目重提。
5. done 是保存完成；可读并激活后才返回播放。优先复用已有保存，STRM 保持 /play。无年份兼容和 NFO 原名读取须满足架构中的身份与范围约束。
6. 在线 SQLite 以普通 mode=ro 读取 WAL，不用 immutable；不直接写 Jellyfin 数据库。维护检查会话、用户状态、置顶和近期访问，读取失败停止；先恢复入口，再删精确云对象，成功后提交 cleaned。
7. 审计通过独立持久游标轮转；不能通过刷新分集时间实现根目录公平。源码同步不等于运行进程已加载。实质改动完成自测和对应现场验证后重写文档；回收站不处理，定向清理本轮无用产物。
