# 搜索插件与存储边界

Quark-VOD 的搜索配置只选择已经安装的 provider 及其 HTTP 端点。JSON 文件不
包含 Python、脚本或远程代码，也不会安装插件。没有配置文件，或者配置文件中
没有启用节点时，服务仍可启动，但搜索报告会返回 `configured=false` 和
`partial=true`；这表示当前部署没有完成搜索，不能据此判断全网没有资源。
节点目的地址和 Python provider 的信任边界见[安全说明](../SECURITY.md)。

搜索 provider 只产生夸克分享候选。作品身份、年份、电影或剧集类型、季集、
文件路径和画质证据仍由点播内核核验，provider 不能绕过这些选择条件。

## 配置文件

通过 `VOD_SEARCH_CONFIG` 显式指定 UTF-8 JSON 文件。文件大小上限为 256 KiB，
顶层只能有 `version` 和 `nodes`，且 `version` 必须是整数 `1`：

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

节点字段按下面的规则校验：

- `nodes` 最多 32 项，`id` 必须唯一，首字符是字母，其余字符只能是字母、数字、
  `.`, `_`, `-`，长度最多 64。`name` 默认使用 `id`；原始字符串最多 100 个字符，
  去掉首尾空白后必须非空，且不能含控制字符。
- `provider` 名称必须符合同样的标识符规则。内置 provider 是 `html` 和 `json`。
- `provider` 默认是 `html`。`enabled` 必须是 JSON 布尔值，默认是 `true`；禁用节点
  可以暂存尚未安装的 provider，启用时该 provider 必须已经在进程中注册。
- `base` 必须是 `http` 或 `https` URL，必须有主机名，不能包含用户名、密码、
  query 或 fragment，端口（如有）必须在 1--65535，长度最多 2048，不能含空格或
  C0 控制字符。不要把 Cookie、token 或签名地址放进 URL。
- `timeout` 是有限的数字，范围为 0.1--7 秒，默认 4 秒；布尔值不算数字。
- `path` 默认是 `/search`，必须以 `/` 开头且不能以 `//` 开头，不能含空格、C0
  控制字符、query 或 fragment，长度最多 1024。`query_param` 默认是 `q`，只能是
  字母、数字和下划线，首字符不能是数字，长度最多 64。两者只由 `json` provider
  使用，`html` provider 会忽略它们。
- 未知字段、重复 ID、坏 URL、坏类型、非法 provider 或其他校验失败会拒绝整份
  配置。需要关闭全部源时，写入合法的 `{ "version": 1, "nodes": [] }`，
  不要删除配置文件。

`key` 是旧配置兼容字段；如果同时出现，必须与 `id` 完全相同。新的配置应只写
`id`。节点地址可以指向本机或私有网络，因此配置维护者必须确认目标可信，并且
适合服务进程访问。

## 内置 provider

### `html`

`html` 对每个关键词从以下路径开始搜索：

```text
/index.php/vod/search.html?wd=<URL 编码的关键词>
```

它只接受与节点 `base` 具有相同 scheme 和主机的搜索下一页及详情链接，再从详情页
提取夸克分享。单个节点最多遍历 3 页；每次 HTTP 响应最多读取 2 MiB；单节点总
截止时间为 22 秒；详情页最多同时请求 3 个。`limit_per_node` 限制详情页数量，
一页详情可以产生多条候选，所以最终候选数可能超过这个值。

### `json`

`json` 对每个关键词只发送一次 GET：

```text
<base><path>?<query_param>=<URL 编码的关键词>
```

响应必须是对象，且 `items` 必须是数组：

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

响应体同样最多 2 MiB。每个 item 的 `name` 长度最多 1000，`share_url` 长度最多
4096，并且必须能规范化为夸克分享地址；无效 item 会计入错误并跳过。`items` 超过
本次 `limit_per_node` 时会标记 `truncated`，多出的项不会被接收。`partial` 按
Python `bool()` 解释，调用方应提供 JSON 布尔值 `true` 或 `false`，不要依赖其他
类型的隐式转换。它只表示这个 provider 自己没有完成枚举，不能表示整个互联网的
资源结论。该 provider 不猜测第三方分页协议；需要分页时由第三方接口聚合，或
安装可信 Python provider。

不同节点返回同一分享时，核心按规范化分享地址去重，保留多个标题和来源，并保留
能够提取的提取码。自定义 provider 返回的条目还会经过同样的字段限制；不能规范
化为夸克地址的条目会在合并阶段丢弃。

## 请求预算、缓存与结果状态

一次 `PanshareMatrix` 搜索的 `limit_per_node` 会被限制在 1--30，默认 24；它是
传给每个 provider 的单节点预算，不等于最终候选数。`max_pages` 被限制在 1--3，
默认 3。

- 一次搜索最多让 5 个启用节点的 provider 并行执行；HTML 节点内部的详情请求另有
  固定的 3 路并发限制。
- 进程最多同时运行 2 组完整搜索，内置 HTTP 读取共享最多 12 个并发槽位。一个
  provider 返回超过 1200 条会被视为无效；异常只使该节点失败，不阻断其他节点。
- 可信 Python provider 不自动获得 HTML provider 的 22 秒节点截止时间，也不一定
  使用内置的 12 个 HTTP 槽位。它必须自行实现请求超时、分页上限、响应大小限制和
  退出条件，并遵守收到的 `limit` 与 `max_pages`。
- 对外节点报告只保留节点、provider、页数、详情数、错误数、是否截断、是否 partial、
  耗时和候选数。provider 的 URL、异常文本和任意自定义字段不会透传；错误会归一为
  安全的 `ProviderError` 诊断。

搜索结果缓存保存在 `VOD_DATA_DIR/search_cache`，进程内最多保留 128 个条目。缓存
键包含关键词、页数、单节点预算和有效配置的 SHA-256 fingerprint：

- 有候选且 `partial=false` 时缓存 1800 秒（30 分钟）。
- 空结果、provider 故障或 `partial=true` 时缓存 45 秒。
- `truncated` 单独不会把结果标成 partial，也不会缩短缓存时间。
- 结果返回前会过滤已经标记失效的分享。

没有启用节点、配置错误、节点错误或 provider 自报 `partial` 时，结果的 `partial`
为 `true`。配置为空和“已完成搜索但没有符合身份的候选”是不同状态，调用方必须
保留这个区别。

## 热重载与请求快照

每次请求开始时，registry 会比较配置文件的设备号、inode、纳秒级 mtime 和大小。
发现变化后，服务会完整读取、解析和校验文件，再切换到不可变 `SearchSnapshot`。
一次搜索及其最多四个标题变体会持有同一个 snapshot；搜索开始后发生的文件变化
只影响后续搜索，已经开始的播放流不受影响。

配置的语义 fingerprint 由规范化后的节点内容和顺序计算：JSON 格式化、字段顺序
或兼容 `key` 的内部表示变化不改变 fingerprint；`nodes` 数组顺序属于语义，调整
顺序会改变 fingerprint。只有 fingerprint 变化才增加 generation，并使新缓存键生效。

文件暂时不可读或内容无效时，registry 保留上一份有效 snapshot，并记录通用错误码；
使用旧 snapshot 的搜索会标记 `partial`。首次就没有有效 snapshot 时，搜索为空且
同样标记 `partial`。显式 reload 在失败时返回错误，绝不会替换上一份有效 snapshot。

编辑时应在同一目录写临时文件，再用 `os.replace` 原子替换。Docker 部署应挂载整个
配置目录，而不是只挂载某一个 inode，否则宿主机替换文件后容器可能仍然看到旧文件。

## 管理 API

以下两个端点都需要管理 Bearer token：

- `GET /api/admin/plugins` 会刷新并返回 `configured`、`config_file_set`、
  `generation`、`fingerprint`、节点数量、节点摘要、已知 provider、当前配置错误和
  `entrypoint_errors`。节点摘要只含 `id`、`name`、`provider`、`timeout`、`enabled`，
  不返回 `base`、`path` 或凭据。
- `POST /api/admin/plugins/reload` 立即严格读取并应用配置。成功返回当前状态；失败
  返回 `422` 和通用错误，上一份有效 snapshot 保持不变。

配置状态也会出现在受保护的能力信息中。`/health` 只证明 HTTP 服务响应，不证明
搜索源已经配置或可用。

## 可信 Python provider

Python provider 通过安装包的 entry point 注册：

```toml
[project.entry-points."quark_vod.search"]
my-search = "my_package.provider:fetch"
```

只有在 `VOD_SEARCH_PLUGINS` 中显式列出的名称才会尝试加载：

```dotenv
VOD_SEARCH_PLUGINS=my-search,another-search
```

未列出的 entry point 不会执行。名称必须符合节点 ID 的标识符规则，不能覆盖内置
`html` 或 `json`。同名 entry point 必须唯一；加载失败只记录通用的
`entrypoint_load_failed`。进程会记录每个名称已经尝试过，即使失败也不会在同一进程
重试；修复安装、entry point 或依赖后必须重启服务。安装或更新 Python provider 代码
也需要重启，已加载 provider 的 JSON 节点参数才支持热加载。

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

`node` 是字典，包含 `id`、`key`、`name`、`provider`、`base`、`timeout`、`enabled`、
`path` 和 `query_param`；`keyword` 是当前搜索词，`limit` 与 `max_pages` 是已经
限制在支持范围内的整数。`report` 可提供 `errors`、`pages`、`details` 计数以及
`truncated`、`partial` 布尔标记；其他字段会被忽略。

`items` 和 `report` 必须分别是 list 和 dict。每个 item 至少包含字符串 `name` 和
`share_url`，字段长度上限与内置 provider 相同；单次返回不得超过 1200 条。调度器
会覆盖 `source`，限制公开计数，过滤异常条目，并把插件异常转成安全错误报告。它
不会把任意报告字段、上游 URL 或异常文本返回给调用方。

这些 provider 是安装在服务进程中的可信 Python 代码，不是沙箱。它们可以使用服务
账号能够访问的文件、网络和凭据，只应从可信来源安装。JSON 配置不支持上传、安装
或远程执行代码。

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

插件专项回归使用随机 loopback HTTP 端口、合成分享和临时目录，不访问公共搜索站、
夸克账号或 AI：

```sh
python -I tests/test_search_plugins.py
```

它覆盖 HTML/JSON 请求、节点增删启停、坏配置保留、显式重载错误、fingerprint 隔离
缓存、在途请求快照、单节点故障、凭据不回显和 entry point allowlist。完整代码变更
还应运行：

```sh
make test
python -m build
```

修改 provider 代码、依赖或容器时，还要按[贡献指南](../CONTRIBUTING.md)完成源码树外
wheel 安装及受限容器边界验证。
