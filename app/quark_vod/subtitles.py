"""Optional same-directory Chinese sidecar saves with a durable no-blind-retry journal."""
import json
import os
from pathlib import Path
import re
import tempfile
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
import urllib.request
from . import config, download_http, strm_manager
from .coordination import file_lock, playback_lease

def matching_subtitles(transfer, files, selected, season, episode):
    selected_path=selected.get('relative_path') or selected.get('file_name','')
    parent=selected_path.rsplit('/',1)[0] if '/' in selected_path else ''
    video_stem=os.path.splitext(selected.get('file_name',''))[0]
    out=[]
    for item in files:
        name=item.get('file_name','')
        ext=os.path.splitext(name)[1].lower()
        path=item.get('relative_path') or name
        folder=path.rsplit('/',1)[0] if '/' in path else ''
        if ext not in ('.ass','.ssa','.srt') or not 0<int(item.get('size') or 0)<15*1024**2:continue
        if folder!=parent and folder not in tuple((parent+'/'+part).lstrip('/') for part in ('Subs','subs','字幕')):continue
        if episode:
            if transfer.extract_episode_num(name,season)!=episode:continue
        elif not os.path.splitext(name)[0].startswith(video_stem):
            continue
        # Prefer explicit Chinese/bilingual labels. A same-stem unlabeled subtitle
        # is acceptable; unrelated English-only tracks do not displace it.
        chinese=re.search('简中|简体|CHS|zh-cn|双语|中字|中文|繁中|繁体|CHT|zh-tw',name,re.I)
        if not chinese and re.search(r'(?i)(?:^|[ ._\[(-])(?:eng|english|en)(?:$|[ ._\])-])|英文|英语',name):continue
        rank=3 if re.search('简中|简体|CHS|zh-cn',name,re.I) else 2 if chinese else 1
        out.append((rank,item))
    chosen=[]
    destinations=set()
    for _,item in sorted(out,key=lambda pair:pair[0],reverse=True):
        name=item['file_name']
        destination=(_language(name),os.path.splitext(name)[1].lower())
        if destination in destinations:continue
        destinations.add(destination)
        chosen.append(item)
        if len(chosen)>=2:break
    return chosen

def _language(name):
    return 'zh-Hant' if re.search(r'繁中|繁体|CHT|zh-tw',name,re.I) else 'zh-Hans'

def _write_state(path,state):
    fd,tmp=tempfile.mkstemp(prefix='.subtitle-intent-',dir=path.parent)
    try:
        os.fchmod(fd,0o600)
        with os.fdopen(fd,'w',encoding='utf8') as stream:
            json.dump(state,stream);stream.flush();os.fsync(stream.fileno())
        os.replace(tmp,path)
    finally:
        if os.path.exists(tmp):os.unlink(tmp)

def _atomic_subtitle(target, data):
    previous=os.stat(target,follow_symlinks=False) if target.exists() else None
    if previous and not stat.S_ISREG(previous.st_mode):raise RuntimeError('字幕不是普通文件')
    attrs={name:os.getxattr(target,name,follow_symlinks=False) for name in os.listxattr(target,follow_symlinks=False)} if previous else {}
    fd,tmp=tempfile.mkstemp(prefix='.subtitle-',dir=target.parent)
    try:
        os.fchmod(fd,stat.S_IMODE(previous.st_mode) if previous else 0o644)
        if previous:
            if os.geteuid()==0:os.fchown(fd,previous.st_uid,previous.st_gid)
            elif previous.st_uid!=os.geteuid() or previous.st_gid not in os.getgroups()+[os.getegid()]:
                raise PermissionError('无法保留字幕属主或组')
            else:os.fchown(fd,-1,previous.st_gid)
            os.fchmod(fd,stat.S_IMODE(previous.st_mode))
        with os.fdopen(fd,'wb') as stream:
            fd=None
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        for name,value in attrs.items():os.setxattr(tmp,name,value,follow_symlinks=False)
        if previous:
            now=os.stat(target,follow_symlinks=False)
            if ((now.st_dev,now.st_ino,now.st_size,now.st_mtime_ns)!=(previous.st_dev,previous.st_ino,previous.st_size,previous.st_mtime_ns)
                    or {name:os.getxattr(target,name,follow_symlinks=False) for name in os.listxattr(target,follow_symlinks=False)}!=attrs):
                raise RuntimeError('字幕并发改变，停止覆盖')
        elif target.exists():raise RuntimeError('字幕路径并发创建，停止覆盖')
        os.replace(tmp,target)
    finally:
        if fd is not None:os.close(fd)
        if os.path.exists(tmp):os.unlink(tmp)

def save_companions(transfer, work, transfer_id, files, selected, share_url, slot):
    """Each uncertain save is recovered by exact filename/task before any retry."""
    companions=matching_subtitles(transfer,files,selected,work.season,work.episode)
    if not companions:return 0
    from .transfer import QUARK_SHARE_SAVE
    state_dir=Path(config.DATA_DIR)/'subtitle_intents'
    state_dir.mkdir(mode=0o700,parents=True,exist_ok=True)
    saved_count=0
    with file_lock('subtitle:'+str(transfer_id),timeout=5):
        for source in companions:
            original=source['file_name']
            safe_id=str(source.get('fid',''))
            if not safe_id or not re.fullmatch('[A-Za-z0-9_-]+',safe_id):continue
            journal=state_dir/(str(transfer_id)+'-'+safe_id+'.json')
            state=json.loads(journal.read_text()) if journal.is_file() else {}
            if state.get('phase')=='complete':continue
            context=state.get('context',{})
            def record(**values):
                state.update(values)
                if context:state['context']=context
                _write_state(journal,state)
            slot_fid=transfer._get_quark_fid(slot)
            if not slot_fid:raise RuntimeError('字幕目标目录不可确认')
            def lookup():
                entries=[entry for entry in transfer._quark_list_fid(slot_fid)
                         if not entry.get('is_dir') and entry.get('name')==original
                         and int(entry.get('size') or 0)==int(source.get('size') or 0)]
                if len(entries)>1:raise RuntimeError('字幕文件身份不唯一')
                return entries[0] if entries else None
            existing=lookup()
            if not existing:
                if state.get('task_id'):
                    transfer._wait_saved_task(state['task_id'])
                    existing=lookup()
                elif state.get('phase') not in ('','queued'):
                    # A lost HTTP reply may have accepted the save. Do not guess.
                    continue
                else:
                    token,pwd,roots=transfer.resolve_share(share_url,account='main')
                    parent=str(source.get('parent_fid','0'))
                    fresh=roots if parent=='0' else transfer._share_page_all(token,pwd,parent)
                    fresh=[item for item in fresh if str(item.get('fid'))==safe_id and item.get('file_name')==original
                           and int(item.get('size') or 0)==int(source.get('size') or 0)]
                    if len(fresh)!=1:continue
                    item=fresh[0]
                    record(phase='submitting',name=original)
                    payload={'pwd_id':pwd,'stoken':token,'to_pdir_fid':str(slot_fid),
                             'fid_list':[item['fid']],'fid_token_list':[item.get('share_fid_token','')],
                             'pdir_fid':parent,'scene':'link'}
                    result=transfer._request(QUARK_SHARE_SAVE,payload=payload,method='POST',timeout=20)
                    if result.get('code')!=0:
                        record(phase='queued')
                        continue
                    task=(result.get('data') or {}).get('task_id')
                    record(phase='accepted',name=original,task_id=str(task) if task else '')
                    if task:transfer._wait_saved_task(str(task))
                    existing=lookup()
            if not existing:continue
            record(phase='saved',name=original,file_fid=str(existing.get('fid','')))
            if config.VOD_JELLYFIN_ENABLED:
                from . import db
                path=db.get_strm_path(*work.args,**work.options)
                if not path or not os.path.isfile(path) or os.path.islink(path):
                    raise RuntimeError('字幕的媒体库入口尚未就绪')
                allowed=strm_manager.activation_paths(*work.args,**work.options)
                resolved=Path(path).resolve()
                if (not resolved.is_relative_to(Path(config.CINEMA2_DIR).resolve())
                        or str(path) not in allowed or strm_manager.variant_from_path(path)!=work.variant):
                    raise RuntimeError('字幕媒体库映射与作品版本不符')
                if path:
                    remote=slot+'/'+original
                    transfer.storage.refresh_path_tree(config.play_path(remote))
                    url=transfer.storage.direct_url(config.play_path(remote))
                    req=urllib.request.Request(url,headers={'User-Agent':'quark-vod'})
                    with download_http.urlopen(req,timeout=15) as response:data=response.read(15*1024**2+1)
                    if len(data)>15*1024**2:raise RuntimeError('字幕超过大小限制')
                    # Keep generated sibling strictly in the validated STRM directory.
                    extension=os.path.splitext(original)[1].lower()
                    language=_language(original)
                    target=Path(path).with_suffix('.'+language+extension)
                    if target.is_symlink():raise RuntimeError('字幕路径不能是符号链接')
                    _atomic_subtitle(target,data)
            journal.unlink(missing_ok=True)
            saved_count+=1
    return saved_count


_executor=ThreadPoolExecutor(max_workers=1,thread_name_prefix='subtitle-recovery')
_schedule_lock=threading.Lock()
_scheduled=False

def queue_companions(transfer, work, transfer_id, files, selected, share_url, slot):
    """Persist source context before dispatch; tokens and cookies never enter disk."""
    directory=Path(config.DATA_DIR)/'subtitle_intents'
    directory.mkdir(mode=0o700,parents=True,exist_ok=True)
    keys=('file_name','relative_path','size','fid','parent_fid')
    queued=False
    with file_lock('subtitle:'+str(transfer_id),timeout=5):
        for source in matching_subtitles(transfer,files,selected,work.season,work.episode):
            fid=str(source.get('fid',''))
            if not re.fullmatch('[A-Za-z0-9_-]+',fid):continue
            journal=directory/(str(transfer_id)+'-'+fid+'.json')
            if journal.exists():continue
            context={'work':list(work.args),'variant':work.variant,'transfer_id':transfer_id,
                     'source':{key:source[key] for key in keys if key in source},
                     'selected':{key:selected[key] for key in keys if key in selected},
                     'share_url':share_url,'slot':slot}
            _write_state(journal,{'phase':'queued','context':context,'retry_after':0})
            queued=True
    if queued:schedule_pending()

def recover_pending(transfer=None,limit=4):
    """Bounded recovery; unknown submission outcomes only reconcile exact files/tasks."""
    from . import db
    from .identity import Work
    directory=Path(config.DATA_DIR)/'subtitle_intents'
    if not directory.is_dir():return 0
    attempted=0
    for journal in directory.glob('*.json'):
        if attempted>=limit:break
        try:
            if journal.is_symlink() or journal.stat().st_size>32768:continue
            state=json.loads(journal.read_text())
            context=state.get('context')
            if not context or state.get('phase')=='complete' or state.get('retry_after',0)>time.time():continue
            if transfer is None:
                from .server import get_transfer
                transfer=get_transfer()
                if transfer is None:return attempted
            attempted+=1
            with playback_lease(),file_lock('subtitle:'+str(context['transfer_id']),timeout=0):
                row=db.get_transfer_by_id(context['transfer_id'])
                work=Work(*context['work'],variant=context.get('variant',''))
                if (not row or row['status']!='done' or row['remote_path'].rsplit('/',1)[0]!=context['slot']
                        or tuple(row[key] for key in ('cat','title','season','episode'))!=work.args
                        or (row['variant'] or '')!=work.variant):continue
                # Persist backoff before external I/O, including process interruption.
                state['retry_after']=time.time()+300
                _write_state(journal,state)
                save_companions(transfer,work,context['transfer_id'],[context['source']],
                                context['selected'],context['share_url'],context['slot'])
        except Exception:
            # Each journal is isolated; errors retain intent for a later bounded pass.
            continue
    return attempted

def schedule_pending():
    """One queued worker per process; playback never waits on subtitle network I/O."""
    global _scheduled
    with _schedule_lock:
        if _scheduled:return False
        _scheduled=True
    def run():
        global _scheduled
        from . import db
        try:recover_pending()
        finally:
            db.close()
            with _schedule_lock:_scheduled=False
    try:_executor.submit(run)
    except Exception:
        with _schedule_lock:_scheduled=False
        raise
    return True
