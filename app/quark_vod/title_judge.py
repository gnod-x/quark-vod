#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
搜索候选标题语义复核（Title Judge with AI Fallback）
================================================================================
背景
    `search.score_search_result` 用约 190 行正则做「候选标题 vs 目标作品」判断，
    并为每个后缀补丁维护 `LEGAL_SUFFIX_TOKENS` 白名单。这条路线永远追不上：
      · `苍兰诀(剧版)`      → 同一作品的发行形态
      · `唐朝诡事录之长安`  → 同一作品的篇章
      · `恶人传记`          → 借主片名词根另起的**不同作品**
    三者字面结构相同（主名 + 后缀），只能靠语义区分。

设计原则（与 name_parser.py 同一套哲学）
    1. **AI 只是复核，不是重新打分**。规则分数原样保留，AI 只能对「边界分数」
       做上浮/下调，且幅度受限，绝不推翻规则给出的 100 分强匹配。
    2. **一次搜索只发一次模型请求**：在 `match_best()` 集中调用，
       绝不挂到 `score_search_result()`（那是逐条热路径，会被放大成 N 次）。
    3. **只复核边界区间**（默认 15~60 分）：低于门槛的、以及规则已判 85/100 分的
       完全不进模型——前者救不活，后者没必要。
    4. 任何异常一律返回原规则结果。
"""

import os
import sys
import json
import time
import threading
import urllib.request

from . import config

# ── 进程内状态 ─────────────────────────────────────────
_lock = threading.Lock()
_mem = {}                      # cache_key -> override(分数增减量)
_fail_streak = 0
_cooldown_until = 0.0
_stats = {"calls": 0, "judged": 0, "overrides": 0, "failures": 0, "cache_hits": 0}

# 边界区间：只有落在此区间的候选才送模型。
#
# 2026-11-13 实测校准：85 分档（`_is_legal_title_suffix` 白名单命中）才是最需要
# 语义判断的地方——`苍兰诀(剧版)` 确实同作，而 `凡人修仙传之魔道争锋` 是不同篇章、
# `庆余年之少年篇` 亦存疑，规则给它们同一分数无法区分。故上限提到 90：
# 仅 100 分（清洗后标题完全相等）与低于 15 分（规则已判不可用）不进模型。
BOUNDARY_LOW = 15.0
BOUNDARY_HIGH = 90.0
# AI 允许的分数调整幅度（防止单次判断把 20 分拔成 100 分）
MAX_UP, MAX_DOWN = 40.0, 40.0


def _log(msg):
    print(f"[{config.SERVICE_NAME}] [TitleJudge] {msg}", flush=True)


_SYSTEM = "你是影视资源标题匹配器。严格只输出 JSON 数组，不要解释、不要 Markdown 围栏。"

_USER = (
    "目标作品：《{title}》（{kind}）\n"
    "判断下列候选标题是否指向**同一部作品**：\n"
    "· 发行形态后缀算同一部：剧版 / 影版 / 完整版 / 未删减版 / 导演剪辑版\n"
    "· 篇章地名后缀算同一部：之长安 / 外海风云 / 月升沧海\n"
    "· 季数、年份、画质规格（4K/2160p/臻彩）后缀算同一部\n"
    "· 借主片名词根另起的**不同作品**不算：如目标《恶人》候选《恶人传记》\n"
    "· 蹭名山寨不算：如目标《赴山海》候选《以歌赴山海》\n"
    "· 短剧/多集合集冒充单部电影不算\n"
    "每个元素输出：{{\"i\":序号,\"same\":true或false,\"confidence\":0到1,"
    "\"file_name\":原候选标题原样复制}}\n"
    "必须对每一个候选都输出一条，条数与输入相等。\n"
    "候选列表：\n{names}"
)


def _load_token():
    v = (os.environ.get("OPENAI_API_KEY") or "").strip()
    if v:
        return v
    for p in (os.environ.get("AI_PARSE_TOKEN_FILE", ""),
              "/home/dong/.config/cliproxy/token",
              os.path.expanduser("~/.config/cliproxy/token")):
        if not p:
            continue
        try:
            with open(p) as f:
                t = f.read().strip()
            if t:
                return t
        except Exception:
            continue
    return ""


def _cache_key(title, kind, name):
    import hashlib
    return hashlib.sha1(f"{title}|{kind}|{name}".encode("utf-8")).hexdigest()


def _circuit_open():
    return time.time() < _cooldown_until


def _record_failure(e):
    global _fail_streak, _cooldown_until
    _stats["failures"] += 1
    _fail_streak += 1
    if _fail_streak >= config.AI_PARSE_MAX_FAILS:
        _cooldown_until = time.time() + config.AI_PARSE_COOLDOWN
        _log(f"连续失败 {_fail_streak} 次，熔断 {config.AI_PARSE_COOLDOWN}s")
        _fail_streak = 0
    _log(f"复核失败，保留规则分数: {type(e).__name__}: {e}")


def _ask(title, kind, names):
    payload = {
        "model": config.AI_PARSE_MODEL,
        "messages": [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": _USER.format(
                title=title, kind=kind, names=json.dumps(names, ensure_ascii=False))},
        ],
        "max_tokens": 64 * len(names) + 128,
        "temperature": 0,
    }
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
    content = (msg.get("content") or msg.get("reasoning_content") or "").strip()
    raw = content
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```", 1)[0]
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```", 1)[0]
    arr = json.loads(raw.strip())
    if not isinstance(arr, list):
        raise ValueError("输出不是 JSON 数组")

    # 按 file_name 归位（与 name_parser 同一防错位策略），下标仅作校验
    by_name = {}
    for it in arr:
        if isinstance(it, dict) and isinstance(it.get("file_name"), str) \
                and it["file_name"] in names and it["file_name"] not in by_name:
            by_name[it["file_name"]] = it
    out = {}
    for idx, nm in enumerate(names):
        it = by_name.get(nm)
        if it is None:
            it = next((x for x in arr if isinstance(x, dict)
                       and x.get("file_name") in (None, "") and _safe_int(x.get("i")) == idx), None)
        if it is None:
            continue
        try:
            conf = float(it.get("confidence", 0))
        except (TypeError, ValueError):
            conf = 0.0
        if conf < config.AI_PARSE_MIN_CONFIDENCE:
            continue                     # 置信不足 → 不改分
        out[nm] = (bool(it.get("same")), conf)
    # 条目不全 → 整批作废，避免部分覆盖造成顺序错乱
    if len(out) != len(names):
        raise ValueError(f"输出条目不完整 ({len(out)}/{len(names)})")
    return out


def _safe_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def judge(results, title, season=0, episode=0):
    """批量复核候选与目标是否同一作品。返回 {id(result): 调整后分数 或 None(剔除)}。

    未参与复核的候选不出现在返回值中，调用方应保留其原分数。
    """
    if not results or not config.AI_PARSE_ENABLED:
        return {}
    if _circuit_open():
        return {}

    kind = "剧集" if season > 0 else "电影"
    targets = []
    for i, r in enumerate(results):
        try:
            from .search import score_search_result
            sc = score_search_result(r, title, season=season, episode=episode)
        except Exception:
            continue
        # 只复核边界区间：低于 15 分救不回来；高于 60 分规则已高度确信
        if BOUNDARY_LOW <= sc <= BOUNDARY_HIGH:
            targets.append((r, sc))
    if not targets:
        return {}

    overrides = {}
    to_ask = []
    with _lock:
        for r, sc in targets:
            k = _cache_key(title, kind, r.name or "")
            if k in _mem:
                _stats["cache_hits"] += 1
                overrides[id(r)] = _apply(sc, _mem[k])
            else:
                to_ask.append(r)

    if not to_ask:
        return overrides

    _stats["calls"] += 1
    try:
        answers = _ask(title, kind, [r.name or "" for r in to_ask])
        _fail_streak = 0
    except Exception as e:
        _record_failure(e)
        return overrides

    _stats["judged"] += len(to_ask)
    with _lock:
        for r, sc in targets:
            nm = r.name or ""
            if nm not in answers:
                continue
            same, conf = answers[nm]
            # 调整幅度不对称（2026-11-13）：
            #   高分档（>=85，白名单命中）若 AI 判同一作品 → 保持原分，不因“再确认一次”
            #   就拔到 100 盖过真正完全同名的资源；判不同作品才下调。
            #   低分档（<85）若 AI 判同一作品 → 上浮，救回被白名单漏掉的合法限定语。
            if same:
                delta = 0.0 if sc >= 85.0 else MAX_UP
            else:
                delta = -MAX_DOWN
            k = _cache_key(title, kind, nm)
            _mem[k] = delta
            overrides[id(r)] = _apply(sc, delta)
            _stats["overrides"] += 1
            _log(f"[候选复核] {nm[:36]!r} 规则={sc:.0f} AI={'同一作品' if same else '不同作品'}"
                 f"(conf={conf:.2f}) → {overrides[id(r)]:.0f}")
    return overrides


def _apply(score, delta):
    v = score + delta
    return max(0.0, min(100.0, v))


def stats():
    with _lock:
        return dict(_stats)
