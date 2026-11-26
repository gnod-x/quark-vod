"""Work identity shared by playback, state lookup and STRM activation."""
from dataclasses import dataclass
import re
import unicodedata


def normalize(text):
    text = unicodedata.normalize('NFKC', text or '')
    return ' '.join(''.join(c for c in text if unicodedata.category(c) != 'Cf').split())


def split_title(title):
    title = normalize(title)
    match = re.search(r'\s*\(((?:19|20)\d{2})\)$', title)
    return (title[:match.start()].strip(), match.group(1)) if match else (title, '')


def same_title(a, b):
    """Unknown years are not proof of identity with a dated remake."""
    an, ay = split_title(a)
    bn, by = split_title(b)
    return bool(an) and an.casefold() == bn.casefold() and ay == by


def media_kind(cat, season=0):
    if '动漫' in cat or '动画' in cat:
        return 'anime'
    if season > 0 or ('剧' in cat and '电影' not in cat):
        return 'tv'
    return 'movie'


@dataclass(frozen=True)
class Work:
    cat: str
    title: str
    season: int = 0
    episode: int = 0
    variant: str = ""
    media_type: str = ""

    def __post_init__(self):
        object.__setattr__(self, 'cat', normalize(self.cat))
        object.__setattr__(self, 'title', normalize(self.title))
        if not self.title or self.season < 0 or self.episode < 0:
            raise ValueError('标题或季集参数无效')
        if self.variant not in ('', 'hdr', 'sdr', 'dubbed', 'remux'):
            raise ValueError('未知播放版本')
        if (self.season > 0 and self.episode == 0
                or self.season == 0 and self.episode > 0
                and self.media_type not in ('tv','anime')
                and media_kind(self.cat) == 'movie'):
            raise ValueError('电影使用 0/0；特别篇使用 Season 00 与明确剧集分类')

    @property
    def key(self):
        return (self.cat, self.title.casefold(), self.season, self.episode, self.variant)

    @property
    def args(self):
        return self.cat, self.title, self.season, self.episode

    @property
    def options(self):
        return {'variant': self.variant} if self.variant else {}
