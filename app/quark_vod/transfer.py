#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
夸克网盘转存层

职责：把第三方分享链接转存到自己网盘的点播目录，并通过 OpenList 暴露为直链。

调用链路（2026-10-15 实测打通）：
  1. POST https://drive.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc
     输入 pwd_id/passcode 换取 stoken
  2. GET  https://drive.quark.cn/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc&pwd_id=...&stoken=...
     获取待转存文件的 fid 与 share_fid_token
  3. POST https://drive.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc
     提交转存任务，返回 task_id
  4. GET  https://drive.quark.cn/1/clouddrive/task?pr=ucpro&fr=pc&task_id=...
     轮询直到 status=2 (已完成)
  5. OpenList move
     将落盘在根目录的内容原子移动到 /夸克网盘/点播/
  6. 扫描视频文件并推导直链，入库并排队 7 天后自动清理
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import db
from . import strm_manager
from . import name_parser
from .backends import RemoteStore, openlist_store

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

QUARK_SHARE_TOKEN = "https://drive.quark.cn/1/clouddrive/share/sharepage/token?pr=ucpro&fr=pc"
QUARK_SHARE_DETAIL = "https://drive.quark.cn/1/clouddrive/share/sharepage/detail?pr=ucpro&fr=pc"
QUARK_SHARE_SAVE = "https://drive.quark.cn/1/clouddrive/share/sharepage/save?pr=ucpro&fr=pc"
QUARK_TASK = "https://drive.quark.cn/1/clouddrive/task?pr=ucpro&fr=pc"
QUARK_FILE_RENAME = "https://drive.quark.cn/1/clouddrive/file/rename?pr=ucpro&fr=pc"

VIDEO_EXTS = {".mp4", ".mkv", ".ts", ".m2ts", ".avi", ".mov", ".flv", ".wmv"}
# 规避审查的伪装后缀（"" 表示无后缀），与 sniff_and_fix_file 的兜底扶正口径保持一致
DISGUISE_EXTS = {".mp3", ".pdf", ".txt", ".apk", ".mkv1", ".mp41", ""}
DISGUISE_MIN_SIZE = 200 * 1024 * 1024


def is_candidate_video(name, size=0):
    """验票/预检阶段判定分享包内条目是否为候选视频。

    2026-11-04 修复：原实现只认 VIDEO_EXTS，分享包把正片伪装成 E28.mp3 (1.5GB) 时，
    验票直接判「未找到 S01E28」拒绝转存，转存后的 sniff_and_fix_file 扶正逻辑根本没机会执行
    （实测《嫁金钗》E27~E30 必现 503）。伪装后缀且体积 > 200MB 一律视为候选视频放行，
    真实格式由转存后的魔数嗅探扶正。
    """
    ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
    if ext in VIDEO_EXTS:
        return True
    return ext in DISGUISE_EXTS and (size or 0) > DISGUISE_MIN_SIZE


def is_poisoned_fake_header(header_bytes):
    """检测是否为恶意拼接假头的投毒文件（如前8字节硬拼PNG魔数避审查，导致播放器OOM崩溃）。"""
    if not header_bytes or len(header_bytes) < 16:
        return False
    # 假 PNG 缝合怪: \x89PNG\r\n\x1a\n 后面紧跟 MKV (1a45dfa3) 或 MP4 (ftyp)
    if header_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        rest = header_bytes[8:]
        if rest.startswith(b"\x1a\x45\xdf\xa3") or b"ftyp" in rest[:16]:
            return True
    return False


def detect_video_magic(header_bytes):
    """根据文件头前 16 字节检测真实视频格式魔数 (Magic Number)。"""
    if not header_bytes or len(header_bytes) < 4:
        return None
    # MKV / WebM
    if header_bytes.startswith(b"\x1a\x45\xdf\xa3"):
        return ".mkv"
    # MP4 / MOV (包含 ftyp)
    if b"ftyp" in header_bytes[:16]:
        return ".mp4"
    # MPEG-TS (以 0x47 同步字节开头)
    if header_bytes[0] == 0x47:
        return ".ts"
    # FLV
    if header_bytes.startswith(b"FLV"):
        return ".flv"
    # AVI
    if header_bytes.startswith(b"RIFF") and len(header_bytes) >= 12 and b"AVI " in header_bytes[8:12]:
        return ".avi"
    return None


# 全局并发限制信号量（防短时并发轰炸网盘触发风控）
_transfer_semaphore = threading.Semaphore(config.TRANSFER_CONCURRENCY)


_request_context = threading.local()


class TransferNotReady(Exception):
    """Cookie 未配置或写能力未就绪。"""


class TransferTaskFailed(Exception):
    """The official task explicitly reported a terminal failure."""


class TransferFailed(Exception):
    """转存过程失败。"""


class QuarkTransfer:
    def inspect_share_video(self, share_url, title, season=0, episode=0, timeout=60, *, aliases=()):
        """Read-only bounded share inspection; reuse the real transfer selector."""
        previous = getattr(_request_context, 'deadline', None)
        _request_context.deadline = min(previous, time.monotonic()+timeout) if previous else time.monotonic()+timeout
        try:
            token, pwd_id, roots = self.resolve_share(share_url)
            files = self.share_files(token, pwd_id, roots)
            return self.select_share_video(files, title, season, episode, aliases=aliases)
        finally:
            _request_context.deadline = previous

    def __init__(self, openlist_client=None, *, storage: RemoteStore | None = None):
        self.ol = openlist_client
        self.storage = storage if storage is not None else (openlist_store(openlist_client) if openlist_client else None)
        self._vod_fid = None

    def get_cookie(self):
        return config.get_quark_cookie()

    # ── 就绪检查 ──────────────────────────────────────
    def is_ready(self):
        return bool(self.get_cookie())

    def ensure_ready(self):
        if not self.is_ready():
            raise TransferNotReady(
                "夸克网盘未授权或 Cookie 为空。请在 OpenList 管理面板重新授权夸克网盘。")

    def check_cookie_alive(self):
        if not self.get_cookie():
            return False, "QUARK_COOKIE is empty"
        try:
            response = self._request(self.QUARK_FILE_SORT + "&pdir_fid=0&_page=1&_size=1", method="GET", timeout=10)
            if response.get("code") != 0:
                raise TransferFailed("Cookie authentication failed")
            return True, "Cookie is valid"
        except Exception as e:
            return False, f"Cookie expired or invalid: {e}"

    # ── HTTP 基础调用 ─────────────────────────────────
    def _request(self, url, payload=None, method="GET", timeout=60):
        deadline = getattr(_request_context, 'deadline', None)
        if deadline is not None:
            remaining = deadline-time.monotonic()
            if remaining <= 0:
                raise TransferFailed('本轮转存检查超时')
            timeout = min(timeout, remaining)
        cookie = self.get_cookie()
        body = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(url, data=body, method=method)
        req.add_header("User-Agent", UA)
        req.add_header("Referer", "https://pan.quark.cn/")
        req.add_header("Origin", "https://pan.quark.cn")
        if payload is not None:
            req.add_header("Content-Type", "application/json")
        if cookie:
            req.add_header("Cookie", cookie)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(8 * 1024**2 + 1)
                if len(body) > 8 * 1024**2:
                    raise TransferFailed('上游响应超过大小限制')
                return json.loads(body.decode('utf-8'))
        except urllib.error.HTTPError as e:
            e.close()
            raise TransferFailed(f'upstream HTTP {e.code}') from None
        except Exception as e:
            raise TransferFailed(type(e).__name__) from None

    # ── 分享链接解析 ──────────────────────────────────
    @staticmethod
    def parse_share(share_url):
        """从分享链接提取 pwd_id 与可选的提取码。"""
        if not share_url:
            raise TransferFailed("分享链接为空")
        u = share_url.strip()
        parsed = urllib.parse.urlparse(u)
        values = urllib.parse.parse_qs(parsed.query)
        values.update(urllib.parse.parse_qs(parsed.fragment.lstrip('?')))
        pwd = (values.get('pwd') or values.get('passcode') or [''])[0]
        m = re.search(r"/s/([A-Za-z0-9]+)", u)
        if not m:
            raise TransferFailed(f"无法解析分享链接: {share_url[:80]}")
        return m.group(1), pwd

    def resolve_share(self, share_url):
        """解析分享链接，返回 (stoken, pwd_id, items)。"""
        self.ensure_ready()
        pwd_id, pwd = self.parse_share(share_url)

        # 1. 取 stoken
        resp = self._request(
            QUARK_SHARE_TOKEN,
            payload={"pwd_id": pwd_id, "passcode": pwd, "pwd": pwd},
            method="POST",
            timeout=20
        )
        if resp.get("code") != 0:
            raise TransferFailed(f"解析分享失败: {resp.get('message')}")
        stoken = (resp.get("data") or {}).get("stoken")
        if not stoken:
            raise TransferFailed("分享解析未返回 stoken")

        items = self._share_page_all(stoken, pwd_id, '0')
        if not items:
            raise TransferFailed('分享链接内没有文件')

        return stoken, pwd_id, items

    # ── 转存任务 ──────────────────────────────────────
    def save_share(self, share_url, to_pdir_fid="0"):
        """提交转存任务并等待完成，返回转存成功的文件列表。"""
        stoken, pwd_id, items = self.resolve_share(share_url)

        fid_list = [it["fid"] for it in items]
        fid_token_list = [it.get("share_fid_token", "") for it in items]

        save_payload = {
            "pwd_id": pwd_id,
            "stoken": stoken,
            "to_pdir_fid": str(to_pdir_fid or "0"),
            "fid_list": fid_list,
            "fid_token_list": fid_token_list,
            "pdir_fid": "0",
            "scene": "link"
        }

        save_resp = self._request(
            QUARK_SHARE_SAVE,
            payload=save_payload,
            method="POST",
            timeout=config.TRANSFER_TIMEOUT
        )
        if save_resp.get("code") != 0:
            raise TransferFailed(f"转存请求失败: {save_resp.get('message')}")

        task_id = (save_resp.get("data") or {}).get("task_id")
        if not task_id:
            return items

        # 轮询等待任务完成 (首轮立即查，次轮间隔 1s，减少无效盲等)
        start_t = time.time()
        first_poll = True
        while time.time() - start_t < config.TRANSFER_TIMEOUT:
            if not first_poll:
                time.sleep(1)
            first_poll = False
            t_url = f"{QUARK_TASK}&task_id={task_id}"
            t_resp = self._request(t_url, method="GET", timeout=15)
            t_data = t_resp.get("data") or {}
            st = t_data.get("status")
            if st == 2:
                break
            elif st == 3:
                raise TransferFailed(f"网盘转存后台任务失败: {t_data}")
        else:
            raise TransferFailed("网盘转存任务未在超时内完成")

        # 2026-10-15 重构：不再验证根目录出现（已改为直接转存到 /点播）
        # 转存到子目录的延迟更短，由 transfer() 负责验证目标路径

        return items

    # ── 目标目录管理 ──────────────────────────────────
    def ensure_vod_dir(self):
        """确保点播根目录存在，并返回夸克侧 fid（带内存缓存，消除重复查询延迟）。"""
        if getattr(self, "_vod_fid", None):
            return self._vod_fid
        if not self.storage:
            raise TransferNotReady("未注入远程存储后端，无法创建点播目录")
        if not self.storage.mkdir(config.QUARK_VOD_ROOT):
            raise TransferFailed("远程存储后端未确认创建点播目录")
        vod_fid = self._get_quark_fid(config.QUARK_VOD_ROOT)
        if not vod_fid:
            raise TransferFailed(f"无法获取 {config.QUARK_VOD_ROOT} 的夸克 fid")
        self._vod_fid = vod_fid
        return vod_fid

    def _get_quark_fid(self, path):
        """通过夸克 API 获取目录的 fid。path 格式如 /夸克网盘/点播"""
        # 拆分路径，逐级查询 fid
        parts = [p for p in path.split("/") if p]
        if not parts or parts[0] != "夸克网盘":
            return None
        # 夸克网盘是挂载名，实际从根目录开始查
        current_fid = "0"
        for part in parts[1:]:  # 跳过挂载名
            # Exhaust pages before declaring absence. Network/authentication
            # failures must propagate; treating them as missing deletes state.
            found = next((f.get("fid") for f in self._quark_list_fid(current_fid)
                          if f.get("name") == part), None)
            if not found:
                return None
            current_fid = found
        return current_fid

    # ── 夸克侧权威列表（绕过 OpenList 目录缓存）────────
    #
    # Why（2026-10-16 狼犬丹尼故障根因）：
    #   OpenList 对夸克驱动的 fs/list 带【目录缓存】，转存后长时间不刷新
    #   （实测不带 refresh 永远读旧缓存，带 refresh 又可能被权限拒绝）。
    #   原 _scan() 正是读这个缓存来判定"转存是否成功"，于是：
    #     文件已真实转存到 /点播 → 缓存里没有 → 轮询 180s 仍没有
    #     → 判定 failed → find_done_transfer 查不到 done → 播放退回搜索
    #     → 重复转存 → 网盘堆出 (1)(2) 副本 → 死循环。
    #   实测铁证：夸克官方 file/sort 直查能看到目录，OpenList 列表看不到，
    #   而 OpenList 加 refresh 后又能看到 → 证明是缓存而非数据问题。
    #   故转存结果判定必须改用夸克官方接口（实时、无缓存）。
    QUARK_FILE_SORT = "https://drive.quark.cn/1/clouddrive/file/sort?pr=ucpro&fr=pc"

    def _quark_list_fid(self, fid, size=200):
        """列出夸克目录内容（权威、实时）。fid="0" 为根目录。

        返回 [{"name", "is_dir", "size", "fid"}]，字段已归一化，
        与 OpenList 的 fs/list 返回结构保持一致的语义。
        """
        out = []
        page = 1
        while True:
            url = (f"{self.QUARK_FILE_SORT}&pdir_fid={fid}"
                   f"&_page={page}&_size={size}&_fetch_total=1"
                   f"&_fetch_sub_dirs=0"
                   f"&_sort=file_type:asc,updated_at:desc")
            resp = self._request(url, method="GET", timeout=25)
            if resp.get("code") != 0:
                raise TransferFailed(
                    f"夸克列目录失败: code={resp.get('code')} {resp.get('message')}")
            data = resp.get("data") or {}
            lst = data.get("list") or []
            for f in lst:
                out.append({
                    "name": f.get("file_name", ""),
                    "is_dir": bool(f.get("dir")),
                    "size": f.get("size", 0) or 0,
                    "fid": f.get("fid", ""),
                })
            total = data.get('_total', data.get('total'))
            if total is not None and len(out) >= int(total):
                break
            if total is None and len(lst) < size:
                break
            if not lst or page >= 50:
                raise TransferFailed('夸克目录分页未完整返回，无法判定文件缺失')
            page += 1
        return out

    def _quark_list(self, remote_path):
        """按 /夸克网盘/... 路径列目录（权威、实时）。

        目录不存在时抛 TransferFailed，调用方可据此判定"确实没有"。
        """
        fid = self._get_quark_fid(remote_path)
        if not fid:
            raise TransferFailed(f"夸克侧不存在该目录: {remote_path}")
        return self._quark_list_fid(fid)

    def rename_quark_file(self, fid, new_name):
        """调用夸克官方接口重命名文件。"""
        payload = {"fid": fid, "file_name": new_name}
        try:
            resp = self._request(QUARK_FILE_RENAME, payload=payload, method="POST")
            return resp.get("status") == 200 or resp.get("code") == 0
        except Exception as e:
            print(f"[QuarkTransfer] 重命名失败 fid={fid} -> {new_name}: {e}", flush=True)
            return False

    def sniff_and_fix_file(self, it, parent_path):
        """嗅探伪装格式视频（如 >50MB 的 .mp3/.pdf/.apk 或无后缀大文件），自动识别真实视频魔数并重命名扶正。"""
        fid = it.get("fid")
        name = it.get("name", "")
        size = it.get("size", 0)

        # 仅对大于 50MB 的非标准视频后缀文件进行嗅探
        if size < 50 * 1024 * 1024 or not fid:
            return None

        ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
        if ext in VIDEO_EXTS:
            return None

        # 优先通过 OpenList 直链（Range 0-15）低开销读取前 16 字节
        correct_ext = None
        real_path = f"{parent_path}/{name}"
        play_path = config.play_path(real_path)
        try:
            url = self.storage.direct_url(play_path) if self.storage else None
            if url:
                req = urllib.request.Request(url, headers={"Range": "bytes=0-15", "User-Agent": "Mozilla/5.0"})
                with urllib.request.urlopen(req, timeout=5) as resp:
                    head = resp.read(16)
                    correct_ext = detect_video_magic(head)
        except Exception:
            pass

        # 容错：若无法在线 Range，但属于高危伪装后缀且文件 > 200MB
        if not correct_ext:
            if ext in {".mkv1"}:
                correct_ext = ".mkv"
            elif ext in {".mp41"}:
                correct_ext = ".mp4"

        if correct_ext:
            if ext:
                new_name = name[:len(name) - len(ext)] + correct_ext
            else:
                new_name = name + correct_ext

            ok = self.rename_quark_file(fid, new_name)
            if ok:
                print(f"[QuarkTransfer] [格式扶正] 发现伪装视频: {name} ({size/1024/1024:.1f}MB) -> 自动重命名为: {new_name}", flush=True)
                if self.storage:
                    try:
                        self.storage.refresh_path_tree(config.play_path(parent_path))
                    except Exception:
                        pass
                return {"path": f"{parent_path}/{new_name}", "name": new_name, "size": size, "fid": fid}
        return None

    # ── 视频检索与集数匹配 ────────────────────────────
    def scan_videos(self, remote_dir, max_depth=3):
        """递归扫描目录下所有视频文件，返回 [{path, name, size, fid}]。

        2026-10-16 重构：改用夸克官方 API 列目录（实时），
        不再依赖 OpenList 的缓存列表（后者会误判转存失败）。
        集成伪装格式（如大体积 .mp3/.pdf 等）自动嗅探与重命名扶正。
        2026-10-19 修复：若传入的本身即为单个视频文件（非目录），直接返回自身，
        杜绝单片电影转存后因调用 _quark_list 误报“未在目录下找到任何视频文件”。
        """
        results = []

        # 1. 若 remote_dir 自身扩展名已是标准视频文件，直接作为单视频项返回
        ext_direct = ("." + remote_dir.rsplit(".", 1)[1].lower()) if "." in remote_dir else ""
        if ext_direct in VIDEO_EXTS:
            fname = remote_dir.rsplit("/", 1)[-1]
            return [{"path": remote_dir, "name": fname, "size": 0, "fid": None}]

        def _walk(path, depth):
            try:
                entries = self._quark_list(path)
            except Exception:
                return
            for it in entries:
                name = it["name"]
                p = f"{path}/{name}"
                if it["is_dir"]:
                    if depth < max_depth:
                        _walk(p, depth + 1)
                else:
                    ext = ("." + name.rsplit(".", 1)[1].lower()) if "." in name else ""
                    if ext in VIDEO_EXTS:
                        results.append({"path": p, "name": name, "size": it["size"], "fid": it.get("fid")})
                    elif it.get("size", 0) > 50 * 1024 * 1024:
                        fixed = self.sniff_and_fix_file(it, path)
                        if fixed:
                            results.append(fixed)

        _walk(remote_dir, 1)
        return results

    @staticmethod
    def extract_episode_num(path_or_name, season=1):
        """精准提取剧集集数（自动过滤分辨率、码率、年份干扰，适配各类压制组规范）。"""
        import os, re
        name = os.path.basename(path_or_name)
        stem, _ = os.path.splitext(name)

        # 1. 清洗常见画质、编码、分辨率等数字噪音标签
        #
        # 2026-11-08（电影源误判为剧集包根治）：
        #   旧规则按「单词」剥离标签，HDR10 → 残留 "10"、HDR10P → 残留 "10P"，
        #   再被规则 D 的独立数字模式识别成第 10 集；DDP5.1 剥成 "DDP ." 后
        #   残留的 "5 1" 被识别成第 5 集；分卷 "d3 1"/"d3 2" 被识别成 E1/E2。
        #   实测后果：含 50GB 完整正片的单部电影源被判为「多集剧集特征」整包拒绝
        #   （宗师叶问2 / 惊蛰无声 / 给阿嬷的情书 / 罗小黑战记 / 后室 /
        #     死侍与金刚狼 / 痴迷 共 7 部）。
        #   现在按「整词优先」清洗，HDR10P/HDR10/DD5.1/DDP5 1 等规格词整体抹除。
        clean = re.sub(
            r"(2160[pP]|1080[pP]|720[pP]|576[pP]|480[pP]|4[kK]|[hH]\.?26[45]|[xX]26[45]|hev[cs]|avc|"
            r"19\d\d|20\d\d|"
            r"[dD][dD][pP]?\s?\d(?:\.\d|\s\d)?|"
            r"(?:aac|ac3|eac3|truehd|dts(?:-?hd)?|flac|atmos)\s?\d(?:\.\d|\s\d)?|"
            r"[57]\.1|"
            r"\d{2,3}\s?[fF][pP][sS]|"
            r"8bit|10bit|12bit|"
            r"hdr1[04][pP]?|dv|hdr|dolby\s?vision|sdr|"
            r"web-dl|webrip|bluray|blu-ray|remux|bdrip|hdrip|"
            r"bd|hd|nf|amzn|\bit\b|ma|chte|cht|chs|"
            r"\bd\d+(?=\s+\d))",
            " ", stem, flags=re.IGNORECASE
        )

        # 规则 A: 显式包含目标季的 S{season}E{ep}
        pat_s_ep = rf"(?:[._\-\s]|^)S0*{season}E0*(\d+)(?:[._\-\s]|$)"
        m = re.search(pat_s_ep, clean, re.IGNORECASE)
        if m:
            return int(m.group(1))

        # 规则 B: 带有任意季的 S\d+E\d+（若指定了 season，季号不同则直接拒绝，防张冠李戴）
        m_any_s = re.search(r"(?:[._\-\s]|^)S0*(\d+)E0*(\d+)(?:[._\-\s]|$)", clean, re.IGNORECASE)
        if m_any_s:
            s_found = int(m_any_s.group(1))
            ep_found = int(m_any_s.group(2))
            if season == 0 or s_found == season:
                return ep_found
            return None

        # 规则 B2: 规格词残留兜底（在原始名上再剥一遍复合规格）
        #   "H.264" 剥掉 h264 后残留 "H. 264"；"4K_HDR_60" 剥离后残留 "_60"；
        #   单独成词的 "1080"（不带 p）也可能只是片名占位。这类残片会被
        #   规则 D 误读成集数，因此在进入宽松匹配前整体抹除。
        #   注意：仅作用于本规则链，不影响上面 S01E01 / 第xx集 等强证据匹配。
        clean = re.sub(
            r"(?<![A-Za-z0-9])[hH](?:\.\s*)?26[45](?![0-9])|"
            r"(?<![A-Za-z0-9])(?:2160|1080|720|576|480)(?![pP0-9])|"
            r"(?<![A-Za-z0-9])\d{2,3}[fF][pP][sS]|"
            r"(?<![A-Za-z0-9])[hH][dD][rR]1[04][pP]?",
            " ", clean, flags=re.IGNORECASE
        )
        clean = re.sub(r"[_\-\.\s]+", " ", clean).strip()

        # 规则 C: 第xx集 / 第xx话 / 第xx回 / EPxx
        patterns_explicit = [
            r"(?:第|EP|E)\s*0*(\d+)\s*(?:集|话|回|v\d+)?",
            r"0*(\d+)\s*(?:集|话|回)",
        ]
        for pat in patterns_explicit:
            m = re.search(pat, clean, re.IGNORECASE)
            if m:
                return int(m.group(1))

        # 规则 D: 连字符分隔、独立括号、空格包裹或带版本后缀的集数 (如 "- 04 [", " 04 ", " 05 4K", "01x", "02v2", "[04]", "05.mp4")
        patterns_sep = [
            r"(?:^|[-_—\s])0*(\d{1,4})(?:[xXvV]\d*|[-_—\[\]\(\)\.\s]|$)",
            r"(?:[-_—]\s*)0*(\d{1,4})(?:\s*[-_—\[\]\(\)\.]|\s*$)",
            r"(?:\[|\()0*(\d{1,4})(?:\]|\))",
            r"(?:^|\s+)0*(\d{1,4})(?:\s+|$)",
            r"(?:[._\-\s]|^)0*(\d{1,4})$"
        ]
        for pat in patterns_sep:
            m = re.search(pat, clean, re.IGNORECASE)
            if m:
                val = int(m.group(1))
                if 1 <= val <= 2500:
                    return val

        return None

    @classmethod
    def prefetch_episodes(cls, names, season=1):
        """AI 文件名解析预热（2026-11-13 试点）。

        对一组文件名做「一次批量」AI 复核并回填缓存，返回 {name: episode}。
        之后同一组文件在各处调用 extract_episode_num 时会直接命中缓存，
        不再逐条触发模型请求——这正是本设计避免 N×RT 的关键。

        AI 不可用/关闭/熔断时自动退回纯规则结果，语义与 extract_episode_num 一致。
        """
        try:
            return name_parser.parse_names_batch(names, season)
        except Exception as e:
            print(f"[{config.SERVICE_NAME}] [NameParser] 预热异常，回落规则: {e}", flush=True)
            return {}

    @classmethod
    def _lookup_ep(cls, path_or_name, season, prefetched=None):
        """供批量上下文使用：优先取预热结果，否则回退原规则。"""
        if prefetched:
            base = os.path.basename(path_or_name)
            if base in prefetched:
                return prefetched[base]
        return cls.extract_episode_num(path_or_name, season)

    @classmethod
    def match_episode_video(cls, vids, season=0, episode=0):
        """从视频列表中精确挑选目标剧集/电影（杜绝张冠李戴）。"""
        from .quality import video_rank
        if not vids:
            return None
        # 电影：选体积最大的最高画质正片
        if not season or not episode:
            # 严格防范短剧/剧集冒充电影：
            # 如果转存的目录包含多个不同集数的剧集特征（如 70 集短剧或 10 集电视剧），
            # 坚决拒绝从中随机抽一集充当电影！
            #
            # 2026-11-08：与 transfer 验票同口径的「强证据优先」。
            #   优先保底：只要存在 >= 500MB 且无集数标记的正片，即认定为本片电影，
            #   取其中体积最大者（杜绝 52GB 4K 正片被同包 d3 1/d3 2 分卷信号拖累）。
            # 2026-11-13：整组一次性 AI 复核（只复核规则 weak/none 分区），
            # 避免下面三处循环逐文件触发模型请求。
            ep1 = cls.prefetch_episodes([v["path"] for v in vids], season=1)
            big_unmarked = [v for v in vids
                            if (v.get("size") or 0) >= 500 * 1024 * 1024
                            and cls._lookup_ep(v["path"], 1, ep1) is None]
            if big_unmarked:
                return max(big_unmarked, key=video_rank)
            if len(vids) > 1:
                ep_counts = 0
                for v in vids:
                    ep_val = cls._lookup_ep(v["path"], 1, ep1)
                    if ep_val is not None and ep_val > 0:
                        ep_counts += 1
                if ep_counts >= 2 or (len(vids) >= 3 and ep_counts >= 1):
                    # 检测到明显的剧集/短剧大包特征，拒绝冒充单部电影
                    return None
            return max(vids, key=video_rank)

        # 剧集：必须精确匹配集数，同集多版本优先选体积最大的（如 4K HDR > 4K SDR）
        ep_s = cls.prefetch_episodes([v["path"] for v in vids if '_confirmed_episode' not in v], season=season)
        matched = []
        for v in vids:
            ep = (v['_confirmed_episode'] if '_confirmed_episode' in v
                  else cls._lookup_ep(v["path"], season, ep_s))
            if ep == episode:
                matched.append(v)

        if matched:
            best_match = max(matched, key=video_rank)
            # 电视剧防微短剧冒充底线校验：正规电视剧单集时长约 45 分钟，单集正常至少 80MB 以上
            # 若单集小于 80MB，通常为竖屏微短剧（单集 1 分钟 ~25MB），拒绝将其作为正统电视剧正片！
            if season > 0 and best_match["size"] < 80 * 1024 * 1024:
                print(f"[{config.SERVICE_NAME}] [微短剧底线拦截] 匹配到的视频单集体积仅有 {best_match['size']/1024/1024:.1f}MB，疑似为竖屏短剧，拒绝匹配电视剧正片！", flush=True)
                return None
            return best_match
        # 未匹配到该集数坚决返回 None，杜绝将其他集数错误冒充
        return None

    @classmethod
    def match_all_episodes(cls, vids, season=1):
        """扫描多集视频列表，提取出同季每一集最佳画质视频映射 {episode: video_dict}。"""
        ep_map = {}
        ep_all = cls.prefetch_episodes([v["path"] for v in vids], season=season)
        for v in vids:
            ep = cls._lookup_ep(v["path"], season, ep_all)
            if ep and ep > 0:
                # 剔除小于 80MB 的超小片头/短剧视频，保障整季批量激活全部为高清正片
                if v.get("size", 0) < 80 * 1024 * 1024:
                    continue
                if ep not in ep_map or v["size"] > ep_map[ep]["size"]:
                    ep_map[ep] = v
        return ep_map

    # ── 高层：一次完整转存 ────────────────────────────
    def transfer(self, cat, title, season, episode, share_url, share_pwd=""):
        """完整转存流程：转存 -> 移入点播目录 -> 匹配视频文件 -> 入库排队清理。

        返回 (transfer_id, video_remote_path)。
        """
        from .coordination import playback_lease, file_lock
        from .identity import Work
        work = Work(cat, title, season, episode)
        name = 'work:' + repr((work.cat, work.title.casefold(), work.season))
        with playback_lease(), file_lock(name, timeout=config.TRANSFER_TIMEOUT), _transfer_semaphore:
            prior_deadline = getattr(_request_context, 'deadline', None)
            _request_context.deadline = time.monotonic() + config.TRANSFER_TIMEOUT
            try:
                return self._do_transfer(*work.args, share_url, share_pwd)
            finally:
                _request_context.deadline = prior_deadline

    def _check_capacity(self, need_bytes=0):
        """容量预检：返回 (是否可转存, 剩余字节数)。

        2026-10-20 新增：全库实测发现网盘 100% 爆满时，转存不会立即报错，
        而是走到 save_share → task 轮询阶段才以 `remain_capacity: -154444449` 失败，
        导致 22 部电影级联失败且每次白耗 3~8 秒。此处提前拦截并要求至少留 500MB 余量。
        """
        try:
            d = self._request("https://drive.quark.cn/1/clouddrive/member?pr=ucpro&fr=pc",
                              method="GET").get("data", {})
            total = d.get("total_capacity", 0) or 0
            used = d.get("use_capacity", 0) or 0
            free = total - used
            SAFETY = 500 * 1024 * 1024
            return (free >= SAFETY and free >= need_bytes), free
        except Exception:
            # 查询失败不阻断，交给后续真实转存流程处理
            return True, 0

    def share_files(self, stoken, pwd_id, items, max_entries=1200, max_depth=6):
        """Enumerate share pages with parent IDs. Never mistake truncation for absence."""
        output = []
        seen = set()
        visited = 0

        def walk(entries, parent, path, depth):
            nonlocal visited
            for item in entries:
                visited += 1
                if visited > max_entries:
                    raise TransferFailed('分享内容超过检查上限')
                name = item.get('file_name', '')
                if not name or '/' in name or name in ('.', '..'):
                    continue
                if item.get('dir'):
                    if depth >= max_depth:
                        raise TransferFailed('分享目录超过检查深度')
                    fid = item.get('fid')
                    if not fid or fid in seen:
                        continue
                    seen.add(fid)
                    walk(self._share_page_all(stoken, pwd_id, fid), fid, path + [name], depth+1)
                elif is_candidate_video(name, item.get('size') or 0):
                    output.append(dict(item, parent_fid=parent, relative_path='/'.join(path+[name])))
        walk(items, '0', [], 0)
        return output

    def _share_page_all(self, stoken, pwd_id, parent):
        items = []
        for page in range(1, 51):
            query = urllib.parse.urlencode({'pwd_id':pwd_id,'stoken':stoken,'pdir_fid':parent,
                                           '_page':page,'_size':100,'_fetch_total':1})
            response = self._request(QUARK_SHARE_DETAIL + '&' + query, timeout=20)
            if response.get('code') != 0:
                raise TransferFailed('分享目录读取失败: ' + str(response.get('code')))
            data = response.get('data') or {}
            part = data.get('list') or []
            items.extend(part)
            total = data.get('_total', data.get('total'))
            if (total is not None and len(items) >= int(total)) or (total is None and len(part) < 100):
                return items
            if not part:
                break
        raise TransferFailed('分享目录分页不完整')

    def _wait_saved_task(self, task_id):
        deadline = time.monotonic() + config.TRANSFER_TIMEOUT
        while time.monotonic() < deadline:
            response = self._request(QUARK_TASK + '&task_id=' + urllib.parse.quote(task_id), timeout=15)
            if response.get('code') != 0:
                raise TransferFailed('转存任务查询失败')
            status = (response.get('data') or {}).get('status')
            if status == 2:
                return
            if status == 3:
                raise TransferTaskFailed('网盘转存任务失败')
            time.sleep(1)
        raise TransferNotReady('网盘转存仍未完成；重试时先核对原任务，不重复提交')

    def _resume_save(self, work, previous):
        """Resolve persisted save intent even if the public share later disappears."""
        target = previous['save_target']
        if not target or not target.startswith(config.QUARK_VOD_ROOT + '/') or '..' in target.split('/'):
            raise TransferNotReady('转存目标记录无效，停止自动重试')
        parent, name = target.rsplit('/',1)
        fid = self._get_quark_fid(parent)
        if not fid:
            raise TransferNotReady('原转存目录缺失，需核对原任务状态')
        def lookup():
            return next((f for f in self._quark_list_fid(fid) if not f['is_dir'] and f['name']==name),None)
        item = lookup()
        if item is None and previous.get('save_task_id'):
            try:
                self._wait_saved_task(previous['save_task_id'])
            except TransferTaskFailed:
                db.upsert_transfer(*work.args,status='failed',save_target=None,save_task_id=None,save_started_at=None)
                raise
            deadline = time.monotonic()+min(30,config.PROBE_VERIFY_TIMEOUT)
            while item is None and time.monotonic()<deadline:
                item=lookup()
                if item is None:
                    time.sleep(1)
        if item is None:
            raise TransferNotReady('上次提交尚未确认，未重复转存；请稍后重试')
        expected = previous.get('file_size') or 0
        if expected and item['size'] != expected:
            raise TransferNotReady('原任务文件大小不符，停止自动覆盖')
        tid = db.upsert_transfer(*work.args,status='done',remote_path=target,file_size=item['size'],error=None,
                                 save_target=None,save_task_id=None,save_started_at=None)
        db.enqueue_cleanup(tid,target,db.now()+config.KEEP_DAYS*86400)
        try:
            self.storage.refresh_path_tree(config.play_path(target))
        except Exception:
            pass
        return tid,target

    def select_share_video(self, files, title, season, episode, *, aliases=()):
        from .search import SearchResult, score_search_result, _extract_season_from_name
        from .identity import split_title
        def trusted_parent(parent):
            # Only a complete work label from this enumerated path supplies
            # missing basename identity; arbitrary substring matches do not.
            core, year = split_title(title)
            label = re.sub(r'^[A-Za-z]\s+', '', parent).strip()
            if not label.startswith(core):
                return False
            rest = label[len(core):]
            if rest and rest[0] not in ' ._-([（【':
                return False
            years = re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)', rest)
            if year and any(value != year for value in years):
                return False
            # Do not erase arbitrary descriptions: another work, sequel,
            # adaptation or special edition must keep this context untrusted.
            rest = re.sub(r'(?<!\d)(?:19|20)\d{2}(?!\d)', '', rest)
            rest = re.sub(r'(?i)4k|2160p?|1080p?|720p?|HDR\d*|WEB[- ]?DL|HEVC|AAC|国语中字|国语|中字|高码率|单集\s*\d+(?:\.\d+)?\s*[GM]B?|全\s*\d+\s*集|德剧|美剧|英剧|韩剧|日剧|国产剧', '', rest)
            return not rest.strip(' ._-()[]（）【】')
        videos=[]
        for item in files:
            confirmed_episode = None
            path=item.get('relative_path') or item['file_name']
            file_title = item['file_name']
            wanted_year=split_title(title)[1]
            named_years=re.findall(r'(?<!\d)((?:19|20)\d{2})(?!\d)',item['file_name'])
            # Later seasons may carry their own release year, distinct from series premiere.
            parents=path.split('/')[:-1]
            indicated=[_extract_season_from_name(parent) for parent in parents]
            explicit_season = _extract_season_from_name(item['file_name'])
            season_year_allowed = season > 1 and (explicit_season == season or season in indicated)
            if wanted_year and named_years and wanted_year not in named_years and not season_year_allowed:
                continue
            if wanted_year and named_years and wanted_year not in named_years and season_year_allowed:
                file_years = {int(value) for value in named_years}
                if max(file_years) < 2010 or abs(max(file_years) - int(wanted_year)) > 10:
                    continue
            if season and any(value and value!=season for value in indicated):
                continue
            if season and explicit_season is not None and explicit_season != season:
                continue
            if season and episode:
                # Variant subdirectories belong to the same file's identity;
                # an exact ancestor must not hide a conflicting descendant.
                variant_parent = False
                for parent in parents:
                    if re.search(r'(?i)(?<![A-Za-z])(?:Anime|Movie|OVA|OAD|Special|Live[ ._-]*Action|Part\s*\d+)(?![A-Za-z])', parent):
                        variant_parent = True
                    sequel_parent = re.sub(r'(?i)(?<!\d)(?:19|20)\d{2}(?!\d)|\b(?:Season\s*\d+|S\d+|2160p?|1080p?|720p?|4k)\b', '', parent)
                    if re.search(r'(?i)[A-Za-z][ ._-]+[1-9]\d?\s*$', sequel_parent):
                        variant_parent = True
                if variant_parent:
                    continue
                file_title = os.path.splitext(file_title)[0]
                # Reject combined episodes before trimming any identity text.
                episode_tokens = re.findall(r'(?i)(?<![A-Za-z])EP?0*\d{1,4}\b|(?<=\d)EP?0*\d{1,4}\b', file_title)
                if len(episode_tokens) > 1:
                    continue
                release_suffix = ''
                token = re.search(r'(?i)S0*(\d{1,2})[ ._-]*EP?0*(\d{1,4})\b', file_title)
                if token:
                    if (int(token.group(1)), int(token.group(2))) != (season, episode):
                        continue
                    if re.match(r'(?i)[ ._-]*EP?0*\d{1,4}\b', file_title[token.end():]):
                        continue
                    release_suffix = file_title[token.end():]
                    file_title = file_title[:token.start()]
                else:
                    episode_match = re.search(r'(?i)\bEP?0*(\d{1,4})\b', file_title)
                    if episode_match and int(episode_match.group(1)) != episode:
                        continue
                    if episode_match:
                        release_suffix = file_title[episode_match.end():]
                        file_title = file_title[:episode_match.start()]
                if re.match(r'\s*[-~～+]\s*\d{1,4}\b', release_suffix):
                    continue
                suffix_identity = re.sub(r'国语中字|简体中字|繁体中字|中文字幕|国语|中字', '', release_suffix)
                if (re.search(r'(?i)(?<![A-Za-z])(?:Anime|Movie|OVA|OAD|Special|Live[ ._-]*Action|Part\s*\d+)(?![A-Za-z])', suffix_identity)
                        or re.search(r'[一-龥\uac00-\ud7af\u3040-\u30ff]', suffix_identity)):
                    continue
                season_match = re.search(r'(?i)(?:第\s*\d+\s*季|Season\s*\d{1,2}|\bS0*\d{1,2}\b)', file_title)
                if season_match:
                    file_title = file_title[:season_match.start()] + file_title[season_match.end():]
                file_title = re.sub(r'(?i)(?:^|[ ._-])(?:20\d{2}|19\d{2})(?=$|[ ._-])', ' ', file_title)
                file_title = re.sub(r'(?i)(?:\b4k\b|\b2160p?\b|\b1080p?\b|\b720p?\b|\bhdr\d*\b|\bweb[- ]?dl\b|\bbluray\b|\bremux\b)', '', file_title)
                file_title = file_title.strip(' ._-()[]')
                if file_title:
                    score = SearchResult(engine='panshare', source='filename', name=file_title,
                                         year='', poster='', episodes=[])
                    if score_search_result(score, title, year=wanted_year, season=season,
                                           episode=episode, aliases=aliases) < 30:
                        # Unknown Latin translation or numeric episode labels
                        # may inherit an exact directory's identity. Known
                        # conflicting Chinese names/versions never do.
                        unknown_name = not re.search(r'[一-龥\uac00-\ud7af\u3040-\u30ff]', file_title)
                        explicit_variant = re.search(r'(?i)\b(?:OVA|OAD|SP|Special|Movie|Anime|Live[ ._-]*Action)\b|\d', file_title)
                        numeric_episode = re.fullmatch(r'0*\d{1,4}', file_title)
                        if numeric_episode:
                            unknown_name = int(file_title) == episode
                            explicit_variant = False
                        parent_identity = any(trusted_parent(p) for p in parents)
                        conflicting_parent = any(re.search(r'[一-龥]', p) and not trusted_parent(p)
                                                 and not re.fullmatch(r'第\s*\d+\s*季', p) for p in parents)
                        # A translated basename loses its title at the episode
                        # token, but its tail must still be technical metadata.
                        # Do not silently discard another work/adaptation/part.
                        safe_suffix = re.sub(r'(?i)-(?:LeagueWEB)$', '', release_suffix)
                        safe_suffix = re.sub(r'(?i)(?<![A-Za-z0-9])(?:WEB[- ]?DL|WEBRIP|BLURAY|REMUX|2160p?|1080p?|720p?|4k|HDR\d*|HEVC|AVC|AAC|H\.?26[45]|X26[45]|DDP?\d?(?:\.\d)?)(?![A-Za-z0-9])', '', safe_suffix)
                        suffix_is_metadata = not safe_suffix.strip(' ._-()[]')
                        if not (unknown_name and not explicit_variant and parent_identity and not conflicting_parent and suffix_is_metadata):
                            continue
                        if numeric_episode:
                            # The exact parent and numeric leaf already prove
                            # this episode. Do not let the weak-name AI stage
                            # overwrite that deterministic context later.
                            confirmed_episode = episode
            video={'path':path,'name':item['file_name'],'size':item.get('size') or 0,'share':item}
            if confirmed_episode is not None:
                video['_confirmed_episode'] = confirmed_episode
            videos.append(video)
        if not season:
            features=[v for v in videos if v['size']>=350*1024**2]
            matched=[]
            for video in features:
                candidate=SearchResult(engine='panshare',source='filename',name=video['name'],year='',poster='',episodes=[])
                if score_search_result(candidate, title, season=0, episode=0, aliases=aliases) >= 30:
                    matched.append(video)
            if matched:
                videos=matched
            elif len(features)>1:
                raise TransferFailed('电影分享含多个无法确认身份的正片，拒绝按体积猜选')
        return self.match_episode_video(videos,season,episode)

    def _do_transfer(self, cat, title, season, episode, share_url, share_pwd=''):
        """Save one selected video into a deterministic work/source directory.

        A durable save intent plus task ID prevents blind duplicate saves after a
        timeout/restart. Readback uses exact names inside this work's directory.
        No whole-package copying, filename prefix guesses or speculative sibling
        activation. Repeated requests reuse the saved leaf before any write.
        """
        import hashlib
        from .identity import Work
        work = Work(cat, title, season, episode)
        self.ensure_ready()
        previous = db.get_transfer(*work.args)
        previous = dict(previous) if previous else {}
        if previous.get('save_started_at'):
            return self._resume_save(work, previous)
        tid = db.upsert_transfer(*work.args, status='searching', share_url=share_url, share_pwd=share_pwd,
                                 pan_type='quark', error=None)
        try:
            stoken, pwd_id, root_items = self.resolve_share(share_url)
            files = self.share_files(stoken, pwd_id, root_items)
            search_title, aliases = strm_manager.library_search_identity(work.cat, work.title, work.season)
            best = self.select_share_video(files, search_title, season, episode, aliases=aliases)
            if not best or (not season and best['size'] < 350 * 1024**2):
                raise TransferFailed('分享内没有匹配的正片或目标集')
            selected = best['share']
            identity = repr((work.cat, work.title, season, pwd_id, selected.get('fid'))).encode()
            slot = config.QUARK_VOD_ROOT + '/vod_' + hashlib.sha256(identity).hexdigest()[:24]
            self.ensure_vod_dir()
            if not self.storage.mkdir(slot):
                raise TransferFailed('远程存储后端未确认创建转存目录')
            fid = self._get_quark_fid(slot)
            if not fid:
                raise TransferFailed('无法确认转存目标目录')
            target = slot + '/' + selected['file_name']

            def existing():
                found = [f for f in self._quark_list_fid(fid) if not f['is_dir'] and f['name'] == selected['file_name']]
                if len(found) > 1:
                    raise TransferFailed('转存目录同名文件不唯一')
                if found and int(found[0].get('size') or 0) == int(selected.get('size') or 0):
                    return found[0]
                if found:
                    raise TransferFailed('已有同名文件大小不符，保留并停止自动覆盖')
                return None

            saved = existing()
            if not saved:
                ok, _ = self._check_capacity(best['size'])
                if not ok:
                    raise TransferFailed('网盘剩余空间不足以保存所选视频')
                db.upsert_transfer(*work.args,status='saving',save_target=target,save_started_at=db.now(),save_task_id=None,file_size=best['size'])
                payload = {'pwd_id':pwd_id,'stoken':stoken,'to_pdir_fid':str(fid),
                           'fid_list':[selected['fid']], 'fid_token_list':[selected.get('share_fid_token','')],
                           'pdir_fid':str(selected.get('parent_fid','0')), 'scene':'link'}
                response = self._request(QUARK_SHARE_SAVE,payload=payload,method='POST',timeout=30)
                if response.get('code') != 0:
                    db.upsert_transfer(*work.args,save_started_at=None,save_target=None,save_task_id=None)
                    raise TransferFailed('转存请求被拒绝: ' + str(response.get('code')))
                task_id = (response.get('data') or {}).get('task_id')
                if task_id:
                    db.upsert_transfer(*work.args,save_task_id=str(task_id))
                if task_id:
                    self._wait_saved_task(str(task_id))
                deadline = time.monotonic() + min(30, config.PROBE_VERIFY_TIMEOUT)
                while time.monotonic() < deadline:
                    saved = existing()
                    if saved:
                        break
                    time.sleep(1)
                if not saved:
                    raise TransferNotReady('任务已提交，目标视频尚未可见；稍后重试将只核对原任务')
            # A disguised filename may be corrected only after inspecting it.
            ext = os.path.splitext(saved['name'])[1].lower()
            if ext not in VIDEO_EXTS:
                fixed = self.sniff_and_fix_file(saved, slot)
                if fixed:
                    target = fixed['path']
            db.upsert_transfer(*work.args,status='done',remote_path=target,file_size=best['size'],error=None,
                               save_target=None,save_task_id=None,save_started_at=None)
            db.enqueue_cleanup(tid,target,db.now()+config.KEEP_DAYS*86400)
            try:
                self.storage.refresh_path_tree(config.play_path(target))
            except Exception:
                pass
            return tid,target
        except Exception as exc:
            if isinstance(exc, TransferTaskFailed):
                db.upsert_transfer(*work.args,save_started_at=None,save_target=None,save_task_id=None)
            db.upsert_transfer(*work.args,status='failed',error=type(exc).__name__ + ': ' + str(exc)[:180])
            raise

    # ── 删除 ──────────────────────────────────────────
    def delete_remote(self, remote_path):
        if not self.storage:
            raise TransferNotReady("未注入远程存储后端")
        if not self.storage.remove(remote_path):
            raise TransferFailed("远程存储后端未确认删除")
        return True
