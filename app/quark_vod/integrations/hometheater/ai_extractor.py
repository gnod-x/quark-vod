
from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import re
import json
import sqlite3
import urllib.request
import urllib.parse
from xml.sax.saxutils import escape as xml_escape
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime


from quark_vod.integrations.hometheater.ai import SNAPSHOT as AI_CONFIG, models, effort, request_chat
OPENAI_BASE_URL = AI_CONFIG['base_url']
OPENAI_FALLBACK_BASE_URL = ''
def _load_cliproxy_token():
    from quark_vod.config import load_cliproxy_token
    return load_cliproxy_token()


OPENAI_API_KEY = _load_cliproxy_token()
MODEL_CANDIDATES = models()
CURRENT_MODEL = next(iter(MODEL_CANDIDATES), '')
DB_PATH = host_config._value('AI_CACHE_DB_PATH', paths.AI_CACHE_DB)

def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("""CREATE TABLE IF NOT EXISTS media_ai_cache (raw_name TEXT PRIMARY KEY, clean_title TEXT NOT NULL, year INTEGER, media_type TEXT DEFAULT 'movie', season INTEGER, episode INTEGER, tags TEXT DEFAULT '[]', confidence REAL DEFAULT 1.0, manual_override INTEGER DEFAULT 0, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, is_ad INTEGER DEFAULT 0)""")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(media_ai_cache)")}
    if "is_ad" not in columns:
        conn.execute("ALTER TABLE media_ai_cache ADD COLUMN is_ad INTEGER DEFAULT 0")
    conn.commit()
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS media_ai_cache (
            raw_name TEXT PRIMARY KEY,
            clean_title TEXT NOT NULL,
            year INTEGER,
            media_type TEXT DEFAULT 'movie',
            season INTEGER,
            episode INTEGER,
            tags TEXT DEFAULT '[]',
            confidence REAL DEFAULT 1.0,
            manual_override INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    try:
        cursor.execute('ALTER TABLE media_ai_cache ADD COLUMN is_ad INTEGER DEFAULT 0')
    except Exception:
        pass
    conn.commit()
    conn.close()


TIMESTAMP_PREFIX_REGEX = re.compile(
    r'^(?:'
    r'[0-9]{14}[-_ ]*|'
    r'[0-9]{8,13}[-_ ]+|'
    r'[0-9]{4}[-_.]+[0-9]{2}[-_.]+[0-9]{2}[-_ ]+'
    r')'
)

def strip_timestamp_prefix(name: str) -> str:
    """剥离网盘/转存工具在文件或目录名头部添加的纯日期时间戳前缀"""
    if not name:
        return ''
    base = os.path.basename(name).strip()
    cleaned = TIMESTAMP_PREFIX_REGEX.sub('', base).strip()
    return cleaned if cleaned else base

def get_cached_item(raw_name):
    if not raw_name:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    # 1. 优先原样精确匹配
    cursor.execute('SELECT * FROM media_ai_cache WHERE raw_name = ?', (raw_name,))
    row = cursor.fetchone()
    if row:
        conn.close()
        return dict(row)

    # 2. 剥离时间戳前缀后匹配
    norm = strip_timestamp_prefix(raw_name)
    if norm and norm != raw_name:
        cursor.execute('SELECT * FROM media_ai_cache WHERE raw_name = ?', (norm,))
        row = cursor.fetchone()
        if row:
            conn.close()
            return dict(row)

    # 3. 容错匹配：如果库中存的是带时间戳的旧条目，而当前请求是不带时间戳的新名称
    if norm:
        cursor.execute(
            "SELECT * FROM media_ai_cache WHERE raw_name LIKE ? ORDER BY manual_override DESC, updated_at DESC",
            (f'%{norm}',)
        )
        candidates = cursor.fetchall()
        for cand in candidates:
            if strip_timestamp_prefix(cand['raw_name']) == norm:
                conn.close()
                return dict(cand)

    conn.close()
    return None

def save_cached_item(raw_name, clean_title, year=None, media_type='movie', season=None, episode=None, tags=None, manual=0, is_ad=0, old_raw_name=None):
    if not raw_name:
        return
    if old_raw_name and old_raw_name != raw_name:
        delete_cached_item(old_raw_name)
    conn = get_db_connection()
    cursor = conn.cursor()
    tags_str = json.dumps(tags or [], ensure_ascii=False) if isinstance(tags, (list, dict)) else str(tags or '[]')
    now_str = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

    norm_name = strip_timestamp_prefix(raw_name)
    names_to_save = [raw_name]
    if norm_name and norm_name != raw_name:
        names_to_save.append(norm_name)

    for n in names_to_save:
        cursor.execute('''
            INSERT INTO media_ai_cache (raw_name, clean_title, year, media_type, season, episode, tags, manual_override, is_ad, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(raw_name) DO UPDATE SET
                clean_title = CASE WHEN excluded.manual_override = 1 THEN excluded.clean_title WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.clean_title ELSE excluded.clean_title END,
                year = CASE WHEN excluded.manual_override = 1 THEN excluded.year WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.year ELSE excluded.year END,
                media_type = CASE WHEN excluded.manual_override = 1 THEN excluded.media_type WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.media_type ELSE excluded.media_type END,
                season = CASE WHEN excluded.manual_override = 1 THEN excluded.season WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.season ELSE excluded.season END,
                episode = CASE WHEN excluded.manual_override = 1 THEN excluded.episode WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.episode ELSE excluded.episode END,
                tags = CASE WHEN excluded.manual_override = 1 THEN excluded.tags WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.tags ELSE excluded.tags END,
                is_ad = CASE WHEN excluded.manual_override = 1 THEN excluded.is_ad WHEN media_ai_cache.manual_override = 1 THEN media_ai_cache.is_ad ELSE excluded.is_ad END,
                manual_override = CASE WHEN excluded.manual_override = 1 THEN 1 WHEN media_ai_cache.manual_override = 1 THEN 1 ELSE excluded.manual_override END,
                updated_at = excluded.updated_at
        ''', (n, clean_title, year, media_type, season, episode, tags_str, manual, 1 if is_ad else 0, now_str))
    conn.commit()
    conn.close()

AD_JUNK_REGEX = re.compile(
    r'(?:'
    r'⚠️|防[失走]联|最新[汁资]源|获取[汁资]源|[汁资]源[裙群]|关注公[众眾]号|公众号|'
    r'点我.*?[汁资]源|外卖红包|省钱区|扫码[进加]|'
    r'(?:qq|微信|交流|首发|备用|福利|资源|更新|内部|影视|片源|电影)\s*[裙群]|'
    r'(?:进|加|入)[裙群]|'
    r'加[Qq微Vv信].*?[群号]|'
    r'[QqＱｑ][QqＱｑ]群|'
    r'样片|trailer|sample'
    r')',
    re.IGNORECASE
)

def is_ad_or_junk_path(path_or_name: str) -> bool:
    if not path_or_name:
        return False
    base = os.path.basename(path_or_name).strip()
    if base in ['群体', '义胆群英']:
        return False
    if AD_JUNK_REGEX.search(path_or_name):
        return True
    return False

def fallback_regex_parse(raw_name, parent_hint=''):
    s = os.path.basename(raw_name)
    s = os.path.splitext(s)[0].replace(chr(160), ' ').strip()
    m_year = re.search(r'(?<!\d)(19\d\d|20\d\d)(?!\d)', s)
    if not m_year and parent_hint:
        m_year = re.search(r'(?<!\d)(19\d\d|20\d\d)(?!\d)', parent_hint)
    year = int(m_year.group(1)) if m_year else None
    clean = re.sub(r'\[.*?\]|\【.*?\】|\(.*?\)|\{.*?\}', ' ', s)
    clean = re.sub(r'(?i)(?:加[微v]|关注|防失联|首发|交流|汁源|资源|QQ|微信).*?群', ' ', clean)
    clean = re.sub(r'(?i)(?:QQ群|微信群|交流群|资源群|防失联群|进群|加群|扫码|汁源[裙群]|关注公[众眾]号|公众号|群号)[:：\s]*\w+', ' ', clean)
    clean = re.sub(r'(?i)[QqＱｑ][QqＱｑ](?:群|号)?[:：\s]*\d+', ' ', clean)
    clean = re.sub(r'(?i)\b(yuntv|link3|kdocs)\.net\b', ' ', clean)
    clean = re.sub(r'(?i)(?<![a-zA-Z0-9])(4k|2160p|1080p|720p|hdr10plus|hdr10\+|hdr10|hdr|dv|dovi|remux|bluray|blu-ray|web-dl|webrip|x264|x265|hevc|h264|h265|avc|aac|dts-hd|dts-x|dts|truehd|atmos|ddp5\.1|ddp|pcm|60fps|120fps|10bit|8bit|uhd|proper|repack)(?![a-zA-Z0-9])', ' ', clean)
    clean = re.sub(r'(?i)(蓝光原盘|remux原盘|原盘|杜比视界|杜比全景声|全景声|国英双音轨|国英音轨|国粤双语|国语|粤语|中文字幕|双语字幕|官方中字|简繁字幕|嵌入中字|外挂简中|外挂字幕|高码率|高码|修复版)', ' ', clean)
    clean = re.sub(r'(?<!\d)(19\d\d|20\d\d)(?!\d)', ' ', clean)
    clean = re.sub(r'^[Xxl~.\-\s]+', '', clean)
    clean = re.sub(r'(?:^|\s)[&+~～]+(?:\s|$)', ' ', clean)
    clean = re.sub(r'[._\-\s]+', ' ', clean).strip(' &_+-')
    clean = re.sub(r'[\\/*?:"<>|]', '', clean).strip()
    if not clean:
        clean = s
    return {
        'raw': raw_name,
        'title': clean,
        'year': year,
        'media_type': 'movie',
        'season': None,
        'episode': None,
        'tags': [],
        'is_ad': is_ad_or_junk_path(raw_name)
    }

def natural_sort_key(s):
    """自然排序键，解决 1.mp4, 10.mp4, 2.mp4 导致的集数乱序"""
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', str(s))]

def extract_episode_smart(filename: str, parent_hint: str = ''):
    """
    智能提取剧集单集的季数与集数，彻底消除 1080p, 2024, 60fps, 5.1 等参数对集数的干扰
    返回 (season_num, episode_num)
    """
    base = os.path.basename(filename)
    rel = f"{parent_hint}/{base}" if parent_hint else base

    # 1. 优先提取 S01E02 这种极规范的标记
    m_se = re.search(r'(?i)\bS(\d{1,2})[._\-\s]*E(\d{1,3})\b', base)
    if m_se:
        return int(m_se.group(1)), int(m_se.group(2))

    # 2. 判断季数（从目录名或文件名中提取）
    season_num = 1
    m_season = re.search(r'(?:第\s*(\d{1,2})\s*季|[Ss]eason\s*(\d{1,2})|\b[Ss](\d{1,2})\b)', rel)
    if m_season:
        s_str = m_season.group(1) or m_season.group(2) or m_season.group(3)
        if s_str:
            season_num = int(s_str)

    # 3. 过滤掉常见干扰数字串（年份、分辨率、声道、帧率、编码规格）
    cleaned = re.sub(r'(?<!\d)(19\d\d|20\d\d)(?!\d)', ' ', base)
    cleaned = re.sub(r'(?i)\b(4k|2160p?|1080p?|720p?|60fps|5\.1|7\.1|h264|h265|x264|x265|hevc|aac|dts)\b', ' ', cleaned)

    # 4. 精准匹配集数
    patterns = [
        r'(?i)[eE][pP]?\s*(\d{1,3})',
        r'第\s*(\d{1,3})\s*[集话回期]',
        r'【\s*(\d{1,3})\s*】|\[\s*(\d{1,3})\s*\]',
        r'(?:^|[._\-\s])(\d{1,3})(?:[._\-\s]|$)'
    ]
    for p in patterns:
        m = re.search(p, cleaned)
        if m:
            num_str = next((g for g in m.groups() if g is not None), None)
            if num_str:
                ep = int(num_str)
                if 1 <= ep <= 999:
                    return season_num, ep

    return season_num, None

def request_ai_completions(messages, timeout=30):
    return request_chat(messages, task="filename", timeout=timeout, max_tokens=4096) or ""


def parse_batch_with_ai(raw_names: list, category_hint: str = ''):
    if not raw_names:
        return {}
    system_prompt = (
        '你是一个专业的影视媒体库元数据识别专家。请解析用户提供的网盘原始目录名或文件名列表。\n'
        '任务要求：\n'
        '1. 消除网盘防屏蔽字符、多余空格、拼音缩写、错别字（如"夏 目 友 人 帐"合并为"夏目友人帐"，"xiao新"转为"小新"、"Du.Sheng圣"转为"赌圣"）。\n'
        '2. 过滤压制参数、画质规格（如4K, 1080p, 60fps, HDR, DoVi, 7.1, 压制组/网站水印等）。\n'
       '3. 官方译名还原：若输入是外文或英文片名（且属于知名院线或华语电影，如 Successor -> 抓娃娃，Creation of the Gods -> 封神第一部：朝歌风云，Zheztyrnak -> 铜爪女妖），请转为标准官方中文片名。\n'
       '4. 广告与群号净化：彻底清除文件名或目录名中包含的任何 QQ群、微信群、公众号、防失联、网站域名（如 yuntv.net）、引流文字等广告内容。若某个条目本身是纯宣传推广、防失联说明、广告视频或网盘导流目录（如包含"防失联"、"汁源裙"、"外卖红包"等非影视内容），必须设置 is_ad: true，否则 is_ad: false。\n'
        '5. 专区类型对齐：参考"所属专区参考"。若所属专区为剧集专区（如"热门韩剧"、"国产剧集"、"欧美剧集"、"国产动漫"），除非条目明确包含"电影"、"Movie"或确认为单部独立电影，否则必须优先识别为 media_type: "series"；若同名存在电影和剧集，在剧集专区下必须识别为剧集。\n'
       '输出格式：必须且仅输出一个纯 JSON 数组，包含在```json```代码块中，每个元素包含：\n'
        '- raw: 原始名称字符串（原样返回）\n'
        '- title: 标准官方中文片名/剧名（不含年份，不含规格）\n'
        '- year: 上映年份（整数或null）\n'
        '- media_type: "movie" 或 "series" 或 "collection"\n'
        '- is_collection: 是否为电影合集包（布尔值）\n'
        '- season: 季数整数（单部电影为null）\n'
        '- episode: 集数整数（整季或电影为null）\n'
        '- tags: 识别出的标签列表（如 ["4K", "HDR"]）\n'
        '- is_ad: 是否为垃圾/导流/广告文件或目录（布尔值）\n'
        '请严格直出 JSON 格式，禁止包含思考过程或额外说明。'
    )
    user_content = f'所属专区参考: {category_hint}\n条目列表:\n' + json.dumps(raw_names, ensure_ascii=False)
    messages = [
        {'role': 'system', 'content': system_prompt},
        {'role': 'user', 'content': user_content}
    ]
    raw_reply = request_ai_completions(messages, timeout=30)
    results = {}
    if raw_reply:
        try:
            m = re.search(r'```json\s*([\s\S]*?)\s*```', raw_reply)
            json_str = m.group(1) if m else raw_reply.strip()
            items = json.loads(json_str)
            if isinstance(items, list):
                for it in items:
                    raw_k = it.get('raw')
                    if raw_k:
                        m_type = it.get('media_type') or ('collection' if it.get('is_collection') else 'movie')
                        is_ad_val = bool(it.get('is_ad', False)) or is_ad_or_junk_path(raw_k)
                        results[raw_k] = {
                            'raw': raw_k,
                            'title': it.get('title') or raw_k,
                            'year': it.get('year'),
                            'media_type': m_type,
                            'is_collection': bool(it.get('is_collection') or m_type == 'collection'),
                            'season': it.get('season'),
                            'episode': it.get('episode'),
                            'tags': it.get('tags') or [],
                            'is_ad': is_ad_val
                        }
        except Exception:
            pass
    for r in raw_names:
        if r not in results:
            results[r] = fallback_regex_parse(r)
    return results

def resolve_entries_smart(raw_names: list, category_hint: str = ''):
    final_results = {}
    pending_to_ai = []
    for r in raw_names:
        cached = get_cached_item(r)
        if cached:
            is_ad_val = bool(cached['is_ad']) if ('is_ad' in cached.keys() and cached['is_ad'] is not None) else is_ad_or_junk_path(cached['raw_name'])
            final_results[r] = {
                'raw': cached['raw_name'],
                'title': cached['clean_title'],
                'year': cached['year'],
                'media_type': cached['media_type'],
                'is_collection': (cached['media_type'] == 'collection'),
                'season': cached['season'],
                'episode': cached['episode'],
                'tags': json.loads(cached['tags']) if cached['tags'] else [],
                'is_ad': is_ad_val
            }
        else:
            pending_to_ai.append(r)

    if not pending_to_ai:
        return final_results

    # 细粒度批次 + 多线程并发处理，极大缩短等待时间
    batch_size = 8
    batches = [pending_to_ai[i:i + batch_size] for i in range(0, len(pending_to_ai), batch_size)]
    max_workers = min(4, len(batches))

    def worker(chunk):
        return parse_batch_with_ai(chunk, category_hint=category_hint)

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_chunk = {executor.submit(worker, chunk): chunk for chunk in batches}
        for future in as_completed(future_to_chunk):
            try:
                ai_res = future.result()
                for r, parsed in ai_res.items():
                    final_results[r] = parsed
                    save_cached_item(
                        raw_name=r,
                        clean_title=parsed['title'],
                        year=parsed['year'],
                        media_type=parsed['media_type'],
                        season=parsed['season'],
                        episode=parsed['episode'],
                        tags=parsed['tags'],
                        is_ad=1 if parsed.get('is_ad') else 0
                    )
            except Exception:
                chunk = future_to_chunk[future]
                for r in chunk:
                    if r not in final_results:
                        final_results[r] = fallback_regex_parse(r)
    return final_results

KNOWN_METADATA_HINTS = {
    '热带夜晚': {
        'tmdbid': '1194966',
        'originaltitle': '열대야',
        'plot': '讲述在午夜热气仍未消退的曼谷，为了生存而倾尽全力的众人度过最火热24小时的硬汉犯罪动作片。',
        'lockdata': True,
    },
    '复仇者联盟': {'tmdbid': '24428', 'originaltitle': 'The Avengers'},
    '复仇者联盟2：奥创纪元': {'tmdbid': '99861', 'originaltitle': 'Avengers: Age of Ultron'},
    '复仇者联盟3：无限战争': {'tmdbid': '299536', 'originaltitle': 'Avengers: Infinity War'},
    '复仇者联盟4：终局之战': {'tmdbid': '299534', 'originaltitle': 'Avengers: Endgame'},
    '流浪地球2': {'tmdbid': '842675', 'originaltitle': '流浪地球2'},
    '夜王': {'tmdbid': '1602727', 'originaltitle': '夜王'},
}

def generate_nfo_file(target_dir: str, title: str, year=None, media_type='movie', original_title=None, overwrite=False, tmdbid=None):
    os.makedirs(target_dir, exist_ok=True)
    nfo_name = 'movie.nfo' if media_type == 'movie' else 'tvshow.nfo'
    nfo_path = os.path.join(target_dir, nfo_name)
    root_tag = 'movie' if media_type == 'movie' else 'tvshow'
    clean_t = str(title or '').strip()
    hints = KNOWN_METADATA_HINTS.get(clean_t, {})
    eff_tmdbid = tmdbid or hints.get('tmdbid')
    eff_orig_title = hints.get('originaltitle')
    eff_plot = hints.get('plot')
    eff_lock = 'true' if hints.get('lockdata') else 'false'

    esc_title = xml_escape(clean_t)
    year_tag = f'  <year>{xml_escape(str(year))}</year>\n' if year else ''
    orig_tag = f'  <originaltitle>{xml_escape(str(eff_orig_title))}</originaltitle>\n' if eff_orig_title else ''
    plot_tag = f'  <plot>{xml_escape(str(eff_plot))}</plot>\n' if eff_plot else ''
    tmdb_tag = f'  <tmdbid>{xml_escape(str(eff_tmdbid))}</tmdbid>\n  <uniqueid type="tmdb" default="true">{xml_escape(str(eff_tmdbid))}</uniqueid>\n' if eff_tmdbid else ''
    raw_tag = f'  <rawname>{xml_escape(str(original_title))}</rawname>\n' if original_title else ''
    content = (
        f'<?xml version="1.0" encoding="utf-8" standalone="yes"?>\n'
        f'<{root_tag}>\n'
        f'  <title>{esc_title}</title>\n'
        f'{orig_tag}{year_tag}{plot_tag}{tmdb_tag}{raw_tag}'
        f'  <lockdata>{eff_lock}</lockdata>\n'
        f'</{root_tag}>\n'
    )
    try:
        if os.path.exists(nfo_path):
            try:
                with open(nfo_path, 'r', encoding='utf-8') as f:
                    old_c = f.read()
                if old_c == content:
                    return False
                needs_fix = ('由 AI 影视元数据引擎智能对齐入库' in old_c) or ('&' in old_c and '&amp;' not in old_c)
            except Exception:
                needs_fix = True
            if not overwrite and not needs_fix:
                return False
        with open(nfo_path, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception:
        pass
    return False

def get_all_cached_items():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM media_ai_cache ORDER BY updated_at DESC')
    rows = cursor.fetchall()
    conn.close()
    return [dict(r) for r in rows]

def delete_cached_item(raw_name: str):
    if not raw_name:
        return False
    conn = get_db_connection()
    cursor = conn.cursor()
    norm = strip_timestamp_prefix(raw_name)
    names = [raw_name]
    if norm and norm != raw_name:
        names.append(norm)
    cursor.execute(f"DELETE FROM media_ai_cache WHERE raw_name IN ({','.join('?' * len(names))})", names)
    deleted = cursor.rowcount > 0
    conn.commit()
    conn.close()
    return deleted

def clean_orphaned_cached_items(valid_raw_names: set):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT raw_name, manual_override FROM media_ai_cache')
    rows = cursor.fetchall()
    valid_normalized = {strip_timestamp_prefix(n) for n in valid_raw_names if n}
    valid_all = set(valid_raw_names) | valid_normalized

    to_delete = []
    for r in rows:
        if r['manual_override']:
            continue
        db_raw = r['raw_name']
        db_norm = strip_timestamp_prefix(db_raw)
        if db_raw not in valid_all and db_norm not in valid_all:
            to_delete.append(db_raw)

    deleted_count = 0
    if to_delete:
        cursor.executemany('DELETE FROM media_ai_cache WHERE raw_name = ?', [(name,) for name in to_delete])
        deleted_count = len(to_delete)
        conn.commit()
    conn.close()
    return deleted_count

def get_existing_media_raw_names(video_root=paths.VIDEO_ROOT, cloud_files=None):
    names = set()
    if cloud_files:
        for rel in cloud_files:
            parts = [p for p in rel.split('/') if p and p != '视频']
            for p in parts[1:]:
                names.add(p)
        if names:
            return names
    if not os.path.exists(video_root):
        return names
    for cat in os.listdir(video_root):
        cat_p = os.path.join(video_root, cat)
        if not os.path.isdir(cat_p):
            continue
        for entry in os.listdir(cat_p):
            names.add(entry)
            entry_p = os.path.join(cat_p, entry)
            if os.path.isdir(entry_p):
                for sub in os.listdir(entry_p):
                    names.add(sub)
                    sub_p = os.path.join(entry_p, sub)
                    if os.path.isdir(sub_p):
                        for sub2 in os.listdir(sub_p):
                            if sub2.lower().endswith(('.mkv', '.mp4', '.ts', '.iso', '.mov')):
                                names.add(sub2)
                    elif sub.lower().endswith(('.mkv', '.mp4', '.ts', '.iso', '.mov')):
                        names.add(sub)
    return names

SERIES_CATEGORIES = {"国产剧集", "热门韩剧", "欧美剧集", "国产动漫"}
MOVIE_CATEGORIES = {"国产电影", "港台经典", "欧美大片", "韩国电影"}
SERIES_TO_MOVIE_CAT = {
    "欧美剧集": "欧美大片",
    "国产剧集": "国产电影",
    "热门韩剧": "韩国电影",
    "国产动漫": "国产动漫",
    "日本动漫": "日本动漫",
}
MOVIE_TO_SERIES_CAT = {
    "欧美大片": "欧美剧集",
    "国产电影": "国产剧集",
    "韩国电影": "热门韩剧",
}

def apply_calibration_to_cinema(raw_name, clean_title, year=None, media_type='movie', is_ad=0, old_raw_name=None, cinema_base=paths.CINEMA_DIR):
    new_folder_name = f"{clean_title} ({year})" if year else clean_title
    encoded_raw = urllib.parse.quote(raw_name)
    raw_names_to_match = [raw_name]
    encoded_names_to_match = [encoded_raw]
    if old_raw_name and old_raw_name != raw_name:
        raw_names_to_match.append(old_raw_name)
        encoded_names_to_match.append(urllib.parse.quote(old_raw_name))
    modified = False
    if not os.path.exists(cinema_base):
        return False
    for cat in os.listdir(cinema_base):
        cat_dir = os.path.join(cinema_base, cat)
        if not os.path.isdir(cat_dir):
            continue
        for item in os.listdir(cat_dir):
            item_path = os.path.join(cat_dir, item)
            if not os.path.isdir(item_path):
                continue
            matched = False
            for check_nfo in ["movie.nfo", "tvshow.nfo"]:
                nfo_file = os.path.join(item_path, check_nfo)
                if os.path.exists(nfo_file):
                    try:
                        with open(nfo_file, "r", encoding="utf-8") as f:
                            content = f.read()
                            for rn in raw_names_to_match:
                                esc_rn = xml_escape(rn)
                                if (
                                    f"<rawname>{rn}</rawname>" in content
                                    or f"<rawname>{esc_rn}</rawname>" in content
                                    or f"<originaltitle>{rn}</originaltitle>" in content
                                    or f"<originaltitle>{esc_rn}</originaltitle>" in content
                                ):
                                    matched = True
                                    break
                    except Exception:
                        pass
                if matched:
                    break
            if not matched:
                for root, _, files in os.walk(item_path):
                    for f in files:
                        if f.endswith(".strm"):
                            try:
                                with open(os.path.join(root, f), "r", encoding="utf-8") as sfp:
                                    strm_c = sfp.read()
                                    unquoted = urllib.parse.unquote(strm_c)
                                    for rn in raw_names_to_match:
                                        if rn in strm_c or rn in unquoted:
                                            matched = True
                                            break
                                    if not matched:
                                        for en in encoded_names_to_match:
                                            if en in strm_c:
                                                matched = True
                                                break
                            except Exception:
                                pass
                        if matched:
                            break
                    if matched:
                        break
            if not matched:
                for rn in raw_names_to_match:
                    if item == rn or item.startswith(rn):
                        matched = True
                        break
            if matched:
                if is_ad:
                    import shutil
                    try:
                        shutil.rmtree(item_path)
                        modified = True
                        print(f"已清理被标记为广告的影视目录: {item_path}")
                    except Exception as e:
                        print(f"清理广告目录失败 {item_path}: {type(e).__name__}")
                    continue
                target_cat = cat
                if media_type == "movie" and cat in SERIES_CATEGORIES:
                    target_cat = SERIES_TO_MOVIE_CAT.get(cat, "欧美大片")
                elif media_type in ("series", "tvshows") and cat in MOVIE_CATEGORIES:
                    target_cat = MOVIE_TO_SERIES_CAT.get(cat, "国产剧集")
                target_cat_dir = os.path.join(cinema_base, target_cat)
                os.makedirs(target_cat_dir, exist_ok=True)
                dest_item_path = os.path.join(target_cat_dir, new_folder_name)
                if item_path != dest_item_path:
                    try:
                        if os.path.exists(dest_item_path):
                            import shutil
                            shutil.rmtree(dest_item_path)
                        shutil.move(item_path, dest_item_path)
                        item_path = dest_item_path
                        modified = True
                    except Exception as e:
                        print(f"重命名目录失败 {item_path} -> {dest_item_path}: {type(e).__name__}")
                if media_type == "movie":
                    # 清理可能残留的剧集元数据
                    old_tv_nfo = os.path.join(item_path, "tvshow.nfo")
                    if os.path.exists(old_tv_nfo):
                        try:
                            os.remove(old_tv_nfo)
                        except Exception:
                            pass
                    # 若存在 Season 目录，将内部首个 strm 提升到电影根目录并清理季度目录
                    import shutil
                    for s_item in os.listdir(item_path):
                        s_path = os.path.join(item_path, s_item)
                        if os.path.isdir(s_path) and s_item.lower().startswith("season"):
                            for sf in os.listdir(s_path):
                                if sf.endswith(".strm"):
                                    target_strm = os.path.join(item_path, f"{new_folder_name}.strm")
                                    shutil.move(os.path.join(s_path, sf), target_strm)
                                    break
                            try:
                                shutil.rmtree(s_path)
                            except Exception:
                                pass
                    for f in os.listdir(item_path):
                        if f.endswith(".strm") and not any(k in f for k in ["- Part", "- CD"]):
                            old_strm = os.path.join(item_path, f)
                            new_strm = os.path.join(item_path, f"{new_folder_name}.strm")
                            if old_strm != new_strm:
                                try:
                                    os.rename(old_strm, new_strm)
                                    modified = True
                                except Exception:
                                    pass
                elif media_type in ("series", "tvshows"):
                    old_m_nfo = os.path.join(item_path, "movie.nfo")
                    if os.path.exists(old_m_nfo):
                        try:
                            os.remove(old_m_nfo)
                        except Exception:
                            pass
                generate_nfo_file(item_path, title=clean_title, year=year, media_type=media_type, original_title=raw_name, overwrite=True)
                modified = True
    return modified
