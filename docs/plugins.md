# 搜索插件与热重载

Quark-VOD 不内置公共搜索站地址。部署者通过 `VOD_SEARCH_CONFIG` 指定一份
JSON 文件，文件只选择已经安装的 provider 和它们的 HTTP 端点；JSON 本身不
包含 Python、脚本或远程代码。没有配置文件或没有启用节点时，服务仍可启动，
但搜索报告会是 `configured=false`、`partial=true`，这表示当前部署没有完成
搜索，不能解释为全网没有资源。

## 配置文件

文件必须是 UTF-8 JSON，大小不超过 256 KiB，顶层只能包含 `version` 和
`nodes`，并且 `version` 必须为整数 `1`：

```json
{
  "version": 1,
  "nodes": [
    {
      "id": "example-json",
      "name": "Example JSON",
      "provider": "json",
      "base": "https://search.example.invalid",
      "path": "/search",
      "query_param": "q",
      "timeout": 4,
      "enabled": false
    }
  ]
}
```

每个节点的约束如下：

- `nodes` 最多 32 项，`id` 必须唯一，首字符为字母，其余可用字母、数字、
  `.`, `_`, `-`，总长不超过 64。`name` 必须非空且不超过 100 个字符。
- `provider` 是 provider 名称；内置名称为 `html` 和 `json`。`enabled` 必须是
  JSON 布尔值。禁用节点可以暂存尚未安装的 provider，启用时必须能在进程中找到它。
- `base` 只能是 `http` 或 `https` URL，不能包含用户名、密码、query 或 fragment，
  端口必须在 1--65535。不要把 Cookie、token 或签名放进 URL。
- `timeout` 是数字，范围为 0.1--7 秒；布尔值不算数字。
- `path` 必须以单个 `/` 开头，不能带 query、fragment，长度不超过 1024；
  `query_param` 只能包含字母、数字和下划线，首字符不能是数字。两者只有
  `json` provider 使用，`html` provider 会忽略它们。
- 未知字段、重复 ID、坏 URL、坏类型或其他校验失败会拒绝整次配置。需要关闭
  全部源时，写入合法的 `{ "version": 1, "nodes": [] }`，不要删除配置文件。

配置由部署者维护。节点地址可以指向本机或私有网络，因此编辑配置的人也必须
负责确认这些目的地可信且适合服务进程访问。

## 内置 provider

`html` 使用有界的 HTML 搜索和详情解析器。它请求
`/index.php/vod/search.html?wd=<关键词>`，只接受同源的搜索下一页和详情链接，
再从详情页提取夸克分享。每个节点最多处理三页，详情请求有单节点截止时间、
响应体上限和并发限制。provider 只提供候选，作品身份、季集、类型和画质证据
仍由核心选择逻辑核验。

`json` 对每个关键词只发一次 GET 请求：
`<base><path>?<query_param>=<URL 编码的关键词>`。响应必须是对象，且 `items`
必须为数组：

```json
{
  "items": [
    {
      "name": "Example show S01 2160p",
      "share_url": "https://pan.quark.cn/s/example"
    }
  ],
  "partial": false
}
```

单次响应最多 2 MiB。每个 item 的 `name` 不得超过 1000 个字符，`share_url`
不得超过 4096 个字符，并且必须能规范化为夸克分享地址；无效 item 会计入错误
并跳过。`partial` 会按 Python `bool()` 解释；调用方应提供 JSON 布尔值 `true`
或 `false`，不要依赖字符串等其他类型的隐式转换。它表示该 provider 自己没有
完整枚举，不能表示整个互联网的资源结论。JSON provider 不猜测第三方分页协议，
需要分页时由第三方接口聚合，或实现下面的 Python provider。

不同节点返回同一分享时，核心按规范化分享地址去重，保留多个标题和来源，并
保留可提取的原始提取码。候选仍须通过身份匹配，provider 不能绕过安全选择。

## 搜索预算与结果状态

调度器将 `limit_per_node` 限制在 1--30，默认值为 24；这是传给每个 provider
的单节点预算，不等于最终候选条数。HTML provider 用它限制要请求的详情页数，
一页详情可以产生多条候选；JSON provider 最多接收该数量的 `items`；trusted Python
provider 会收到该值并应自行遵守。HTML 每个节点最多请求 3 页，节点最多 5 路并行；
全局最多同时运行 2 组搜索和 12 个 HTTP 请求。单 provider 返回超过 1200 条会被
视为无效，异常不会阻止其他节点。公开报告只保留节点、provider、
页数、详情数、错误数、是否截断、是否 partial、耗时和候选数；插件的 URL、异常
文本和其他任意字段不会透传。

有候选且 `partial=false` 的结果缓存 30 分钟；空结果或 `partial=true` 的结果缓存
约 45 秒。全局 `partial` 由配置错误、provider 错误或 provider 自报 partial 决定；
`truncated` 单独不会将结果标记为 partial，也不会改变缓存时长。缓存键包含有效配置的 SHA-256
fingerprint，因此增删、启停、地址或参数变化不会命中
旧配置的结果。已标记为失效的分享会在返回前过滤。

## 热重载

配置文件的设备、inode、纳秒级 mtime 和大小在请求开始时检查。发现变化后，服务
会完整读取和校验，再切换到不可变快照。一次搜索（包括它的多个标题变体）持有
同一个快照；搜索已经开始后，配置变化只影响新搜索，正在播放的流不受影响。

有效配置的语义 fingerprint 不变时，JSON 格式化和对象字段顺序变化不会增加
generation；`nodes` 数组顺序属于配置语义，调整顺序会改变 fingerprint。语义
变化才会切换快照和 generation，并使新缓存键生效。文件暂时不可读或内容
无效时，服务保留上一份有效快照并记录通用错误代码；旧快照产生的结果标记为
partial。首次就无有效快照时，结果为空且仍标记 partial。

配置文件建议在同一目录内用临时文件加 `os.replace` 原子替换；Docker 部署应挂载
整个目录而不是只挂载某个 inode，以便容器看到替换后的文件。

管理 API 提供两个受 Bearer 保护的操作：

- `GET /api/admin/plugins` 返回 `configured`、`config_file_set`、generation、
  fingerprint、节点摘要、已知 provider、当前配置错误和进程级
  `entrypoint_errors`。节点摘要只含 ID、名称、provider、timeout 和 enabled，
  不返回 `base`、`path` 或凭据。
- `POST /api/admin/plugins/reload` 立即严格校验并应用文件。失败返回错误，上一份
  有效快照保持不变。

## 可信 Python provider

Python provider 通过安装包的 entry point 注册：

```toml
[project.entry-points."quark_vod.search"]
my-search = "my_package.provider:fetch"
```

只有在 `VOD_SEARCH_PLUGINS` 中显式列出名称时才会加载，例如：

```dotenv
VOD_SEARCH_PLUGINS=my-search,another-search
```

未列出的 entry point 不会执行。名称必须符合节点 ID 的字符规则，不能覆盖内置
`html` 或 `json`。加载失败会记录通用的 `entrypoint_load_failed`；同一进程会记住
这次尝试，修复安装或 entry point 后需要重启服务才能重试。安装或更新 Python
代码也需要重启；只修改已加载 provider 的 JSON 节点可以热加载。

provider 函数接收四个位置参数，并返回 `(items, report)`：

```python
def fetch(node, keyword, limit, max_pages):
    items = [{
        "name": keyword,
        "share_url": "https://pan.quark.cn/s/example",
    }]
    report = {
        "errors": 0,
        "pages": 1,
        "details": 0,
        "truncated": False,
        "partial": False,
    }
    return items, report
```

返回的 item 至少要有字符串 `name` 和 `share_url`。调度器会覆盖 `source`，限制
字段长度和计数，并把插件异常或错误计数统一为 `ProviderError`；不可信的异常文本
不会进入 API 响应。provider 应自行实现请求超时、分页上限、响应上限和退出条件。

这些 provider 是安装在服务进程中的可信 Python 代码，不是沙箱。它们可使用服务
账号能使用的文件、网络和凭据；只应从信任来源安装。JSON 配置不支持上传、安装或
远程执行代码。

## 存储后端边界

搜索 provider 与媒体存储不是同一个插件接口。公开包提供代码级 `RemoteStore`
协议和 `OpenListStore` 适配器，但当前通过 Python 构造注入，没有运行时 backend
选择、entry point 发现或热加载。夸克分享解析、官方目录核验、转存任务和生命周期
仍由 `QuarkTransfer` 负责，不能仅修改搜索 JSON 来替换网盘。

实现新的存储适配器时遵循四个方法：

- `mkdir(write_path)`、`remove(write_path)` 接收写挂载路径，只有后端接受操作后
  才返回 `True`；失败返回 `False` 或抛异常。
- `direct_url(playback_path)`、`refresh_path_tree(playback_path)` 接收经过
  `config.play_path()` 的播放挂载路径。
- `refresh_path_tree` 只是最佳努力刷新提示，返回值不能证明对象存在；查询或刷新
  失败也不能当作文件缺失。
- 转存内核会把失败的变更转成 `TransferFailed`，不推进新的保存或清理状态；删除
  未确认时保留原远端路径和可重试记录。

这组接口的实现和测试见 `app/quark_vod/backends.py`、`tests/test_backends.py`。

## 开发验证

搜索插件的隔离回归使用随机 loopback HTTP 端口、合成分享和临时目录，不访问公共
搜索站、夸克账号或 AI：

```sh
python -I tests/test_search_plugins.py
```

它覆盖 HTML/JSON 请求、节点增删启停、坏配置保留、显式重载错误、fingerprint
隔离缓存、在途快照、单节点故障、凭据不回显和 entry point allowlist。完整变更仍
应运行 `make test`，provider 代码或服务依赖变更还要按[贡献指南](../CONTRIBUTING.md)
验证打包和容器边界。
