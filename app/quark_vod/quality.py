"""Resolution evidence from names. Hints never certify a decoded video."""
import re


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


def video_rank(video):
    # Only compare files whose work/season/episode identity already matched.
    hint = quality_hint(video.get('name') or video.get('file_name') or video.get('path', ''))
    return ({'4k': 3, 'hd': 2, 'unknown': 1}[hint['level']], video.get('size') or 0)
