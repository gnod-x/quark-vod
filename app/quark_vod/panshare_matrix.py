"""Bounded multi-node Quark search with pagination and observable failures."""
import hashlib
import html
import json
import os
import re
import tempfile
import threading
import time
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from . import config, db

UA = {"User-Agent": "Mozilla/5.0"}
ACTIVE_PAN_NODES = [
    {"key": "kuaiying",     "name": "快映•4K",     "base": "http://38.76.197.172:12521",  "timeout": 4.0},
    {"key": "huban_ip",     "name": "虎斑•高速",   "base": "http://38.76.197.172:16969", "timeout": 3.5},
    {"key": "labi",         "name": "蜡笔•4K",     "base": "https://www.fmcf.fun",        "timeout": 4.5},
    # 2026-11-13 移除「蜡笔•备用 mmpanpan.site」：三次实测 3.1/3.0/5.8s，
    #   内容与蜡笔•4K 完全同源（详情页数一致），去重后零增益却拖慢整轮并发。
    {"key": "duoduo",       "name": "多多•4K",     "base": "https://tv.yydsys.cc",        "timeout": 4.5},
    {"key": "duoduo_xyz",   "name": "多多•备用",   "base": "https://tv.214521.xyz",       "timeout": 4.5},
    # 2026-11-13 移除「虎斑•备用 xhban.xyz」：三次实测全部 TimeoutError（稳定失效），
    #   每个失效节点都要耗满 timeout 才返回，是单轮耗时的主要浪费来源。
    #   虎斑主 IP 38.76.197.172:16969 健康（0.3~0.4s / 详情页 10），保留。
    # 2026-11-16: woog.nxog.eu.org 搜索连续 HTTP 失败，复测 520，暂移出。
    # 恢复后先通过搜索和详情解析验活再加入；不自动改用未经验证的镜像。
]

SEARCH_CACHE_TTL = 1800
SEARCH_CACHE_DIR = os.path.join(config.DATA_DIR, "search_cache")
_CACHE_MAGIC = 3
_mem_cache = {}
_cache_lock = threading.RLock()
_search_slots = threading.BoundedSemaphore(2)
_http_slots = threading.BoundedSemaphore(12)


def canonical_share(url):
    m = re.search(r'https?://pan\.quark\.cn/s/([a-zA-Z0-9]+)', html.unescape(url or ''))
    return 'https://pan.quark.cn/s/' + m.group(1) if m else ''


def extract_quark_links_and_pwd(text):
    text = html.unescape(text or '')
    found = {}
    for m in re.finditer(r'https?://pan\.quark\.cn/s/[a-zA-Z0-9]+(?:[?#][^\s"\'<>，；]+)?', text):
        raw = m.group().rstrip('.,。;)）')
        base = canonical_share(raw)
        parsed = urllib.parse.urlsplit(raw)
        pwd = urllib.parse.parse_qs(parsed.query + '&' + parsed.fragment).get('pwd', [''])[0]
        if not pwd:
            tail = re.sub('<[^>]+>', ' ', text[m.end():m.end()+120])
            code = re.match(r'[\s,，;；:：]*(?:提取码|密码|pwd)[:：\s]*([a-zA-Z0-9]{4})(?![a-zA-Z0-9])', tail, re.I)
            pwd = code.group(1) if code else ''
        result = base + ('#pwd=' + pwd if re.fullmatch('[a-zA-Z0-9]{4}', pwd) else '')
        if base not in found or '#pwd=' in result:
            found[base] = result
    return list(found.values())


def parse_download_rows(text):
    rows=[]
    chunks=re.split(r'<div\b[^>]*class=["\'][^"\']*\bmodule-row-title\b[^"\']*["\'][^>]*>', text or '', flags=re.I)
    for chunk in chunks[1:]:
        title=re.search(r'<h4\b[^>]*>(.*?)</h4>',chunk,re.S|re.I)
        if not title:continue
        name=html.unescape(re.sub('<[^>]+>', '', title.group(1))).strip()
        # Preserve version/season suffixes; never truncate at the first hyphen.
        for link in extract_quark_links_and_pwd(chunk):
            rows.append({'title':name,'url':link})
    return rows


def detail_paths(text):
    links=re.findall(r'href\s*=\s*["\']([^"\']+)["\']',text,re.I)
    return list(dict.fromkeys(html.unescape(p) for p in links
        if re.fullmatch(r'/(?:index\.php/vod/detail/id/\d+|voddetail/\d+)\.html',p)))


def next_page(text, current, base):
    origin=urllib.parse.urlsplit(base)
    for match in re.finditer(r'<a\b([^>]*?)>(.*?)</a>',text,re.I|re.S):
        label=html.unescape(re.sub('<[^>]+>','',match.group(2))).strip()
        if not ('下一页' in label or label.lower() in ('next','>','›','»') or re.search(r'rel=["\']next',match.group(1),re.I)):
            continue
        href=re.search(r'href=["\']([^"\']+)',match.group(1),re.I)
        if not href:continue
        url=urllib.parse.urljoin(current,html.unescape(href.group(1)))
        parsed=urllib.parse.urlsplit(url)
        if parsed.netloc==origin.netloc and parsed.scheme==origin.scheme and '/search' in parsed.path and url!=current:
            return url
    return None


def _cache_key(keyword, pages=3, limit=24):
    raw=f'{keyword.strip()}|{pages}|{limit}'
    return 'ps3_'+hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_path(key):return os.path.join(SEARCH_CACHE_DIR,key+'.json')


def _cache_get(key):
    with _cache_lock:
        blob=_mem_cache.get(key)
        if blob is None:
            try:
                with open(_cache_path(key),encoding='utf-8') as f:blob=json.load(f)
            except (OSError,ValueError):return None
        if blob.get('magic')!=_CACHE_MAGIC or blob.get('expire',0)<=time.time():
            _mem_cache.pop(key,None);return None
        _mem_cache[key]=blob
        while len(_mem_cache)>128:_mem_cache.pop(next(iter(_mem_cache)))
        return blob['payload']


def _cache_put(key,payload):
    # Partial/outage results expire quickly; never hide failures for 30 minutes.
    ttl=SEARCH_CACHE_TTL if payload['items'] and not payload['partial'] else 45
    blob={'magic':_CACHE_MAGIC,'expire':time.time()+ttl,'payload':payload}
    with _cache_lock:
        _mem_cache[key]=blob
        while len(_mem_cache)>128:_mem_cache.pop(next(iter(_mem_cache)))
    tmp=None
    try:
        os.makedirs(SEARCH_CACHE_DIR,exist_ok=True)
        with tempfile.NamedTemporaryFile(mode='w',encoding='utf-8',dir=SEARCH_CACHE_DIR,delete=False) as f:
            tmp=f.name;json.dump(blob,f,ensure_ascii=False)
        os.replace(tmp,_cache_path(key));tmp=None
    except OSError:pass
    finally:
        if tmp:
            try:os.unlink(tmp)
            except OSError:pass


def cache_purge(keyword=None):
    with _cache_lock:
        keys=[_cache_key(keyword)] if keyword else list(_mem_cache)
        for k in keys:_mem_cache.pop(k,None)
    if os.path.isdir(SEARCH_CACHE_DIR):
        for name in os.listdir(SEARCH_CACHE_DIR):
            if name.endswith('.json') and (not keyword or name==_cache_key(keyword)+'.json'):
                try:os.unlink(os.path.join(SEARCH_CACHE_DIR,name))
                except OSError:pass
    return len(keys)


def _fetch_html(url,timeout):
    if not _http_slots.acquire(timeout=timeout):raise TimeoutError('search capacity')
    try:
        with urllib.request.urlopen(urllib.request.Request(url,headers=UA),timeout=timeout) as response:
            data=response.read(2*1024*1024+1)
            if len(data)>2*1024*1024:raise ValueError('search response too large')
            return data.decode('utf-8','replace')
    finally:_http_slots.release()


def _fetch_node(node,keyword,limit,max_pages):
    start=time.monotonic();deadline=start+22
    base=node['base'].rstrip('/')
    url=base+'/index.php/vod/search.html?wd='+urllib.parse.quote(keyword)
    report={'node':node['key'],'pages':0,'details':0,'errors':0,'truncated':False}
    paths=[];visited=set();items=[]
    try:
        for page in range(max_pages):
            if not url or url in visited:break
            visited.add(url)
            text=_fetch_html(url,min(7,max(.1,deadline-time.monotonic())))
            report['pages']+=1
            paths=list(dict.fromkeys(paths+detail_paths(text)))
            following=next_page(text,url,base)
            if len(paths)>=limit or time.monotonic()>=deadline:
                report['truncated']=bool(following) or len(paths)>limit;break
            url=following
        else:report['truncated']=bool(url)
    except Exception as exc:report['errors']+=1;report['error_type']=type(exc).__name__
    def detail(path):
        if time.monotonic()>=deadline:raise TimeoutError('node deadline')
        text=_fetch_html(base+path,min(7,max(.1,deadline-time.monotonic())))
        rows=parse_download_rows(text)
        if not rows:
            tm=re.search(r'<title[^>]*>(.*?)</title>',text,re.S|re.I)
            name=html.unescape(re.sub('<[^>]+>','',tm.group(1))).strip() if tm else keyword
            rows=[{'title':name,'url':url} for url in extract_quark_links_and_pwd(text)]
        return [{'source':node['key'],'name':r['title'],'share_url':r['url']} for r in rows]
    with ThreadPoolExecutor(max_workers=3) as pool:
        futures=[pool.submit(detail,path) for path in paths[:limit]]
        for future in as_completed(futures):
            try:items.extend(future.result());report['details']+=1
            except Exception as exc:report['errors']+=1;report['error_type']=type(exc).__name__
    report.update(seconds=round(time.monotonic()-start,3),candidates=len(items))
    return items,report


def merge_items(items):
    from .quality import quality_hint
    merged={}
    # Deterministic dedup, retain all title evidence and a later extraction code.
    for item in sorted(items,key=lambda i:(i.get('source',''),i.get('name',''))):
        base=canonical_share(item.get('share_url'))
        if not base:continue
        links=extract_quark_links_and_pwd(item['share_url'])
        if not links:continue
        name=item.get('name','').strip()
        if base not in merged:
            merged[base]={**item,'share_url':links[0],'names':[],'sources':[]}
        dst=merged[base]
        for title in item.get('names',[name]):
            if title and title not in dst['names']:dst['names'].append(title)
        for source in item.get('sources',[item.get('source','')]):
            if source and source not in dst['sources']:dst['sources'].append(source)
        if '#pwd=' in links[0]:dst['share_url']=links[0]
        dst['name']=max(dst['names'],key=lambda n:(quality_hint(n)['level']=='4k',len(n)))
    return list(merged.values())


class PanshareMatrix:
    @classmethod
    def search_report(cls,keyword,limit_per_node=24,max_workers=5,use_cache=True,max_pages=3):
        keyword=' '.join((keyword or '').split())[:120]
        empty={'items':[],'nodes':[],'partial':False,'cached':False}
        if not keyword:return empty
        max_pages=max(1,min(int(max_pages),3));limit_per_node=max(1,min(int(limit_per_node),30))
        key=_cache_key(keyword,max_pages,limit_per_node)
        if use_cache:
            hit=_cache_get(key)
            if hit is not None:return cls._filter_dead({**hit,'cached':True})
        if not _search_slots.acquire(timeout=2):return {**empty,'partial':True,'busy':True}
        try:
            items=[];reports=[]
            try:
                with open(os.path.join(config.BASE_DIR,'web','daily_hot.json'),encoding='utf-8') as f:hot=json.load(f)
                for row in hot if isinstance(hot,list) else []:
                    if keyword.casefold() in row.get('clean_name','').casefold():
                        items.append({'source':'daily_hot','name':row.get('title') or row['clean_name'],'share_url':row.get('quark_url','')})
            except (OSError,ValueError,KeyError,TypeError):pass
            with ThreadPoolExecutor(max_workers=max(1,min(max_workers,len(ACTIVE_PAN_NODES)))) as pool:
                futures=[pool.submit(_fetch_node,n,keyword,limit_per_node,max_pages) for n in ACTIVE_PAN_NODES]
                for future in as_completed(futures):
                    found,report=future.result();items.extend(found);reports.append(report)
            result={'items':merge_items(items),'nodes':sorted(reports,key=lambda n:n['node']),
                    'partial':any(r['errors'] for r in reports),'cached':False}
            if use_cache:_cache_put(key,result)
            return cls._filter_dead(result)
        finally:_search_slots.release()

    @staticmethod
    def _filter_dead(report):
        dead={canonical_share(u) for u in db.get_dead_sources_set('panshare')}
        return {**report,'items':[r for r in report['items'] if canonical_share(r['share_url']) not in dead]}

    @classmethod
    def search(cls,keyword,limit_per_node=24,max_workers=5,use_cache=True):
        from .search import SearchResult
        report=cls.search_report(keyword,limit_per_node,max_workers,use_cache)
        return [SearchResult(engine='panshare',source=r['source'],name=r['name'],year='',poster='',episodes=[],
                share_url=r['share_url'],pan_type='quark',names=r.get('names'),sources=r.get('sources')) for r in report['items']]
