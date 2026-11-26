"""Single authoritative deterministic episode parser shared with AI classification."""
import os
import re

_NOISE_RE = re.compile(
    r"(2160[pP]|1080[pP]|720[pP]|576[pP]|480[pP]|4[kK]|[hH]\.?26[45]|[xX]26[45]|hev[cs]|avc|"
    r"(?<!\d)(?:19|20)\d{2}(?!\d)|"
    r"[dD][dD][pP]?\s?\d(?:\.\d|\s\d)?|"
    r"(?:aac|ac3|eac3|truehd|dts(?:-?hd)?|flac|atmos)\s?\d(?:\.\d|\s\d)?|"
    r"[57]\.1|"
    r"\d{2,3}\s?[fF][pP][sS]|"
    r"8bit|10bit|12bit|"
    r"hdr1[04][pP]?|dv|hdr|dolby\s?vision|sdr|"
    r"web-dl|webrip|bluray|blu-ray|remux|bdrip|hdrip|"
    r"bd|hd|nf|amzn|\bit\b|ma|chte|cht|chs|"
    r"\bd\d+(?=\s+\d))",
    re.IGNORECASE)

# B2 步残留兜底：与 transfer.extract_episode_num 第二段清洗保持一致
_RESIDUE_RE = re.compile(
    r"(?<![A-Za-z0-9])[hH](?:\.\s*)?26[45](?![0-9])|"
    r"(?<![A-Za-z0-9])(?:2160|1080|720|576|480)(?![pP0-9])|"
    r"(?<![A-Za-z0-9])\d{2,3}[fF][pP][sS]|"
    r"(?<![A-Za-z0-9])[hH][dD][rR]1[04][pP]?",
    re.IGNORECASE)

# 集数恰为年份形态（S01E2010 / S01E1910）——清洗层会误剥，需交 AI 复核
_S_YEARLIKE_RE = re.compile(r"(?:[._\-\s]|^)S0*\d+E0*(?:19|20)\d{2}(?!\d)", re.IGNORECASE)

# 规则 D 的宽松模式（5 组）；命中即判为 weak
_WEAK_PATTERNS = (
    r"(?:^|[-_—\s])0*(\d{1,4})(?:[xXvV]\d*|[-_—\[\]\(\)\.\s]|$)",
    r"(?:[-_—]\s*)0*(\d{1,4})(?:\s*[-_—\[\]\(\)\.]|\s*$)",
    r"(?:\[|\()0*(\d{1,4})(?:\]|\))",
    r"(?:^|\s+)0*(\d{1,4})(?:\s+|$)",
    r"(?:[._\-\s]|^)0*(\d{1,4})$",
)


def _clean_stem(path_or_name):
    name = os.path.basename(path_or_name)
    stem, _ = os.path.splitext(name)
    return stem


def classify_by_rules(path_or_name, season=1):
    """按规则给文件名分级。

    返回 (episode_or_None, tier)：
      (集数, 'strong')  规则 A/B(季号相符)/C 命中 —— 硬证据，AI 无权改写
      (None, 'conflict') 规则 B 季号冲突 —— 跨季隔离，AI 无权改写
      (集数, 'weak')    规则 D 宽松匹配命中 —— 可疑，送 AI 复核
      (None, 'none')    完全未命中 —— 送 AI 确认是否为真实集数
    """
    stem = _clean_stem(path_or_name)

    # ── 强证据必须在清洗前判定：清洗会剥掉年月，但 S04E37 不受影响 ──
    #
    # 2026-11-13 补充：_NOISE_RE 里无边界保护的 `19\d\d|20\d\d` 会在
    #   「集数恰好是年份形态」时误伤集数——`某剧.S01E2010.mkv` 被清洗成
    #   `S01E10`，规则退回 None，播放时提示「未找到对应集数」。
    #   此类命名在此处用【未清洗的原始 stem】预判强证据，仅用于 tier 分级，
    #   不改动 transfer.extract_episode_num 的任何既有行为。
    if _S_YEARLIKE_RE.search(stem):
        return None, "weak"   # 交 AI 复核，由模型从原始文件名读出真实集数

    if re.search(r"(?<!\d)20\d{6}(?!\d)", stem):
        return None, "none"
    clean = _NOISE_RE.sub(" ", stem)

    # 规则 A：显式目标季 S{season}E{ep}
    # 注意：此处刻意采用与 transfer.extract_episode_num 完全相同的正则与优先级——
    #   ① 前后断言用 (?:[._\-\s]|$) ，因此 "S04E37 - 标题" 这类「集数后接空格」
    #      在现有规则里同样匹配不到（它最终落到规则 B 被季号冲突判 None）。
    #      name_parser 是旁路兜底，必须与主路径口径一致，不得擅自「修好」主路径。
    #   ② transfer 已先判规则 D 再判 A/B（见红线 8），故 "4K_HDR_60" 在现有规则
    #      里仍返回 60。这些已知缺口正是 AI 兜底要覆盖的对象。
    m = re.search(rf"(?:[._\-\s]|^)S0*{season}E0*(\d+)(?:[._\-\s]|$)", clean, re.IGNORECASE)
    if m:
        return int(m.group(1)), "strong"

    # 规则 B：任意季 SxEy —— 季号不符即冲突，绝不交给 AI
    m_any_s = re.search(r"(?:[._\-\s]|^)S0*(\d+)E0*(\d+)(?:[._\-\s]|$)", clean, re.IGNORECASE)
    if m_any_s:
        s_found, ep_found = int(m_any_s.group(1)), int(m_any_s.group(2))
        if season == 0 or s_found == season:
            return ep_found, "strong"
        return None, "conflict"

    # B2 残留兜底后再判 C/D
    clean = _RESIDUE_RE.sub(" ", clean)
    clean = re.sub(r"[_\-\.\s]+", " ", clean).strip()

    # 规则 C：第xx集 / 第xx话 / EPxx —— 强证据
    for pat in (r"(?:第|EP|E)\s*0*(\d+)\s*(?:集|话|回|v\d+)?",
                r"0*(\d+)\s*(?:集|话|回)"):
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            return int(m.group(1)), "strong"

    # 规则 D：宽松匹配 —— 这是事故来源，标记 weak 交由 AI 复核
    for pat in _WEAK_PATTERNS:
        m = re.search(pat, clean, re.IGNORECASE)
        if m:
            val = int(m.group(1))
            if 1 <= val <= 2500:
                return val, "weak"

    return None, "none"


def extract_episode_num(name, season=1):
    return classify_by_rules(name, season)[0]
