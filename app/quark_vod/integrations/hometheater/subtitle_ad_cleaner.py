#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Subtitle advertisement cleanup. Definite patterns are local; ambiguous dialogue is reviewed only when the shared AI settings explicitly enable a model."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import os
import re
import sys
import json
import urllib.request
from typing import List, Dict, Tuple


from quark_vod.integrations.hometheater.ai import SNAPSHOT as AI_CONFIG, models, effort, request_chat
OPENAI_BASE_URL = AI_CONFIG['base_url']
def _load_cliproxy_token():
    from quark_vod.config import load_cliproxy_token
    return load_cliproxy_token()


OPENAI_API_KEY = _load_cliproxy_token()
CLEANER_MODEL = next(iter(models()), '')

# 高置信度直接判定为广告的正则模式（绝对不会出现在影视正片对白中的导流特征）
DEFINITE_AD_PATTERNS = [
    r'(?i)[QqＱｑ][QqＱｑ](?:群|号)?[:：\s]*\d{5,11}',
    r'(?i)加(?:片源|影视|资源|交流|高清|备用)?[QqＱｑ]群',
    r'(?i)(?:微信|vx|vx号|微信号)[:：\s]*[a-zA-Z0-9_\-]{4,25}',
    r'(?i)(?:关注|微信)公众号[:：\s]*[^\s\n,，。！!]{2,20}',
    r'防失联(?:群|主页|地址|备用)',
    r'永久防失联',
    r'(?:最新|更多)(?:片源|资源|汁源).*?(?:请加|关注|获取)',
    r'影片封装[:：\s]*\S+',
    r'压制组[:：\s]*\S+.*?群',
    r'(?:交流|资源|首发|福利|发布|更新)群[:：\s]*\d+',
    r'澳门首家线上赌场',
    r'(?:菠菜|百家乐|威尼斯人|盘搜|优惠券|外卖红包)',
    r'(?:link3\.cc|kdocs\.cn|yuntv\.net|pan\.quark\.cn|t\.me/)'
]

# 疑似广告词（需要结合 AI 进行语义上下文仲裁，防止误杀像“那群人”、“我们在群里说”等正常台词）
SUSPICIOUS_PATTERNS = [
    r'(?:压制|字幕|校对|时间轴|后期|特效|初翻|听译|监制)[:：]',
    r'仅供学习交流',
    r'严禁用于商业用途',
    r'请于24小时内删除',
    r'首发于',
    r'独家发布',
    r'更多精彩',
    r'扫码进群',
    r'汁源[裙群]'
]

def request_ai_ad_judgment(dialogues_to_check: List[Dict]) -> Dict[int, bool]:
    """通过本地 AI 批量仲裁疑似广告台词，返回 {idx: is_ad} 映射"""
    api_key = _load_cliproxy_token()
    if not dialogues_to_check or not models():
        return {}

    items_payload = []
    for item in dialogues_to_check:
        items_payload.append({
            "id": item["id"],
            "text": item["text"]
        })

    prompt = (
        "你是一个专业的影视字幕审查专家。你的任务是严格区分影视剧的正片角色对白与字幕制作者/网盘压制者植入的广告推广内容。\n"
        "广告包括：QQ群、微信、公众号、压制人员名单片头片尾宣传、防失联、网站引流、外卖红包、免责声明（如仅供学习交流）等。\n"
        "正片台词不是广告：例如角色的正常对话（如“那群傻子”、“我们在群里商量一下”等剧情相关台词）。\n"
        "请对以下字幕列表逐一判断，返回一个包含所有条目的 JSON 数组，格式为：[{\"id\": 编号, \"is_ad\": true/false}]。\n"
        "待检测字幕：\n" + json.dumps(items_payload, ensure_ascii=False)
    )

    try:
        content = request_chat([
            {"role": "system", "content": "请严格返回纯 JSON 数组。"},
            {"role": "user", "content": prompt}
        ], task="subtitle", timeout=20, max_tokens=2048)
        if not content:
            return {}
        match = re.search(r'```json\s*([\s\S]*?)\s*```', content)
        arr = json.loads(match.group(1) if match else content.strip())
        allowed = {item["id"] for item in dialogues_to_check}
        return {x["id"]: x["is_ad"] for x in arr if isinstance(x, dict)
                and x.get("id") in allowed and type(x.get("is_ad")) is bool}
    except (ValueError, KeyError, TypeError):
        return {}


def is_ad_text(text: str, check_ai: bool = True) -> bool:
    """快速单句判定"""
    clean = text.strip()
    if not clean:
        return False
    for pat in DEFINITE_AD_PATTERNS:
        if re.search(pat, clean):
            return True
    if check_ai:
        for pat in SUSPICIOUS_PATTERNS:
            if re.search(pat, clean):
                res = request_ai_ad_judgment([{"id": 1, "text": clean}])
                return res.get(1, False)
    return False

def clean_srt_dialogues(dialogues: List[Dict], enable_ai: bool = True) -> Tuple[List[Dict], int]:
    """清洗 SRT 对白列表，输入为 [{'start': ..., 'end': ..., 'text': ...}]"""
    suspicious_queue = []
    definite_ad_ids = set()
    cleaned = []
    removed_count = 0

    for idx, d in enumerate(dialogues):
        text = d["text"].strip()
        is_definite_ad = False
        for pat in DEFINITE_AD_PATTERNS:
            if re.search(pat, text):
                is_definite_ad = True
                break
        if is_definite_ad:
            definite_ad_ids.add(idx)
            continue
        is_suspicious = False
        for pat in SUSPICIOUS_PATTERNS:
            if re.search(pat, text):
                is_suspicious = True
                break
        if is_suspicious and enable_ai:
            suspicious_queue.append({"id": idx, "text": text})

    ai_decisions = {}
    if suspicious_queue and enable_ai:
        ai_decisions = request_ai_ad_judgment(suspicious_queue)

    for idx, d in enumerate(dialogues):
        if idx in definite_ad_ids:
            removed_count += 1
            continue
        if idx in ai_decisions and ai_decisions[idx]:
            removed_count += 1
            continue
        cleaned.append(d)

    return cleaned, removed_count

def clean_srt_file(file_path: str, output_path: str = None, enable_ai: bool = True, dry_run: bool = False) -> Dict:
    """读取并清洗 SRT 文件"""
    if not os.path.exists(file_path):
        return {"code": 1, "msg": "文件不存在", "removed": 0}
    if output_path is None:
        output_path = file_path
    try:
        with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
            content = f.read()
    except Exception as e:
        return {"code": 2, "msg": str(e), "removed": 0}

    blocks = re.split(r'\n\s*\n', content.strip())
    dialogues = []
    time_pat = re.compile(r'(\d{2}:\d{2}:\d{2}[,\.]\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2}[,\.]\d{3})')
    for b in blocks:
        lines = b.strip().split('\n')
        if len(lines) >= 2:
            t_match = None
            text_lines = []
            for i, line in enumerate(lines):
                m = time_pat.search(line)
                if m:
                    t_match = (m.group(1), m.group(2))
                    text_lines = lines[i+1:]
                    break
            if t_match and text_lines:
                text = '\n'.join(text_lines).strip()
                dialogues.append({
                    "start": t_match[0].replace(".", ","),
                    "end": t_match[1].replace(".", ","),
                    "text": text
                })

    if not dialogues:
        return {"code": 0, "msg": "未解析到有效字幕行", "removed": 0}

    cleaned_dialogues, removed_count = clean_srt_dialogues(dialogues, enable_ai=enable_ai)
    if not dry_run and (removed_count > 0 or output_path != file_path):
        out_lines = []
        for idx, d in enumerate(cleaned_dialogues, 1):
            out_lines.append(f"{idx}\n{d['start']} --> {d['end']}\n{d['text']}\n")
        with open(output_path, "w", encoding="utf-8") as f:
            f.write("\n".join(out_lines).strip() + "\n")

    return {
        "code": 0,
        "msg": f"清洗完成，已移除 {removed_count} 处广告",
        "total": len(dialogues),
        "kept": len(cleaned_dialogues),
        "removed": removed_count
    }

def scan_and_clean_cinema_subtitles(cinema_dir: str = paths.CINEMA_DIR, enable_ai: bool = True, dry_run: bool = False) -> Dict:
    """全量扫描并清洗 cinema 媒体库下的所有外挂字幕"""
    results = []
    total_removed = 0
    cleaned_files = 0
    scanned_files = 0
    if not os.path.exists(cinema_dir):
        return {"code": 1, "msg": "cinema 目录不存在", "data": []}

    for root, dirs, files in os.walk(cinema_dir):
        for f in files:
            if f.lower().endswith(('.srt', '.zh.default.srt', '.zh.srt', '.chi.srt')):
                scanned_files += 1
                full_p = os.path.join(root, f)
                res = clean_srt_file(full_p, enable_ai=enable_ai, dry_run=dry_run)
                if res.get("removed", 0) > 0:
                    cleaned_files += 1
                    total_removed += res["removed"]
                    rel_p = os.path.relpath(full_p, cinema_dir)
                    results.append({
                        "path": rel_p,
                        "removed": res["removed"],
                        "total": res["total"],
                        "kept": res["kept"]
                    })

    return {
        "code": 0,
        "scanned_files": scanned_files,
        "cleaned_files": cleaned_files,
        "total_removed": total_removed,
        "details": results
    }

if __name__ == "__main__":
    if len(sys.argv) > 1:
        target = sys.argv[1]
        if os.path.isdir(target):
            print(f"扫描并清洗目录: {target}")
            res = scan_and_clean_cinema_subtitles(target)
            print(json.dumps(res, ensure_ascii=False, indent=2))
        else:
            print(f"清洗单字幕文件: {target}")
            res = clean_srt_file(target)
            print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print("未指定文件，执行全库扫描:")
        res = scan_and_clean_cinema_subtitles()
        print(json.dumps(res, ensure_ascii=False, indent=2))
