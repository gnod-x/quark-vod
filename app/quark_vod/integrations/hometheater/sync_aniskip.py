
from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import os
import sys
import json
import re
import urllib.request
import urllib.parse
from quark_vod.integrations.hometheater.skip_manager import write_episode_timestamps, episode_items, _segments


BASE_DIR = paths.BASE_DIR

ANILIST_URL = "https://graphql.anilist.co"
ANISKIP_URL = "https://api.aniskip.com/v2/skip-times"
BANGUMI_URL = "https://api.bgm.tv/search/subject"

def fetch_json(url, headers=None, data=None, timeout=10):
    req_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, headers=req_headers, data=data)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception:
        return None

def resolve_japanese_title(title):
    clean = re.sub(r'\s*\(\d{4}\)$', '', title).strip()
    url = f"{BANGUMI_URL}/{urllib.parse.quote(clean)}?type=2"
    data = fetch_json(url)
    if data and data.get("list"):
        first = data["list"][0]
        return first.get("name") or clean
    return clean

def query_anilist_media(title):
    query = """
    query ($search: String) {
      Page(page: 1, perPage: 25) {
        media(search: $search, type: ANIME) {
          id
          idMal
          format
          episodes
          title {
            romaji
            english
            native
          }
        }
      }
    }
    """
    body = json.dumps({"query": query, "variables": {"search": title}}).encode('utf-8')
    res = fetch_json(ANILIST_URL, headers={"Content-Type": "application/json"}, data=body)
    if res and "data" in res and res["data"].get("Page"):
        return res["data"]["Page"].get("media", [])
    return []

def map_seasons_to_mal(media_list):
    season_patterns = {
        1: [r'season\s*1\b', r'1st\s*season', r'first\s*season', r'第1季', r'第一季'],
        2: [r'season\s*2\b', r'2nd\s*season', r'second\s*season', r'\bzoku\b', r'続', r'弐', r'第2季', r'第二季'],
        3: [r'season\s*3\b', r'3rd\s*season', r'third\s*season', r'\bsan\b', r'参', r'第3季', r'第三季'],
        4: [r'season\s*4\b', r'4th\s*season', r'fourth\s*season', r'\bshi\b', r'肆', r'第4季', r'第四季'],
        5: [r'season\s*5\b', r'5th\s*season', r'fifth\s*season', r'\bgo\b', r'伍', r'第5季', r'第五季'],
        6: [r'season\s*6\b', r'6th\s*season', r'sixth\s*season', r'\broku\b', r'陸', r'陆', r'第6季', r'第六季'],
        7: [r'season\s*7\b', r'7th\s*season', r'seventh\s*season', r'\bshichi\b', r'漆', r'第7季', r'第七季'],
        8: [r'season\s*8\b', r'8th\s*season', r'eighth\s*season', r'\bhachi\b', r'捌', r'第8季', r'第八季'],
    }

    mapped = {}
    valid_media = []
    for m in media_list:
        if not m.get("idMal"):
            continue
        eng = (m["title"].get("english") or "").lower()
        rom = (m["title"].get("romaji") or "").lower()
        nat = (m["title"].get("native") or "").lower()
        full = f"{eng} {rom} {nat}"
        if any(bad in full for bad in ["special", "ova", "movie", "gekijouban", "tokubetsu", "road to"]):
            continue
        valid_media.append((m["idMal"], full))

    for s_num in range(2, 9):
        patterns = season_patterns[s_num]
        for id_mal, full in valid_media:
            if any(re.search(pat, full, re.IGNORECASE) for pat in patterns):
                mapped[s_num] = id_mal
                break

    for id_mal, full in valid_media:
        if any(re.search(pat, full, re.IGNORECASE) for pat in season_patterns[1]):
            mapped[1] = id_mal
            break

    if 1 not in mapped and valid_media:
        for id_mal, full in valid_media:
            has_other = False
            for s_num in range(2, 9):
                if any(re.search(pat, full, re.IGNORECASE) for pat in season_patterns[s_num]):
                    has_other = True
                    break
            if not has_other:
                mapped[1] = id_mal
                break

    return mapped

def fetch_aniskip_single(mal_id, episode_num):
    url = f"{ANISKIP_URL}/{mal_id}/{episode_num}?types=op&types=ed&episodeLength=0"
    data = fetch_json(url)
    if not data or not data.get("found"):
        return None
    res = {}
    for item in data.get("results", []):
        st = item.get("skipType")
        interval = item.get("interval", {})
        start = interval.get("startTime", 0.0)
        end = interval.get("endTime", 0.0)
        if st == "op":
            res["op"] = (round(start, 3), round(end, 3))
        elif st == "ed":
            res["ed"] = (round(start, 3), round(end, 3))
    return res if res else None

def sync_series(series_name):
    episodes = episode_items(series_name)
    clean_name = re.sub(r'\s*\(\d{4}\)$', '', series_name).strip()
    japanese = resolve_japanese_title(clean_name)
    media = query_anilist_media(japanese)
    if not media and japanese != clean_name:
        media = query_anilist_media(clean_name)
    mapping = map_seasons_to_mal(media)
    tagged = 0
    for item in episodes:
        season = item.get("ParentIndexNumber")
        ep = item.get("IndexNumber")
        if not season or not ep or season not in mapping:
            continue
        if _segments(item["Id"]) and "--force" not in sys.argv:
            continue
        intervals = fetch_aniskip_single(mapping[season], ep)
        if intervals:
            write_episode_timestamps(None, None, item["Id"], intervals.get("op"), intervals.get("ed"), provider="AniSkip")
            tagged += 1
    return tagged


def scan_all_anime():
    series = sorted({x["SeriesName"] for x in episode_items()
                     if "/日本动漫/" in (x.get("Path") or "") and x.get("SeriesName")})
    return sum(sync_series(name) for name in series)


if __name__ == "__main__":
    print(scan_all_anime() if len(sys.argv) < 2 or sys.argv[1] == "all" else sync_series(sys.argv[1]))
