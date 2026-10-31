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
| **`app/main.py`** | ~310 行 | **FastAPI 主入口**：定义 `/pending`、`/clean`、`/preheat`、`/health`、`/capability` 以及 `/admin` 控制台与 `/api/admin/*` 接口 |
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
| **`db.py`** | ~390 行 | **SQLite 状态库封装**：管理 `quark_plays.db` 的 `transfers`、`plays`、`strm_map`、`dead_sources`、`source_audits` 五张核心表 |

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
3. **免锁读取红线**：读取 OpenList `data.db` 必须带 `?mode=ro&immutable=1` 并包裹在 `closing()` 中立即释放句柄。
4. **元数据保护红线**：`trigger_jellyfin_refresh_item` 必须保持 `ReplaceAllMetadata=false`，防止覆盖 Jellyfin2 中已同步的 AI 精修简介。
5. **双向同步契约**：对 `app/quark_vod/` 核心模块做修改后，如需同步回原仓库脚本，确保 `make test` 与 `hometheater/CLAUDE.md` 验证清单全部通过。
