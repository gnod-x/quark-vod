# 搜索插件入口

搜索配置和 provider 协议的唯一详细说明在[插件与热重载指南](plugins.md)。本页只
帮助确认该读哪一部分：

- 部署者先设置 `VOD_SEARCH_CONFIG`，创建 `version: 1`、`nodes` 配置；源码 checkout
  可从 `examples/search.example.json` 复制禁用占位项。安装包不会替你提供公共搜索站。
- 内置 `html` 和 `json` provider 只负责产生夸克分享候选。配置文件变化会自动加载，
  无效替换保留上一份有效快照；没有启用节点时是未完成搜索，不是全网无资源。
- 需要 Python provider 时，先安装带 `quark_vod.search` entry point 的可信包，再在
  `VOD_SEARCH_PLUGINS` 中显式列出名称并重启服务。JSON 配置不能上传或执行代码。
- `RemoteStore` 是转存使用的独立代码契约，当前不通过搜索配置热插拔；存储后端边界
  和失败语义也在[插件指南](plugins.md)的末尾。

需要查看节点字段、请求预算、错误状态、缓存隔离、管理 API 或 provider 函数签名时，
直接以[插件与热重载指南](plugins.md)为准。
