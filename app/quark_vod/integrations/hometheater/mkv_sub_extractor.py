#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Optional Matroska subtitle extraction with local advertisement filtering."""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import os
import sys
import time
import struct
import io
import json
import re
import urllib.parse
import subprocess


BASE_DIR = paths.BASE_DIR
MEDIA_DIR = paths.SOURCE_DIR

def read_vint(f):
    b = f.read(1)
    if not b: return None, None
    b0 = b[0]
    mask = 0x80
    length = 1
    while mask > 0 and not (b0 & mask):
        mask >>= 1
        length += 1
    if length > 8: return None, None
    val = b0 & (mask - 1)
    for _ in range(length - 1):
        nb = f.read(1)
        if not nb: return None, None
        val = (val << 8) | nb[0]
    return val, length

def read_element_id(f):
    b = f.read(1)
    if not b: return None, None
    b0 = b[0]
    mask = 0x80
    length = 1
    while mask > 0 and not (b0 & mask):
        mask >>= 1
        length += 1
    if length > 4: return None, None
    val = b0
    for _ in range(length - 1):
        nb = f.read(1)
        if not nb: return None, None
        val = (val << 8) | nb[0]
    return val, length

def ms_to_srt(ms):
    h = ms // 3600000
    ms %= 3600000
    m = ms // 60000
    ms %= 60000
    s = ms // 1000
    rem_ms = ms % 1000
    return f"{h:02d}:{m:02d}:{s:02d},{rem_ms:03d}"

def prepare_cues(strm_path, temp_dir):
    os.makedirs(temp_dir, exist_ok=True)
    t0 = time.time()
    with open(strm_path, 'r', encoding='utf-8') as f:
        url = f.read().strip()

    parsed = urllib.parse.urlsplit(url)
    allowed = urllib.parse.urlsplit(host_config.OPENLIST_PUBLIC)
    if (parsed.scheme, parsed.netloc) != (allowed.scheme, allowed.netloc) or not parsed.path.startswith("/d/"):
        raise ValueError("subtitle source is outside configured OpenList")
    rel_path = urllib.parse.unquote(parsed.path[len("/d/"):])
    local_path = os.path.realpath(os.path.join(MEDIA_DIR, rel_path))
    if os.path.commonpath((os.path.realpath(MEDIA_DIR), local_path)) != os.path.realpath(MEDIA_DIR):
        raise ValueError("subtitle source escapes the configured mount")

    if not os.path.exists(local_path):
        print(f"[错误] 本地挂载路径不存在: {local_path}", file=sys.stderr)
        return 0

    print(f"\n=== 开始提取: {os.path.basename(strm_path)} ===")
    with open(local_path, 'rb') as f:
        # 1. Read EBML header
        el_id, _ = read_element_id(f)
        size, _ = read_vint(f)
        f.seek(f.tell() + size)

        # 2. Segment
        seg_id, _ = read_element_id(f)
        seg_size, _ = read_vint(f)
        seg_start = f.tell()

        # 3. Read SeekHead
        cues_pos = None
        tracks_pos = None
        while f.tell() < seg_start + 100000:
            sub_id, _ = read_element_id(f)
            if not sub_id: break
            sub_size, _ = read_vint(f)
            if sub_id == 0x114d9b74:
                shend = f.tell() + sub_size
                while f.tell() < shend:
                    sid, _ = read_element_id(f)
                    ssz, _ = read_vint(f)
                    if sid == 0x4dbb:
                        send = f.tell() + ssz
                        seek_id = None
                        seek_pos = None
                        while f.tell() < send:
                            ssid, _ = read_element_id(f)
                            sssz, _ = read_vint(f)
                            data = f.read(sssz)
                            if ssid == 0x53ab: seek_id = data.hex()
                            elif ssid == 0x53ac: seek_pos = int.from_bytes(data, 'big')
                        if seek_id == '1c53bb6b': cues_pos = seg_start + seek_pos
                        elif seek_id == '1654ae6b': tracks_pos = seg_start + seek_pos
                break
            else:
                f.seek(f.tell() + sub_size)

        if not cues_pos or not tracks_pos:
            print("[错误] 未找到 Cues 或 Tracks 元数据索引")
            return False

        # 4. Check tracks
        f.seek(tracks_pos)
        tid, _ = read_element_id(f)
        tsz, _ = read_vint(f)
        tend = f.tell() + tsz
        sub_track_num = None
        tracks_found = []
        while f.tell() < tend:
            eid, _ = read_element_id(f)
            esz, _ = read_vint(f)
            if eid == 0xae:
                te_end = f.tell() + esz
                tnum = None
                tcodec = None
                tname = ""
                while f.tell() < te_end:
                    eeid, _ = read_element_id(f)
                    eesz, _ = read_vint(f)
                    data = f.read(eesz)
                    if eeid == 0xd7: tnum = int.from_bytes(data, 'big')
                    elif eeid == 0x86: tcodec = data.decode('utf-8', 'ignore')
                    elif eeid == 0x536e: tname = data.decode('utf-8', 'ignore')
                if tcodec in ('S_TEXT/ASS', 'S_TEXT/UTF8'):
                    tracks_found.append((tnum, tcodec, tname))
                    if '简英' in tname or sub_track_num is None:
                        sub_track_num = tnum
            else:
                f.seek(f.tell() + esz)

        print(f"识别到字幕轨: {tracks_found}, 优先选定轨号: {sub_track_num}")
        if not sub_track_num:
            print("[警告] 视频内未包含 ASS/SRT 字幕轨")
            return False

        # 5. Parse Cues
        f.seek(cues_pos)
        cid, _ = read_element_id(f)
        csz, _ = read_vint(f)
        cend = f.tell() + csz

        cues = []
        while f.tell() < cend:
            pid, _ = read_element_id(f)
            psz, _ = read_vint(f)
            if pid == 0xbb:
                pend = f.tell() + psz
                ctime = None
                while f.tell() < pend:
                    ccid, _ = read_element_id(f)
                    ccsz, _ = read_vint(f)
                    if ccid == 0xb3:
                        ctime = int.from_bytes(f.read(ccsz), 'big')
                    elif ccid == 0xb7:
                        tpdata = f.read(ccsz)
                        mf = io.BytesIO(tpdata)
                        tp_num = None
                        tp_clust = None
                        tp_rel = None
                        tp_dur = None
                        while mf.tell() < len(tpdata):
                            mid, _ = read_element_id(mf)
                            msz, _ = read_vint(mf)
                            mdata = mf.read(msz)
                            if mid == 0xf7: tp_num = int.from_bytes(mdata, 'big')
                            elif mid == 0xf1: tp_clust = int.from_bytes(mdata, 'big')
                            elif mid == 0xf0: tp_rel = int.from_bytes(mdata, 'big')
                            elif mid == 0xb2: tp_dur = int.from_bytes(mdata, 'big')
                        if tp_num == sub_track_num:
                            cues.append((ctime, tp_clust, tp_rel, tp_dur))
                    else:
                        f.seek(f.tell() + ccsz)
            else:
                f.seek(f.tell() + psz)

    print(f"解析到 {len(cues)} 处字幕关键点 (耗时: {time.time() - t0:.2f}s)")
    if not cues:
        print("[警告] 未索引到任何字幕帧")
        return 0

    os.makedirs(temp_dir, exist_ok=True)
    config_path = os.path.join(temp_dir, "curl_config.txt")
    meta_path = os.path.join(temp_dir, "meta.json")

    blocks = []
    for i, (ctime, clust, rel, dur) in enumerate(cues):
        abs_off = seg_start + clust + rel
        block = f'url = "{url}"\nrange = "{abs_off}-{abs_off+650}"\noutput = "{temp_dir}/{i:05d}.bin"'
        blocks.append(block)

    with open(config_path, 'w', encoding='utf-8') as f:
        f.write('\nnext\n'.join(blocks) + '\n')

    with open(meta_path, 'w', encoding='utf-8') as f:
        json.dump({"cues": cues, "seg_start": seg_start, "strm_path": strm_path}, f)

    return len(cues)

def assemble_subtitles(temp_dir, out_srt_path):
    meta_path = os.path.join(temp_dir, "meta.json")
    if not os.path.exists(meta_path):
        print(f"[错误] 元数据文件不存在: {meta_path}", file=sys.stderr)
        return False
    with open(meta_path, 'r', encoding='utf-8') as f:
        meta = json.load(f)
    cues = meta["cues"]
    raw_dialogues = []
    for i, (ctime, clust, rel, dur) in enumerate(cues):
        chunk_file = f"{temp_dir}/{i:05d}.bin"
        if not os.path.exists(chunk_file): continue
        with open(chunk_file, 'rb') as cf:
            chunk = cf.read()

        idx_a1 = chunk.find(b'\xa1')
        if idx_a1 == -1: continue

        mf = io.BytesIO(chunk[idx_a1:])
        _ = read_element_id(mf)
        bsz, _ = read_vint(mf)
        bdata = mf.read(bsz)
        if not bdata or len(bdata) < 4: continue

        bf = io.BytesIO(bdata)
        _ = read_vint(bf)
        rel_tc = int.from_bytes(bf.read(2), 'big', signed=True)
        b_flags = bf.read(1)
        if not b_flags:
            continue
        flags = b_flags[0]
        text = bf.read().decode('utf-8', 'ignore')

        dur_ms = dur if (dur and dur > 0) else None
        if dur_ms is None:
            idx_9b = chunk.find(b'\x9b', idx_a1 + bsz)
            if idx_9b != -1 and idx_9b < idx_a1 + bsz + 10:
                mf_dur = io.BytesIO(chunk[idx_9b:])
                _ = read_element_id(mf_dur)
                dsz, _ = read_vint(mf_dur)
                dur_ms = int.from_bytes(mf_dur.read(dsz), 'big')
        if not dur_ms or dur_ms <= 0:
            dur_ms = 2500

        start_ms = ctime
        end_ms = start_ms + dur_ms

        parts = text.split(',', 8)
        dialogue_text = parts[8] if len(parts) >= 9 else text

        clean_text = re.sub(r'\{[^}]*\}', '', dialogue_text)
        clean_text = re.sub(r'\{[^}]*$', '', clean_text)
        clean_text = re.sub(r'<[^>]+>', '', clean_text)
        clean_text = clean_text.replace('\\N', '\n').replace('\\n', '\n').strip()

        if clean_text and re.search(r'[\u4e00-\u9fff\w]', clean_text):
            raw_dialogues.append({
                'start_ms': start_ms,
                'end_ms': max(end_ms, start_ms + 1000),
                'text': clean_text
            })

    if not raw_dialogues:
        print(f"[跳过] 临时目录中无有效字幕块，跳过组装: {out_srt_path}")
        return True

    # 8. Sort chronologically
    raw_dialogues.sort(key=lambda d: (d['start_ms'], d['end_ms']))

    # 9. Merge consecutive identical animation frames
    merged = []
    for d in raw_dialogues:
        matched = False
        for m in reversed(merged[-15:]):
            if m['text'] == d['text'] and d['start_ms'] <= m['end_ms'] + 600:
                m['end_ms'] = max(m['end_ms'], d['end_ms'])
                matched = True
                break
        if not matched:
            merged.append(d)

    # 10. Clean ads
    try:
        from quark_vod.integrations.hometheater.subtitle_ad_cleaner import clean_srt_dialogues
        formatted = [{'start': ms_to_srt(d['start_ms']), 'end': ms_to_srt(d['end_ms']), 'text': d['text']} for d in merged]
        cleaned, rem = clean_srt_dialogues(formatted, enable_ai=False)
        if rem > 0:
            print(f"清洗了 {rem} 处广告水印")
    except Exception as e:
        print(f"广告清洗器加载跳过: {type(e).__name__}")
        cleaned = [{'start': ms_to_srt(d['start_ms']), 'end': ms_to_srt(d['end_ms']), 'text': d['text']} for d in merged]

    # 11. Write SRT
    tmp_out = out_srt_path + ".tmp"
    with open(tmp_out, 'w', encoding='utf-8') as f:
        for idx, d in enumerate(cleaned, 1):
            f.write(f"{idx}\n{d['start']} --> {d['end']}\n{d['text']}\n\n")
    os.replace(tmp_out, out_srt_path)

    print(f"生成字幕成功: {out_srt_path} (共 {len(cleaned)} 行对白)")
    try:
        for f in os.listdir(temp_dir):
            if f.endswith('.bin'):
                os.remove(os.path.join(temp_dir, f))
    except Exception:
        pass
    return True

def batch_prepare(target_dir, temp_base, force=False):
    os.makedirs(temp_base, exist_ok=True)
    master_curl = os.path.join(temp_base, "curl_config.txt")
    episodes_meta = os.path.join(temp_base, "batch_meta.json")
    episodes = []
    all_curl_lines = []

    strm_files = []
    for root, _, files in os.walk(target_dir):
        for file in files:
            if file.endswith('.strm'):
                strm_files.append(os.path.join(root, file))
    strm_files.sort()

    for idx, strm_path in enumerate(strm_files):
        out_srt = re.sub(r'\.strm$', '.zh.default.srt', strm_path)
        if not force and os.path.exists(out_srt) and os.path.getsize(out_srt) > 1000:
            print(f"[跳过] 已有字幕: {os.path.basename(out_srt)}")
            continue

        ep_dir = os.path.join(temp_base, f"ep_{idx:03d}")
        cues_count = prepare_cues(strm_path, ep_dir)
        if cues_count > 0:
            episodes.append({
                "strm_path": strm_path,
                "out_srt": out_srt,
                "ep_dir": ep_dir,
                "cues_count": cues_count
            })

    for ep in episodes:
        ep_curl = os.path.join(ep["ep_dir"], "curl_config.txt")
        if os.path.exists(ep_curl):
            with open(ep_curl, 'r', encoding='utf-8') as ecf:
                content = ecf.read().strip()
                if content:
                    all_curl_lines.append(content)

    with open(master_curl, 'w', encoding='utf-8') as mcf:
        mcf.write('\nnext\n'.join(all_curl_lines) + '\n')

    with open(episodes_meta, 'w', encoding='utf-8') as emf:
        json.dump(episodes, emf, ensure_ascii=False, indent=2)

    print(f"\n>>> 批处理准备就绪: 共 {len(episodes)} 个待提取剧集, curl 配置文件: {master_curl}")
    return len(episodes)

def batch_assemble(temp_base):
    episodes_meta = os.path.join(temp_base, "batch_meta.json")
    if not os.path.exists(episodes_meta):
        print(f"[错误] 未找到批处理元数据: {episodes_meta}")
        return False
    with open(episodes_meta, 'r', encoding='utf-8') as emf:
        episodes = json.load(emf)
    success = 0
    for ep in episodes:
        print(f"\n--- 组装字幕: {os.path.basename(ep['out_srt'])} ---")
        if assemble_subtitles(ep['ep_dir'], ep['out_srt']):
            success += 1
    print(f"\n>>> 批量组装完成: 成功 {success}/{len(episodes)}")
    return success

if __name__ == '__main__':
    if len(sys.argv) >= 3 and sys.argv[1] == 'prepare':
        strm_path = sys.argv[2]
        temp_dir = sys.argv[3] if len(sys.argv) > 3 else f"/tmp/sub_work_{os.getpid()}"
        count = prepare_cues(strm_path, temp_dir)
        print(f"PREPARED:{count}:{temp_dir}")
    elif len(sys.argv) >= 3 and sys.argv[1] == 'assemble':
        temp_dir = sys.argv[2]
        out_srt_path = sys.argv[3]
        assemble_subtitles(temp_dir, out_srt_path)
    elif len(sys.argv) >= 3 and sys.argv[1] == 'batch_prepare':
        target_dir = sys.argv[2]
        temp_base = sys.argv[3]
        force = '--force' in sys.argv
        batch_prepare(target_dir, temp_base, force=force)
    elif len(sys.argv) >= 3 and sys.argv[1] == 'batch_assemble':
        temp_base = sys.argv[2]
        batch_assemble(temp_base)
    else:
        print("Usage:")
        print("  mkv_sub_extractor.py prepare <file.strm> <temp_dir>")
        print("  mkv_sub_extractor.py assemble <temp_dir> <out.srt>")
        print("  mkv_sub_extractor.py batch_prepare <dir> <temp_base> [--force]")
        print("  mkv_sub_extractor.py batch_assemble <temp_base>")
