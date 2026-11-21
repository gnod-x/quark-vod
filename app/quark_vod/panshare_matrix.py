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
from . import search_plugins

UA = {"User-Agent": "Mozilla/5.0"}
# Deprecated test/embedding override; deployment sources live in VOD_SEARCH_CONFIG.
ACTIVE_PAN_NODES = []

SEARCH_CACHE_TTL = 1800
SEARCH_CACHE_DIR = os.path.join(config.DATA_DIR, "search_cache")
_CACHE_MAGIC = 4
_mem_cache = {}
_cache_lock = threading.RLock()
_cache_sweep_at = 0.0
CACHE_SWEEP_INTERVAL = 60
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


def source_snapshot():
    registry = search_plugins.get_registry()
    snapshot = registry.snapshot()
    error = registry.status(refresh=False)['error']
    # Old embedding/test code may explicitly set this list. An explicit JSON
    # configuration always wins, including an intentionally empty one.
    if not os.environ.get('VOD_SEARCH_CONFIG') and ACTIVE_PAN_NODES:
        snapshot = search_plugins.snapshot_from_nodes(ACTIVE_PAN_NODES)
        error = None
    return snapshot, error


def _cache_key(keyword, pages=3, limit=24, fingerprint=None):
    if fingerprint is None:
        fingerprint = source_snapshot()[0].fingerprint
    raw=f'{keyword.strip()}|{pages}|{limit}|{fingerprint}'
    return 'ps4_'+hashlib.sha256(raw.encode()).hexdigest()[:32]


def _cache_path(key):return os.path.join(SEARCH_CACHE_DIR,key+'.json')


def _cache_get(key):
    with _cache_lock:
        blob=_mem_cache.get(key)
        if blob is None:
            try:
                with open(_cache_path(key),encoding='utf-8') as f:blob=json.load(f)
            except (OSError,ValueError):return None
        if blob.get('magic')!=_CACHE_MAGIC or blob.get('expire',0)<=time.time():
            _mem_cache.pop(key,None)
            try:os.unlink(_cache_path(key))
            except OSError:pass
            return None
        _mem_cache[key]=blob
        while len(_mem_cache)>128:_mem_cache.pop(next(iter(_mem_cache)))
        return blob['payload']


def _expire_disk_cache():
    """Remove only our regular cache files after their maximum possible TTL."""
    global _cache_sweep_at
    with _cache_lock:
        now = time.monotonic()
        if now < _cache_sweep_at:
            return
        _cache_sweep_at = now + CACHE_SWEEP_INTERVAL
        cutoff = time.time() - SEARCH_CACHE_TTL
        try:
            with os.scandir(SEARCH_CACHE_DIR) as entries:
                for entry in entries:
                    if not re.fullmatch(r"ps4_[0-9a-f]{32}\.json", entry.name):
                        continue
                    try:
                        if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime <= cutoff:
                            os.unlink(entry.path)
                    except OSError:
                        pass
        except OSError:
            pass


def _cache_put(key,payload):
    _expire_disk_cache()
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
            text=_fetch_html(url,min(node.get('timeout', 7),max(.1,deadline-time.monotonic())))
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
        text=_fetch_html(base+path,min(node.get('timeout', 7),max(.1,deadline-time.monotonic())))
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



def _fetch_json_node(node, keyword, limit, max_pages):
    start = time.monotonic()
    report = {'node': node['key'], 'pages': 0, 'details': 0, 'errors': 0,
              'truncated': False}
    items = []
    try:
        url = node['base'].rstrip('/') + node.get('path', '/search') + '?' + urllib.parse.urlencode({
            node.get('query_param', 'q'): keyword})
        body = json.loads(_fetch_html(url, node.get('timeout', 4.0)))
        if not isinstance(body, dict) or not isinstance(body.get('items'), list):
            raise ValueError('invalid provider response')
        report['pages'] = 1
        report['truncated'] = len(body['items']) > limit
        report['partial'] = bool(body.get('partial', False))
        for row in body['items'][:limit]:
            if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                or not row['name'].strip() or len(row['name']) > 1000
                or not isinstance(row.get('share_url'), str)
                or len(row['share_url']) > 4096
                or not canonical_share(row['share_url'])):
                report['errors'] += 1
                continue
            items.append({'source': node['key'], 'name': row['name'].strip(),
                          'share_url': row['share_url']})
    except Exception as exc:
        report['errors'] += 1
        report['error_type'] = type(exc).__name__
    report.update(seconds=round(time.monotonic()-start, 3), candidates=len(items))
    return items, report


def _fetch_provider(node, keyword, limit, max_pages):
    provider = node.get('provider', 'html')
    handler = (_fetch_node if provider == 'html' else
               _fetch_json_node if provider == 'json' else search_plugins.get_provider(provider))
    started = time.monotonic()
    try:
        if handler is None:
            raise ValueError('provider unavailable')
        items, report = handler(node, keyword, limit, max_pages)
        if not isinstance(items, list) or not isinstance(report, dict):
            raise ValueError('invalid provider result')
        if len(items) > 1200:
            raise ValueError('provider result too large')
        normalized = []
        invalid = 0
        for row in items:
            if (not isinstance(row, dict) or not isinstance(row.get('name'), str)
                or not isinstance(row.get('share_url'), str)
                or len(row['name']) > 1000 or len(row['share_url']) > 4096):
                invalid += 1
                continue
            normalized.append({'name': row['name'], 'share_url': row['share_url'],
                               'source': node['key']})
        # Retain diagnostic counters only. Plugins must not leak upstream URLs
        # or arbitrary error text into the public report.
        safe = {'node': node['key'], 'provider': provider,
                'errors': max(0, min(int(report.get('errors', 0)), 10000)) + invalid,
                'pages': max(0, min(int(report.get('pages', 0)), 10000)),
                'details': max(0, min(int(report.get('details', 0)), 10000)),
                'truncated': bool(report.get('truncated')),
                'partial': bool(report.get('partial')),
                'seconds': round(time.monotonic()-started, 3),
                'candidates': len(normalized)}
        if safe['errors']:
            safe['error_type'] = 'ProviderError'
        return normalized, safe
    except Exception:
        return [], {'node': node['key'], 'provider': provider, 'pages': 0,
                    'details': 0, 'errors': 1, 'truncated': False,
                    'error_type': 'ProviderError', 'candidates': 0,
                    'seconds': round(time.monotonic()-started, 3)}


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
    def search_report(cls,keyword,limit_per_node=24,max_workers=5,use_cache=True,max_pages=3,
                      *, snapshot=None, configuration_error=None):
        keyword=' '.join((keyword or '').split())[:120]
        if snapshot is None:
            snapshot, configuration_error = source_snapshot()
        nodes = [node.as_dict() for node in snapshot.enabled_nodes]
        empty={'items':[],'nodes':[],'partial':not bool(nodes) or bool(configuration_error),
               'cached':False,'configured':bool(nodes),
               'configuration_error':configuration_error,
               'config_fingerprint':snapshot.fingerprint}
        if not keyword:return empty
        max_pages=max(1,min(int(max_pages),3));limit_per_node=max(1,min(int(limit_per_node),30))
        key=_cache_key(keyword,max_pages,limit_per_node,snapshot.fingerprint)
        if use_cache:
            hit=_cache_get(key)
            if hit is not None:
                return cls._filter_dead({**hit,'cached':True,
                    'configuration_error':configuration_error,
                    'partial':bool(hit.get('partial')) or bool(configuration_error)})
        if not nodes:
            return empty
        if not _search_slots.acquire(timeout=2):return {**empty,'partial':True,'busy':True}
        try:
            items=[];reports=[]
            try:
                with open(os.path.join(config.BASE_DIR,'web','daily_hot.json'),encoding='utf-8') as f:hot=json.load(f)
                for row in hot if isinstance(hot,list) else []:
                    if keyword.casefold() in row.get('clean_name','').casefold():
                        items.append({'source':'daily_hot','name':row.get('title') or row['clean_name'],'share_url':row.get('quark_url','')})
            except (OSError,ValueError,KeyError,TypeError):pass
            workers = max(1,min(int(max_workers),5,len(nodes)))
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures=[pool.submit(_fetch_provider,n,keyword,limit_per_node,max_pages) for n in nodes]
                for future in as_completed(futures):
                    found,report=future.result();items.extend(found);reports.append(report)
            result={**empty,'items':merge_items(items),'nodes':sorted(reports,key=lambda n:n['node']),
                    'partial':bool(configuration_error) or any(r['errors'] or r.get('partial') for r in reports)}
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
