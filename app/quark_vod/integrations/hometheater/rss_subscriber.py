#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
KK网盘 4K 影视自动抓取与每日更新聚合器
1. 自动订阅 kkpans.com 实时 RSS 流
2. 提取最新 4K 国产电影、院线大片与热播动漫夸克直链
3. 生成局域网极简一键直通看台 (HTML/JSON)
4. 仅更新推荐列表；不执行转存或自动播放
"""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config

import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import re
import json
import os
import time
import tempfile
from quark_vod.coordination import file_lock, Busy

RSS_URL = host_config._value("VOD_HOST_RSS_URL", "")
OUTPUT_DIR = paths.RSS_OUTPUT_DIR
DATA_FILE = os.path.join(OUTPUT_DIR, "daily_hot.json")
HTML_FILE = os.path.join(OUTPUT_DIR, "daily_hot.html")

def fetch_rss():
    if not RSS_URL:
        raise ValueError("VOD_HOST_RSS_URL is not configured")
    print("正在拉取配置的影视推荐源...")
    req = urllib.request.Request(RSS_URL, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=10) as resp:
        xml_data = resp.read(2 * 1024**2 + 1)
        if len(xml_data) > 2 * 1024**2:
            raise ValueError("RSS too large")
    root = ET.fromstring(xml_data)
    items = root.findall("./channel/item")
    print(f"成功获取 {len(items)} 条最新资源条目")
    return items

def extract_quark_link(detail_url):
    try:
        req = urllib.request.Request(detail_url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=6) as resp:
            final = urllib.parse.urlsplit(resp.geturl())
            if final.scheme != 'https' or final.hostname != urllib.parse.urlsplit(RSS_URL).hostname:
                raise ValueError('untrusted RSS detail redirect')
            html = resp.read(2 * 1024**2).decode("utf-8", "replace")
            m = re.search(r"https://pan.quark.cn/s/([a-zA-Z0-9]+)", html)
            if m:
                return m.group(0)
    except Exception as e:
        print(f"提取链接失败: {type(e).__name__}")
    return None

def run_subscriber():
    items = fetch_rss()
    results = []

    # 取前 30 条高质量内容
    count = 0
    for it in items[:100]:
        if count >= 30:
            break
        raw_title = it.findtext("title") or ""
        detail_link = it.findtext("link") or ""
        pub_date = it.findtext("pubDate") or ""

        # RSS titles often omit the drive name. Detail content is authoritative.
        if not re.search(r'4k|2160p|1080p|蓝光|合集|原盘|remux|杜比|电影|季|全集', raw_title, re.I):
            continue
        parsed = urllib.parse.urlsplit(detail_link)
        if parsed.scheme != 'https' or parsed.hostname != urllib.parse.urlsplit(RSS_URL).hostname:
            continue

        clean_name = re.sub(r'\[.*?\]|\【.*?\】|\(.*?\)|\{.*?\}', ' ', raw_title)
        clean_name = re.sub(r'[._\-\s]+', ' ', clean_name).strip()

        quark_url = extract_quark_link(detail_link)
        if quark_url:
            results.append({
                "title": raw_title,
                "clean_name": clean_name,
                "quark_url": quark_url,
                "detail_url": detail_link,
                "pub_date": pub_date
            })
            count += 1
            print(f"[{count}/30] 已抓取: {clean_name[:20]}")
            time.sleep(0.3) # 友好间隔

    # 容灾保护：仅在本次抓取到有效数据时才覆写；若抓取失败或为 0，坚决保留旧缓存，防止清空数据
    if results:
        from quark_vod.integrations.hometheater.vod_search_and_add import submit_wishlist
        for item in results:
            try:
                submit_wishlist(item['clean_name'], source='kkpans_rss')
            except Exception:
                print('RSS 待匹配条目稍后重试')
        os.makedirs(OUTPUT_DIR, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix='.daily-hot-', dir=OUTPUT_DIR)
        try:
            with os.fdopen(fd, 'w', encoding='utf-8') as f:
                json.dump(results, f, ensure_ascii=False, indent=2)
            os.replace(temporary, DATA_FILE)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        print(f"每日最新 4K 数据已持久化更新: {DATA_FILE} (共 {len(results)} 条)")
    else:
        print(f"警告: 本次未抓取到有效夸克 4K 数据，自动保留旧缓存，防止覆写为空！")
    from quark_vod.notifier import notify
    if not results:
        notify('rss_empty', '推荐源没有更新', '本次未获得有效夸克资源，保留旧缓存。', level='P1')
    if not os.path.exists(HTML_FILE):
        print("提示: 静态页面文件不存在，请确保 daily_hot.html 已部署")
    else:
        print("静态看板与交互逻辑已完备，客户端将实时加载最新数据:", HTML_FILE)

if __name__ == "__main__":
    try:
        with file_lock('rss-subscriber'):
            run_subscriber()
    except Busy:
        print('RSS 更新已在运行，跳过本次')
