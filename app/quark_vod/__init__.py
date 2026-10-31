"""cinema-quark 点播服务包。

模块划分：
  config           配置单一事实来源（.env 注入 cookie）
  db               SQLite 状态库（转存/播放/清理）
  openlist_client  OpenList 操作（mkdir/list/remove/直链）
  transfer         夸克分享解析与转存
  strm_manager     strm 三态管理（pending/live/还原）
  server           HTTP 服务（8099，Jellyfin2 播放入口）
  cleanup          定时清理任务
"""

__all__ = [
    "config", "db", "openlist_client", "transfer",
    "strm_manager", "server", "cleanup",
]

__version__ = "1.0.0"
