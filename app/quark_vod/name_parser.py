#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 文件名语义解析兜底（Name Parser with AI Fallback）
================================================================================
背景（为什么需要它）
    红线 7（末路兼职 S0E0）与红线 8（7 部电影被误杀）的共同根因是同一个：
    `extract_episode_num` 的宽松规则 D 把「规格残片」读成集数——
    HDR10 → 10、4K_HDR_60 → 60、DDP5 1 → 5、H.264 残留 → 264、分卷 d3 1 → E1。
    发布组的命名方式是无界的，而正则是有限的：每踩一个坑补一条规则，
    新规则又可能反噬老样本。这类工作是「语义判断」，不该由写死的代码独自承担。

设计原则（改动前务必先读懂这四条）
    ┌──────────────────────────────────────────────────────────────────────┐
    │ 1. 规则负责确定性，AI 只负责判断力。                                  │
    │    AI 从不推翻规则的硬结论，只在规则本来就拿不准的地方介入。          │
    │ 2. 分区铁律（AI 的战场只有两个）：                                    │
    │      strong  规则 A/C 命中（S04E37 / 第64集 / EP12）→ 直接采信，不送  │
    │      conflict 规则 B 季号冲突 → 直接采信 None，不送（跨季隔离铁律）  │
    │      weak    规则 D 弱匹配命中 → 送 AI 复核（事故主灾区）            │
    │      none    全部未命中 → 送 AI 确认（防漏判真实集数）               │
    │ 3. 按「分享包」批量调用，绝不逐文件调用。                             │
    │    实测 12 个文件名一次调用仅 2.56s；逐文件调用会放大成 40×RT 的灾难。 │
    │ 4. 任何异常一律回落规则结果。模型挂了，系统行为必须退化为改造前。     │
    └──────────────────────────────────────────────────────────────────────┘

调用方须知
    `parse_names_batch()` 是唯一的对外入口，返回 {原始文件名: 集数或 None}。
    返回值与 `extract_episode_num(name, season)` 语义完全一致，可直接替换使用。
    未提供批量上下文的单条查询请走 `parse_one()`，它将走缓存 + 单条调用。
"""

import os
import re
import sys
import json
import time
import hashlib
import threading
import urllib.request

from . import config
from . import db

# ── 进程内状态 ─────────────────────────────────────────
_mem = {}                      # cache_key -> episode_or_None（本进程内存缓存）
_lock = threading.Lock()
_fail_streak = 0               # 连续失败次数，用于熔断
_cooldown_until = 0.0          # 熔断冷却截止时间戳
_stats = {"calls": 0, "cache_hits": 0, "rule_decided": 0,
          "ai_decided": 0, "shadow_diffs": 0, "failures": 0}


def _log(msg):
    print(f"[{config.SERVICE_NAME}] [NameParser] {msg}", flush=True)


# ══════════════════════════════════════════════════════════════════════════
# 一、规则分级（复用 transfer.py 的同一套正则，保证口径完全一致）
# ══════════════════════════════════════════════════════════════════════════

# 规格词清洗：与 transfer.extract_episode_num 第一步保持完全一致。
# 注意：两处必须同步修改，否则「规则分级」与「规则结果」会出现口径漂移。
from .episode_rules import (_NOISE_RE, _RESIDUE_RE, _WEAK_PATTERNS,
                            _clean_stem, classify_by_rules)


# ══════════════════════════════════════════════════════════════════════════
# 二、缓存
# ══════════════════════════════════════════════════════════════════════════
def _cache_key(name, season):
    return hashlib.sha1(f"{os.path.basename(name)}|{season}".encode("utf-8")).hexdigest()


def _cache_get_many(names, season):
    """内存 → SQLite 两级缓存查询。返回 {name: (hit: bool, episode)}"""
    out = {}
    keys = {n: _cache_key(n, season) for n in names}
    with _lock:
        for n, k in keys.items():
            if k in _mem:
                out[n] = (True, _mem[k])
    missing = [n for n in names if n not in out]
    if missing:
        try:
            disk = db.get_name_parse_cache([keys[n] for n in missing])
        except Exception:
            disk = {}
        with _lock:
            for n in missing:
                k = keys[n]
                if k in disk:
                    _mem[k] = disk[k]
                    out[n] = (True, disk[k])
    return out


def _cache_put_many(rows, season):
    """rows = {name: (episode, confidence)}"""
    try:
        db.put_name_parse_cache(
            [( _cache_key(n, season), os.path.basename(n), season, ep, conf)
             for n, (ep, conf) in rows.items()])
    except Exception as e:
        _log(f"缓存写入失败（不影响主流程）: {e}")
    with _lock:
        for n, (ep, _c) in rows.items():
            _mem[_cache_key(n, season)] = ep


# ══════════════════════════════════════════════════════════════════════════
# 三、模型调用
# ══════════════════════════════════════════════════════════════════════════
_SYSTEM_PROMPT = (
    "你是影视文件名解析器。严格只输出 JSON 数组，不要任何解释、不要 Markdown 围栏。"
)

_USER_TMPL = (
    "目标季号：{season}\n"
    "判断下列每个文件名标出的「第几集」：\n"
    "1) 规格词不是集数：HDR10 / HDR10P / DDP5.1 / DD5 1 / H.264 / H.265 / 1080p / 2160p / "
    "60fps / 分卷 d3 1 / d3 2 / 8bit / 10bit / WEB-DL / 5.1 等一律不是集数。\n"
    "2) 普通电影名、无集数的正片一律返回 null。\n"
    "3) 只有明确的集数标记才算：S03E07、第12集、EP05、[04]、E28、01.mp4 等。\n"
    "4) 季号与目标季号不符时返回 null（跨季不算本季集数）。\n"
    "5) 拿不准就给低置信度，不要猜。\n"
    "6) 必须对输入列表中的【每一个】文件名都输出一条，不得省略、不得合并，"
    "条数与输入完全相等。\n"
    "每个元素输出：{{\"i\":序号,\"file_name\":原文件名原样复制,\"episode\":整数或null,"
    "\"confidence\":0到1之间的数}}\n"
    "文件列表：\n{names}"
)


def _load_token():
    return config.load_cliproxy_token()


def _strip_fence(raw):
    raw = (raw or "").strip()
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    return raw.strip()


def _ask_model(names, season):
    """单次批量请求。成功返回 {下标: (episode, confidence)}；失败抛异常。"""
    payload = {
        "model": config.AI_PARSE_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _USER_TMPL.format(
                season=season, names=json.dumps(names, ensure_ascii=False))},
        ],
        "max_tokens": config.AI_PARSE_MAX_TOKENS,
        "temperature": 0,
    }
    if config.AI_PARSE_REASONING_EFFORT:
        payload["reasoning_effort"] = config.AI_PARSE_REASONING_EFFORT
    req = urllib.request.Request(
        f"{config.AI_PARSE_BASE_URL.rstrip('/')}/chat/completions",
        data=json.dumps(payload).encode("utf-8"), method="POST")
    tok = _load_token()
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=config.AI_PARSE_TIMEOUT) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    msg = data.get("choices", [{}])[0].get("message", {})
    content = (msg.get("content") or "").strip()
    if not content:
        raise ValueError("模型返回空内容")
    arr = json.loads(_strip_fence(content))
    if not isinstance(arr, list):
        raise ValueError("模型输出不是 JSON 数组")

    # ── 硬校验：按 file_name 归位、下标完整、值域合法、置信度达标 ──
    #
    # 2026-11-13 实测：模型偶尔「从 1 开始编号」或漏掉首项（12 项只回 9 项且
    # i 整体偏移）。若纯按下标采信，会把 A 文件的集数错挂到 B 文件上——这是比
    # 集数识别错误更危险的错位污染。故改为：
    #   ① 优先按 file_name 精确归位（下标只作一致性校验）；
    #   ② 无 file_name 时才按下标归位；
    #   ③ 最终仍要求每个输入都有对应结果，否则整批作废 → 回落规则。
    by_name = {}
    for item in arr:
        if not isinstance(item, dict):
            continue
        fn = item.get("file_name")
        if isinstance(fn, str) and fn in names and fn not in by_name:
            by_name[fn] = item

    out = {}
    for idx, nm in enumerate(names):
        item = by_name.get(nm)
        if item is None:
            # 回退：按下标归位，但必须校验下标与内容无冲突
            cand = next((x for x in arr if isinstance(x, dict)
                         and x.get("file_name") in (None, "") and _safe_int(x.get("i")) == idx), None)
            item = cand
        if item is None:
            continue
        ep = item.get("episode")
        try:
            conf = float(item.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if ep is None:
            out[idx] = (None, conf)
            continue
        try:
            ep = int(ep)
        except (TypeError, ValueError):
            out[idx] = (None, conf)
            continue
        # 值域不符或置信度不足 → 一律判 None（宁可回落规则，也不采信模糊判断）
        if not (1 <= ep <= 2500) or conf < config.AI_PARSE_MIN_CONFIDENCE:
            out[idx] = (None, conf)
        else:
            out[idx] = (ep, conf)
    # 任一输入没有拿到结果 → 本次请求不可靠，整批作废（防止错位污染）
    if len(out) != len(names):
        raise ValueError(f"模型输出条目不完整 ({len(out)}/{len(names)})")
    return out


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _circuit_open():
    return time.time() < _cooldown_until


def _record_failure(e):
    global _fail_streak, _cooldown_until
    _stats["failures"] += 1
    _fail_streak += 1
    if _fail_streak >= config.AI_PARSE_MAX_FAILS:
        _cooldown_until = time.time() + config.AI_PARSE_COOLDOWN
        _log(f"连续失败 {_fail_streak} 次，熔断 {config.AI_PARSE_COOLDOWN}s，期间回落纯规则")
        _fail_streak = 0
    _log(f"模型调用失败，回落规则: {type(e).__name__}: {e}")


def _record_success():
    global _fail_streak
    _fail_streak = 0


# ══════════════════════════════════════════════════════════════════════════
# 四、对外入口
# ══════════════════════════════════════════════════════════════════════════
def parse_names_batch(names, season=1):
    """批量解析文件名集数（唯一对外入口）。

    返回 {原始文件名: 集数或 None}，与 extract_episode_num(name, season) 语义一致。
    保证：返回值中每一项都必然存在（不会因 AI 失败而缺项）。
    """
    names = list(dict.fromkeys(n for n in names if n))
    if not names:
        return {}
    _stats["calls"] += 1

    result = {}
    pending = []          # [(name, rule_ep, tier)]，需要 AI 判断的
    for n in names:
        try:
            ep, tier = classify_by_rules(n, season)
        except Exception:
            ep, tier = None, "none"
        if tier in ("strong", "conflict"):
            _stats["rule_decided"] += 1
            result[n] = ep
        else:
            pending.append((n, ep, tier))

    if not pending:
        return result

    # 1) 缓存命中
    cached = _cache_get_many([n for n, _e, _t in pending], season)
    unresolved = []
    for n, rule_ep, tier in pending:
        hit, ep = cached.get(n, (False, None))
        if hit:
            _stats["cache_hits"] += 1
            result[n] = ep
        else:
            unresolved.append((n, rule_ep, tier))

    if not unresolved:
        return result

    # 影子模式：只观测，不采纳，最终仍以规则结果为准
    if config.AI_PARSE_SHADOW:
        _shadow_observe(unresolved, season)
        for n, rule_ep, _t in unresolved:
            result[n] = rule_ep
        return result

    if not config.AI_PARSE_ENABLED or _circuit_open():
        for n, rule_ep, _t in unresolved:
            result[n] = rule_ep
        return result

    # 2) 分片批量调用模型
    to_cache = {}
    bs = max(1, config.AI_PARSE_BATCH_SIZE)
    for i in range(0, len(unresolved), bs):
        chunk = unresolved[i:i + bs]
        chunk_names = [n for n, _e, _t in chunk]
        try:
            answers = _ask_model(chunk_names, season)
            _record_success()
        except Exception as e:
            _record_failure(e)
            for n, rule_ep, _t in chunk:
                result[n] = rule_ep
            continue
        _stats["ai_decided"] += len(chunk)
        for idx, (n, rule_ep, tier) in enumerate(chunk):
            ai_ep, conf = answers.get(idx, (None, 0.0))
            result[n] = ai_ep
            to_cache[n] = (ai_ep, conf)

    # 3) 回填缓存
    if to_cache:
        _cache_put_many(to_cache, season)
    return result


def _shadow_observe(unresolved, season):
    """影子模式：调用模型并与规则结果比对，只写日志与独立缓存，不改变任何行为。"""
    if not config.AI_PARSE_ENABLED or _circuit_open():
        return
    bs = max(1, config.AI_PARSE_BATCH_SIZE)
    for i in range(0, len(unresolved), bs):
        chunk = unresolved[i:i + bs]
        try:
            answers = _ask_model([n for n, _e, _t in chunk], season)
            _record_success()
        except Exception as e:
            _record_failure(e)
            return
        rows = {}
        for idx, (n, rule_ep, tier) in enumerate(chunk):
            ai_ep, conf = answers.get(idx, (None, 0.0))
            rows[n] = (ai_ep, conf)
            if ai_ep != rule_ep:
                _stats["shadow_diffs"] += 1
                _log(f"[影子分歧] name={n!r} tier={tier} rule={rule_ep} ai={ai_ep} conf={conf:.2f}")
        # 影子结果单独落库（origin=shadow），供人工审查，绝不参与生效
        try:
            db.put_name_parse_cache(
                [(_cache_key(n, season), os.path.basename(n), season, ep, c)
                 for n, (ep, c) in rows.items()], origin="shadow")
        except Exception:
            pass


def parse_one(name_or_path, season=1):
    """单条查询：缓存优先，缓存未命中且规则拿不准时才发一次模型请求。"""
    return parse_names_batch([name_or_path], season).get(name_or_path)


def stats():
    with _lock:
        return dict(_stats)


def reset_stats():
    with _lock:
        for k in _stats:
            _stats[k] = 0
