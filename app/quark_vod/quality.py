"""Structured release hints; no filename can certify a decoded video."""
import re
from . import config

def quality_hint(name, parent=''):
    text = name or ''
    dimensions = re.search(r'(?<!\d)(\d{3,5})\s*[xX×]\s*(\d{3,5})(?!\d)', text)
    if dimensions:
        width, height = map(int, dimensions.groups())
        level = '4k' if width >= 3840 and height >= 1600 else 'hd'
        return {'level': level, 'label': f'{width}×{height} 标注', 'evidence': 'dimensions', 'verified': False}
    low = re.search(r'(?i)(?<!\d)(1080|720|576|480)\s*[pi](?![a-z])', text)
    if low:
        return {'level': 'hd', 'label': low.group(1) + 'p 标注', 'evidence': 'filename', 'verified': False}
    if re.search(r'(?i)(?<![\da-z])(?:4\s*k|uhd)(?![\da-z])|(?<!\d)2160\s*p?(?![\dx×])', text):
        return {'level': '4k', 'label': '4K 标注·待实测', 'evidence': 'filename', 'verified': False}
    if parent:
        hint = quality_hint(parent)
        if hint['level'] == '4k':
            return {**hint, 'label': '分享标注 4K·文件未标注', 'evidence': 'share_title'}
    return {'level': 'unknown', 'label': '分辨率未标注', 'evidence': 'none', 'verified': False}

def parse_media_tags(name, size_bytes=0, media_type='movie'):
    """Parse five release dimensions without claiming probe verification."""
    text = name or ''
    hint = quality_hint(text)
    match = re.search(r'(?i)(?<!\d)(2160|1080|720|576|480)\s*[pi]?', text)
    resolution = int(match.group(1)) if match else (2160 if hint['level']=='4k' else 0)
    dimensions=re.search(r'(?<!\d)(\d{3,5})\s*[xX×]\s*(\d{3,5})(?!\d)',text)
    if dimensions:
        width,height=map(int,dimensions.groups())
        resolution=2160 if width>=3840 and height>=1600 else min(height,1080)
    dv = bool(re.search(r'(?i)(?<![a-z])(?:DV|DoVi|Dolby[ ._-]*Vision)(?![a-z])|杜比视界', text))
    hdr = bool(re.search(r'(?i)HDR(?:10\+?)?|HLG', text))
    profile5 = dv and not hdr
    audio = ('dual' if re.search(r'国粤双语|多音轨|双音轨|双语',text) else
             'dubbed' if re.search(r'国语|国配',text) else 'original')
    subtitles = ('effects' if re.search(r'特效字幕|简繁特效',text) else
                 'chinese' if re.search(r'内封简繁|中字|中文字幕|简中|CHS',text,re.I) else 'unknown')
    remux = bool(re.search(r'(?i)REMUX|原盘',text))
    max_size = getattr(config, 'MAX_FILE_SIZE', 30*1024**3)
    size = max(0,int(size_bytes or 0))
    resolution_score = 400 if resolution>=2160 else 300 if resolution>=1080 else 180 if resolution>=720 else 50 if resolution else 120
    score = resolution_score + (45 if dv and hdr else -30 if profile5 else 40 if hdr else 20 if re.search(r'(?i)SDR',text) else 0)
    score += 25 if audio=='dual' else 15 if audio=='dubbed' else 0
    score += 10 if re.search(r'(?i)Atmos|杜比全景声',text) else 0
    score += 20 if subtitles=='effects' else 10 if subtitles=='chinese' else 0
    low, high = ((1.2,6) if media_type in ('tv','anime') else (6,28))
    gib=size/1024**3
    score += 15 if low<=gib<=high else 0
    score += 8 if re.search(r'(?i)HEVC|H[ .]?265|x265',text) and re.search(r'(?i)10[ ._-]?bit',text) else 0
    if max_size and size>max_size: score -= 65
    badges=[]
    if resolution: badges.append('4K' if resolution>=2160 else f'{resolution}P')
    if dv: badges.append('DV·需设备支持' if profile5 else 'DV+HDR')
    elif hdr: badges.append('HDR10')
    if audio!='original': badges.append('国粤双语' if audio=='dual' else '国语')
    if subtitles=='effects': badges.append('特效字幕')
    if remux: badges.append('REMUX')
    return {'resolution':resolution,'dynamic_range':'dv_profile5' if profile5 else 'dv_hdr' if dv else 'hdr' if hdr else 'sdr' if re.search('SDR',text,re.I) else 'unknown',
            'dv_requires_support':profile5,'audio':audio,'subtitles':subtitles,'remux':remux,
            'size_bytes':size,'oversized':bool(max_size and size>max_size),'badges':badges,'score':score,'verified':False}

def matches_variant(tags, variant):
    if variant=='hdr': return tags['resolution']>=2160 and tags['dynamic_range'] in ('hdr','dv_hdr')
    if variant=='sdr': return not tags['dv_requires_support'] and tags['dynamic_range'] not in ('hdr','dv_hdr')
    if variant=='dubbed': return tags['audio'] in ('dual','dubbed')
    if variant=='remux': return tags['remux']
    return True

def video_rank(video):
    name=video.get('name') or video.get('file_name') or video.get('path','')
    tags=parse_media_tags(name,video.get('size') or 0,video.get('media_type','movie'))
    return tags['score'], -(video.get('size') or 0) if tags['oversized'] else (video.get('size') or 0)
