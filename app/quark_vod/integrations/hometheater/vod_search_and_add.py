#!/usr/bin/env python3
"""Portal catalogue -> source discovery -> read-only inspection -> queued add.

TMDB supplies work identity. Public indexes only supply candidates. A selected
share is inspected with the actual transfer selector before creating a pending
item. No fabricated episodes, resolution certification or ready claims.
"""

from quark_vod.integrations.hometheater import paths
from quark_vod import config as host_config
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import OrderedDict
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor

BASE = paths.BASE_DIR
from quark_vod.integrations.hometheater import tmdb_populate
from quark_vod import config, db, search, strm_manager
from quark_vod import coordination
from quark_vod.openlist_client import OpenListClient
from quark_vod.transfer import QuarkTransfer
from quark_vod.quality import quality_hint

CINEMA2 = paths.CINEMA2_DIR
IMG_BASE = 'https://image.tmdb.org/t/p'
def _api_base():
    host = config.HOST
    if host in {"0.0.0.0", "::", "[::]"}:
        host = "127.0.0.1"
    elif ":" in host and not host.startswith("["):
        host = "[" + host + "]"
    value = config._value("VOD_API_BASE", f"http://{host}:{config.PORT}").rstrip("/")
    parsed = urllib.parse.urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None
            or parsed.query or parsed.fragment):
        raise ValueError("invalid VOD_API_BASE")
    try:
        parsed.port
    except ValueError:
        raise ValueError("invalid VOD_API_BASE") from None
    return value

VOD_API = _api_base()
_lock = threading.RLock()
_details = OrderedDict()
_candidates = OrderedDict()
_jobs = OrderedDict()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='portal-add')
_metadata_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix='portal-metadata')
_slots = threading.BoundedSemaphore(4)
_inspect_slots = threading.BoundedSemaphore(6)
_metadata_slots = threading.BoundedSemaphore(2)


def _remember(cache, key, value, ttl=900, limit=128):
    with _lock:
        cache[key] = (time.monotonic()+ttl, value)
        cache.move_to_end(key)
        while len(cache) > limit:
            cache.popitem(last=False)


def _recall(cache, key):
    with _lock:
        hit = cache.get(key)
        if hit and hit[0] > time.monotonic():
            return hit[1]
        cache.pop(key, None)
    return None


def _tmdb(path, **query):
    try:
        key = tmdb_populate.read_tmdb_key()
    except (Exception, SystemExit):
        raise ValueError('TMDB 尚未配置，无法确认作品资料') from None
    if not key:
        raise ValueError('TMDB 尚未配置，无法确认作品资料')
    params = urllib.parse.urlencode({'api_key':key,'language':'zh-CN',**query})
    try:
        req = urllib.request.Request('https://api.themoviedb.org/3/'+path+'?'+params, headers=tmdb_populate.UA)
        with urllib.request.urlopen(req, timeout=12) as response:
            raw=response.read(2*1024*1024+1)
            if len(raw)>2*1024*1024:raise ValueError('metadata too large')
            data=json.loads(raw)
        if not isinstance(data, dict) or data.get('success') is False:
            raise ValueError()
        return data
    except Exception:
        raise ValueError('片目信息服务暂时不可用，请稍后重试') from None


def guess_category(item):
    countries = item.get('origin_country') or [x.get('iso_3166_1') for x in item.get('production_countries',[])]
    genres = item.get('genre_ids') or [g.get('id') for g in item.get('genres',[])]
    tv = item.get('media_type') == 'tv'
    if 16 in genres:
        return '国产动漫' if 'CN' in countries else '日本动漫'
    if 'CN' in countries:return '国产剧集' if tv else '国产电影'
    if any(c in countries for c in ['HK','TW']) and not tv:return '港台经典'
    if 'KR' in countries:return '热门韩剧' if tv else '韩国电影'
    if 'JP' in countries:return '日本剧集' if tv else '日本电影'
    if 'TH' in countries:return '泰国剧集' if tv else '泰国电影'
    return '欧美剧集' if tv else '欧美大片'


def _has_library_media(path):
    return path.is_dir() and (any(path.glob('*.strm')) or any(path.glob('Season */*.strm')))


def _nfo_tmdb_ids(path):
    found = set()
    for name in ('tvshow.nfo', 'movie.nfo'):
        try:
            root = ET.parse(path/name).getroot()
        except (OSError, ET.ParseError):
            continue
        value = (root.findtext('tmdbid') or '').strip()
        if value:
            found.add(value)
        for unique in root.findall('uniqueid'):
            value = (unique.text or '').strip()
            if value and (unique.get('type') or '').casefold() == 'tmdb':
                found.add(value)
    return found


def _library_folder(cat, title, year, tmdb_id=None):
    """Return the one existing folder whose NFO proves this TMDB identity."""
    root = Path(CINEMA2)/cat
    expected = root/(f'{title} ({year})' if year else title)
    if not tmdb_id:
        return expected if _has_library_media(expected) else None
    wanted = str(tmdb_id)
    matches = []
    if expected.is_dir():
        ids = _nfo_tmdb_ids(expected)
        if ids and wanted not in ids:
            raise ValueError('同名年份目录的 NFO 身份不符，已停止覆盖片库内容')
        if wanted in ids or (not ids and _has_library_media(expected)):
            matches.append(expected)
    if root.is_dir():
        for path in root.iterdir():
            if path == expected or not _has_library_media(path):
                continue
            if wanted in _nfo_tmdb_ids(path):
                matches.append(path)
    if len(matches) > 1:
        raise ValueError('片库中有多个目录记录了相同 TMDB ID，已停止重复入库')
    return matches[0] if matches else None


def check_in_library(cat, title, year, tmdb_id=None):
    # Prefix matches conflate sequels and remakes. Prefer the stable TMDB identity
    # recorded in NFO when legacy folders do not include their release year.
    try:
        return _library_folder(cat, title, year, tmdb_id) is not None
    except ValueError:
        return True


def _summary(item):
    title = tmdb_populate.sanitize(tmdb_populate.fmt_title(item))
    year = (tmdb_populate.fmt_date(item) or '')[:4]
    cat = guess_category(item)
    return {'id':item['id'],'media_type':item['media_type'],'title':title,'year':year,'cat':cat,
            'original_title':item.get('original_title') or item.get('original_name') or '',
            'poster':IMG_BASE+'/w500'+item['poster_path'] if item.get('poster_path') else '',
            'rating':round(item.get('vote_average') or 0,1),'overview':(item.get('overview') or '').strip(),
            'in_library':check_in_library(cat,title,year,item.get('id')),
            'source_status':'unchecked','has_4k_quark':None}


def _search_summary(item):
    # TMDB search/movie omits country data. Resolve the same stable identity
    # before choosing a library category or claiming it is absent from it.
    if item['media_type'] == 'movie' and not (item.get('origin_country') or item.get('production_countries')):
        key = ('movie', item['id'])
        detail = _recall(_details, key)
        if detail is None:
            detail = _tmdb(f"movie/{item['id']}", append_to_response='keywords,alternative_titles,translations')
            if detail.get('id') != item['id']:
                raise ValueError('作品信息不一致')
            detail = {**detail, 'media_type': 'movie'}
            _remember(_details, key, detail)
        item = {**item, **detail, 'media_type': 'movie'}
    if tmdb_populate.is_ai_content(item, cat=guess_category(item)):
        return None
    return _summary(item)


def search_all(keyword):
    query = str(keyword).strip()
    if not query or len(query)>100:raise ValueError('片名长度需为 1 到 100 字符')
    if not _metadata_slots.acquire(timeout=1):
        raise ValueError('片目信息查询繁忙，请稍后重试')
    try:
        data = _tmdb('search/multi',query=query,include_adult='false')
        items = [item for item in data.get('results', [])
                 if item.get('media_type') in ('movie', 'tv') and tmdb_populate.fmt_title(item)][:20]
        return [result for result in _metadata_pool.map(_search_summary, items) if result is not None]
    finally:
        _metadata_slots.release()


def _work(payload):
    kind = payload.get('media_type')
    ident = int(payload.get('id') or 0)
    if kind not in ('movie','tv') or ident<=0:raise ValueError('请选择有效的电影或剧集')
    cache_key = (kind,ident)
    detail = _recall(_details,cache_key)
    if detail is None:
        detail = _tmdb(f'{kind}/{ident}',append_to_response='keywords,alternative_titles,translations')
        if detail.get('id') != ident:raise ValueError('作品信息不一致')
        detail['media_type']=kind
        _remember(_details,cache_key,detail)
    if tmdb_populate.is_ai_content(detail,detail,cat=guess_category(detail)):
        raise ValueError('该条目属于已过滤的微短剧或 AIGC 内容')
    item=_summary(detail)
    season=int(payload.get('season',1 if kind=='tv' else 0))
    episode=int(payload.get('episode') or (1 if kind=='tv' else 0))
    seasons=[{'number':s['season_number'],'episodes':s.get('episode_count',0)}
             for s in detail.get('seasons',[]) if s.get('season_number',-1)>=0]
    ep_data=None
    if kind=='tv':
        if not any(s['number']==season for s in seasons):raise ValueError('所选季不存在，不能猜测集数')
        season_key=(kind,ident,season)
        season_detail=_recall(_details,season_key)
        if season_detail is None:
            season_detail=_tmdb(f'tv/{ident}/season/{season}')
            _remember(_details,season_key,season_detail)
        aired=[e for e in season_detail.get('episodes',[]) if e.get('episode_number',0)>0
               and tmdb_populate.is_episode_aired(e,season,e['episode_number'],detail)]
        ep_data=next((e for e in aired if e['episode_number']==episode),None)
        if ep_data is None:raise ValueError('所选集尚未确认播出，请核对季集')
        item['episodes']=[e['episode_number'] for e in aired]
    elif season or episode:raise ValueError('电影不接受季集参数')
    requested_folder=f"{item['title']} ({item['year']})" if item['year'] else item['title']
    existing_folder=_library_folder(item['cat'],item['title'],item['year'],ident)
    folder=existing_folder.name if existing_folder else requested_folder
    if not folder or folder in ('.','..') or '/' in folder or '\\' in folder:raise ValueError('片名无效')
    return {'item':item,'detail':detail,'folder':folder,'season':season,'episode':episode,
            'seasons':seasons,'episode_data':ep_data,'variant':db.validate_variant(payload.get('variant',''))}



def _aliases(work):
    detail=work["detail"]; title=work["item"]["title"]
    values=[work["item"].get("original_title","")]
    alternative=detail.get("alternative_titles") or {}
    for row in alternative.get("titles",alternative.get("results",[])):
        if row.get("iso_3166_1") in ("CN","HK","TW","US","GB","JP","KR"):
            values.append(row.get("title",""))
    for row in (detail.get("translations") or {}).get("translations",[]):
        if row.get("iso_639_1") in ("en","zh","ja","ko"):
            data=row.get("data") or {}; values.append(data.get("name") or data.get("title") or "")
    for row in detail.get("seasons",[]):
        if row.get("season_number")==work["season"]:
            name=row.get("name") or ""
            if name and not __import__("re").fullmatch(r"(?:Season\s*\d+|第\s*[零〇一二三四五六七八九十百\d]+\s*季|Specials|特别篇)",name,__import__("re").I):
                values.append(name)
    return tuple(dict.fromkeys(value.strip() for value in values if value and value.strip().casefold()!=title.casefold()))[:20]


def _options(work):
    return {"variant":work["variant"]} if work.get("variant") else {}


def _register_pending(path, cat, folder, season, episode, work):
    if work.get("variant"):
        created=strm_manager.ensure_variant(cat,folder,season,episode,work["variant"])
        return Path(created)==path and strm_manager.is_pending(path.read_text())
    return tmdb_populate.add_pending_strm(str(path),cat,folder,season,episode)


def source_search(payload):
    work=_work(payload);item=work['item'];season=work['season'];episode=work['episode']
    original=item['original_title']
    # Keep year/season identity while searching the title used by foreign releases.
    aliases=_aliases(work)
    locked_url, locked_4k, locked_title=db.get_locked_4k_share(item['cat'],work['folder'],season,episode,**_options(work))
    try:
        report=search.search_report(work['folder'],aliases=aliases,season=season,episode=episode)
    except Exception as exc:
        # A verified exact-work lock remains useful when public index nodes fail.
        logging.getLogger(__name__).warning('portal_source_search_failed error_type=%s', type(exc).__name__)
        report={'results':[],'queries':[],'nodes':[],'partial':True,'cached':False,
                'configured':None,'configuration_error':'source_search_failed'}
    scored=[]
    for candidate in report['results']:
        if not search.matches_kind(candidate,item['cat']):continue
        score=search.score_search_result(candidate,work['folder'],season=season,episode=episode)
        for value in aliases:
            alias=value+(f" ({item['year']})" if item['year'] else '')
            score=max(score,search.score_search_result(candidate,alias,season=season,episode=episode))
        if score>=30:
            scored.append((score,candidate,False))
    if locked_url:
        scored=[row for row in scored if row[1].share_url != locked_url]
        locked=search.SearchResult(engine='panshare',source='locked',name=locked_title or work['folder'],
            year='',poster='',episodes=[],share_url=locked_url,pan_type='quark',sources=['已审核锁源'])
        scored.append((100,locked,True))
    scored.sort(key=lambda row:(-int(row[2]),-(row[0]>=85),
        -int(locked_4k if row[2] else quality_hint(row[1].name)['level']=='4k'),-row[0],row[1].share_url))
    candidates=[]
    for score,candidate,is_locked in scored[:80]:
        token=secrets.token_urlsafe(18)
        _remember(_candidates,token,{'work':work,'candidate':candidate},ttl=1200,limit=320)
        quality=quality_hint(candidate.name)
        if is_locked and locked_4k and quality['level']!='4k':
            quality={'level':'4k','label':'已审目标文件标注 4K·待解码',
                     'evidence':'audited_filename','verified':False}
        candidates.append({'token':token,'name':candidate.name,'sources':candidate.sources,'score':score,
                           'quality':quality,'share_url':candidate.share_url,
                           'locked':is_locked,'status':'unchecked'})
    saved=db.find_done_transfer(item['cat'],work['folder'],season,episode,**_options(work))
    if saved and saved['remote_path']:
        token=secrets.token_urlsafe(18)
        _remember(_candidates,token,{'work':work,'candidate':None},ttl=1200,limit=320)
        candidates.insert(0,{'token':token,'name':'播放已保存视频（不会重复转存）',
            'sources':['当前作品已有转存'],'score':101,
            'quality':{'level':'unknown','label':'已保存文件优先复用，不覆盖'},
            'share_url':'','locked':False,'reuse_saved':True,'status':'saved'})
        candidates=candidates[:80]
    return {'code':0,'saved':bool(saved and saved['remote_path']),'item':item,'season':season,'episode':episode,'seasons':work['seasons'],
            'candidates':candidates,'queries':report['queries'],'nodes':report['nodes'],
            'partial':report['partial'],'cached':report['cached'],'truncated':len(scored)>80,
            'configured':report.get('configured'),
            'configuration_error':report.get('configuration_error')}


def _selected(payload):
    selected=_recall(_candidates,str(payload.get('token') or ''))
    if selected is None:raise ValueError('片源列表已过期，请重新查找片源')
    return selected


def _inspect(selected):
    checked=selected.get('inspection')
    if checked and checked.get('checked_at',0)>time.time()-600:
        return checked
    if not _inspect_slots.acquire(timeout=1):raise ValueError('正在检查其他片源，请稍后重试')
    try:
        work=selected['work'];candidate=selected['candidate']
        if candidate is None:raise ValueError('所选条目使用已保存视频，无需重新检查分享')
        tr=QuarkTransfer(None)
        original=work['item'].get('original_title') or ''
        aliases=_aliases(work)
        video=tr.inspect_share_video(candidate.share_url,work['folder'],work['season'],work['episode'],aliases=aliases,**_options(work))
        if selected.get('selected_fid'):
            token,pwd_id,root_files=tr.resolve_share(candidate.share_url)
            files=tr.share_files(token,pwd_id,root_files)
            wanted=[f for f in files if str(f.get('fid',''))==selected['selected_fid']]
            video=tr.select_share_video(wanted,work['folder'],work['season'],work['episode'],aliases=aliases,**_options(work))
        if not video:raise ValueError('该分享中没有匹配所选季集的完整视频')
        result={'name':video['name'],'size':video['size'],'fid':str((video.get('share') or {}).get('fid') or video.get('fid') or ''),'quality':quality_hint(video['name'],candidate.name),
                'status':'file_found','checked_at':int(time.time())}
        selected['inspection']=result
        return result
    finally:_inspect_slots.release()


def inspect_source(payload):
    try:return {'code':0,'file':_inspect(_selected(payload))}
    except ValueError:raise
    except Exception:raise ValueError('未能确认目标文件，分享可能失效或暂时不可读；可重试或换源') from None


def _refresh():
    key=config.get_jellyfin2_key()
    if not key:return False
    req=urllib.request.Request(config.JELLYFIN2_API+'/Library/Refresh',data=b'',method='POST',
        headers={'Authorization':'MediaBrowser Token="'+key+'"'})
    try:
        with urllib.request.urlopen(req,timeout=10) as response:return response.status in (200,204)
    except Exception:return False


def _queue_preheat(cat, folder, season, episode, *, variant=''):
    params=urllib.parse.urlencode({'cat':cat,'t':folder,'s':season,'e':episode,**({'variant':variant} if variant else {})})
    token = config.get_admin_token()
    if not token:
        raise ValueError('点播服务管理凭证尚未配置')
    request = urllib.request.Request(VOD_API+'/preheat?'+params,
        headers={'Authorization': 'Bearer '+token})
    with urllib.request.urlopen(request,timeout=12) as response:
        if response.status not in (200,202):raise ValueError('点播服务暂时无法接收转存')


def _ensure_metadata(root, path, work):
    detail=work['detail'];item=work['item'];season=work['season'];episode=work['episode']
    if episode:
        if not (root/'tvshow.nfo').is_file():tmdb_populate.gen_tv_nfo(str(root),detail,detail)
        season_dir=path.parent
        if not (season_dir/'season.nfo').is_file():
            data=next((s for s in detail.get('seasons',[]) if s.get('season_number')==season),None)
            if data is None:raise ValueError('TMDB 季资料缺失，未创建片库元数据')
            tmdb_populate.gen_season_nfo(str(season_dir),detail,detail,data)
        if not path.with_suffix('.nfo').is_file():
            tmdb_populate.gen_episode_nfo(str(path.with_suffix('.nfo')),
                (work.get('episode_data') or {}).get('name'),season,episode,work.get('episode_data') or {})
    elif not (root/'movie.nfo').is_file():
        tmdb_populate.gen_movie_nfo(str(root),detail,detail)


def _work_strm_path(root, work):
    item=work['item'];season=work['season'];episode=work['episode']
    if work.get('variant'):
        return Path(strm_manager.strm_path_for(item['cat'],work['folder'],season,episode,**_options(work)))
    if episode:
        return Path(tmdb_populate.episode_strm_path(str(root/f'Season {season:02d}'),item['title'],season,episode))
    if root.is_dir():
        existing=sorted(path for path in root.glob('*.strm') if not strm_manager.variant_from_path(str(path)))
        if len(existing)>1:raise ValueError('片库电影目录存在多个 STRM，无法安全选定原播放入口')
        if existing:return existing[0]
    return root/(work['folder']+'.strm')


@contextmanager
def _pending_entry_lock(cat, folder, season):
    # Pending STRM/NFO writes are non-destructive. Share the library lease so
    # playback can continue, while serializing this work against the resolver
    # and other adds for the same season.
    lock_name = 'work:' + repr((cat, folder.casefold(), season))
    with coordination.playback_lease(), coordination.file_lock(lock_name, timeout=config.TRANSFER_TIMEOUT):
        yield


def _do_add(selected,job):
    try:
        work=selected['work'];item=work['item'];detail=work['detail'];candidate=selected['candidate']
        folder=work['folder'];cat=item['cat'];season=work['season'];episode=work['episode']
        key=(cat,folder,season,episode)
        if job.get('replace'):
            job['stage']='saving'
            result=_core_resolve(selected,replace=True,reason=job.get('reason','corrupted'))
            cleanup_pending=bool(result.get('cleanup_pending'))
            message='已替换片源并激活播放入口'
            if cleanup_pending: message+='；旧文件清理尚待重试，新片源可正常播放'
            job.update(stage='ready',message=message,cleanup_pending=cleanup_pending,
                       refresh_ok=_refresh_work(work),refresh_attempted=True)
            return
        root=Path(CINEMA2)/cat/folder
        path=_work_strm_path(root,work)
        job['strm_path']=str(path)
        if candidate is not None and path.is_file():
            with _pending_entry_lock(cat, folder, season):
                try:existing=path.read_text()
                except (OSError,UnicodeError):raise ValueError('现有播放入口不可读取，已停止修改') from None
                if strm_manager.is_live(existing):
                    # Existing live media remains the active item; an add request
                    # never replaces it with another candidate.
                    job.update(stage='preserved',message='该集已有播放入口，已保持原样；未验证当前可读性，也未执行新转存。',
                               refresh_ok=True)
                    return
        saved=db.find_done_transfer(*key,**_options(work))
        mapped=db.get_strm_path(*key,**_options(work))
        if saved and saved['remote_path']:
            root_real=root.resolve()
            mapped_candidate=Path(mapped).resolve() if mapped else None
            mapped_path=(mapped_candidate if mapped_candidate and mapped_candidate.is_relative_to(root_real) else None)
            if mapped_path is None:
                mapped_path=(Path(strm_manager.strm_path_for(cat,folder,season,episode,**_options(work)))
                             if work.get('variant') else
                             Path(tmdb_populate.episode_strm_path(str(root/f'Season {season:02d}'),item['title'],season,episode))
                             if episode else root/(folder+'.strm'))
            with _pending_entry_lock(cat, folder, season):
                try:
                    current=mapped_path.read_text()
                    reusable_entry=strm_manager.is_live(current) or strm_manager.is_pending(current)
                except (OSError,UnicodeError):
                    current=''
                    reusable_entry=not mapped_path.exists()
                if reusable_entry:
                    # Existing saved media wins by design. Do not inspect or claim a new
                    # candidate the playback resolver will not use.
                    job['strm_path']=str(mapped_path)
                    if not mapped_path.exists():
                        mapped_path.parent.mkdir(parents=True,exist_ok=True)
                        if not _register_pending(mapped_path,cat,folder,season,episode,work):
                            raise ValueError('已有保存视频的片库入口无法安全恢复')
                        current=mapped_path.read_text()
                    _ensure_metadata(root,mapped_path,work)
                    if not mapped:
                        db.save_strm_map(*key,str(mapped_path),**_options(work))
            if saved and saved['remote_path'] and reusable_entry:
                job['stage']='preparing'
                if strm_manager.is_live(current):
                    # A working live entry may be a user's explicitly selected source.
                    # Leave it byte-for-byte alone and do not start another resolution.
                    job.update(stage='preserved',message='该集已有播放入口，已保持原样；未验证当前可读性，也未执行新转存。')
                    return
                else:
                    # /preheat calls the normal resolver, which reuses this exact saved
                    # transfer and activates the pending entry without saving a copy.
                    _queue_preheat(cat,folder,season,episode,**_options(work))
                job['stage']='submitted'
                return
        if candidate is None:
            raise ValueError('当前作品没有可复用的已保存视频')
        job['stage']='checking'
        inspected=_inspect(selected)
        with _pending_entry_lock(cat, folder, season):
            if path.is_file():
                try:existing=path.read_text()
                except (OSError,UnicodeError):raise ValueError('现有播放入口不可读取，已停止修改') from None
                if not strm_manager.is_pending(existing):
                    raise ValueError('片库中已有非待点播入口；为保护现有播放地址，本次没有入库或转存')
            path.parent.mkdir(parents=True,exist_ok=True)
            _ensure_metadata(root,path,work)
            if not _register_pending(path,cat,folder,season,episode,work):
                raise ValueError('片库中已有非待点播入口；为保护现有播放地址，本次没有入库或转存')
            db.save_strm_map(*key,str(path),**_options(work))
            db.pin_item(cat,folder,reason='manual_add')
            # The actual target file was verified. This is a hint, not decoded resolution.
            db.save_source_audit(cat,folder,season,episode,has_quark_source=1,
                is_4k=int(inspected['quality']['level']=='4k'),best_share_url=candidate.share_url,
                best_share_title=candidate.name,check_status='selected_file',**_options(work))
        job['stage']='preparing'
        selected.setdefault('selected_fid',inspected.get('fid',''))
        _core_resolve(selected)
        job.update(stage='ready',refresh_ok=_refresh_work(work),refresh_attempted=True)
    except coordination.Busy as exc:
        job.update(stage='failed', message=str(exc))
    except Exception as exc:
        job.update(stage='failed',message=str(exc) if isinstance(exc,ValueError) else '入库或转存暂时失败，请检查服务后重试')
    finally:
        db.close()
        _slots.release()


def add_to_library(payload):
    selected=_selected(payload)
    work=selected['work'];key=(work['item']['cat'],work['folder'],work['season'],work['episode'])
    with _lock:
        for ident,(expires,job) in _jobs.items():
            if expires>time.monotonic() and job['key']==key and job.get('variant','')==work.get('variant','') and job['stage']!='failed':
                return {'code':0,'job':ident,'msg':'任务已存在，继续查看状态'}
        if not _slots.acquire(blocking=False):raise ValueError('点播任务繁忙，请稍后重试')
        ident=secrets.token_urlsafe(18)
        job={'key':key,'stage':'queued','message':'已排队，尚未开始转存','refresh_ok':False,'refresh_attempted':False,'work':work,'variant':work.get('variant','')}
        _remember(_jobs,ident,job,ttl=3600)
        try:_pool.submit(_do_add,selected,job)
        except Exception:_slots.release();raise
    return {'code':0,'job':ident,'msg':'已排队，正在检查所选文件'}


def job_status(ident):
    job=_recall(_jobs,ident)
    if job is None:raise ValueError('任务记录已过期；可到 Jellyfin2 或管理台查看保存状态')
    state=job['stage'];message=job.get('message','')
    opts={'variant':job['variant']} if job.get('variant') else {}
    row=db.find_done_transfer(*job['key'],**opts) or db.get_transfer(*job['key'],**opts)
    if row and state in ('submitted','preparing'):
        if row['status']=='failed':state='failed';message='转存失败，可在管理台查看原因并重试'
        elif row['status']=='done':
            path=job.get('strm_path','')
            try:live=strm_manager.is_live(Path(path).read_text())
            except OSError:live=False
            state='ready' if live else 'preparing'
            message='文件已保存，播放入口已激活' if live else '文件已保存，正在准备播放入口'
        else:state='preparing';message='正在搜索、保存或检查可读性'
    if state=='failed':job['stage']='failed'
    if state in ('ready','preserved') and not job.get('refresh_attempted'):
        job['refresh_attempted']=True
        job['refresh_ok']=_refresh_work(job.get('work') or {})
    links=job.get('links') or {'play_url':'','jellyfin_item_id':None}
    if state in ('ready','preserved') and not job.get('links'):
        links=job['links']=playback_links(job['key'],variant=job.get('variant',''))
    return {'code':0,'state':state,'message':message,'refresh_ok':job['refresh_ok'],'cleanup_pending':bool(job.get('cleanup_pending')),**links,
            'cat':job['key'][0],'title':job['key'][1],'season':job['key'][2],'episode':job['key'][3],'variant':job.get('variant',''),'item':(job.get('work') or {}).get('item')}


def _core_resolve(selected,replace=False,reason="corrupted"):
    work=selected["work"]; candidate=selected["candidate"]
    if not candidate: raise ValueError("请选择新片源")
    payload={"cat":work["item"]["cat"],"title":work["folder"],"season":work["season"],"episode":work["episode"],
             "share_url":candidate.share_url,"reason":reason,"selected_fid":selected.get("selected_fid",""),**_options(work)}
    token=config.get_admin_token()
    if not token: raise ValueError("点播管理凭证未配置")
    req=urllib.request.Request(VOD_API+"/api/admin/"+("replace" if replace else "resolve"),
        data=json.dumps(payload).encode(),method="POST",headers={"Authorization":"Bearer "+token,"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(req,timeout=config.TRANSFER_TIMEOUT+20) as response:
            data=json.loads(response.read(65536))
        if not data.get("ok"): raise ValueError("新片源保存或读取尚未就绪")
        return data
    except urllib.error.HTTPError as exc:
        try: error=json.loads(exc.read(65536)).get("error","点播请求失败")
        except Exception: error="点播请求失败，请稍后重试"
        raise ValueError(error) from None


def replace_to_library(payload):
    selected=_selected(payload)
    reason=payload.get("reason","corrupted")
    if reason not in ("dv_color_issue","wrong_audio","wrong_episode","hardcoded_ad","corrupted"):
        raise ValueError("请选择有效的换源原因")
    if selected["candidate"] is None: raise ValueError("请选择其他片源")
    if not _slots.acquire(blocking=False): raise ValueError("点播任务繁忙，请稍后重试")
    work=selected["work"];key=(work["item"]["cat"],work["folder"],work["season"],work["episode"])
    ident=secrets.token_urlsafe(18)
    job={"key":key,"work":work,"stage":"queued","message":"已排队换源","refresh_ok":False,"refresh_attempted":False,"replace":True,"reason":reason,"variant":work.get("variant","")}
    _remember(_jobs,ident,job,ttl=3600)
    try: _pool.submit(_do_add,selected,job)
    except Exception: _slots.release(); raise
    return {"code":0,"job":ident,"msg":"已开始校验新片源"}


def replace_source(cat,title,season,episode,candidate,reason="quality_upgrade",*,variant="",selected_fid=""):
    selected={"work":{"item":{"cat":cat},"folder":title,"season":season,"episode":episode,
                      "variant":db.validate_variant(variant)},
              "candidate":candidate,"selected_fid":selected_fid}
    return _core_resolve(selected,replace=True,reason=reason)


def catalogue(payload):
    variant=db.validate_variant(payload.get("variant",""))
    kind=payload.get("media_type"); ident=int(payload.get("id") or 0)
    if kind not in ("movie","tv") or ident<=0: raise ValueError("作品无效")
    detail=_tmdb(f"{kind}/{ident}",append_to_response="alternative_titles,translations")
    detail["media_type"]=kind
    _remember(_details,(kind,ident),detail)
    item=_summary(detail)
    seasons=[{"number":row["season_number"],"name":row.get("name",""),"episodes":row.get("episode_count",0),"air_date":row.get("air_date","")}
             for row in detail.get("seasons",[]) if row.get("season_number",-1)>=0]
    season=int(payload.get("season",next((x["number"] for x in seasons if x["number"]>0),0)))
    episodes=[]
    if kind=="tv" and any(row["number"]==season for row in seasons):
        data=_tmdb(f"tv/{ident}/season/{season}")
        _remember(_details,(kind,ident,season),data)
        existing=_library_folder(item["cat"],item["title"],item["year"],ident)
        folder=existing.name if existing else (f"{item['title']} ({item['year']})" if item["year"] else item["title"])
        for ep in data.get("episodes",[]):
            number=ep.get("episode_number",0)
            if number<=0: continue
            row=db.get_transfer(item["cat"],folder,season,number,variant=variant)
            episodes.append({"number":number,"name":ep.get("name",""),"air_date":ep.get("air_date",""),
                             "aired":tmdb_populate.is_episode_aired(ep,season,number,detail),
                             "saved":bool(row and row["status"]=="done")})
    return {"code":0,"item":item,"seasons":seasons,"season":season,"episodes":episodes,"variant":variant}


def subscribe_season(payload):
    work=_work(payload);item=work["item"];detail=work["detail"]
    if item["media_type"]!="tv": raise ValueError("仅剧集支持整季追更")
    next_ep=detail.get("next_episode_to_air") or {}
    db.upsert_airing_calendar(item["id"],item["cat"],work["folder"],work["season"],
        next_ep.get("air_date") or time.strftime("%Y-%m-%d"),is_user_watching=True,
        metadata={"item":detail,"folder":work["folder"]})
    db.pin_item(item["cat"],work["folder"],reason="subscribed")
    return {"code":0,"msg":"已订阅本季自动追更"}


def list_source_files(payload):
    selected=_selected(payload); candidate=selected["candidate"]
    if not candidate: raise ValueError("已保存文件无需展开分享")
    work=selected["work"]
    tr=QuarkTransfer(None)
    token,pwd_id,roots=tr.resolve_share(candidate.share_url)
    files=tr.share_files(token,pwd_id,roots)
    rows=[]
    original=work["item"].get("original_title") or ""
    for video in files:
        if len(rows)>=100: break
        try:
            chosen=tr.select_share_video([video],work["folder"],work["season"],work["episode"],aliases=_aliases(work),**_options(work))
        except Exception: continue
        if not chosen: continue
        ident=secrets.token_urlsafe(18)
        fid = str((chosen.get('share') or {}).get('fid') or video.get('fid') or '')
        if not fid: continue
        child={"work":work,"candidate":candidate,"selected_fid":fid,
               "inspection":{"name":chosen["name"],"size":chosen.get("size",0),"fid":fid,
                             "quality":quality_hint(chosen["name"],candidate.name),"status":"file_found","checked_at":int(time.time())}}
        _remember(_candidates,ident,child,ttl=600,limit=320)
        rows.append({"token":ident,"name":chosen["name"],"size":chosen.get("size",0),"quality":child["inspection"]["quality"]})
    return {"code":0,"files":rows}


def _jf_request(path,method="GET",payload=None):
    key=config.get_jellyfin2_key()
    if not key: return None
    data=json.dumps(payload).encode() if payload is not None else b"" if method=="POST" else None
    headers={"Authorization":'MediaBrowser Token="'+key+'"'}
    if payload is not None:headers['Content-Type']='application/json'
    req=urllib.request.Request(config.JELLYFIN2_API+path,method=method,data=data,headers=headers)
    with urllib.request.urlopen(req,timeout=6) as response:
        raw=response.read(2*1024*1024+1)
        if len(raw)>2*1024*1024: raise ValueError("Jellyfin response too large")
        return json.loads(raw) if raw else {}


def _jellyfin_id(cat,title,tmdb_id=None):
    from quark_vod.identity import same_title
    if tmdb_id:
        query=urllib.parse.urlencode({"Recursive":"true","AnyProviderIdEquals":"tmdb."+str(tmdb_id),"Fields":"Path,ProviderIds","Limit":100})
    else:
        query=urllib.parse.urlencode({"Recursive":"true","SearchTerm":search.sanitize_title(title),"Fields":"Path,ProviderIds","Limit":100})
    data=_jf_request("/Items?"+query) or {}
    root=(Path(CINEMA2)/cat/title).resolve()
    for item in data.get("Items",[]):
        path=str(item.get("Path") or "")
        valid_path=path.startswith("/cinema2/"+cat+"/"+title+"/") or path=="/cinema2/"+cat+"/"+title or path==str(root) or path.startswith(str(root)+"/")
        if valid_path and (not tmdb_id or str((item.get("ProviderIds") or {}).get("Tmdb"))==str(tmdb_id)):
            return item.get("Id")
    return None


def _refresh_work(work):
    if not work: return False
    try:
        item=work.get("item") or {}
        ident=_jellyfin_id(item.get("cat",""),work.get("folder",""),item.get("id"))
        if ident:
            return _jf_request("/Items/"+urllib.parse.quote(ident,safe="")+"/Refresh?Recursive=true",method="POST") is not None
        # A newly created work cannot have an item ID yet. Notify Jellyfin of
        # this exact existing directory; never fall back to a full-library scan.
        cat=item.get('cat','');folder=work.get('folder','')
        if any(not value or value in ('.','..') or '/' in value or '\\' in value for value in (cat,folder)):
            return False
        root=Path(CINEMA2)/cat/folder
        if root.is_symlink() or not _has_library_media(root):return False
        relative=root.resolve().relative_to(Path(CINEMA2).resolve())
        container_path=config.JELLYFIN_LIBRARY_ROOT.rstrip('/')+'/'+relative.as_posix()
        return _jf_request('/Library/Media/Updated',method='POST',
            payload={'Updates':[{'Path':container_path,'UpdateType':'Created'}]}) is not None
    except Exception: return False


def playback_links(key, *, variant=""):
    params=urllib.parse.urlencode({**dict(zip(("cat","t","s","e"),key)),**({"variant":variant} if variant else {})})
    result={"play_url":"/services/vod/play?"+params,"jellyfin_item_id":None}
    try: result["jellyfin_item_id"]=_jellyfin_id(key[0],key[1])
    except Exception: pass
    return result


def discovery():
    groups={"saved":[],"watching":[],"trending":[]}
    rows=db._conn().execute("SELECT DISTINCT cat,title FROM transfers WHERE status='done' ORDER BY updated_at DESC LIMIT 30")
    for row in rows:
        path=Path(CINEMA2)/row["cat"]/row["title"]
        for kind,name in (("movie","movie.nfo"),("tv","tvshow.nfo")):
            try:
                nfo=ET.parse(path/name).getroot()
                ident=nfo.findtext("tmdbid") or next((v.text for v in nfo.findall("uniqueid") if v.get("type")=="tmdb"),None)
                if not ident: continue
                groups["saved"].append({"id":int(ident),"media_type":kind,"title":search.sanitize_title(row["title"]),
                    "cat":row["cat"],"poster":"","year":nfo.findtext("year") or "","source_status":"saved"})
                break
            except (OSError,ET.ParseError,ValueError): pass
    for row in db.list_airing_calendar(limit=30):
        groups["watching"].append({"id":row["tmdb_id"],"media_type":"tv","title":search.sanitize_title(row["title"]),
            "cat":row["cat"],"year":"","poster":"","next_air_date":row["next_air_date"]})
    try:
        data=_tmdb("trending/all/day")
        for item in data.get("results",[]):
            if item.get("media_type") in ("tv","movie"):
                groups["trending"].append(_summary(item))
    except ValueError: groups["trending_error"]="片目信息服务尚未连接，可直接搜索或配置 TMDB。"
    return {"code":0,**groups}


def submit_wishlist(text,source="bot"):
    text=str(text).strip()
    if not text or len(text)>500: raise ValueError("请发送片名或夸克分享链接")
    from quark_vod.panshare_matrix import extract_quark_links_and_pwd
    links=extract_quark_links_and_pwd(text)
    query=text
    if links:
        for link in links: query=query.replace(link,"")
        query=query.strip(" :：,，")
        if not query:
            return {"status":"needs_title","message":"请在分享链接旁附上片名和年份，用于确认作品身份"}
    results=search_all(query[:100])
    exact=[item for item in results if search.sanitize_title(item["title"]).casefold()==search.sanitize_title(query).casefold()
           or f"{item['title']} ({item['year']})".casefold()==query.casefold()]
    if len(exact)!=1:
        return {"status":"needs_choice","candidates":results[:5],"message":"请补充年份或完整片名后重试"}
    item=exact[0]
    db.upsert_staging_media(item["id"],item["cat"],item["title"],item["year"],item["media_type"],
        metadata={"wishlist_source":source,"item":item,"share_urls":links})
    # Real saves still pass the server-side TMDB identity and transfer selector.
    try:
        if links:
            from quark_vod.panshare_matrix import canonical_share
            work=_work({"id":item["id"],"media_type":item["media_type"]})
            candidates=[]
            for link in links[:5]:
                if not canonical_share(link): continue
                candidate=search.SearchResult(engine="panshare",source="wishlist",name=work["folder"],
                    year=item["year"],poster="",episodes=[],share_url=link,pan_type="quark",sources=[source])
                ident=secrets.token_urlsafe(18)
                _remember(_candidates,ident,{"work":work,"candidate":candidate},ttl=1200,limit=320)
                candidates.append({"token":ident})
        else:
            candidates=source_search({"id":item["id"],"media_type":item["media_type"]}).get("candidates",[])
        if candidates:
            result=add_to_library({"token":candidates[0]["token"]})
            return {"status":"queued","job":result["job"],"item":item}
    except ValueError: pass
    return {"status":"staged","item":item,"message":"已加入心愿单，出源后自动上架"}
