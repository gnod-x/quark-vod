# 自动化与 AI 设置

Quark-VOD 将影院任务随 Python 包统一发布。API 负责设置和排队；独立
`quark-vod jobs worker` 负责定时调度及启动任务子进程。启动 API 不会隐式启动
worker，也不会自动把旧宿主 cron 迁入新调度器。

## 默认行为

- 调度总开关、九个任务开关、AI 均默认关闭。
- 默认保留示例时间表，时区为 `Asia/Shanghai`。
- 未配置主模型和 HTTP(S) API 地址时不能启用 AI。密钥继续来自现有显式环境/
  文件配置；网页不接受、保存或显示 API key。
- CLI 和手动 API 默认演练。演练只记录 `dry_run`，不启动业务子进程，
  不证明搜源、转存、清理、模型或播放器可用。
- 启用某项任务后，可在总调度关闭时手动运行。总开关只控制按时间表入队。

任务仍遵守各自的 Jellyfin、维护、会话、身份与云写保护开关。开启任务不会绕过
这些保护，清理也不会处理夸克回收站。

## 配置

管理台“任务与模型”页读写共享 `VOD_DATA_DIR/automation-settings.json`。
文件为 0600，使用跨进程锁、临时文件 fsync 和原子替换。设置文件上限 32 KiB，运行状态文件上限 64 KiB；使用独立有界存储，不继承通知文件的 16 KiB 上限。超限在替换前拒绝，保留旧文件。坏文件、符号链接、
权限异常停止读取/写入，不退回启用的环境默认值。纯读取未配置设置不会创建目录
或锁文件。

设置结构如下；这里只展开一项任务，提交时 `tasks` 必须包含下表全部固定 ID：

```json
{
  "revision": 0,
  "enabled": false,
  "timezone": "Asia/Shanghai",
  "ai": {
    "enabled": false,
    "base_url": "",
    "primary_model": "",
    "fallback_models": [],
    "reasoning": {
      "probe": "none",
      "subtitle": "low",
      "people": "low",
      "plot": "high",
      "filename": "low"
    }
  },
  "tasks": {
    "ai_night_worker": {
      "enabled": false,
      "schedules": ["0 23 * * *"],
      "timeout_seconds": 16200
    }
  }
}
```

GET 还返回只读 `version: 1` 与 `source: defaults|server`；PUT 不提交这两个字段。
每次保存必须带当前 `revision`；成功递增，过期版本返回 409，避免多页面覆盖。
字段类型严格校验，拒绝未知任务、额外字段、重复 JSON 键和非有限数。

主备模型只接受模型 ID，不接受 shell、脚本或凭据。备选最多五个，执行顺序为
主模型后依次备用、去重。思考强度可选 `none/low/medium/high`，是否支持仍取决于
所选 API/provider。初始 `base_url` 可来自显式 `OPENAI_BASE_URL`；保存后以页面值
为准，空值不会偷偷恢复旧地址。URL 仅支持 HTTP(S)，禁止用户信息、查询和片段。

时间表使用五段 cron，支持数字、`*`、范围、逗号和步长；不支持 shell、
`@reboot` 或秒字段。日与星期均受限时使用标准 cron 的 OR 语义。
`timeout_seconds` 为 10–21600 的整数；时区必须是有效 IANA 时区。
子进程收到同一 `TZ` 和超时预算。AI 设置在启动时冻结；运行中的任务不受后续网页
修改影响，记录中的 `settings_revision` 对应实际启动快照。

| 固定任务 ID | 默认时间表 | 默认预算（秒） |
| --- | --- | --- |
| ai_night_worker | 0 23 * * * | 16200 |
| rss_subscriber | 0 3,15 * * * | 1800 |
| auto_sync | 0 4 * * * | 3600 |
| daily_update_and_prune | 0 5 * * * | 10800 |
| airing_chase | 30 0,12,18 * * *；0 20,22 * * * | 1800 |
| digest | 0 8 * * * | 300 |
| quality_upgrade | 30 2 * * 0 | 3600 |
| douban_watchlist_sync | 30 7,19 * * * | 1800 |
| wishlist_bot | * * * * * | 55 |

## 管理 API

全部接口要求管理 Bearer token，未配置管理认证返回 503、认证失败返回 401。
页面令牌仅留在内存。

| 方法与路径 | 行为 |
| --- | --- |
| GET /api/admin/automation | 返回设置；不启动任务 |
| PUT /api/admin/automation | 完整保存设置，包含 revision |
| GET /api/admin/automation/jobs | 返回固定任务目录 |
| GET /api/admin/automation/runs | 返回最近运行、worker 心跳和 max_active |
| POST /api/admin/automation/tasks/{id}/run | 排队手动任务 |

POST JSON 为 `{"revision": 1, "dry_run": true}`；省略 `dry_run` 仍为演练。
显式 `false` 才排队真实执行。成功为 202，返回 `run_id/status/dry_run`。
禁用任务、相同任务已排队或运行、队列满、版本冲突返回 409；未知任务返回 404。
此接口只入队，需要独立 worker 消费，不在 HTTP 请求里执行维护。

## CLI 与 worker

API 和 worker 必须使用相同安装版本、显式环境文件及共享数据目录。
例如：

```sh
quark-vod jobs --env /path/to/private.env list
quark-vod jobs --env /path/to/private.env status
quark-vod jobs --env /path/to/private.env run digest
quark-vod jobs --env /path/to/private.env run digest --execute
quark-vod jobs --env /path/to/private.env tick
quark-vod jobs --env /path/to/private.env worker
quark-vod jobs --env /path/to/private.env worker --execute
quark-vod jobs --env /path/to/private.env worker --once
```

`run` 入队并尝试执行所选任务；两执行槽已占用时保留排队，由 worker 后续消费。
`tick` 只检查当前分钟并入队，不执行任务。`worker --once` 检查当前分钟，然后
最多消费一项；适合外部调度测试。常驻 worker 使用一个调度循环与两个执行线程，
每项业务在独立子进程运行，因此长夜任务不会阻止其他到期任务入队。
同数据目录只有一个常驻 worker，所有 CLI/worker 领取任务都服从最多两个活动任务
的预算；相同任务同一时刻只保留一项排队或运行。

`--execute` 决定 CLI 手动任务或该次调度生成的任务是否真实执行。
队列已有任务保留各自明确的 `dry_run` 值；演练模式 worker 也会消费之前明确排队
的真实任务。因此需要绝对无业务 I/O 的验证时，应使用独立数据目录。

调度只处理当前分钟，不补跑离线期间所有漏触发时间；同任务同分钟持久化去重。
任务因忙或队列满未入队时，可在同一分钟后续检查重试，过时不自动补跑。

## 状态、恢复与限制

运行记录保存在私密原子文件 `automation-runs.json`。最多保留 32 条运行历史，
最多 16 项排队；只淘汰已结束记录，不丢弃排队或活动任务。状态包括：

| 状态 | 含义 |
| --- | --- |
| queued / running | 已排队 / 子进程开始执行 |
| dry_run | 演练结束，未执行业务 |
| complete | 任务进程返回 0；不等于真实播放验收 |
| partial | 返回 1，任务未完整完成 |
| busy | 返回 75，保护/锁冲突等暂缓 |
| failed | 其他非零退出、启动异常或遗留子孙进程 |
| timeout | 超时，已回收所属进程组 |
| interrupted | worker 停止、执行前任务关闭或失主已安全恢复 |

记录包含排队/起止时间、实际设置版本、退出码、固定错误码和输出字节数。
业务原始 stdout/stderr 可能含上游 URL 或凭据，因此计数后丢弃，不保存到管理历史；
任务自己的结构化报告/检查点保持各自职责。worker 的 `updated_at` 是最近一次心跳，
不是永久存活证明；需结合时间新鲜度判断。

子进程先通过固定启动门：父进程必须持久化 PID 与 Linux 进程启动身份后，
它才可以执行白名单任务。超时或退出时回收整个进程组，包括忽略 SIGTERM 的孙进程；
父进程返回 0 却遗留存活子孙会记为失败。SIGKILL 后还会检查非 Zombie 进程已退出；无法确认回收时保留 running/recovery_required，阻止新写入。正常 SIGTERM/SIGINT 关闭 worker 会终止
活动子进程并记录 interrupted。

worker 崩溃后，不能把“父进程消失”当作业务写入已停止。新 worker 启动会检查
失主任务；确认原 owner 不存在且所属进程组无存活写进程时，才标记 interrupted，
由任务自己的检查点/save intent 在后续轮次恢复，绝不直接重复云写。
如仍有写进程，保留 running/recovery_required，阻止同任务重提。

```sh
quark-vod jobs --env /path/to/private.env recover
quark-vod jobs --env /path/to/private.env recover --terminate
```

第一条只检查恢复；第二条显式终止已核实身份、属于该任务的旧进程组，再恢复。
运行记录还包含内核 boot_id 和 PID namespace。相同 boot 下 namespace 不同（例如宿主 CLI 与 Docker worker）时，恢复会拒绝，不能把当前容器看不到 PID 当作旧写进程已停止；须回到原 namespace 检查。不同 boot 的旧任务可确认已跨重启，记为 interrupted/previous_boot。本调度器只支持同机共享数据，不支持跨宿主共享网络文件系统。

PID 被复用、进程归属无法确认或状态文件损坏时停止恢复，不猜测杀进程。
这些命令不能回滚已经完成的网盘写入或用户活动。

旧 cron 与新 worker 不应同时调度同一任务。切换必须先完成隔离验证并停用旧入口，
确认共享锁、任务配置、数据路径与资源预算后再启用新 worker。文件和代码整合本身
不表示生产已切换，也不表示通知接收、模型、云保存或用户设备播放已经验收。
