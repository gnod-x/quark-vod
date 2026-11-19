#!/usr/bin/env python3
"""Portal catalogue -> source discovery -> read-only inspection -> queued add.

TMDB supplies work identity. Public indexes only supply candidates. A selected
share is inspected with the actual transfer selector before creating a pending
item. No fabricated episodes, resolution certification or ready claims.
"""
import copy
import hashlib
import json
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
from concurrent.futures import ThreadPoolExecutor

BASE = os.environ.get('HOMETHEATER_BASE_DIR', '/home/dong/projects/hometheater')
sys.path.insert(0, os.path.join(BASE, 'scripts'))
import tmdb_populate
from quark_vod import config, db, search, strm_manager
from quark_vod.coordination import maintenance_window
from quark_vod.openlist_client import OpenListClient
from quark_vod.transfer import QuarkTransfer
from quark_vod.quality import quality_hint

CINEMA2 = os.path.join(BASE, 'cinema2')
IMG_BASE = 'https://image.tmdb.org/t/p'
VOD_API = os.environ.get('VOD_API_BASE', 'http://127.0.0.1:8099').rstrip('/')
_lock = threading.RLock()
_details = OrderedDict()
_candidates = OrderedDict()
_jobs = OrderedDict()
_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix='portal-add')
_slots = threading.BoundedSemaphore(4)
_inspect_slots = threading.BoundedSemaphore(2)


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


def search_all(keyword):
    query = str(keyword).strip()
    if not query or len(query)>100:raise ValueError('片名长度需为 1 到 100 字符')
    data = _tmdb('search/multi',query=query,include_adult='false')
    results=[]
    for item in data.get('results',[]):
        if item.get('media_type') not in ('movie','tv') or not tmdb_populate.fmt_title(item):continue
        if tmdb_populate.is_ai_content(item,cat=guess_category(item)):continue
        results.append(_summary(item))
    return results[:20]


def _work(payload):
    kind = payload.get('media_type')
    ident = int(payload.get('id') or 0)
    if kind not in ('movie','tv') or ident<=0:raise ValueError('请选择有效的电影或剧集')
    cache_key = (kind,ident)
    detail = _recall(_details,cache_key)
    if detail is None:
        detail = _tmdb(f'{kind}/{ident}',append_to_response='keywords')
        if detail.get('id') != ident:raise ValueError('作品信息不一致')
        detail['media_type']=kind
        _remember(_details,cache_key,detail)
    if tmdb_populate.is_ai_content(detail,detail,cat=guess_category(detail)):
        raise ValueError('该条目属于已过滤的微短剧或 AIGC 内容')
    item=_summary(detail)
    season=int(payload.get('season') or (1 if kind=='tv' else 0))
    episode=int(payload.get('episode') or (1 if kind=='tv' else 0))
    seasons=[{'number':s['season_number'],'episodes':s.get('episode_count',0)}
             for s in detail.get('seasons',[]) if s.get('season_number',0)>0]
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
            'seasons':seasons,'episode_data':ep_data}


def source_search(payload):
    work=_work(payload);item=work['item'];season=work['season'];episode=work['episode']
    original=item['original_title']
    # Keep year/season identity while searching the title used by foreign releases.
    aliases=[original] if original and original.casefold()!=item['title'].casefold() else []
    locked_url, locked_4k, locked_title=db.get_locked_4k_share(item['cat'],work['folder'],season,episode)
    try:
        report=search.search_report(work['folder'],aliases=aliases,season=season,episode=episode)
    except Exception:
        # A verified exact-work lock remains useful when public index nodes fail.
        report={'results':[],'queries':[],'nodes':[],'partial':True,'cached':False}
    scored=[]
    for candidate in report['results']:
        if not search.matches_kind(candidate,item['cat']):continue
        score=search.score_search_result(candidate,work['folder'],season=season,episode=episode)
        if aliases:
            alias=original+(f" ({item['year']})" if item['year'] else '')
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
    saved=db.find_done_transfer(item['cat'],work['folder'],season,episode)
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
            'partial':report['partial'],'cached':report['cached'],'truncated':len(scored)>80}


def _selected(payload):
    selected=_recall(_candidates,str(payload.get('token') or ''))
    if selected is None:raise ValueError('片源列表已过期，请重新查找片源')
    return selected


def _inspect(selected):
    if not _inspect_slots.acquire(timeout=1):raise ValueError('正在检查其他片源，请稍后重试')
    try:
        work=selected['work'];candidate=selected['candidate']
        if candidate is None:raise ValueError('所选条目使用已保存视频，无需重新检查分享')
        tr=QuarkTransfer(None)
        original=work['item'].get('original_title') or ''
        aliases=(original,) if original and original.casefold()!=work['item']['title'].casefold() else ()
        video=tr.inspect_share_video(candidate.share_url,work['folder'],work['season'],work['episode'],aliases=aliases)
        if not video:raise ValueError('该分享中没有匹配所选季集的完整视频')
        return {'name':video['name'],'size':video['size'],'quality':quality_hint(video['name'],candidate.name),
                'status':'file_found','checked_at':int(time.time())}
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


def _queue_preheat(cat, folder, season, episode):
    params=urllib.parse.urlencode({'cat':cat,'t':folder,'s':season,'e':episode})
    token = config.get_admin_token()
    if not token:
        raise ValueError('点播服务管理凭证尚未配置')
    request = urllib.request.Request(VOD_API+'/preheat?'+params,
        headers={'Authorization': 'Bearer '+token})
    with urllib.request.urlopen(request,timeout=12) as response:
        if response.status not in (200,202):raise ValueError('点播服务暂时无法接收转存')


def _ensure_metadata(root, path, work):
    detail=work['detail'];item=work['item'];season=work['season'];episode=work['episode']
    if season:
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
    if season:
        return Path(tmdb_populate.episode_strm_path(str(root/f'Season {season:02d}'),item['title'],season,episode))
    if root.is_dir():
        existing=sorted(root.glob('*.strm'))
        if len(existing)>1:raise ValueError('片库电影目录存在多个 STRM，无法安全选定原播放入口')
        if existing:return existing[0]
    return root/(work['folder']+'.strm')


def _do_add(selected,job):
    try:
        work=selected['work'];item=work['item'];detail=work['detail'];candidate=selected['candidate']
        folder=work['folder'];cat=item['cat'];season=work['season'];episode=work['episode']
        key=(cat,folder,season,episode)
        root=Path(CINEMA2)/cat/folder
        path=_work_strm_path(root,work)
        job['strm_path']=str(path)
        if candidate is not None and path.is_file():
            with maintenance_window():
                try:existing=path.read_text()
                except (OSError,UnicodeError):raise ValueError('现有播放入口不可读取，已停止修改') from None
                if strm_manager.is_live(existing):
                    # Existing live media remains the active item; an add request
                    # never replaces it with another candidate.
                    job.update(stage='preserved',message='该集已有播放入口，已保持原样；未验证当前可读性，也未执行新转存。',
                               refresh_ok=True)
                    return
        saved=db.find_done_transfer(*key)
        mapped=db.get_strm_path(*key)
        if saved and saved['remote_path']:
            root_real=root.resolve()
            mapped_candidate=Path(mapped).resolve() if mapped else None
            mapped_path=(mapped_candidate if mapped_candidate and mapped_candidate.is_relative_to(root_real) else None)
            if mapped_path is None:
                mapped_path=(Path(tmdb_populate.episode_strm_path(str(root/f'Season {season:02d}'),item['title'],season,episode))
                             if season else root/(folder+'.strm'))
            with maintenance_window():
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
                        if not tmdb_populate.write_pending_strm(str(mapped_path),cat,folder,season,episode):
                            raise ValueError('已有保存视频的片库入口无法安全恢复')
                        current=mapped_path.read_text()
                    _ensure_metadata(root,mapped_path,work)
                    if not mapped:
                        with db._conn() as con:
                            con.execute('INSERT OR REPLACE INTO strm_map(cat,title,season,episode,strm_path) VALUES(?,?,?,?,?)',
                                        (*key,str(mapped_path)))
            if saved and saved['remote_path'] and reusable_entry:
                job['stage']='preparing'
                if strm_manager.is_live(current):
                    # A working live entry may be a user's explicitly selected source.
                    # Leave it byte-for-byte alone and do not start another resolution.
                    job.update(stage='preserved',message='该集已有播放入口，已保持原样；未验证当前可读性，也未执行新转存。')
                    return
                else:
                    job['refresh_ok']=_refresh()
                    # /preheat calls the normal resolver, which reuses this exact saved
                    # transfer and activates the pending entry without saving a copy.
                    _queue_preheat(cat,folder,season,episode)
                job['stage']='submitted'
                return
        if candidate is None:
            raise ValueError('当前作品没有可复用的已保存视频')
        job['stage']='checking'
        inspected=_inspect(selected)
        with maintenance_window():
            if path.is_file():
                try:existing=path.read_text()
                except (OSError,UnicodeError):raise ValueError('现有播放入口不可读取，已停止修改') from None
                if not strm_manager.is_pending(existing):
                    raise ValueError('片库中已有非待点播入口；为保护现有播放地址，本次没有入库或转存')
            path.parent.mkdir(parents=True,exist_ok=True)
            _ensure_metadata(root,path,work)
            if not tmdb_populate.write_pending_strm(str(path),cat,folder,season,episode):
                raise ValueError('片库中已有非待点播入口；为保护现有播放地址，本次没有入库或转存')
            with db._conn() as con:
                con.execute('INSERT OR REPLACE INTO strm_map(cat,title,season,episode,strm_path) VALUES(?,?,?,?,?)',
                            (cat,folder,season,episode,str(path)))
            db.pin_item(cat,folder,reason='manual_add')
            # The actual target file was verified. This is a hint, not decoded resolution.
            db.save_source_audit(cat,folder,season,episode,has_quark_source=1,
                is_4k=int(inspected['quality']['level']=='4k'),best_share_url=candidate.share_url,
                best_share_title=candidate.name,check_status='selected_file')
        job['refresh_ok']=_refresh()
        job['stage']='preparing'
        _queue_preheat(cat,folder,season,episode)
        job['stage']='submitted'
    except Exception as exc:
        job.update(stage='failed',message=str(exc) if isinstance(exc,ValueError) else '入库或转存暂时失败，请检查服务后重试')
    finally:_slots.release()


def add_to_library(payload):
    selected=_selected(payload)
    work=selected['work'];key=(work['item']['cat'],work['folder'],work['season'],work['episode'])
    with _lock:
        for ident,(expires,job) in _jobs.items():
            if expires>time.monotonic() and job['key']==key and job['stage']!='failed':
                return {'code':0,'job':ident,'msg':'任务已存在，继续查看状态'}
        if not _slots.acquire(blocking=False):raise ValueError('点播任务繁忙，请稍后重试')
        ident=secrets.token_urlsafe(18)
        job={'key':key,'stage':'queued','message':'已排队，尚未开始转存','refresh_ok':False}
        _remember(_jobs,ident,job,ttl=3600)
        try:_pool.submit(_do_add,selected,job)
        except Exception:_slots.release();raise
    return {'code':0,'job':ident,'msg':'已排队，正在检查所选文件'}


def job_status(ident):
    job=_recall(_jobs,ident)
    if job is None:raise ValueError('任务记录已过期；可到 Jellyfin2 或管理台查看保存状态')
    state=job['stage'];message=job.get('message','')
    row=db.find_done_transfer(*job['key']) or db.get_transfer(*job['key'])
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
    if state=='ready' and not job['refresh_ok']:job['refresh_ok']=_refresh()
    return {'code':0,'state':state,'message':message,'refresh_ok':job['refresh_ok'],
            'cat':job['key'][0],'title':job['key'][1],'season':job['key'][2],'episode':job['key'][3]}
