#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenList 客户端

封装点播服务需要的网盘操作：登录、建目录、列目录、删除、生成直链。
所有写操作在驱动不具备写能力时明确报错（不会假装成功）。

认证方式（2026-10-15 实测）：
  Authorization: <token>        —— 不要加 Bearer，加了会 401
  响应体的 code 字段才是结果     —— HTTP 200 也可能是 {"code":500}
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config


class OpenListError(Exception):
    pass


class OpenListClient:
    def __init__(self, base=None, user=None, password=None):
        self.base = base or config.OPENLIST_API
        self.user = user or config.OPENLIST_ADMIN_USER
        self.password = password or config.OPENLIST_ADMIN_PASS
        self.token = ""
        self.token_expires = 0
        self._login()

    # ── 基础请求 ──────────────────────────────────────
    def _request(self, path, data=None, method="POST", timeout=60, retry=True):
        url = self.base + path
        body = json.dumps(data).encode() if data is not None else None
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = self.token
        req = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            # token 失效则重登录重试一次
            if e.code == 401 and retry:
                self._login()
                return self._request(path, data, method, timeout, retry=False)
            raise OpenListError(f"HTTP {e.code}: {raw[:200]}")
        except Exception as e:
            raise OpenListError(f"{type(e).__name__}: {e}")

    def _ok(self, resp, action):
        """统一校验：HTTP 200 不代表成功，必须看 code。"""
        if not isinstance(resp, dict):
            raise OpenListError(f"{action}: 响应异常 {resp}")
        code = resp.get("code")
        if code == 200:
            return resp.get("data")
        raise OpenListError(f"{action}: code={code} msg={resp.get('message')}")

    def _login(self):
        resp = self._request("/api/auth/login",
                             {"username": self.user, "password": self.password},
                             retry=False)
        data = self._ok(resp, "登录")
        self.token = data["token"]
        # config.token_expires_in 单位小时，留 1 小时余量
        self.token_expires = time.time() + 47 * 3600

    # ── 读操作 ────────────────────────────────────────
    def list(self, path, password="", refresh=False):
        payload = {"path": path, "password": password}
        if refresh:
            # OpenList 的 fs/list 支持 refresh 参数，但对 admin API 之外的
            # 调用可能返回 "Refresh without permission"。此时降级为普通列表，
            # 由调用方用轮询 + 模糊匹配兜底（见 transfer.py）。
            try:
                resp = self._request("/api/fs/list",
                                     dict(payload, refresh=True))
                data = self._ok(resp, f"list {path}")
                return (data or {}).get("content") or []
            except OpenListError:
                pass
        resp = self._request("/api/fs/list", payload)
        data = self._ok(resp, f"list {path}")
        return (data or {}).get("content") or []

    def get(self, path, password=""):
        resp = self._request("/api/fs/get", {"path": path, "password": password})
        return self._ok(resp, f"get {path}")

    def refresh_path_tree(self, path):
        """自顶向下逐级刷新目录缓存，确保新转存的子目录与文件立即可见。

        支持传入目录或视频文件路径，自动剔除文件名逐级 refresh 父目录。
        """
        clean = path.strip("/")
        parts = [p for p in clean.split("/") if p]
        if not parts:
            return True
        # 若末尾带有常见视频后缀，视为文件，仅刷新其父目录
        video_exts = {".mp4", ".mkv", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv"}
        last = parts[-1].lower()
        if any(last.endswith(ext) for ext in video_exts):
            parts = parts[:-1]

        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                self.list(cur, refresh=True)
            except Exception:
                pass
        return True

    # ── 写操作 ────────────────────────────────────────
    def mkdir(self, path):
        """建目录（含父级不存在时逐级创建）。"""
        if not path.startswith("/"):
            raise OpenListError(f"非法路径: {path}")
        parts = [p for p in path.split("/") if p]
        cur = ""
        for p in parts:
            cur += "/" + p
            try:
                self._request("/api/fs/mkdir", {"path": cur})
            except OpenListError:
                # 已存在则继续；真实失败向上抛
                try:
                    items = self.list(cur.rsplit("/", 1)[0] or "/")
                except OpenListError:
                    raise
                if not any(x.get("name") == p for x in items):
                    raise
        return True

    def move(self, src_path, dst_dir):
        """移动文件或目录到目标目录。"""
        src_parent = src_path.rsplit("/", 1)[0] or "/"
        name = src_path.rsplit("/", 1)[1]
        resp = self._request("/api/fs/move", {"src_dir": src_parent, "dst_dir": dst_dir, "names": [name]})
        self._ok(resp, f"move {src_path} -> {dst_dir}")
        return True

    def remove(self, path):
        """删除文件或目录。

        契约（2026-10-15 读源码确认，server/handles/fsmanage.go:351 FsRemove）：
          字段名是 **dir** 不是 path；names 是待删的文件名数组。
        用错字段名的症状：`storage not found; rawPath: /.wtest`。
        """
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[1]
        resp = self._request("/api/fs/remove", {"dir": parent, "names": [name]})
        self._ok(resp, f"remove {path}")
        return True

    def rename(self, path, new_name):
        """重命名。契约同 remove：字段名为 dir/name。"""
        parent = path.rsplit("/", 1)[0] or "/"
        name = path.rsplit("/", 1)[1]
        resp = self._request("/api/fs/rename",
                             {"dir": parent, "name": name, "new_name": new_name})
        self._ok(resp, f"rename {path}")
        return True

    def put(self, path, data, timeout=300):
        """上传文件（流式）。

        契约（2026-10-15 读源码确认，server/handles/fsup.go:47 FsStream）：
          PUT /api/fs/put，路径在 **File-Path 请求头**（URL 转义），
          不是 query 参数；body 为原始字节。
        放 query 的症状：返回前端 HTML / `storage not found`。
        """
        import urllib.parse
        req = urllib.request.Request(self.base + "/api/fs/put", data=data, method="PUT")
        req.add_header("Authorization", self.token)
        req.add_header("File-Path", urllib.parse.quote(path, safe=""))
        if isinstance(data, (bytes, bytearray)):
            req.add_header("X-File-Size", str(len(data)))
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                resp = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raise OpenListError(f"put HTTP {e.code}: {e.read().decode('utf-8','ignore')[:200]}")
        except Exception as e:
            raise OpenListError(f"put {type(e).__name__}: {e}")
        self._ok(resp, f"put {path}")
        return True

    def exists(self, path):
        try:
            self.get(path)
            return True
        except OpenListError:
            return False

    # ── 直链 ──────────────────────────────────────────
    def direct_url(self, path):
        """生成 OpenList 直链（播放器最终地址）。

        2026-10-16 读写分离：入参若是转存写入路径（/夸克网盘/...），
        先经 config.play_path() 换成播放挂载（/夸克，QuarkTV，不限速）。
        注意播放重定向优先走 server._direct_url()（不依赖本实例，
        避免 OpenList 瞬时不可用阻塞已转存影片）。
        """
        enc = urllib.parse.quote(config.play_path(path))
        return f"{config.OPENLIST_PUBLIC}/d{enc}"

    # ── 自检 ──────────────────────────────────────────
    def check_write_capable(self):
        """探测写能力：在点播根下建/删一个探针目录。

        返回 (可用, 原因)。UI/Allow 头层面缺 PUT/MKCOL 时这里会明确失败，
        不会伪装成成功（CLAUDE.md 红线 #9 精神）。
        """
        probe = f"{config.QUARK_VOD_ROOT}/.write_probe"
        try:
            self.mkdir(probe)
        except OpenListError as e:
            return False, f"mkdir 失败: {e}"
        # 必须回读确认真的存在（不能只看返回码）
        if not self.exists(probe):
            return False, "mkdir 返回成功但目录不存在（驱动假成功）"
        try:
            self.remove(probe)
        except OpenListError as e:
            return False, f"remove 失败: {e}"
        return True, "写能力就绪"
