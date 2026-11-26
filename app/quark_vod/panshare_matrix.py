"""Bounded multi-node Quark search with pagination and observable failures."""
import atexit
from collections import OrderedDict, deque
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
import httpx
from concurrent.futures import Future, ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED, TimeoutError as FuturesTimeout
from . import config, db
from . import search_plugins

UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
      "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
      "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7"}
# Deprecated test/embedding override; deployment sources live in VOD_SEARCH_CONFIG.
ACTIVE_PAN_NODES = []

SEARCH_CACHE_TTL = 1800
SEARCH_CACHE_DIR = os.path.join(config.DATA_DIR, "search_cache")
_CACHE_MAGIC = 5
_mem_cache = OrderedDict()
_cache_lock = threading.RLock()
_cache_sweep_at = 0.0
CACHE_SWEEP_INTERVAL = 60
_search_slots = threading.BoundedSemaphore(2)
_http_slots = threading.BoundedSemaphore(24)
_task_slots = threading.BoundedSemaphore(64)
NODE_HARD_DEADLINE = 22.0
_clients = OrderedDict()
_clients_lock = threading.RLock()
_health = {}
_health_lock = threading.RLock()
_health_limit = 128
CIRCUIT_FAILURES = 3
CIRCUIT_COOLDOWN = 300.0


class ProviderRateLimited(RuntimeError):
    """Upstream challenge/redirect, with safe status but no upstream details."""
    def __init__(self, message, *, http_status=0, redirect=False):
        super().__init__(message)
        self.http_status = http_status
        self.redirect = redirect


def _error_status(exc, fallback=0):
    status = (exc.response.status_code if isinstance(exc, httpx.HTTPStatusError)
              else getattr(exc, "http_status", fallback))
    return max(0, min(int(status), 599))


def _caller_budget_exhausted(node, report):
    deadline = node.get("_request_deadline")
    return bool(deadline is not None and time.monotonic() >= deadline-.025
                and (report.get("errors") or report.get("partial") or report.get("truncated"))
                and report.get("http_status", 0) < 300
                and not report.get("rate_limited") and not report.get("redirect"))


def _start_task(function, *args):
    """A hung trusted plugin cannot block shutdown or create unbounded threads."""
    if not _task_slots.acquire(blocking=False):
        return None
    future = Future()
    def run():
        try:
            if future.set_running_or_notify_cancel():
                try:
                    future.set_result(function(*args))
                except BaseException as exc:
                    future.set_exception(exc)
        finally:
            _task_slots.release()
    threading.Thread(target=run, daemon=True, name="vod-search").start()
    return future


def _health_key(node):
    # Config changes must not inherit another endpoint's circuit.
    return hashlib.sha256(json.dumps(node, sort_keys=True, default=str).encode()).hexdigest()


def node_health():
    with _health_lock:
        return [{"node": value["node"], "state": (
                    "HALF_OPEN" if value.get("probing") else
                    "OPEN" if value.get("opened_until", 0) > time.monotonic() else "CLOSED"),
                 "consecutive_failures": value["failures"],
                 "successes": value["successes"], "attempts": value["attempts"],
                 "ewma_seconds": round(value["latency"], 3),
                 "candidates": value["candidates"],
                 "budget_exhaustions": value.get("budget_exhaustions", 0),
                 "success_rate_24h": round(sum(ok for ts,ok in value["events"] if ts >= time.monotonic()-86400) / max(1,sum(ts >= time.monotonic()-86400 for ts,ok in value["events"])), 3)}
                for value in _health.values()]


def _admit_node(node):
    key = _health_key(node)
    with _health_lock:
        state = _health.get(key)
        if state is None:
            if len(_health) >= _health_limit:
                _health.pop(next(iter(_health)))
            _health[key] = {"node": node["key"], "failures": 0, "opened_until": 0,
                            "probing": False, "successes": 0, "attempts": 0,
                            "latency": 0.0, "candidates": 0, "events": deque(maxlen=2048)}
            return True
        if state["opened_until"]:
            if time.monotonic() < state["opened_until"] or state["probing"]:
                return False
            state["probing"] = True
        return True


def _record_node(node, report):
    with _health_lock:
        state = _health.get(_health_key(node))
        if state is None:
            return
        # Caller cancellation/capacity says nothing about upstream health.
        if report.get("budget_exhausted") or report.get("error_type") in ("SearchDeadline", "SearchCapacity"):
            state["probing"] = False
            state["budget_exhaustions"] = state.get("budget_exhaustions", 0) + 1
            return
        state["attempts"] += 1
        elapsed = max(0.0, float(report.get("seconds", 0)))
        state["latency"] = elapsed if state["attempts"] == 1 else .25*elapsed + .75*state["latency"]
        state["candidates"] += max(0, int(report.get("candidates", 0)))
        failed = bool(report.get("errors") or report.get("partial")) and not report.get("degraded")
        if failed:
            state["failures"] += 1
            if state["failures"] >= CIRCUIT_FAILURES or state["probing"]:
                state["opened_until"] = time.monotonic() + CIRCUIT_COOLDOWN
        else:
            state["successes"] += 1
            state["failures"] = 0
            state["opened_until"] = 0
        state["events"].append((time.monotonic(), not failed))
        while state["events"] and state["events"][0][0] < time.monotonic()-86400:
            state["events"].popleft()
        state["probing"] = False


def _close_clients():
    with _clients_lock:
        clients = list(_clients.values())
        _clients.clear()
    for client in clients:
        client.close()


atexit.register(_close_clients)



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
        if re.fullmatch(r'/(?:index\.php/vod/detail/id/\d+|voddetail/\d+|vod/\d+|detail/\d+|play/\d+-\d+-\d+)\.html',p)))


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
        if hasattr(_mem_cache, 'move_to_end'):_mem_cache.move_to_end(key)
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
        if hasattr(_mem_cache, 'move_to_end'):_mem_cache.move_to_end(key)
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


def _http_request(url, timeout, *, method="GET", json_body=None):
    """Persistent per-origin cookie/connection pool; bounded bytes and time."""
    origin = urllib.parse.urlsplit(url)
    client_key = (origin.scheme, origin.netloc)
    with _clients_lock:
        client = _clients.get(client_key)
        if client is None:
            # Config has at most 32 nodes; reject excess distinct origins rather
            # than closing a client which another request may still be using.
            if len(_clients) >= 64:
                raise RuntimeError("search client capacity")
            client = httpx.Client(headers=UA, follow_redirects=False,
                                  limits=httpx.Limits(max_connections=4, max_keepalive_connections=4),
                                  trust_env=False)
            _clients[client_key] = client
    timeout = max(.01, float(timeout))
    started = time.monotonic()
    if not _http_slots.acquire(timeout=timeout):
        raise TimeoutError("search capacity")
    try:
        remaining = max(.01, timeout - (time.monotonic()-started))
        with client.stream(method, url, timeout=remaining, json=json_body) as response:
            # Reject redirects; never send cookies or requests to Location.
            if response.status_code in (301, 302, 303, 307, 308):
                raise ProviderRateLimited("redirect or challenge",
                    http_status=response.status_code, redirect=True)
            response.raise_for_status()
            data = bytearray()
            for chunk in response.iter_bytes():
                data.extend(chunk)
                if len(data) > 2*1024*1024:
                    raise ValueError("search response too large")
                if time.monotonic()-started > timeout:
                    raise TimeoutError("search response deadline")
            return bytes(data).decode("utf-8", "replace")
    finally:
        _http_slots.release()


def _fetch_html(url, timeout):
    return _http_request(url, timeout)


def _soft_block(text):
    return bool(re.search(r"验证码|搜索太频繁|安全验证|Just a moment|cf-browser-verification",
                          text, re.I))



def _fetch_node(node,keyword,limit,max_pages):
    start=time.monotonic();deadline=min(start+NODE_HARD_DEADLINE,node.get('_request_deadline',float('inf')))
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
            if _soft_block(text) and not detail_paths(text):raise ProviderRateLimited('challenge')
            paths=list(dict.fromkeys(paths+detail_paths(text)))
            following=next_page(text,url,base)
            if len(paths)>=limit or time.monotonic()>=deadline:
                report['truncated']=bool(following) or len(paths)>limit;break
            url=following
        else:report['truncated']=bool(url)
    except Exception as exc:
        report["errors"] += 1
        report["error_type"] = type(exc).__name__
        report["search_failed"] = True
        report["http_status"] = _error_status(exc, 200 if report["pages"] else 0)
        report["redirect"] = bool(getattr(exc, "redirect", False))
        report["rate_limited"] = report["http_status"] == 429 or isinstance(exc, ProviderRateLimited)
    def detail(path):
        if time.monotonic()>=deadline:raise TimeoutError('node deadline')
        text=_fetch_html(base+path,min(node.get('timeout', 7),max(.1,deadline-time.monotonic())))
        if _soft_block(text) and not extract_quark_links_and_pwd(text):raise ProviderRateLimited('challenge')
        rows=parse_download_rows(text)
        if not rows:
            tm=re.search(r'<title[^>]*>(.*?)</title>',text,re.S|re.I)
            name=html.unescape(re.sub('<[^>]+>','',tm.group(1))).strip() if tm else keyword
            rows=[{'title':name,'url':url} for url in extract_quark_links_and_pwd(text)]
        return [{'source':node['key'],'name':r['title'],'share_url':r['url']} for r in rows]
    todo = iter(paths[:limit])
    pending = set()
    exhausted = False
    while not exhausted or pending:
        while not exhausted and len(pending) < 2 and time.monotonic() < deadline:
            path = next(todo, None)
            if path is None:
                exhausted = True
                break
            future = _start_task(detail, path)
            if future is None:
                report["errors"] += 1
            else:
                pending.add(future)
        if not pending or time.monotonic() >= deadline:
            if pending or not exhausted:
                report["errors"] += len(pending) or 1
                report["truncated"] = True
            break
        done, _ = wait(pending, timeout=max(0,deadline-time.monotonic()), return_when=FIRST_COMPLETED)
        if not done:
            report["errors"] += len(pending)
            report["truncated"] = True
            break
        for future in done:
            pending.remove(future)
            try:
                items.extend(future.result())
                report["details"] += 1
            except Exception as exc:
                report["errors"] += 1
                report["error_type"] = type(exc).__name__
                report["http_status"] = _error_status(exc, report.get("http_status", 200))
                report["redirect"] = bool(getattr(exc, "redirect", False))
    if report["errors"] <= 2 and report["errors"] and not report.get("search_failed") and len(items) >= 8:
        report["degraded"] = True
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
                or len(row['share_url']) > 4096):
                report['errors'] += 1
                continue
            if not canonical_share(row['share_url']):
                continue
            items.append({'source': node['key'], 'name': row['name'].strip(),
                          'share_url': row['share_url']})
    except Exception as exc:
        report['errors'] += 1
        report['error_type'] = type(exc).__name__
        report["http_status"] = _error_status(exc)
        report["redirect"] = bool(getattr(exc, "redirect", False))
        report["rate_limited"] = report["http_status"] == 429 or isinstance(exc, ProviderRateLimited)
    report.update(seconds=round(time.monotonic()-start, 3), candidates=len(items))
    return items, report


def _fetch_provider(node, keyword, limit, max_pages):
    provider = node.get('provider', 'html')
    from .search_providers import fetch_tg_web, fetch_pansou
    handler = (_fetch_node if provider == 'html' else
               _fetch_json_node if provider == 'json' else
               fetch_tg_web if provider == 'tg_web' else
               fetch_pansou if provider == 'pansou' else search_plugins.get_provider(provider))
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
                               'source': node['key'], **({'published_at': row['published_at']} if isinstance(row.get('published_at'), str) and re.fullmatch(r'[0-9TZ:+. -]{10,40}', row['published_at']) else {})})
        # Retain diagnostic counters only. Plugins must not leak upstream URLs
        # or arbitrary error text into the public report.
        safe = {'node': node['key'], 'provider': provider,
                'errors': max(0, min(int(report.get('errors', 0)), 10000)) + invalid,
                'pages': max(0, min(int(report.get('pages', 0)), 10000)),
                'details': max(0, min(int(report.get('details', 0)), 10000)),
                'truncated': bool(report.get('truncated')),
                'partial': bool(report.get('partial')),
                'degraded': bool(report.get('degraded')),
                'rate_limited': bool(report.get('rate_limited')) or report.get('error_type') == 'ProviderRateLimited',
                'http_status': max(0,min(int(report.get('http_status',200 if report.get('pages') else 0)),599)),
                'redirect': bool(report.get('redirect')),
                'http_status_counts': {str(code): max(0, min(int(count), 10000))
                    for code, count in report.get('http_status_counts', {}).items()
                    if str(code).isdigit() and 100 <= int(code) <= 599},
                'seconds': round(time.monotonic()-started, 3),
                'candidates': len(normalized)}
        safe["budget_exhausted"] = _caller_budget_exhausted(node, safe)
        if safe["budget_exhausted"]:
            safe["partial"] = True
        if safe['errors']:
            safe['error_type'] = 'SearchDeadline' if safe["budget_exhausted"] else 'ProviderError'
        return normalized, safe
    except Exception as exc:
        status = _error_status(exc)
        report = {'node': node['key'], 'provider': provider, 'pages': 0,
                  'http_status': status, 'redirect': bool(getattr(exc, 'redirect', False)),
                  'rate_limited': status == 429 or isinstance(exc, ProviderRateLimited),
                  'details': 0, 'errors': 1, 'truncated': False,
                  'error_type': 'ProviderError', 'candidates': 0,
                  'seconds': round(time.monotonic()-started, 3)}
        report["budget_exhausted"] = _caller_budget_exhausted(node, report)
        if report["budget_exhausted"]:
            report.update(partial=True, error_type="SearchDeadline")
        return [], report


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
                      *, snapshot=None, configuration_error=None, category=None, deadline_seconds=None,
                      deadline_at=None, stop_when=None, reuse_partial_cache=True):
        caller_limited = deadline_seconds is not None or deadline_at is not None
        search_started = time.monotonic()
        deadline_all = search_started + min(NODE_HARD_DEADLINE,
            max(.001, deadline_seconds if deadline_seconds is not None else NODE_HARD_DEADLINE))
        if deadline_at is not None:
            deadline_all = min(deadline_all, deadline_at)
        keyword=' '.join((keyword or '').split())[:120]
        if snapshot is None:
            snapshot, configuration_error = source_snapshot()
        nodes = [node.as_dict() for node in snapshot.enabled_nodes
                 if not category or not node.categories or category in node.categories]
        empty={'items':[],'nodes':[],'partial':not bool(nodes) or bool(configuration_error),
               'cached':False,'configured':bool(nodes),
               'configuration_error':configuration_error,
               'config_fingerprint':snapshot.fingerprint}
        if not keyword:return empty
        max_pages=max(1,min(int(max_pages),3));limit_per_node=max(1,min(int(limit_per_node),30))
        key=_cache_key(keyword,max_pages,limit_per_node,snapshot.fingerprint + ('|category='+str(category) if category else '') +
                       ('|budget='+str(deadline_seconds) if deadline_seconds else '') + ('|early' if stop_when else ''))
        if use_cache:
            hit=_cache_get(key)
            if hit is not None and (reuse_partial_cache or not hit.get("partial")):
                return cls._filter_dead({**hit,'cached':True,
                    'configuration_error':configuration_error,
                    'partial':bool(hit.get('partial')) or bool(configuration_error)})
        if not nodes:
            return empty
        remaining = max(0, deadline_all-time.monotonic())
        if not _search_slots.acquire(timeout=min(2, remaining)):
            reason = "SearchDeadline" if time.monotonic() >= deadline_all else "SearchCapacity"
            return {**empty, 'partial': True, 'busy': True, 'nodes': [
                {"node": node["key"], "provider": node.get("provider", "html"),
                 "pages": 0, "details": 0, "errors": 1, "partial": True,
                 "truncated": True, "error_type": reason, "candidates": 0,
                 "seconds": round(time.monotonic()-search_started, 3)} for node in nodes]}
        try:
            items=[];reports=[]
            try:
                with open(os.path.join(config.BASE_DIR,'web','daily_hot.json'),encoding='utf-8') as f:hot=json.load(f)
                for row in hot if isinstance(hot,list) else []:
                    if keyword.casefold() in row.get('clean_name','').casefold():
                        items.append({'source':'daily_hot','name':row.get('title') or row['clean_name'],'share_url':row.get('quark_url','')})
            except (OSError,ValueError,KeyError,TypeError):pass
            workers = max(1,min(int(max_workers),5,len(nodes)))
            early_stopped = False
            queue = iter(nodes)
            pending = {}
            exhausted = False
            def failure(node, reason):
                return {"node":node["key"], "provider":node.get("provider","html"),
                        "pages":0, "details":0, "errors":1, "partial":True,
                        "truncated":False, "error_type":reason,
                        "budget_exhausted":caller_limited and reason in ("ProviderTimeout", "SearchDeadline"),
                        "seconds":round(time.monotonic()-search_started,3),
                        "candidates":0}
            while not exhausted or pending:
                if time.monotonic() >= deadline_all:
                    for future, (node, _) in pending.items():
                        future.cancel()
                        report = failure(node, "ProviderTimeout")
                        reports.append(report)
                        _record_node(node, report)
                    pending.clear()
                    for node in queue:
                        reports.append(failure(node, "SearchDeadline"))
                    break
                while not exhausted and len(pending) < workers:
                    node = next(queue, None)
                    if node is None:
                        exhausted = True
                        break
                    if not _admit_node(node):
                        reports.append(failure(node, "CircuitOpen"))
                        continue
                    future = _start_task(_fetch_provider, {**node, '_request_deadline':deadline_all-.05,
                        'timeout':min(node.get('timeout',4),max(.01,deadline_all-time.monotonic()-.05))}, keyword, limit_per_node, max_pages)
                    if future is None:
                        report = failure(node, "SearchCapacity")
                        reports.append(report)
                        _record_node(node, report)
                    else:
                        pending[future] = (node, min(deadline_all, time.monotonic()+NODE_HARD_DEADLINE))
                if not pending:
                    continue
                delay = max(0, min(deadline for _,deadline in pending.values())-time.monotonic())
                done, _ = wait(pending, timeout=delay, return_when=FIRST_COMPLETED)
                for future in list(pending):
                    node, deadline = pending[future]
                    if future in done:
                        found, report = future.result()
                        items.extend(found)
                    elif time.monotonic() >= deadline:
                        future.cancel()
                        report = failure(node, "ProviderTimeout")
                    else:
                        continue
                    pending.pop(future)
                    reports.append(report)
                    _record_node(node, report)
                if stop_when and stop_when(merge_items(items)):
                    early_stopped = True
                    for future, (node, _) in pending.items():
                        future.cancel()
                        reports.append({"node":node["key"], "provider":node.get("provider","html"),
                            "errors":0, "pages":0, "details":0, "truncated":True,
                            "partial":False, "skipped":True, "candidates":0, "seconds":0})
                        with _health_lock:
                            state = _health.get(_health_key(node))
                            if state:state["probing"] = False
                    pending.clear()
                    break
            result={**empty,'items':merge_items(items),'nodes':sorted(reports,key=lambda n:n['node']),
                    'degraded_nodes':[r['node'] for r in reports if r.get('degraded')],
                    'early_stopped':early_stopped,
                    'partial':bool(configuration_error) or any(
                        (r['errors'] and not r.get('degraded')) or r.get('partial') for r in reports)}
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
