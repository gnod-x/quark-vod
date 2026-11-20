# 搜索插件与热重载 / Search providers

Quark-VOD 不内置公共搜索站地址。管理员在部署机器上创建搜索配置，
通过 VOD_SEARCH_CONFIG 指定文件；无节点时服务报告 configured=false、
partial=true，不把未配置说成“没有资源”。

## JSON schema

配置文件不超过256KiB，使用UTF-8；顶层只能有 version 和 nodes：

~~~json
{
  "version": 1,
  "nodes": [
    {
      "id": "example",
      "name": "Example search",
      "provider": "json",
      "base": "https://example.invalid",
      "path": "/search",
      "query_param": "q",
      "timeout": 4.0,
      "enabled": false
    }
  ]
}
~~~

- 最多32个节点，id唯一，格式为字母开头、后接字母数字/点/下划线/减号，最多64字符。
- provider内置 html 和 json；enabled必须是JSON布尔值，timeout为0.1—7秒数字。
- base只允许HTTP/HTTPS，禁止userinfo、query和fragment，不把凭证写进URL。
- path只用于json，默认 /search；query_param默认 q。
- 未知字段、重复ID、无效URL、未安装却启用的provider都会拒绝整次重载。
- 可使用enabled=false保留尚未安装的provider配置。
- 配置由可信管理员管理。HTTP端点可以是本地服务，因此管理员仍须控制其可访问目标。

## 内置 providers

html 使用现有有界HTML搜索/详情解析器：搜索路径为
/index.php/vod/search.html?wd=...，解析同源下一页、详情页及夸克分享。
每关键词最多三页，详情请求有独立预算、并发和响应大小限制。
身份评分、季集判断及画质证据仍走原核心，不由provider绕过。

json 发送 GET <base><path>?<query_param>=<URL编码关键词>，期望：

~~~json
{
  "items": [
    {
      "name": "Example show S01 2160p",
      "share_url": "https://pan.quark.cn/s/example"
    }
  ],
  "partial": false
}
~~~

items必须是数组；单次响应最多2MiB。只接受可规范化的夸克分享地址。
可在share_url附加原提取码；合并逻辑会保留丰富片名证据及提取码。
json provider是单次有界查询，不自动猜测供应商分页协议；第三方服务可实现其聚合接口，
或通过下面的扩展实现分页。

## 热重载语义

请求开始时检查配置文件的inode、纳秒mtime和大小。变化时完整读取和校验，
验证成功后原子切换不可变快照。一次搜索的多个关键词变体使用同一快照。
已在途搜索继续使用旧快照，新搜索使用新快照；运行中的媒体流不受影响。

配置无效或暂时不可读时，保留上一有效快照并记录通用错误代码；
仍用旧源产生的搜索结果会标partial。首次配置无效时保持空源状态。
管理员需要关闭所有源时应提交合法的 {"version":1,"nodes":[]}，而不是删除文件。

缓存key包含有效配置的SHA-256 fingerprint；增删、启停或更换源不会命中旧配置缓存。
格式化JSON但语义不变不会增加generation。
文件变化不会执行Python、安装包或任意外部脚本。

管理API可以读取注册表状态并触发严格重载；具体鉴权与路由见主README/API说明。
严格重载失败返回错误，上一快照不变。状态只暴露节点ID、名称、provider、
启用状态和timeout，不返回base/path或URL凭证。

## 第三方可信代码扩展

安装包通过Python entry point注册：

~~~toml
[project.entry-points."quark_vod.search"]
my-search = "my_package.provider:fetch"
~~~

设置 VOD_SEARCH_PLUGINS=my-search 才会加载这个已安装的entry point；
未选择的entry point不执行。新安装/更新Python插件代码需重启服务；
已加载provider的节点地址、启停和增删则通过JSON热重载。

~~~python
def fetch(node, keyword, limit, max_pages):
    # Implement bounded requests and use node["timeout"].
    items = [{"name": keyword,
              "share_url": "https://pan.quark.cn/s/example"}]
    report = {"errors": 0, "pages": 1, "details": 0,
              "truncated": False, "partial": False}
    return items, report
~~~

函数接受四个位置参数，返回 (items, report)。dispatcher覆盖source为节点ID，
只保留标准结果字段和有界计数，不输出插件任意错误文本；单provider异常不影响其余源。
单provider输出最多1200条，调度最多两个并发搜索、五个并行节点，内置HTTP槽最多十二个。

扩展是管理员信任的本地Python代码，不是安全沙箱；它必须自行保证超时、资源上限和退出条件。
JSON不支持上传、下载或远程执行插件代码。内置html/json不能被第三方同名覆盖。

## 开发接口与验证

quark_vod.search_plugins.get_registry() 返回共享注册表：

- snapshot() → frozen SearchSnapshot，含 nodes、enabled_nodes、fingerprint、generation。
- status() → 无端点的状态摘要；error为空或通用配置错误代码。
- reload(strict=True) → 验证并更新，失败抛 PluginConfigError，code可用于API响应。
- register_provider(name, fetch) → 仅供可信启动代码注册扩展。

tests/test_search_plugins.py 使用随机本地HTTP端口、合成分享和临时目录，
覆盖真实HTML/JSON请求、自动增删/启停、坏配置保留、显式错误、
缓存跨配置隔离、在途快照、单provider故障、凭证不回显和显式entrypoint加载。
它不调用公共搜索站、夸克账号或AI。
