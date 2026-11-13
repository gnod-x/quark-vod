# Quark-VOD Engine (Jellyfin2 伴生点播引擎 · 白盒工程规范)

> 本仓库 (`/home/dong/projects/quark-vod`) 是从 `hometheater` 中独立抽离并白盒化封装的 **Jellyfin2 网盘点播微服务与可视化管理台**（方案 C：FastAPI + Swagger `/docs` + `/admin` 控制台 + Docker 容器化编排）。

---

## 一、 人类开发者白盒导读手册 (Architecture Guide)

### 1. 全局数据流向图

```text
[Jellyfin2 客户端点击播放]
        │
        ▼
[custom-ffmpeg/ffprobe & ffmpeg] ──(命中 :8099/pending)──> 发起 curl -r 0-0 单字节探测
        │
        ▼
[quark-vod 容器 (FastAPI :8099)] (app/main.py)
   ├─ 1. 已转存 (DB=done) ──> 夸克官方 API 校验存在性 (防线①)
   │                             ├─ 存在 ──> 生成 QuarkTV 直链 (/夸克/点播/...) ──> 302 重定向
   │                             └─ 不存在 ─> 作废记录，走下方未转存分支
   │
   ├─ 2. 未转存 (Pending) ──> search.py + panshare_matrix.py (6 节点并发检索 + MD5 双层缓存)
   │                             │
   │                             ▼ (五维身份隔离打分 >= 30.0 + 优先调取 source_audits 4K 锁存源)
   │                          transfer.py (Quark Web Cookie 验票 -> 转存 -> 轮询 move -> 魔数扶正)
   │                             │
   │                             ▼
   │                          openlist_client.py (逐级刷新 OpenList /夸克 目录树缓存 + Ping 直链 200/206)
   │                             │
   │                             ▼
   │                          strm_manager.py (原子回写 .strm 为直链 + 同步 DB=done + 触发 Jellyfin2 刷新流信息)
   │                             │
   │                             ▼
   │                          返回 302 Location (直链或 :8099/clean 透明清洗流)
   │
   └─ 3. 伪装流清洗 (:8099/clean) ──> cleaner.py (剥离前导 8 字节 PNG 图种混淆头，透明平移 HTTP Range)
```

### 2. 核心代码模块职责速查 (`app/quark_vod/`)

| 文件名 | 代码行数 | 核心职责与白盒修改指南 |
| :--- | :--- | :--- |
| **`app/main.py`** | ~310 行 | **FastAPI 主入口**：定义 `/pending`、`/clean`、`/preheat`、`/health`、`/capability` 以及 `/admin` 控制台与 `/api/admin/*` 接口；`/preheat` 经 `_resolve_strm_row()` 做剧集形态纠偏（见红线 7） |
| **`app/quark_vod/server.py`** | ~800 行 | **播放/预热 HTTP 服务**：`_handle_pending` 现场转存、`trigger_preheat_task` 后台预热（含剧集形态纠偏与**多候选源换源重试**，见红线 8） |
| **`app/static/admin.html`** | ~290 行 | **白盒可视化管理台**：浏览器访问 `http://192.168.101.240:8099/admin` 即可调试搜索打分、查看转存队列、热更新 Cookie、触发自愈 |
| **`config.py`** | ~255 行 | **全局配置中心**：管理路径、OpenList 挂载点转换 (`play_path`)、Cookie 多级读取 (`quark_cookie.txt` > `.env` > `data.db?immutable=1`) |
| **`search.py`** | ~730 行 | **搜索引擎与五维打分**：`sanitize_title` 标题清洗、`score_search_result` 五维隔离（类型/年份±1/季数/特别篇/短剧熔断） |
| **`panshare_matrix.py`** | ~350 行 | **多源并发盘搜矩阵**：并发请求快映/木偶/多多/蜡笔/八零/虎斑 6 大站点，带 1800s 内存+磁盘双层缓存 |
| **`transfer.py`** | ~850 行 | **夸克云端转存状态机**：`resolve_share` 递归验票、`save_share` 转存、`match_episode_video` 集数提取与全季连带激活、伪装后缀扶正 |
| **`strm_manager.py`** | ~530 行 | **STRM 文件与元数据管家**：`activate()` 双向定位回写 live strm、`_sync_transfer_done()` 强同步 DB、异步通知 Jellyfin2 刷新流信息 |
| **`cleaner.py`** | ~210 行 | **图种混淆流清洗器**：嗅探前 64 字节识别 8 字节 PNG 假头，提供 Range 偏移计算与上游失效即时自愈触发 |
| **`selfheal.py`** | ~360 行 | **后台自愈守护线程**：每天单次批量拉取 `/点播` 目录树比对 live strm，文件被删自动回退为 pending |
| **`cleanup.py`** | ~460 行 | **网盘容量配额治理**：递归统计 `/点播` 真实占用（2TB 上限 / 1TB 水位），按 `(cat, title)` 整组淘汰过期资源 |
| **`auditor.py`** | ~415 行 | **前置 4K 预检锁源器**：只读解析分享包树结构，提前锁定真 4K 资源存入 `source_audits` 表 |
| **`db.py`** | ~400 行 | **SQLite 状态库封装**：管理 `quark_plays.db` 的 `transfers`、`plays`、`strm_map`、`dead_sources`、`source_audits`、`name_parse_cache` 六张表 |
| **`title_judge.py`** | ~200 行 | **搜索候选语义复核**：只在 `match_best()` 集中整批调用一次，仅复核 15~90 分边界档（见红线 11） |
| **`name_parser.py`** | ~440 行 | **AI 文件名语义解析兜底**：规则四分区（strong/conflict/weak/none），仅 weak/none 送模型批量复核；带缓存、硬校验、熔断与影子模式（见红线 10） |

---

## 二、 常用运维与开发命令 (`Makefile`)

在 `/home/dong/projects/quark-vod` 目录下：

```bash
make status     # 查看容器状态与 8099 /capability 读写就绪度
make logs       # 实时查看 quark-vod 容器日志
make restart    # 修改 app/ 下代码后秒级重启生效（已挂载 ./app:/app）
make build      # 修改 Dockerfile 或 requirements.txt 后重新构建镜像并拉起
make test       # 运行自动化验收测试套件
```

### Web 调试入口
- **白盒可视化控制台**：`http://192.168.101.240:8099/admin`
- **Swagger 交互式 API 文档**：`http://192.168.101.240:8099/docs`

---

## 三、 核心防劣化红线（继承自 `hometheater/CLAUDE.md`）

1. **能耗红线**：严禁唤醒 GTX 1050 独显（必须保持 `D3cold`）；容器必须声明 `deploy.resources.limits.memory: 256M`。
2. **读写分离红线**：写入与清理走 `/夸克网盘` (Web Cookie)，播放直链统一經 `config.play_path()` 走 `/夸克` (QuarkTV)。
3. **免锁读取红线**：读取 OpenList `data.db` 与 `jellyfin2/config/data/jellyfin.db` 必须带 `?mode=ro&immutable=1` 并包裹在 `closing()` 中立即释放句柄。
4. **元数据保护与限流红线**：`trigger_jellyfin_refresh_item` 必须保持 `ReplaceAllMetadata=false`，防止覆盖 Jellyfin2 中已同步的 AI 精修简介；整季连带激活（`background_batch=True`）自动走 1.5s 间隔串行队列，严禁并发风暴抢占首播带宽。
5. **双向同步契约**：`make restart` 自动触发 `make sync` 将 `app/quark_vod/*.py` 同步至 `/home/dong/projects/hometheater/scripts/quark_vod/`，并由 `make test` 第 8 项強制校验零漂移。
6. **伪装后缀验票与预检放行红线（2026-11-04 根治）**：第三方分享包为规避网盘和谐审查，常将正片改为 `.mp3/.pdf/.txt/.apk/.mkv1/.mp41` 或无后缀。`transfer.py` 转存前验票与 `auditor.py` 4K 预检锁源必须统一通过 `is_candidate_video(name, size)` 判定（伪装后缀且体积 `> 200MB` 视为候选视频放行），严禁仅凭 `VIDEO_EXTS` 白名单前置拦截导致转存后的 `sniff_and_fix_file` 魔数扶正逻辑形同虚设。
7. **剧集形态纠偏与空心分享熔断红线（2026-11-08 根治，末路兼职 S0E0 事故）**：三个连锁缺陷曾导致《末路兼职》整季永久卡在 `/preheat` 失败；三者必须同时保持，任一回退即复现。
   - **形态纠偏**：控制台「预热点播」常只填片名不填季集（`s=0&e=0`）。`server.py::_resolve_strm_row()` 必须先反查 `strm_map`：若 `season > 0` 条目存在则判为剧集，季集为 0 时自动取最小集号。**严禁**把 `S0E0` 直接传给 `transfer.py`，否则剧集会被电影验票门槛（`if not season or not episode` 分支的「多集剧集特征」判定）误杀。
   - **空心分享熔断**：分享链接未过期、标题仍在，但根目录已被作者清空（`data._total == 0`）时必须判死并**继续遍历下一个候选源**。`resolve_share()` 返回空列表不是异常，旧实现仅凭「没抛错」即判 alive，导致空心源长期霸占 `source_audits` 锁定位。
   - **锁定位作废闭环**：锁源失效时 `auditor.py` 必须调用 `db.purge_audit_locked_share()` 作废整季锁定位；`db.get_locked_4k_share()` 必须过滤 `dead/not_found` 状态。严禁出现「分享已拉黑但锁定位仍指向它」——播放链路始终优先调取 locked 源，残留即被永远反选。
   - **验证口径**：修复后必须以**原失败参数**（`cat=&t=<剧名>&s=0&e=0`）重放一次预热，日志应出现 `[预热校正] … 纠正为 SxxExx` 与 `[预热成功]`，且 `transfers` 表对应集数 `status=done`、`.strm` 指向 OpenList 直链。
8. **电影验票「强证据优先」红线（2026-11-08 根治，7 部电影被误杀）**：`extract_episode_num(name, season=1)` 的宽松模式会把**规格残片**读成集数——`HDR10`→10、`4K_HDR_60`→60、`DDP5 1`→5、`H.264` 残留→264、分卷 `d3 1`/`d3 2`→E1/E2。旧验票规则只要 ≥3 个文件命中任一弱信号就整包拒绝，导致含 50GB 完整正片的单部电影源被判为「多集剧集特征」（宗师叶问2 / 惊蛰无声 / 给阿嬷的情书 / 罗小黑战记 / 后室 / 死侍与金刚狼 / 痴迷）。
   - **判定顺序铁律**：先看强证据——存在「非集数标记 且 ≥350MB」的单片即放行；**只有包内全部视频都带集数标记时**才按剧集包拒绝（真正的短剧/合集源仍会被拦，已用 38 集短剧与 16 集韩剧源验证）。
   - **三处必须同口径**：`transfer.py` 电影验票 B 门槛、`transfer.py::match_episode_video` 选片（≥500MB 非集数正片优先，杜绝 52GB 正片被分卷信号拖累）、`auditor.py::audit_single_item` 锁源校验。任一处回退都会让对应环节重新误杀。
   - **预热换源红线**：`trigger_preheat_task` 严禁只尝试 `resolve_for_play` 选出的单一源。实测同一影片常有 3~7 个可访问候选，首源 41031/41012 即放弃会造成「明明有源却预热失败」（罗小黑战记 7 个候选中 5 个可用）。必须遍历全部合格候选（locked 源置顶），首个转存成功即停。
   - **清洗规则维护**：`extract_episode_num` 第一步须按**整词优先**剥离规格词（`hdr1[04][pP]?` / `[dD][dD][pP]?\s?\d(?:\.\d|\s\d)?` / `\bd\d+(?=\s+\d)` 等），第 B2 步在宽松匹配前再对 `H.264`、裸 `1080`、`NNfps` 做一次残留兜底。新增发布组命名规范时必须回归测试：**剧集样本 `01.mp4` / `第64集.mp4` / `S01E04.mp4` / `[04].mp4` 提取结果不得改变**。
9. **连载剧集未来未播出集数严禁生成 pending 占位（2026-11-12 根治）**：
   - **痛点根因**：TMDB 剧集分季详情会列出整季规划总集数（如 12、24 或 40 集）。旧逻辑为所有规划集数无差别生成 `.strm` 占位，导致电视端 Jellyfin2 充斥尚未播出的未来假集数（用户点击必报 503），未开播新剧甚至创建幽灵空目录引发海报墙裂图。
   - **判定口径**：连载更新中（`status != 'Ended'` 且 `status != 'Canceled'`）剧集必须通过 `is_episode_aired(ep_data, s_num, e, detail)` 校验 `air_date <= today` 或 `last_episode_to_air`；整季第一集都未开播者严禁创建目录，仅允许已播出集数生成 `.strm`。
   - **存量治理**：`daily_update_and_prune.py` 每日 05:00 自动调用 `prune_unaired_pending_strms()` 清理未来未播出占位，真实转存 live 直链（含 5244 或 /d/）无论播出日期均受保护严禁删除。
10. **AI 文件名语义解析分区红线（2026-11-13 试点，`name_parser.py`）**：
   - **Why**：红线 7/8 的共同根因是 `extract_episode_num` 的规则 D 把规格残片读成集数（`HDR10`→10、`4K_HDR_60`→60、`DDP5 1`→5、`d3 1`→E1）。发布组命名无界、正则有限，语义判断改由模型承担。
   - **分区铁律**：`classify_by_rules()` 四分区——`strong`（规则 A/B季号相符/C 命中）与 `conflict`（季号冲突）**永不送 AI、结果不可被推翻**；只有 `weak`（规则 D 命中）与 `none`（全未命中）才送模型复核。严禁放宽此边界，否则电影验票依赖的 `is None` 语义全线失守。
   - **批量铁律**：严禁逐文件调用模型。`match_episode_video` / `match_all_episodes` / 电影验票 / `auditor` 必须先 `prefetch_episodes()` 整组一次调用，再用 `_lookup_ep()` 查结果（实测 12 项 2.26s，逐文件会放大成 N×RT）。
   - **归位铁律**：模型输出必须按 `file_name` 精确归位，下标仅作校验——实测模型偶发「从 1 编号/漏首项」，纯按 `i` 采信会把 A 的集数错挂到 B（比集数识别错误更危险的错位污染）。任一条目缺失则整批作废、回落规则。
   - **硬校验**：值域 `1~2500`、置信度 `< AI_PARSE_MIN_CONFIDENCE(0.7)` 一律判 `None`；连续失败 3 次熔断 300s。任何异常都必须退化为改造前的纯规则行为。
   - **年份误剥降级（2026-11-13 补充）**：`_NOISE_RE` 无边界保护的 `19\d\d|20\d\d` 会把「集数恰为年份形态」的名字清洗坏（`S01E2010` → `S01E10` → None）。`classify_by_rules()` 用**未清洗的原始 stem** 经 `_S_YEARLIKE_RE` 预判，downgrade 为 `weak` 交 AI 复核；**严禁**因此去改 `extract_episode_num` 的清洗正则（那是电影年份剥离的关键，改错即触碰红线 8）。
   - **密钥红线**：容器以 `HOME=/root` 运行，`expanduser("~")` 不可靠；token 固定按 `/home/dong/.config/cliproxy/token` 读取，docker-compose 仅**只读挂载该单文件**，严禁挂载整个 `hometheater/.env`。
   - **灰度开关**：`AI_PARSE_ENABLED` / `AI_PARSE_SHADOW`（影子模式只写 `origin=shadow` 缓存与 `[影子分歧]` 日志，绝不改变行为）。
   - **验证口径**：`make test` 第 9 项跑 `tests/test_name_parser.py`（红线 7/8 黄金样本 + 归位/值域/熔断回归），第 10 项校验全部模块双目录零漂移。

11. **搜索候选 AI 语义复核红线（2026-11-13，`title_judge.py`）**：
   - **Why**：`score_search_result` 的 `_is_legal_title_suffix` 白名单永远追不上发布形态。实测 `苍兰诀(剧版)`（同作）、`凡人修仙传之魔道争锋`（不同篇章）、`庆余年之少年篇`（独立作品）三者规则一律 85 分，无法区分。
   - **调用位置铁律**：**只允许在 `match_best()` 集中调用一次**。严禁挂到 `score_search_result()`——那是逐条热路径，一次搜索会被放大成 N 次模型请求。
   - **边界铁律**：只复核 15~90 分区间；100 分（清洗后标题完全相等）与低于 15 分（规则已判不可用）**永不进模型**。
   - **不对称调整**：高分档（≥85，白名单命中）AI 判同作时**保持原分不拔高**，判不同作才下调 40；低分档判同作可上浮至多 40，判不同作下调至多 40，结果钳制在 0~100。严禁让单次判断把 20 分拔到 100 盖过真正同名资源。
   - **归位铁律**：与 `name_parser` 同口径——按 `file_name` 精确归位，条目不全整批作废回落规则分数。
12. **4K 判定红线（2026-11-13 根治）**：`is_4k_candidate` 只认 `2160p / 4K / UHD / 3840×2160`；`HDR / DV / REMUX / 原盘` **不是分辨率**，不得参与 4K 判定（1080p HDR、720p DV 均真实存在）。显式标注 1080p/720p 时**无论体积多大都判非 4K**。`server.py` 候选排序与 `auditor.py` 锁源必须统一走该函数，严禁再内联 `re.search(r"4K|臻彩|2160|UHD|HDR")`。
13. **配额水位红线（2026-11-13）**：配额按**账号整盘使用率**治理（≥85% 触发，降至 70%），不再硬编码 2TB/1TB；点播低于 200GB 时**永不淘汰**——空间被 `/视频`、`/游戏` 等非本系统目录占用时，删点播既救不了空间又会误删资源。
