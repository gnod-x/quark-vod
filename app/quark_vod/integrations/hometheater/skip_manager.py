#!/usr/bin/env python3
"""Jellyfin skip rules through the Intro Skipper API; never writes online SQLite."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import argparse
import datetime
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from quark_vod import config

BASE_DIR = paths.BASE_DIR
PRESETS_FILE = paths.SKIP_PRESETS_FILE
JELLYFIN_API_BASE = host_config._value("JELLYFIN_API_BASE", config.JELLYFIN2_API)
SERVER = "jellyfin2"
HOST_CHAPS_DIR = os.path.join(paths.JELLYFIN2_METADATA_DIR, "chapters")
CONTAINER_CHAPS_DIR = "/config/metadata/chapters"
ENABLE_CHAPTER_THUMBNAILS = False


def writes_enabled():
    """Plugin writes require a separately verified, explicit host opt-in."""
    return host_config._value("INTRO_SKIPPER_WRITE_ENABLED", "0") == "1"


def disabled_result():
    return {"status": "disabled", "reason": "INTRO_SKIPPER_WRITE_ENABLED is not 1",
            "tagged": 0, "total": 0, "server": SERVER}


def _api(method, path, payload=None):
    if method.upper() not in {"GET", "HEAD"} and not writes_enabled():
        raise PermissionError("Intro Skipper writes are disabled")
    key = config.get_jellyfin2_key() if SERVER == "jellyfin2" else host_config._secret("JELLYFIN_KEY")
    if not key:
        raise RuntimeError("Jellyfin skip integration has no credential")
    request = urllib.request.Request(JELLYFIN_API_BASE + path,
        data=json.dumps(payload).encode() if payload is not None else None,
        method=method, headers={"Authorization": 'MediaBrowser Token="' + key + '"',
                                "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        body = response.read(4 * 1024**2 + 1)
        if len(body) > 4 * 1024**2:
            raise ValueError("oversized Jellyfin API response")
        return json.loads(body) if body else {}


def episode_items(series_name=None, season=None):
    query = {"Recursive": "true", "IncludeItemTypes": "Episode",
             "Fields": "Path,RunTimeTicks,SeriesName", "Limit": 500, "StartIndex": 0}
    results = []
    while True:
        body = _api("GET", "/Items?" + urllib.parse.urlencode(query))
        rows = body.get("Items") if isinstance(body, dict) else None
        total = body.get("TotalRecordCount")
        if not isinstance(rows, list) or type(total) is not int:
            raise ValueError("incomplete Jellyfin episode inventory")
        results.extend(rows)
        query["StartIndex"] += len(rows)
        if query["StartIndex"] >= total:
            break
        if not rows or query["StartIndex"] > 20000:
            raise ValueError("episode inventory incomplete or budget exceeded")
    return [x for x in results if (series_name is None or x.get("SeriesName") == series_name)
            and (season is None or x.get("ParentIndexNumber") == season)]


def read_strm_url(strm_path):
    if not strm_path:
        return None
    roots = ((config.JELLYFIN_LIBRARY_ROOT.rstrip("/") + "/", config.CINEMA2_DIR),
             (host_config._value("VOD_HOST_JELLYFIN_LIBRARY_ROOT", "/cinema").rstrip("/") + "/", paths.CINEMA_DIR))
    for prefix, host_root in roots:
        if strm_path.startswith(prefix):
            strm_path = os.path.join(host_root, strm_path[len(prefix):])
            break
    path = Path(strm_path)
    if not any(path.resolve().is_relative_to(Path(root).resolve()) for _, root in roots):
        return None
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 4096:
        return None
    url = path.read_text().strip()
    parsed = urllib.parse.urlsplit(url)
    allowed = urllib.parse.urlsplit(config.PENDING_BASE_URL)
    # ffprobe must never follow an arbitrary URL from a tampered STRM.
    if parsed.scheme not in {"http", "https"} or (parsed.scheme, parsed.hostname, parsed.port) != (allowed.scheme, allowed.hostname, allowed.port):
        return None
    if parsed.path not in {"/play", "/pending"}:
        return None
    return url


def probe_duration_fast(url):
    # Header-limited probe on the already configured playback entry point.
    probe = host_config._value("VOD_HOST_FFPROBE_BINARY", "ffprobe")
    result = subprocess.run([probe,
        "-v", "quiet", "-print_format", "json", "-show_format",
        "-probesize", "1048576", "-analyzeduration", "1048576", url],
        capture_output=True, timeout=15, check=False)
    try:
        duration = float(json.loads(result.stdout)["format"]["duration"])
        return duration if math.isfinite(duration) and duration > 0 else None
    except (ValueError, KeyError, TypeError):
        return None


def _segments(item_id):
    body = _api("GET", "/MediaSegments/" + urllib.parse.quote(str(item_id), safe=""))
    rows = body.get("Items") if isinstance(body, dict) else None
    if not isinstance(rows, list):
        raise ValueError("invalid media-segment response")
    return rows


def write_episode_timestamps(cur_jf, cur_is, item_id, op=None, ed=None, provider="Hometheater", video_url=None):
    # Positional cursor arguments remain accepted for old callers but are never used.
    if not writes_enabled():
        raise PermissionError("Intro Skipper writes are disabled")
    existing = _segments(item_id)
    added = 0
    for kind, interval in (("Intro", op), ("Outro", ed)):
        if not interval:
            continue
        start, end = map(float, interval)
        if not all(math.isfinite(x) for x in (start, end)) or start < 0 or end <= start:
            raise ValueError("invalid skip interval")
        ticks = (round(start * 10000000), round(end * 10000000))
        same_type = [s for s in existing if s.get("Type") == kind]
        if any((s.get("StartTicks"), s.get("EndTicks")) == ticks for s in same_type):
            continue
        if same_type:
            raise RuntimeError("existing segment differs; edit it in Intro Skipper before replacing")
        # This endpoint belongs to Intro Skipper. Native Jellyfin MediaSegments is read-only.
        path = "/MediaSegmentsApi/" + urllib.parse.quote(str(item_id), safe="")
        path += "?" + urllib.parse.urlencode({"providerId": provider})
        _api("POST", path, {"ItemId": item_id, "Type": kind,
            "StartTicks": ticks[0], "EndTicks": ticks[1]})
        current = _segments(item_id)
        if not any(s.get("Type") == kind and (s.get("StartTicks"), s.get("EndTicks")) == ticks for s in current):
            raise RuntimeError("segment accepted but not yet projected; retry on next schedule")
        added += 1
    return added


def apply_series_rule(series_name, season=None, op_start=0.0, op_end=90.0, ed_duration=90.0,
                      ed_start=None, probe_duration=True, with_thumbnails=None):
    if not writes_enabled():
        return disabled_result()
    episodes = episode_items(series_name, season)
    tagged = 0
    for item in episodes:
        op = (op_start, op_end) if op_end > op_start else None
        ticks = item.get("RunTimeTicks")
        duration = float(ticks) / 10000000 if ticks else None
        if duration is None and ed_duration and probe_duration:
            url = read_strm_url(item.get("Path"))
            if url:
                duration = probe_duration_fast(url)
        ed = None
        if ed_start is not None and ed_duration:
            ed = (ed_start, ed_start + ed_duration)
        elif ed_duration and duration and duration > ed_duration:
            ed = (duration - ed_duration, duration)
        # Unknown duration never authorizes an invented 24/60-minute outro.
        write_episode_timestamps(None, None, item["Id"], op, ed, provider="HometheaterRules")
        tagged += 1
    return {"tagged": tagged, "total": len(episodes), "server": SERVER}


def load_presets():
    try:
        value = json.loads(Path(PRESETS_FILE).read_text())
        return value if isinstance(value, dict) else {}
    except FileNotFoundError:
        return {}


def save_presets(presets):
    if not isinstance(presets, dict):
        raise ValueError("presets must be an object")
    path = Path(PRESETS_FILE)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + ".tmp")
    temp.write_text(json.dumps(presets, ensure_ascii=False, indent=2) + "\n")
    temp.replace(path)


def apply_all_presets():
    if not writes_enabled():
        return disabled_result()
    results = []
    for name, rule in load_presets().items():
        allowed = {k: v for k, v in rule.items() if k in
                   {"season", "op_start", "op_end", "ed_duration", "ed_start"}}
        results.append(apply_series_rule(name, **allowed))
    return results


def get_skip_status_json():
    try:
        items = episode_items()
        counts = {}
        deadline = time.monotonic() + 4
        checked = 0
        for item in items:
            if checked >= 100 or time.monotonic() >= deadline:
                break
            checked += 1
            key = (item.get("SeriesName") or "", item.get("ParentIndexNumber") or 0)
            row = counts.setdefault(key, {"name": key[0], "season": key[1], "total": 0, "tagged": 0})
            row["total"] += 1
            row["tagged"] += int(bool(_segments(item["Id"])))
        rows = list(counts.values())
        total = sum(x["total"] for x in rows)
        tagged = sum(x["tagged"] for x in rows)
        return {"code": 0, "msg": "ok", "data": rows, "total": total, "tagged": tagged,
                "coverage_pct": round(100 * tagged / total, 1) if total else 0, "server": SERVER,
                "partial": checked < len(items), "inventory_total": len(items)}
    except Exception as exc:
        return {"code": 1, "msg": type(exc).__name__, "data": [], "coverage_pct": 0, "server": SERVER}


def show_status():
    print(json.dumps(get_skip_status_json(), ensure_ascii=False))


def heal_chapters():
    # Jellyfin and its plugin own projection. There are no shadow SQL tables to heal.
    return {"status": "managed_by_api"}


def extract_missing_thumbnails(*args, **kwargs):
    return {"status": "jellyfin_managed", "reason": "no direct database chapter mutation"}


def auto_tag_all():
    if not writes_enabled():
        return disabled_result()
    from quark_vod.integrations.hometheater.sync_aniskip import scan_all_anime
    anime = scan_all_anime()
    return {"anime": anime, "presets": apply_all_presets()}


def analyze_subtitle_timing(srt_path, default_op_end=90.0, default_ed_duration=90.0):
    """
    通过字幕时间轴分析精算片头结束时间和片尾开始时间。

    片头检测逻辑：
    - 欧美剧通常冷开场（正片对话立即开始），然后出现无对白的 title card（字幕空白）
    - 国产剧通常开头有纯片头音乐段（字幕为空），然后正片对白开始
    - 统一策略：在前 1/4 时长内找「持续 ≥15s 的字幕稀疏段」，稀疏段结束处即为片头结束
    - 若无明显稀疏段，降级为默认值

    片尾检测：从结尾向前找字幕密度骤降点（正片结束处）
    """
    import re as _re
    try:
        with open(srt_path, 'r', encoding='utf-8', errors='ignore') as f:
            raw = f.read()

        # 提取所有字幕条目的开始时间（秒），每隔一个取（start timestamp）
        all_ts = []
        for m in _re.finditer(
            r"([0-9]{2}):([0-9]{2}):([0-9]{2})[,.]([0-9]{3})",
            raw
        ):
            h, mn, s, ms = int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))
            all_ts.append(h * 3600 + mn * 60 + s + ms / 1000.0)

        # 只保留 start timestamps（每对中的第一个）
        timestamps = all_ts[::2]

        if len(timestamps) < 5:
            return None, None

        total_dur = timestamps[-1]
        if total_dur < 120:
            return None, None

        avg_density = len(timestamps) / total_dur  # 条/秒

        # ---- 片头检测：找「持续 ≥15s 的字幕稀疏段（密度 < 均值10%）」 ----
        # 用 5s 步进的 15s 窗口扫描，找稀疏段结束时间点
        op_end_detected = None
        scan_limit = min(total_dur * 0.3, 210.0)  # 扫描前 30% 或最多 210s
        sparse_start = None

        t = 0.0
        while t <= scan_limit:
            cnt = sum(1 for ts in timestamps if t <= ts < t + 15.0)
            density = cnt / 15.0
            is_sparse = density < avg_density * 0.15  # 极低密度窗口

            if is_sparse:
                if sparse_start is None:
                    sparse_start = t
            else:
                if sparse_start is not None:
                    sparse_duration = t - sparse_start
                    if sparse_duration >= 15.0:
                        # 找到持续稀疏段，结束点即为片头结束
                        op_end_detected = round(t, 1)
                        break
                    sparse_start = None  # 短暂稀疏，不算
            t += 5.0

        if op_end_detected is None:
            # 无明显稀疏段：检查最早字幕时间，若 >10s 则视为片头在此之前
            first_ts = timestamps[0] if timestamps else 0.0
            if first_ts > 10.0:
                op_end_detected = round(first_ts - 2.0, 1)
            else:
                op_end_detected = default_op_end

        # ---- 片尾检测：从后向前找字幕密度从正常→稀疏的转折点 ----
        ed_start_detected = None
        scan_from = total_dur * 0.65  # 从 65% 处往后扫

        # 以 30s 窗口，5s 步进从结尾向前
        t = total_dur - 30.0
        while t > scan_from:
            cnt_here = sum(1 for ts in timestamps if t <= ts < t + 30.0)
            cnt_prev = sum(1 for ts in timestamps if t - 30.0 <= ts < t)
            if cnt_prev > 3 and cnt_here < cnt_prev * 0.35:
                # 密度骤降：正片结束处
                ed_start_detected = round(t + 5.0, 1)
                break
            t -= 5.0

        if ed_start_detected is None:
            ed_start_detected = round(max(0.0, total_dur - default_ed_duration), 1)

        return op_end_detected, ed_start_detected

    except Exception:
        return None, None


def find_episode_subtitle(ep_path, cinema_base=None):
    """
    根据剧集的 strm 路径查找对应的本地字幕文件（.srt/.ass/.ssa）。
    strm 文件在 cinema 目录下，字幕与 strm 同目录同名。
    """
    if not ep_path:
        return None
    # ep_path 可能是 /cinema/xxx/S01/xxx.strm 或实际 strm 路径
    base = os.path.splitext(ep_path)[0]
    for ext in ('.zh.default.srt', '.zh.srt', '.chs.srt', '.srt', '.ass', '.ssa'):
        candidate = base + ext
        if os.path.exists(candidate):
            return candidate
    # 在同目录下模糊匹配（防止文件名不完全一致）
    ep_dir = os.path.dirname(ep_path)
    ep_stem = os.path.splitext(os.path.basename(ep_path))[0]
    if os.path.isdir(ep_dir):
        for fn in os.listdir(ep_dir):
            if fn.startswith(ep_stem) and fn.endswith(('.srt', '.ass', '.ssa')):
                return os.path.join(ep_dir, fn)
    return None



def main(argv=None):
    global SERVER, JELLYFIN_API_BASE, HOST_CHAPS_DIR
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--server", choices=["jellyfin", "jellyfin2"], default="jellyfin2")
    commands = parser.add_subparsers(dest="command")
    for name in ["status", "auto-tag", "apply-presets"]:
        commands.add_parser(name)
    manual = commands.add_parser("set")
    manual.add_argument("name")
    manual.add_argument("--season", type=int)
    manual.add_argument("--op-start", type=float, default=0)
    manual.add_argument("--op-end", type=float, default=90)
    manual.add_argument("--ed-duration", type=float, default=90)
    manual.add_argument("--ed-start", type=float)
    manual.add_argument("--save-preset", action="store_true")
    args = parser.parse_args(argv)
    SERVER = args.server
    JELLYFIN_API_BASE = config.JELLYFIN2_API if SERVER == "jellyfin2" else host_config._value("JELLYFIN_API_BASE", paths.TRADITIONAL_API)
    HOST_CHAPS_DIR = os.path.join(paths.JELLYFIN2_METADATA_DIR if SERVER == "jellyfin2" else os.path.join(paths.JELLYFIN_CONFIG, "metadata"), "chapters")
    if args.command == "set":
        rule = {key: getattr(args, key) for key in ["season", "op_start", "op_end", "ed_duration", "ed_start"]}
        result = apply_series_rule(args.name, **rule)
        if args.save_preset:
            presets = load_presets()
            presets[args.name] = rule
            save_presets(presets)
    elif args.command == "auto-tag":
        result = auto_tag_all()
    elif args.command == "apply-presets":
        result = apply_all_presets()
    else:
        result = get_skip_status_json()
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
