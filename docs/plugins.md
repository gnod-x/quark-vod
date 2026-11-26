# 搜索插件与存储边界

搜索配置只选择已安装的可信 provider 及 HTTP 端点。JSON 不包含可执行代码，不安装插件。
所有结果只是候选，作品、年份、类型、季集、相对目录和画质仍由内核核验。
无配置、网络不完整、当前节点无结果都不能描述为全网无源。

## 配置与快照热重载

显式设置 VOD_SEARCH_CONFIG，UTF-8 JSON 最大 256 KiB，顶层只能有整数 version=1
和 nodes 数组。完整的默认禁用配置见 [search.example.json](../examples/search.example.json)。

- nodes 最多 32 项，id 唯一，符合 [A-Za-z][A-Za-z0-9_.-]{0,63}。
  旧 key 可兼容，和 id 同时提供时必须一致。name 最多 100 字、不含控制符，默认 id。
- provider 默认 html；内置为 html/json/tg_web/pansou。enabled 必须是布尔值，
  默认 true；禁用节点可以暂存未安装的 provider。
- base 必须是 HTTP(S) URL，无用户名、密码、query、fragment、空白或控制符，
  最长 2048 字；私网目标由可信管理员审核。tg_web 默认 https://t.me。
- timeout 是 0.1～7 秒有限数字，默认 4；拒绝布尔值。
- path 必须是最长 1024 字的站内绝对路径，不接受 //、空白、query 或 fragment；
  默认 /search，pansou 默认 /api/search。query_param 默认 q，符合
  [A-Za-z_][A-Za-z0-9_]{0,63}。
- channels 最多 64 个 Telegram 频道名，不含 @，符合 [A-Za-z][A-Za-z0-9_]{3,63}。
  空列表使用内置 15 项候选。候选名不保证存活或作品覆盖，运维者需实测替换。
- categories 最多 32 项、每项 80 字，表示分类亲和。调用 search_report(category=cat)
  时保留匹配节点和未设分类的通用节点；未传分类时使用全部启用节点。
- 未知字段、坏值、重复 ID 拒绝整份配置。关闭全部源使用合法空 nodes，不删除文件。

registry 按设备号、inode、mtime_ns、size 检测修改，完整解析后切换不可变快照；
同次多变体请求保留同一快照。文件坏或不可读保留最后有效快照、报告 configuration_error
及 partial；首次无配置返回 configured=false、partial=true。fingerprint 包含节点语义、
顺序、channels/categories，不受 JSON 格式化影响。建议临时文件+os.replace，容器
挂载整个配置目录，以便更换 inode 仍可见。

## 四个内置 provider

### html

起始请求 /index.php/vod/search.html?wd=编码词。最多 3 页，响应最多 2 MiB；
下一页与详情必须同源，支持 /index.php/vod/detail/id/N.html、/voddetail/N.html、
/vod/N.html、/detail/N.html、/play/N-N-N.html。默认最多 24 个详情（上限 30），
每节点 2 路详情并发；每页详情可以产生多个分享。

HTTP 200 页面若含验证码、搜索太频繁、安全验证、Just a moment 或
cf-browser-verification 且无有效详情/分享，记为错误并参与熔断，不作成功空结果。
搜索页成功、仅 1～2 个边缘详情失败且已产出至少 8 条时记 degraded，允许有效缓存；
其他不完整枚举仍是 partial，不能当作否定片源证据。

### json

发送 GET base+path?query_param=编码词。响应为对象，items 为数组，每条 name
最多 1000 字、share_url 最多 4096 字；可以附带布尔 partial。
格式损坏计错误；格式合法但不是夸克的链接静默跳过，不污染 partial。超过 limit
标 truncated。该 provider 不猜测第三方分页。

### tg_web

无需登录或 Bot token，请求 base/s/channel?q=编码词，最多同时 4 个频道。
按 tgme_widget_message_wrap 分块读取消息正文、链接、提取码和 time datetime，
候选保留 published_at；不混用其他消息标题。最多 2 个频道失败且结果至少 8 条可
降级为 degraded，其余错误仍是 partial。配置示例含 15 个候选频道，必须实测筛选。

### pansou

POST base+path，请求体为 {"kw":"关键词","cloud_types":["quark"],"res":"merge"}。
接受 data.merged_by_type.quark、data.results[].links 或兼容 items，
提取 name/title/note/content、url/link/share_url、password/pwd，仅接收夸克。
上游 code 非 0/200 视作失败，不回传上游错误正文。

PanSou 应独立容器、限制 128 MiB，只暴露可信内部端点。示例 127.0.0.1:8888 仅适合
共享网络命名空间；跨容器应配置实际可信地址。镜像编排由宿主负责。

## 并发、截止时间、健康度与缓存

HTTP 使用按 origin 隔离 CookieJar 的持久 httpx.Client，最多 64 origin，
每 origin 最多 4 个 Keep-Alive 连接、全局 24 HTTP 槽。单响应最多 2 MiB，
连接/读取/总读取均有预算，不跟随任何跳转或自动解浏览器挑战。301/302/303/307/308
保留原始 http_status 与 redirect=true，绝不请求 Location 或回传其内容。TG 多频道报告
还保留 http_status_counts，混合成功/失败不会将已见 302 抹成 0。0 只代表没有可用状态，
不代表已经证明上游未响应。

矩阵最多 2 组同时搜索，每组 5 节点；直接调用矩阵默认硬上限 22 秒。
高层调用传入共享绝对截止，取得搜索槽前的排队时间也计入预算，不在排队后重新计时。
槽等待最多 2 秒且不超过剩余预算；失败保留每个未执行节点的 SearchCapacity 或
SearchDeadline 诊断，不能当作成功空结果。
完整搜索、子详情及插件共用最多 64 个 daemon 任务。超时返回 ProviderTimeout
并释放搜索槽；Python 无法安全强杀可信插件，挂起任务占用有限槽直到退出，
不会无界建线程。容量耗尽后失败关闭，插件仍须自行遵守请求超时和退出条件。

节点连续 3 轮错误或 partial 熔断 300 秒，到期仅放行一个 HALF_OPEN 探测，
成功闭合、失败重新冷却。健康度按配置语义隔离，保留 EWMA 延迟、候选数和计数。
node_health() 返回脱敏摘要及最多 2048 条事件计算的 24 小时成功率。
调用方整轮预算耗尽、排队到期或本地容量不足不惩罚上游健康；budget_exhausted
诊断和 budget_exhaustions 计数保留，不增加节点连续失败。节点自身较早发生的超时、
明确 HTTP 错误、重定向或反爬仍计失败并可熔断，避免把真正坏源永久视作健康。

高层 search_report 最多规划 16 词：中文季号、紧凑中文、Sxx、阿拉伯季号、
Unicode/独立 ASCII 罗马数字、年份以及调用方传入的多语别名。
首批并发前 2 词，每词最多 3.5 秒；不足才补剩余词，每词最多 7 秒、两路。
所有阶段共享默认 3.8 秒整轮预算：查询、排队与补召回不会各自获得新一轮时间。
这以潜在 partial 和候选覆盖下降换取前台延迟边界，不代表 95% 召回或完整来源复核。
至少 5 个高身份相关候选且含 4K 时可早停，已有实质节点故障仍保留 partial。

需要更完整复核时，Python 调用方显式使用
search_report(title, ..., deadline_seconds=22)。deadline_seconds 接受 0.05～22 的有限
int/float，拒绝布尔值、字符串、NaN、Infinity 和范围外值；None 使用默认 3.8 秒。
这只是较长预算，不保证每个上游历史资源都可枚举；当前没有新增环境变量或 HTTP 参数。

queries 与 planned_queries 区分已派发和计划；unexecuted_queries 列出尚未派发的词，
query_reports 逐词保留 status/partial，incomplete_queries 列出已报告不完整的查询。
已收到的候选保留；预算耗尽返回 deadline_exhausted=true、partial=true 及
SearchDeadline/ProviderTimeout 节点诊断，budget_seconds 是请求的整轮预算。
节点附带 query 以区分不同词的诊断。提前达到候选阈值可以 early_stopped=true，
未执行词仍显式列出，但不宣称完成全网搜索。审计遇到 deadline 截断或其他 partial
绝不能持久化“无源”否定结论，应保留待复核状态并视需要显式采用 22 秒深搜。

trusted_initial_alias 使用 pypinyin，只在候选明确年份与唯一 SxxExx 同时吻合时
提供拼音首字母辅助。缩写不能放宽年份、季集或异名冲突，最终仍须转存身份核验。

分享按 canonical URL 合并，保留标题、来源和提取码，返回前过滤 dead_sources。
内存 128 项真 LRU（命中移动到末尾），磁盘原子写入。键隔离配置、分类、分页、
limit、截止预算和早停模式。非空且非 partial 缓存 1800 秒，空或 partial 45 秒。
显式大于默认快搜预算的高层调用会读取但绕过 partial 缓存并重新请求来源；
不会让快搜短预算的 partial 阻止 22 秒深搜复核。完整缓存仍可复用。
truncated/degraded 单独不缩短 TTL；配置错误即使命中缓存也标 partial。

节点报告只保留 node/provider、pages/details/errors/candidates/seconds、
truncated/partial/degraded、http_status/redirect/http_status_counts/rate_limited 等安全字段，不返回上游 URL、
异常正文或任意自定义插件字段。整组新增 degraded_nodes 和 early_stopped。

## 管理与可信 Python 插件

GET /api/admin/plugins、POST /api/admin/plugins/reload 继续强制 Bearer，
reload 失败返回 422 且保留快照。公开摘要不包含 base/path 或凭据。

安装包通过 quark_vod.search entry point 提供 fetch(node, keyword, limit, max_pages)，
返回 (items, report)，其中 items 是最多 1200 条 name/share_url 字典，report 包含
errors/pages/details/truncated/partial。node 包含配置及内部截止提示，
插件不可依赖内部字段。核心覆盖 source、校验字段并脱敏报告。

仅 VOD_SEARCH_PLUGINS 显式列出的 provider 尝试加载，不得覆盖四个内置 provider；
失败只记录 entrypoint_load_failed，同进程不重试。代码安装/更新需要重启。
此为可信代码接口而非沙箱：插件可以使用进程文件、网络与凭据，只能安装可信包。

## 存储后端不是搜索插件

搜索 provider 与媒体存储使用不同的接口。公共包提供代码级 `RemoteStore` 协议和
`OpenListStore` 适配器，但当前通过 Python 构造注入，没有运行时 backend 选择、entry
point 发现或热加载。夸克分享解析、官方目录核验、转存任务和任务恢复仍由
`QuarkTransfer` 负责；修改搜索 JSON 不能替换网盘。

新的存储适配器必须实现四个操作：

- `mkdir(write_path)` 和 `remove(write_path)` 接收写挂载路径。只有后端确认操作成功
  才返回 `True`；返回 `False` 或抛异常都会让生命周期停在失败状态。
- `direct_url(playback_path)` 和 `refresh_path_tree(playback_path)` 接收经过
  `config.play_path()` 的播放挂载路径。
- `refresh_path_tree` 只是最佳努力的刷新提示，返回值不能证明文件存在；刷新失败或
  查询不到对象也不能直接当作文件缺失。
- 删除未确认成功时，内核保留原远端路径和可重试的清理记录，不把对象标为已清理。

实现和回归见 `app/quark_vod/backends.py`、`tests/test_backends.py`。这一边界允许
替换播放存储适配器，但不会把 Quark 专属的分享、鉴权和官方保存流程变成通用网盘
协议。

## 验证

分别运行 python -I tests/test_search_plugins.py、tests/test_search_evolution.py、
tests/test_search_quality.py，再执行 make test 和 python -m build。
专项使用 loopback HTTP、临时状态和合成分享，不触及真实账号或 AI，覆盖 TG/PanSou
协议与密码、HTTP连接/Cookie复用、软拦截、非夸克忽略、熔断半开、插件硬截止、
搜索槽释放与排队预算、多变体共享截止/保留候选、显式深搜与参数验证、全部内置 provider
的重定向状态与不跟随跳转、LRU、并发早停、S00 和强证据缩写。

真实 30 作品召回率、P95 耗时、外挂字幕落盘和生产 TG/PanSou 连通需另行实测，
不能用合成测试代替。实际搜索不达目标时应报告实测值。
