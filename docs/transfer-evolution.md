# 转存、身份与多版本实现说明

更新：2026-11-25。实现源于 v3.0.1 / ade51a6 之后的开发，并随3.1.0发行。本文的one离线验收属于历史开发记录，不代表已用真实账号完成生产云转存或全部播放验收；正式范围见[3.1.0发行说明](releases/3.1.0.md)。

## 身份与确定性选集

作品身份为分类、完整标题（含已知年份）、季、集和可选 variant。默认版本仍兼容四元组接口，显式版本仅允许 hdr、sdr、dubbed、remux。数据库映射、播放任务锁、远端槽位、STRM 激活和清理按版本隔离；分类先于画质。

episode_rules 是 season/episode 确定性解析的唯一来源。支持 S00 特典、中文分集副标题、音画标签、字幕组、剧集 Part/Remaster；明确年份、季、集或 Part 冲突仍拒绝。八位日期不拆成集数，只能由本地目标 NFO 的 aired 精确匹配。绝对集数只在前季连续 NFO 可推导 offset、当前目录身份与目标季明确且没有本季编号时转换；不凭猜测跨季。

分享树只读枚举最多 3000 条；裁掉明确其他季与 Extras，并保留同目录字幕。枚举完整结果为 64 项、10 分钟 LRU。播放对 Top3 候选并发验源（最多 3 个 worker、6 个待执行槽），单源预算 8 秒、收集预算 9 秒，再按真实目标文件画质排序；保存仍只选一个精确目标。

## 画质与保存恢复

parse_media_tags 输出分辨率、HDR/DV、音轨、字幕与风险提示；DV 单层无回退降权，体积超过 MAX_FILE_SIZE 降权。尺寸证据优先，384×2160 不得误标 4K。标签是文件名/体积证据，不能冒充 ffprobe 技术实测。

已知归档、文档、安装包和音频扩展名不因体积大就成为视频。mkv1/mp41/mov1、bin/dat 和大体积无后缀候选需读取魔数。网络失败或短响应为未知，保留 save intent；足量响应明确非视频才是终态。终态只在精确槽位无其他引用、STRM 已还原且远端删除确认后清 intent、标记分享不可用并允许候选回退。绝不碰回收站。

已存播放在任务锁前复用；readiness 结果 300 秒缓存。父子目录 refresh 与探针采用有界退避，readiness 和 PNG 前缀判断复用同一次 64 字节探针。只有找到实际 MKV/MP4 魔数才剥前缀。根目录 fid 缓存 300 秒，404 失效；selfheal 复用父枚举中的目录 fid，后台线程关闭自己的 DB 连接。

## 账号配置

- QUARK_SCOUT_COOKIES / QUARK_SCOUT_COOKIES_FILE：可选 JSON Cookie 数组，只读枚举轮换；最多 32 个。
- QUARK_REQUEST_RATE：单个 Python 进程内共享请求令牌桶的补充率，默认每秒 3。容器与宿主任务分别计数，尚非跨进程全局限流。
- QUARK_REQUEST_BURST：突发令牌数，默认 5。
- QUARK_COOKIE_STATE_FILE：默认空，滚动 Cookie 仅更新内存。显式配置才启用 0600 原子状态文件；读取限 1MiB、普通非链接私有文件。此选项属于凭据落盘，实际启用前应单独取得操作授权。

分享 token 绑定其枚举账号。真正保存前重新以主号取得 token，并在所选精确父目录刷新 fid_token；不把 scout token 用于主号保存。429 有限指数退避。运行日志不输出 Cookie、token 或带凭据的直链。

## 字幕与多版本

字幕仅匹配已枚举的同目录或 Subs/字幕子目录、同一集、15MiB 内 ass/ssa/srt。同一 language/扩展名侧车仅选一份，优先明确简中，其次其他中文或双语；明确 English-only 且无中文标记的文件排除。视频先完成，字幕独立排队，不阻塞播放。每个字幕先在 DATA_DIR/subtitle_intents 写入无 Cookie/token 的 0600 意图，单 worker 每轮最多 4 项、至少 5 分钟退避，由播放成功或启用 maintenance 的 selfheal 周期触发。

未知保存回复只核对精确文件或既有 task，不盲目重发。完成后删除意图。旧转存不会补扫字幕；未知提交且始终没有文件/task 证据时保留意图，需人工核对。只有集成媒体库且已有安全 STRM 映射时才落本地字幕；映射须在 CINEMA2 内、非符号链接、通过同作品激活路径核对且文件名 variant 与任务一致，不满足时保留意图等待恢复。文件名跟随 STRM 的 zh-Hans/zh-Hant 侧车；原子替换保留 owner/group/mode/xattr 并检查并发修改。

多版本在既有作品目录旁按 Jellyfin 标准文件名建立 pending：4K HDR 原声、SDR 兼容、1080P 国配、4K REMUX。创建、扫描、激活和清理只改变目标版本；激活映射还核对真实路径与文件名 variant，空合法路径直接拒绝，换源 snapshot、发布和旧源清理同样不以空集合视为成功。SDR 电影名不再被误判为 SxxExx 剧集。NFO aka、season.nfo 副标题可补检索别名，不改变存储身份。

## 下一集预取

单个有界低优先级队列只在 Jellyfin 确认当前集播放进度至少 45%、未暂停时预取下一集。会话须匹配安全 STRM 的宿主路径或 /cinema2 路径、精确季集和 variant；有 MediaSourceId 时只接受该唯一选中源，多版本未给出选中源则视为未知。SeriesName 不含年份不再导致合法观看状态漏判，脏映射不能触发其他作品或版本的预取。真实 Jellyfin 会话触发尚待集成验证。

## 换源发布与恢复

POST /api/admin/replace 经维护租约与精确 Work.key（含 variant）锁，保留旧 done 行与旧审计，创建独立候选 transfer。预检及实际保存均使用选中的 selected_fid。DATA_DIR/replacement_intents 下的 0600 原子 journal 记录 preparing/activating/committed、候选与旧行ID、原 STRM 内容及原因，不存 Cookie/token。未知保存结果只能恢复原候选，不因超时重新提交。

候选保存就绪后先以 found 暂存；激活全部安全映射并读回精确新 remote_path，才用单个 SQLite 事务写新 done、source_audits、旧分享黑名单及 replacement_commits(candidate_id, old_transfer_id, committed_at)。只有独立 receipt 能证明本次发布成功，旧 URL 或同分享的 selected_file 均不能代替。激活或发布前失败恢复本次入口快照；提交后崩溃按 receipt 恢复，不重新保存。

发布成功后才退休旧 leaf。共享引用、入口变化、删除异常或 remove 返回 False 均保留旧文件并返回 cleanup_pending=true；新片源仍 ready。清理重试再次核对候选 done 和全部旧 snapshot 路径当前精确指向候选。后续换其他片源可保留 .cleanup-<id>.json 证据继续，不递归删旧槽位。journal 不能作为可随意删除的临时文件；人工恢复先停同作品 writer，核对 receipt、candidate save intent、精确云文件和入口。不要手工把未决任务重置后重提。

tests/test_source_replacement.py 使用真实临时 SQLite 与 STRM、模拟远端 I/O，覆盖提交前后崩溃、同分享不同文件、独立事务回滚、清理失败和入口被改动后的保留。

## 运行通知

主号认证失败、14018 风控、保存空间不足、配额超水位但保护策略无法释放目标空间发 P0；OpenList 连接/限时探针失败发 P1。operational_events 只允许固定事件文案与异常类别，独立单 worker、最多 8 个待处理任务，不把 Cookie、URL、上游 message 或标题放入运维通知。notifier 未配置时安静跳过，已配置时按事件/异常类别去重。预演清理不发容量告警；测试拦截所有发送接口，没有发送真实通知。

## 验收与边界

宿主自动洗版只对已完成且旧文件证据明确的HD执行升级：候选自身明确4K、同variant、综合分严格提升；默认/SDR拒绝单层DV无回退。评分沿用本模块V2，并对明确HDR10+加8分，国粤/特效按V2加分而非强制包含；该自动化适配属于发行包以外的独立宿主工程，不是本公共内核默认启用的服务。旧文件由同分享done transfer的remote_path与file_size提供证据，不用audit包标题代替精确文件。

tests/test_prefetch_and_subtitle_safety.py 覆盖预取精确路径/选中源/版本、字幕去重与安全映射、历史默认映射、越界链接和空激活路径。宿主工程另有洗版 variant、季与 selected_fid 的适配回归，其源码和证据不随公共内核发行。

专项 tests/test_transfer_evolution.py 覆盖视频候选、精确身份、S00、日期/绝对集数、画质、版本选择、分享剪枝/LRU、魔数三态、确认删除、fid 复用、单探针、账号轮换/主号重验、字幕匹配/未知提交恢复、多版本扫描激活清理和 Top3 并发排序。

外部账号、真实分享可用性、OpenList/Jellyfin 真环境与真实媒体兼容性仍需部署后的受控集成验证。本次不改变回收站策略、不直接写 Jellyfin DB、不自动添加账号或启用凭据持久化。
