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
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config
from . import db
from . import strm_manager

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


class TransferNotReady(Exception):
    """Cookie 未配置或写能力未就绪。"""


class TransferFailed(Exception):
    """转存过程失败。"""


class QuarkTransfer:
    def __init__(self, openlist_client=None):
        self.ol = openlist_client
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
            self._quark_list_fid("0", size=1)
            return True, "Cookie is valid"
        except Exception as e:
            return False, f"Cookie expired or invalid: {e}" 

    # ── HTTP 基础调用 ─────────────────────────────────
    def _request(self, url, payload=None, method="GET", timeout=60):
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
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            raw = e.read().decode("utf-8", "ignore")
            raise TransferFailed(f"HTTP {e.code}: {raw[:200]}")
        except Exception as e:
            raise TransferFailed(f"{type(e).__name__}: {e}")

    # ── 分享链接解析 ──────────────────────────────────
    @staticmethod
    def parse_share(share_url):
        """从分享链接提取 pwd_id 与可选的提取码。"""
        if not share_url:
            raise TransferFailed("分享链接为空")
        u = share_url.strip()
        pwd = ""
        if "#" in u:
            u, frag = u.split("#", 1)
            q = urllib.parse.parse_qs(urllib.parse.urlparse(frag).query)
            if q.get("pwd"):
                pwd = q["pwd"][0]
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

        # 2. 取文件列表与 fid_token
        det_url = (f"{QUARK_SHARE_DETAIL}&pwd_id={pwd_id}"
                   f"&stoken={urllib.parse.quote(stoken)}&pdir_fid=0&_page=1&_size=100")
        det_resp = self._request(det_url, method="GET", timeout=20)
        if det_resp.get("code") != 0:
            raise TransferFailed(f"获取分享详情失败: {det_resp.get('message')}")
        items = (det_resp.get("data") or {}).get("list", [])
        if not items:
            raise TransferFailed("分享链接内没有文件")

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

        # 2026-10-15 重构：不再验证根目录出现（已改为直接转存到 /点播）
        # 转存到子目录的延迟更短，由 transfer() 负责验证目标路径

        return items

    # ── 目标目录管理 ──────────────────────────────────
    def ensure_vod_dir(self):
        """确保点播根目录存在，并返回夸克侧 fid（带内存缓存，消除重复查询延迟）。"""
        if getattr(self, "_vod_fid", None):
            return self._vod_fid
        if not self.ol:
            raise TransferNotReady("OpenList 客户端未注入，无法建目录")
        self.ol.mkdir(config.QUARK_VOD_ROOT)
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
            url = (f"https://drive.quark.cn/1/clouddrive/file/sort"
                   f"?pr=ucpro&fr=pc&pdir_fid={current_fid}"
                   f"&_page=1&_size=100&_fetch_total=1&_fetch_sub_dirs=0"
                   f"&_sort=file_type:asc,updated_at:desc")
            try:
                resp = self._request(url, method="GET", timeout=20)
                files = resp.get("data", {}).get("list", [])
                found = None
                for f in files:
                    if f.get("file_name") == part:
                        found = f.get("fid")
                        break
                if not found:
                    return None
                current_fid = found
            except Exception:
                return None
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
            total = data.get("_total") or 0
            # 翻页终止：本页为空 / 已取满 / 安全上限（防异常时死循环）
            if not lst or len(out) >= total or page >= 50:
                break
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
            url = self.ol.direct_url(play_path) if self.ol else None
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
            elif ext in DISGUISE_EXTS and size > DISGUISE_MIN_SIZE:
                # 常见规避和谐伪装，默认为 .mkv
                correct_ext = ".mkv"

        if correct_ext:
            if ext:
                new_name = name[:len(name) - len(ext)] + correct_ext
            else:
                new_name = name + correct_ext

            ok = self.rename_quark_file(fid, new_name)
            if ok:
                print(f"[QuarkTransfer] [格式扶正] 发现伪装视频: {name} ({size/1024/1024:.1f}MB) -> 自动重命名为: {new_name}", flush=True)
                if self.ol:
                    try:
                        self.ol.refresh_path_tree(config.play_path(parent_path))
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
    def match_episode_video(cls, vids, season=0, episode=0):
        """从视频列表中精确挑选目标剧集/电影（杜绝张冠李戴）。"""
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
            big_unmarked = [v for v in vids
                            if (v.get("size") or 0) >= 500 * 1024 * 1024
                            and cls.extract_episode_num(v["path"], season=1) is None]
            if big_unmarked:
                return max(big_unmarked, key=lambda x: x["size"])
            if len(vids) > 1:
                ep_counts = 0
                for v in vids:
                    ep_val = cls.extract_episode_num(v["path"], season=1)
                    if ep_val is not None and ep_val > 0:
                        ep_counts += 1
                if ep_counts >= 2 or (len(vids) >= 3 and ep_counts >= 1):
                    # 检测到明显的剧集/短剧大包特征，拒绝冒充单部电影
                    return None
            return max(vids, key=lambda x: x["size"])

        # 剧集：必须精确匹配集数，同集多版本优先选体积最大的（如 4K HDR > 4K SDR）
        matched = []
        for v in vids:
            ep = cls.extract_episode_num(v["path"], season=season)
            if ep == episode:
                matched.append(v)

        if matched:
            best_match = max(matched, key=lambda x: x["size"])
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
        for v in vids:
            ep = cls.extract_episode_num(v["path"], season=season)
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
        with _transfer_semaphore:
            return self._do_transfer(cat, title, season, episode, share_url, share_pwd)

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

    def _do_transfer(self, cat, title, season, episode, share_url, share_pwd=""):
        tid = db.upsert_transfer(cat, title, season, episode,
                                 status="searching", share_url=share_url,
                                 share_pwd=share_pwd, pan_type="quark",
                                 error=None)
        try:
            self.ensure_ready()
            vod_fid = self.ensure_vod_dir()

            # ── 容量预检（2026-10-20）：爆满时立即失败，不走 save+task 轮询 ──
            ok_cap, free_b = self._check_capacity()
            if not ok_cap:
                raise TransferFailed(
                    f"夸克网盘空间不足（剩余 {free_b/1024**3:.2f}GB），"
                    f"请先清理 /点播 目录或扩容")

            # ── 铁闸 1: 网盘已落盘资源复用与防重检查（彻底消灭重复转存与 (1)(2) 垃圾副本！）──
            from .search import sanitize_title
            clean_t = sanitize_title(title).lower()
            try:
                cur_entries = self._quark_list(config.QUARK_VOD_ROOT)
            except Exception:
                cur_entries = []

            for cur_e in cur_entries:
                en_name = cur_e.get("name", "")
                en_path = f"{config.QUARK_VOD_ROOT}/{en_name}"
                # 检查网盘中是否已有该片相关的已落盘目录
                if clean_t and clean_t in en_name.lower():
                    existing_vids = self.scan_videos(en_path)
                    best_existing = self.match_episode_video(existing_vids, season, episode)
                    if best_existing and best_existing.get("path"):
                        # 网盘已存活有效正片！直接复用已有文件，0 耗费空间，绝不调用 save_share！
                        print(f"[{config.SERVICE_NAME}] [防重生效] 网盘已存在 {title} 正片: {best_existing['path']}，直接复用，0 重复转存！", flush=True)
                        # 2026-10-21 修复（复用路径不落 DB）：原实现此处直接 return，
                        # 跳过了 _do_transfer 末尾的 upsert_transfer(status='done')。
                        # 若该片历史记录是 failed/cleaned（如早前网盘爆满转存失败），
                        # 记录会永久停在失败态，find_done_transfer 查不到 →
                        # 下次点播重复跑 8~12s 搜索（实测《万米危机 (2026)》）。
                        # 复用同样是"文件确已落盘"，必须把记录同步为 done。
                        try:
                            db.upsert_transfer(cat, title, season, episode,
                                               status="done",
                                               remote_path=best_existing["path"],
                                               file_size=best_existing.get("size") or 0,
                                               error=None)
                        except Exception as ex:
                            print(f"[{config.SERVICE_NAME}] [防重生效][DB同步异常] "
                                  f"{title} S{season}E{episode}: {ex}", flush=True)
                        return tid, best_existing["path"]

            # ── 铁闸 2: 转存前强制只读轻量验票（0 空间消耗，杜绝短剧冒充与牛头不对马嘴！）──
            stoken, pwd_id, share_items = self.resolve_share(share_url)
            # 2026-10-20 修复：原实现只展开一层子目录，遇到「分享根 → 片名目录 →
            # 集数区间目录 → 视频文件」这类三层结构时，视频文件全在第二层，
            # 验票看到 0 个视频文件，把活着的 4K 大包误判为
            # 「分享包内未包含任何标准视频文件」而拒绝转存。
            # 实测《凡人修仙传》分享 1fd0ea7e2bb3 根目录仅 1 个 dir，
            # 其下还有 177-193 / 194- 两个子目录才到 mkv 文件。
            # 现改为递归展开（限深 4 层、限 400 项，兼顾性能与风控）。
            inspect_items = []
            seen_fids = set()

            def _collect(fid_list, depth):
                if depth > 4 or len(inspect_items) >= 400:
                    return
                for fid in fid_list:
                    if not fid or fid in seen_fids:
                        continue
                    seen_fids.add(fid)
                    try:
                        det_url = (f"{QUARK_SHARE_DETAIL}&pwd_id={pwd_id}"
                                   f"&stoken={urllib.parse.quote(stoken)}&pdir_fid={fid}&_page=1&_size=100")
                        sub_resp = self._request(det_url, method="GET", timeout=10)
                        sub_list = (sub_resp.get("data") or {}).get("list", [])
                    except Exception:
                        continue
                    inspect_items.extend(sub_list)
                    sub_dirs = [x.get("fid") for x in sub_list if x.get("dir") and x.get("fid")]
                    if sub_dirs:
                        _collect(sub_dirs, depth + 1)

            # 根级 fid=0 一次拿全，再逐层递归
            try:
                root_url = (f"{QUARK_SHARE_DETAIL}&pwd_id={pwd_id}"
                            f"&stoken={urllib.parse.quote(stoken)}&pdir_fid=0&_page=1&_size=100")
                root_resp = self._request(root_url, method="GET", timeout=10)
                root_list = (root_resp.get("data") or {}).get("list", [])
            except Exception:
                root_list = share_items
            inspect_items.extend(root_list)
            _collect([x.get("fid") for x in root_list if x.get("dir") and x.get("fid")], 1)

            valid_vids = [
                fit for fit in inspect_items
                if not fit.get("dir") and is_candidate_video(fit.get("file_name", ""), fit.get("size"))
            ]
            if not valid_vids:
                raise TransferFailed("分享包内未包含任何标准视频文件，拒绝转存")

            if not season or not episode:
                # 电影验票门槛：
                # A. 必须包含单个体积 >= 350MB 的正片大文件（排除短剧、广告碎片）
                large_vids = [v for v in valid_vids if (v.get("size") or 0) >= 350 * 1024 * 1024]
                if not large_vids:
                    max_mb = (max(v.get('size', 0) for v in valid_vids) / 1024 / 1024) if valid_vids else 0
                    raise TransferFailed(f"分享包内无电影正片大文件(最大仅 {max_mb:.1f}MB)，疑似短剧或广告碎片，拒绝转存")
                # B. 若包含多个带集数标记的文件，判定为剧集大包，拒绝冒充单片电影
                #
                # 2026-11-08（7 部电影被误杀根治）：改为「强证据优先」。
                #   旧实现只要 3 个以上文件命中任一弱集数信号即整包拒绝，而
                #   extract_episode_num 在宽松模式(season=1)下会把规格残片读成集数：
                #     "4K_HDR_60" → 60、"d3 1"(分卷碟号) → 1、"…H 264-…" → 264。
                #   结果：含 50GB 完整正片的单部电影源被判为剧集包（宗师叶问2 /
                #   惊蛰无声 / 给阿嬷的情书 / 罗小黑战记 / 后室 / 死侍与金刚狼 / 痴迷）。
                #   现在先看强证据：存在「非集数标记且 >= 350MB」的单片即放行；
                #   只有全部视频都带集数标记时，才按剧集包拒绝（真短剧源如此）。
                _unmarked = [v for v in large_vids
                             if self.extract_episode_num(v.get("file_name", ""), 1) is None]
                if _unmarked:
                    pass  # 强证据：包内存在明确单片正片，允许作为电影转存
                elif len(valid_vids) >= 3 and any(
                        self.extract_episode_num(v.get("file_name", ""), 1) for v in valid_vids):
                    raise TransferFailed("该分享包包含多集剧集特征，拒绝作为电影转存")
            else:
                # 剧集验票门槛：必须包含目标集数！
                has_target_ep = any(self.extract_episode_num(v.get("file_name", ""), season=season) == episode for v in valid_vids)
                if not has_target_ep:
                    raise TransferFailed(f"分享包内未找到 S{season:02d}E{episode:02d} 对应文件，拒绝转存")

            # ── 验票通过，开始安全转存 ──
            db.upsert_transfer(cat, title, season, episode, status="saving")
            saved_items = self.save_share(share_url, to_pdir_fid=vod_fid)

            # 2. 回读确认转存结果 —— 必须查【夸克官方 API】，不能用 OpenList 列表
            #
            #    2026-10-16 重构（狼犬丹尼故障根因）：
            #    原实现用 self.ol.list(QUARK_VOD_ROOT) 判定，而 OpenList 对该目录
            #    带缓存且长期不刷新。实测：文件已真实落盘 /点播/狼犬丹尼 1080p remux (2005)，
            #    OpenList 不带 refresh 的列表恒为 18 项（无此目录），
            #    加 refresh 才变 19 项（有）——证明是缓存问题，不是转存失败。
            #    后果：误判 failed → 反复重试 → 网盘堆 (1)(2) 副本 → 播放永远走不到直链。
            #    现在改查夸克官方 file/sort：实时、无缓存、不依赖 OpenList refresh 权限。
            #
            #    注：仍保留长窗口轮询，因为夸克侧自身也可能有短暂可见性延迟。
            moved_paths = []
            for item in saved_items:
                fname = item["file_name"]

                def _scan():
                    """列 /点播 现有条目，按文件名匹配（含 (n) 变体）。

                    返回匹配到的实际路径，未匹配返回 None。
                    """
                    try:
                        entries = self._quark_list(config.QUARK_VOD_ROOT)
                    except Exception:
                        return None
                    for it in entries:
                        n = it.get("name", "")
                        if not n:
                            continue
                        # 精确 / 带 (n) 后缀 / 以 fname 为前缀的扩展名
                        if (n == fname or n.startswith(fname + "(")
                                or n.startswith(fname + ".")):
                            return f"{config.QUARK_VOD_ROOT}/{n}"
                    return None

                matched = None
                deadline = time.time() + config.PROBE_VERIFY_TIMEOUT
                first_scan = True
                while time.time() < deadline:
                    if not first_scan:
                        time.sleep(1)
                    first_scan = False
                    hit = _scan()
                    if hit:
                        matched = hit
                        break

                if not matched:
                    raise TransferFailed(
                        f"转存后未在 {config.QUARK_VOD_ROOT} 找到 {fname}"
                        f"（已等待 {config.PROBE_VERIFY_TIMEOUT}s，"
                        f"文件实际应已由夸克侧转存，请手动核实目录内容）")
                moved_paths.append(matched)

            # 3. 扫描并选出对应集数的视频文件
            all_vids = []
            for mp in moved_paths:
                all_vids.extend(self.scan_videos(mp))

            if not all_vids:
                raise TransferFailed(f"转存成功但未在目录下找到任何视频文件 ({moved_paths})")

            best_vid = self.match_episode_video(all_vids, season, episode)
            if not best_vid:
                raise TransferFailed(f"未能匹配到 S{season:02d}E{episode:02d} 对应的视频文件")

            video_remote_path = best_vid["path"]
            video_size = best_vid.get("size") or 0

            # 3.2 伪装头前置嗅探感知：若存在假 PNG 头，由零拷贝平移流清洗代理透明平移播放，严禁误杀好源
            if self.ol:
                try:
                    probe_url = self.ol.direct_url(video_remote_path)
                    req_h = urllib.request.Request(probe_url, headers={"Range": "bytes=0-31"})
                    with urllib.request.urlopen(req_h, timeout=3) as resp_h:
                        head_b = resp_h.read()
                        if is_poisoned_fake_header(head_b):
                            print(f"[{config.SERVICE_NAME}] [伪装头感知] 检测到 8 字节混淆头，将由零拷贝清洗代理透明平移播放: {video_remote_path}", flush=True)
                except Exception:
                    pass

            # 3.5 主动同步 OpenList QuarkTV 驱动目录树缓存（彻底根治转存后直链 500 秒退）
            if self.ol:
                try:
                    for mp in moved_paths:
                        self.ol.refresh_path_tree(config.play_path(mp))
                    self.ol.refresh_path_tree(config.play_path(video_remote_path))
                    print(f"[{config.SERVICE_NAME}] [缓存同步] OpenList 播放目录树已逐级刷新: {config.play_path(video_remote_path)}", flush=True)
                except Exception as ex:
                    print(f"[{config.SERVICE_NAME}] [缓存同步告警] OpenList 目录树刷新失败: {ex}", flush=True)

            # 4. 登记完成与过期清理任务
            db.upsert_transfer(cat, title, season, episode,
                               status="done", remote_path=video_remote_path,
                               file_size=video_size, error=None)

            due = db.now() + config.KEEP_DAYS * 86400
            for mp in moved_paths:
                db.enqueue_cleanup(tid, mp, due)

            # 5. 剧集整包协同激活：若该分享包包含同季多集视频，顺带批量激活整包集数
            if season > 0:
                try:
                    ep_map = self.match_all_episodes(all_vids, season=season)
                    for other_ep, other_vid in ep_map.items():
                        if other_ep != episode:
                            try:
                                other_rpath = other_vid["path"]
                                db.upsert_transfer(cat, title, season, other_ep,
                                                   status="done", remote_path=other_rpath,
                                                   file_size=other_vid.get("size") or 0, error=None)
                                # 拼接播放直链并原子激活对应集数的 strm（后台限流刷新，不抢首播带宽）
                                enc = urllib.parse.quote(config.play_path(other_rpath))
                                d_url = f"{config.OPENLIST_PUBLIC}/d{enc}"
                                strm_manager.activate(cat, title, season, other_ep, d_url, background_batch=True)
                                print(f"[{config.SERVICE_NAME}] [整季连带激活] {title} S{season}E{other_ep} -> {d_url}", flush=True)
                            except Exception:
                                pass
                except Exception:
                    pass

            return tid, video_remote_path

        except TransferNotReady as e:
            db.upsert_transfer(cat, title, season, episode,
                               status="failed", error=f"NOT_READY: {e}")
            raise
        except Exception as e:
            db.upsert_transfer(cat, title, season, episode,
                               status="failed", error=str(e)[:300])
            raise
        finally:
            # 2026-10-20：任何异常路径都不得把记录留在 searching（半途状态），
            # 否则 find_done_transfer 查不到、selfheal 也无法识别，形成僵尸记录。
            # 2026-10-25 修复：原判定只覆盖 searching，漏了验票通过后写入的
            # saving —— 一旦 save_share / 目录轮询在 saving 之后异常，
            # 记录会永久滞留（实测 ID 3285/3287 滞留 116 小时）。
            try:
                row = db._conn().execute(
                    "SELECT status FROM transfers WHERE cat=? AND title=? AND season=? AND episode=?",
                    (cat, title, season, episode)).fetchone()
                if row and row["status"] in ("searching", "saving"):
                    db.upsert_transfer(cat, title, season, episode,
                                       status="failed", error="[状态自愈] 转存中断，未完成")
            except Exception:
                pass

    # ── 删除 ──────────────────────────────────────────
    def delete_remote(self, remote_path):
        if not self.ol:
            raise TransferNotReady("OpenList 客户端未注入")
        return self.ol.remove(remote_path)
